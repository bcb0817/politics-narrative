"""xAI Responses client with durable reservations and conservative ambiguous accounting."""
import json
import math
import os
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from .article_schema import validate

class BudgetExceeded(Exception): pass
class AmbiguousCall(Exception): pass
class AuthenticationError(Exception): pass

def read_settings_env(path):
    allowed={'XAI_API_KEY','XAI_MONTHLY_BUDGET_USD','TOTAL_MONTHLY_API_BUDGET_USD',
             'XAI_BUDGET_RESERVE_USD','TOTAL_BUDGET_RESERVE_USD'}
    result={}
    if path and Path(path).is_file():
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k,v=line.split('=',1)
                if k.strip() in allowed: result[k.strip()]=v.strip().strip('"').strip("'")
    result.update({k:os.environ[k] for k in allowed if k in os.environ})
    return result

@contextmanager
def process_lock(path):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    f=path.open('a+b')
    try:
        if os.fstat(f.fileno()).st_size==0: f.write(b'0'); f.flush()
        f.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        f.close(); raise RuntimeError('article_generation_already_running') from None
    try: yield
    finally:
        f.seek(0)
        if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
        else: fcntl.flock(f,fcntl.LOCK_UN)
        f.close()

def measured_usage(raw, pricing):
    usage=raw.get('usage') or {}
    inp=usage.get('input_tokens'); out=usage.get('output_tokens')
    cached=(usage.get('input_tokens_details') or {}).get('cached_tokens')
    reasoning=(usage.get('output_tokens_details') or {}).get('reasoning_tokens')
    ticks=usage.get('cost_in_usd_ticks')
    estimated=None
    if isinstance(inp,int) and isinstance(out,int):
        cache=min(inp,cached or 0)
        # Responses output_tokens already includes reasoning_tokens. Do not add again.
        estimated=((inp-cache)*pricing['input_per_million']+cache*pricing['cached_per_million']+out*pricing['output_per_million'])/1e6
    return dict(input_tokens=inp,output_tokens=out,reasoning_tokens=reasoning,cached_input_tokens=cached,
                search_usage=usage.get('server_side_tool_usage_details',raw.get('server_side_tool_usage_details')),
                server_tool_count=usage.get('num_server_side_tools_used'),raw_usage=usage,
                actual_cost_usd=ticks/1e10 if isinstance(ticks,int) else None,
                estimated_token_cost_usd=estimated)

class Ledger:
    def __init__(self,path,settings,env,clock=None):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.settings=settings; self.env=env; self.clock=clock or (lambda:datetime.now(timezone.utc))
        with closing(self.connect()) as c:
            c.execute('''CREATE TABLE IF NOT EXISTS article_calls (
            run_id TEXT,stage TEXT,attempt INTEGER,month TEXT,status TEXT,reserved REAL,
            charged REAL,usage_json TEXT,response_json TEXT,PRIMARY KEY(run_id,stage,attempt))'''); c.commit()
    def connect(self): return sqlite3.connect(self.path,timeout=20)
    def legacy_spend(self,c,month):
        names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        total=xai=0.0
        if 'x_manual_smoke' in names:
            total+=c.execute('SELECT COALESCE(SUM(reserved_usd),0) FROM x_manual_smoke WHERE month=?',(month,)).fetchone()[0]
        if 'api_usage_events' in names:
            for provider,amount in c.execute("SELECT provider,COALESCE(SUM(estimated_cost_usd),0) FROM api_usage_events WHERE timestamp LIKE ? AND (provider<>'xai' OR error_type='reserved') GROUP BY provider",(month+'%',)):
                total+=amount
                if provider=='xai': xai+=amount
        if 'xai_usage_events' in names:
            amount=c.execute("SELECT COALESCE(SUM(CASE WHEN cost_source='actual' THEN actual_cost_usd ELSE estimated_cost_usd END),0) FROM xai_usage_events WHERE timestamp LIKE ?",(month+'%',)).fetchone()[0]
            total+=amount; xai+=amount
        return total,xai
    def reserve(self,run,stage,attempt,amount,cap):
        month=self.clock().astimezone(timezone(timedelta(hours=9))).strftime('%Y-%m')
        with closing(self.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT 1 FROM article_calls WHERE run_id=? AND status='reserved'",(run,)).fetchone():
                raise AmbiguousCall('unresolved_request_reservation')
            spent,count=c.execute('SELECT COALESCE(SUM(charged),0),COUNT(*) FROM article_calls WHERE run_id=?',(run,)).fetchone()
            monthly=c.execute('SELECT COALESCE(SUM(charged),0) FROM article_calls WHERE month=?',(month,)).fetchone()[0]
            legacy_total,legacy_xai=self.legacy_spend(c,month)
            def limit(k):
                v=float(self.env.get(k,self.settings['monthly_defaults'][k]))
                if not math.isfinite(v) or v<0: raise ValueError('invalid_monthly_limit')
                return v
            if (spent+amount>cap+1e-10 or count>=self.settings['max_calls'] or
                monthly+legacy_xai+amount>limit('XAI_MONTHLY_BUDGET_USD')-limit('XAI_BUDGET_RESERVE_USD') or
                monthly+legacy_total+amount>limit('TOTAL_MONTHLY_API_BUDGET_USD')-limit('TOTAL_BUDGET_RESERVE_USD')):
                raise BudgetExceeded('budget_or_call_limit')
            c.execute('INSERT INTO article_calls VALUES(?,?,?,?,?,?,?,?,?)',
                      (run,stage,attempt,month,'reserved',amount,amount,None,None)); c.commit()
    def settle(self,run,stage,attempt,status,usage=None,response=None):
        with closing(self.connect()) as c:
            row=c.execute('SELECT reserved FROM article_calls WHERE run_id=? AND stage=? AND attempt=?',(run,stage,attempt)).fetchone()
            actual=(usage or {}).get('actual_cost_usd')
            charge=actual if actual is not None else row[0]
            c.execute('UPDATE article_calls SET status=?,charged=?,usage_json=?,response_json=? WHERE run_id=? AND stage=? AND attempt=?',
                      (status,charge,json.dumps(usage),json.dumps(response),run,stage,attempt)); c.commit()
    def rows(self,run):
        with closing(self.connect()) as c:
            c.row_factory=sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM article_calls WHERE run_id=? ORDER BY rowid',(run,))]
    def summary(self,run):
        rows=self.rows(run)
        actual=[json.loads(r['usage_json'] or 'null') or {} for r in rows]
        return dict(calls=[{k:v for k,v in r.items() if k!='response_json'} for r in rows],
                    actual_cost_usd=sum(x['actual_cost_usd'] for x in actual if x.get('actual_cost_usd') is not None) if rows and all(x.get('actual_cost_usd') is not None for x in actual) else None,
                    accounted_usd=sum(r['charged'] for r in rows),
                    note='Accounted cost retains full reservations for missing/ambiguous usage; not a provider-enforced hard cap.')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def transport(payload,key,timeout=360):
    request=urllib.request.Request('https://api.x.ai/v1/responses',data=json.dumps(payload,ensure_ascii=False).encode(),
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    with opener.open(request,timeout=timeout) as r:
        data=r.read(2000001)
        if len(data)>2000000: raise ValueError('api_response_too_large')
        return json.loads(data)

class Client:
    def __init__(self,settings,ledger,env,send=transport,sleep=time.sleep):
        self.cfg=settings; self.ledger=ledger; self.key=env.get('XAI_API_KEY'); self.sleep=sleep
        self.send=(lambda p,k:transport(p,k,settings.get('request_timeout_seconds',360))) if send is transport else send
    def call(self,run,stage,prompt,schema,budget,system,search=False):
        if self.ledger.summary(run)['accounted_usd']>budget:
            raise BudgetExceeded('reported_cost_exceeds_cap')
        rows=[r for r in self.ledger.rows(run) if r['stage']==stage]
        for row in rows:
            if row['status']=='completed':
                raw=json.loads(row['response_json']); return self.parse(raw,schema)
            if row['status'] in ('reserved','ambiguous'): raise AmbiguousCall('manual_reconciliation_required')
        if rows and rows[-1]['status'] not in ('http_429','http_500','http_502','http_503','http_504'):
            raise RuntimeError('previous_stage_failed')
        if not self.key: raise AuthenticationError('XAI_API_KEY_missing')
        limit=1500 if search else 9000 if stage.startswith(('draft','revision')) else 4500
        limit=min(limit,self.cfg['max_output_tokens'])
        payload=dict(model=self.cfg['model'],input=[{'role':'system','content':system},{'role':'user','content':prompt}],
            reasoning={'effort':self.cfg['reasoning_effort']},max_output_tokens=limit,store=False,
            text={'format':{'type':'json_schema','name':'article_'+stage.replace('-','_'),'strict':True,'schema':schema}})
        if search: payload.update(tools=[{'type':'web_search'}],max_tool_calls=self.cfg['max_search_calls'])
        size=len(json.dumps(payload,ensure_ascii=False).encode())
        if size>self.cfg['max_input_bytes']: raise ValueError('input_limit_exceeded')
        # Live responses may report output (including reasoning) above max_output_tokens.
        # Reserve a separate uncertainty allowance, but do not double-count actual usage.
        allowance=self.cfg.get('reasoning_reservation_tokens',8000)
        reserve=(size*self.cfg['pricing']['input_per_million']+(limit+allowance)*self.cfg['pricing']['output_per_million'])/1e6 + .005
        if search: reserve+=self.cfg['search_reservation_usd']
        for attempt in range(len(rows),self.cfg['max_retries']+1):
            self.ledger.reserve(run,stage,attempt,reserve,budget)
            try:
                raw=self.send(payload,self.key)
            except urllib.error.HTTPError as exc:
                exc.close()
                self.ledger.settle(run,stage,attempt,'http_'+str(exc.code))
                if exc.code in (401,403): raise AuthenticationError('xai_authentication_failed') from None
                if exc.code in (429,500,502,503,504) and attempt<self.cfg['max_retries']:
                    self.sleep(min(8,2**(attempt+1))); continue
                raise RuntimeError('xai_http_'+str(exc.code)) from None
            except Exception as exc:
                self.ledger.settle(run,stage,attempt,'ambiguous',{'error_type':type(exc).__name__})
                raise AmbiguousCall('response_lost_do_not_automatically_retry') from None
            usage=measured_usage(raw,self.cfg['pricing'])
            # Store only text and public usage, not hidden reasoning or secrets.
            safe={'status':raw.get('status'),'output':[x for x in raw.get('output',[]) if x.get('type')=='message'],'usage':raw.get('usage'), 'id':raw.get('id')}
            self.ledger.settle(run,stage,attempt,'completed',usage,safe)
            if self.ledger.summary(run)['accounted_usd']>budget: raise BudgetExceeded('reported_cost_exceeds_cap')
            return self.parse(safe,schema)
        raise RuntimeError('retry_limit_reached')
    @staticmethod
    def parse(raw,schema):
        if raw.get('status')!='completed': raise ValueError('incomplete_api_response')
        text=''.join(c.get('text','') for item in raw.get('output',[]) if item.get('type')=='message' for c in item.get('content',[]) if c.get('type')=='output_text')
        data=json.loads(text); validate(data,schema); return data
