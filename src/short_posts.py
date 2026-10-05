"""Scheduled ordinary X news posts. Persistent delivery; no force/retry bypass."""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from .article_api import Client, Ledger, BudgetExceeded, AmbiguousCall, AuthenticationError, process_lock, read_settings_env
from .article_generation import ROOT, load, save, digest
from .article_schema import obj, arr, S, I, B, validate
from .article_sources import fetch, rank
from .rss_candidates import collect, date
from .manual_x_smoke import request as x_request, environment as x_environment

JST=timezone(timedelta(hours=9))
DRAFT=obj(text=S, claims=arr(obj(text=S, segment_ids=arr(I))))
REVIEW=obj(approved=B, coverage_complete=B, attribution_ok=B, conditions_preserved=B,
           checks=arr(obj(claim_index=I, supported=B, segment_ids=arr(I))), issues=arr(S))

def timestamp(): return datetime.now(timezone.utc)
def stamp(at): return at.astimezone(JST).isoformat()
def weighted(text):
    # Conservative upper bound: all non-ASCII count twice, including emoji components.
    return sum(1 if ord(c)<128 else 2 for c in unicodedata.normalize('NFC',text))

def segments(source):
    return {str(i):line for i,line in enumerate(source['text'].splitlines(),1) if line.strip()}

def validate_post(draft, review, source):
    validate(draft,DRAFT); validate(review,REVIEW)
    errors=[]; text=draft['text']; parts=segments(source)
    if not text.startswith('NHKによると、'): errors.append('missing_attribution')
    if not 40<=len(text) or weighted(text)>280: errors.append('length')
    if re.search(r'https?://|www\.|[A-Za-z0-9-]+\.[A-Za-z]{2,}|[@#＃＠]|📌|売国|完全論破|ネット騒然',text): errors.append('style_or_url')
    if re.search(r'本文.{0,12}(切れ|途中)|取得.{0,6}(失敗|でき)|見出しだけ|資料不足',text): errors.append('internal_process_in_post')
    if not draft['claims']: errors.append('no_claims')
    if not all(review[k] for k in ('approved','coverage_complete','attribution_ok','conditions_preserved')) or review['issues']:
        errors.append('review_rejected')
    checks={c['claim_index']:c for c in review['checks']}
    if len(checks)!=len(review['checks']) or set(checks)!=set(range(len(draft['claims']))): errors.append('review_coverage')
    for i,claim in enumerate(draft['claims']):
        ids=claim['segment_ids']; check=checks.get(i,{})
        if not claim['text'].strip() or claim['text'] not in text: errors.append('claim_not_in_text')
        if not ids or any(str(n) not in parts for n in ids): errors.append('evidence_missing')
        if not check.get('supported') or not check.get('segment_ids') or any(str(n) not in parts for n in check.get('segment_ids',[])):
            errors.append('review_evidence_missing')
    if source.get('error'): errors.append('source_failed')
    compact=lambda s:re.sub(r'\s+','',s)
    if SequenceMatcher(None,compact(text),compact(source['text']),autojunk=False).find_longest_match().size>45:
        errors.append('source_copy_too_long')
    return sorted(set(errors))

class Store:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS short_items(
              id TEXT PRIMARY KEY,url TEXT UNIQUE,title TEXT,created TEXT,expires TEXT,
              status TEXT,body TEXT,post_id TEXT,reason TEXT,config_hash TEXT);
              CREATE TABLE IF NOT EXISTS short_x_calls(
              id INTEGER PRIMARY KEY,month TEXT,day TEXT,kind TEXT,reserved REAL,status TEXT);
              CREATE TABLE IF NOT EXISTS short_events(at TEXT,reason TEXT);
              CREATE TABLE IF NOT EXISTS short_state(key TEXT PRIMARY KEY,value TEXT);'''); c.commit()
    def connect(self):
        c=sqlite3.connect(self.path,timeout=20); c.row_factory=sqlite3.Row; return c
    def items(self):
        with closing(self.connect()) as c: return [dict(r) for r in c.execute('SELECT * FROM short_items ORDER BY created DESC')]
    def put(self,item,at,cfg):
        identity=hashlib.sha256(item['url'].encode()).hexdigest()
        with closing(self.connect()) as c:
            c.execute('INSERT INTO short_items VALUES(?,?,?,?,?,?,?,?,?,?)',
                (identity,item['url'],item['title'],stamp(at),item['expires_at'],'generating',None,None,None,digest(cfg))); c.commit()
        return identity
    def update(self,identity,**fields):
        if set(fields)-{'status','body','post_id','reason','created'}: raise ValueError('invalid_update')
        with closing(self.connect()) as c:
            c.execute('UPDATE short_items SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',tuple(fields.values())+(identity,)); c.commit()
    def state(self,key,value=None):
        with closing(self.connect()) as c:
            if value is not None:
                c.execute('INSERT OR REPLACE INTO short_state VALUES(?,?)',(key,value)); c.commit(); return value
            row=c.execute('SELECT value FROM short_state WHERE key=?',(key,)).fetchone(); return row[0] if row else None
    def event(self,reason,at):
        with closing(self.connect()) as c: c.execute('INSERT INTO short_events VALUES(?,?)',(stamp(at),reason)); c.commit()

def gate(store,cfg,at,env):
    rows=store.items(); today=stamp(at)[:10]
    confirmed=[r for r in rows if r['status']=='published' and r['created'].startswith(today)]
    if store.state('halt'): return 'halt:'+store.state('halt')
    if any(r['status'] in ('sending','ambiguous') for r in rows): return 'ambiguous_delivery_requires_reconciliation'
    if len(confirmed)>=min(cfg['daily_target'],int(env['X_POST_CREATE_MAX_PER_DAY'])): return 'daily_target_reached'
    if at.astimezone(JST).hour>=cfg['end_hour_jst']: return 'outside_window'
    due=sum(t<=at.astimezone(JST).strftime('%H:%M') for t in cfg['slots_jst'])
    if len(confirmed)>=due: return 'not_due'
    sent=[date(r['created']) for r in rows if r['status']=='published']
    if sent and (at-max(sent)).total_seconds()<cfg['min_interval_minutes']*60: return 'minimum_interval'
    return None

def reserve_x(store,ledger,env,kind,cfg,at):
    amount=cfg['x_user_read_cost_usd'] if kind=='account' else cfg['x_post_cost_usd']
    day=stamp(at)[:10]; month=day[:7]
    def limit(k):
        v=float(env[k])
        if not math.isfinite(v) or v<0: raise ValueError('invalid_budget')
        return v
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        daily,monthly=c.execute('SELECT COALESCE(SUM(day=?),0),COUNT(*) FROM short_x_calls WHERE month=? AND kind=?',(day,month,kind)).fetchone()
        prefix='X_OWNED_READ' if kind=='account' else 'X_POST_CREATE'
        if daily>=limit(prefix+'_MAX_PER_DAY') or monthly>=limit(prefix+'_MAX_PER_MONTH'): raise BudgetExceeded('x_quota')
        total,_=ledger.legacy_spend(c,month)
        model=c.execute('SELECT COALESCE(SUM(charged),0) FROM article_calls WHERE month=?',(month,)).fetchone()[0]
        xcost=c.execute('SELECT COALESCE(SUM(reserved),0) FROM short_x_calls WHERE month=?',(month,)).fetchone()[0]
        names={r[0] for r in c.execute('SELECT name FROM sqlite_master')}
        if 'x_manual_smoke' in names: xcost+=c.execute('SELECT COALESCE(SUM(reserved_usd),0) FROM x_manual_smoke WHERE month=?',(month,)).fetchone()[0]
        if xcost+amount>limit('X_MONTHLY_BUDGET_USD')-limit('X_BUDGET_RESERVE_USD') or total+model+amount>limit('TOTAL_MONTHLY_API_BUDGET_USD')-limit('TOTAL_BUDGET_RESERVE_USD'):
            raise BudgetExceeded('x_budget')
        cur=c.execute('INSERT INTO short_x_calls(month,day,kind,reserved,status) VALUES(?,?,?,?,?)',(month,day,kind,amount,'reserved')); c.commit(); return cur.lastrowid

def call_x(store,ledger,env,cfg,method,path,at,send,payload=None):
    call_id=reserve_x(store,ledger,env,'account' if method=='GET' else 'post',cfg,at)
    try: status,data=send(method,path,env,payload)
    except Exception:
        with closing(store.connect()) as c: c.execute('UPDATE short_x_calls SET status=? WHERE id=?',('ambiguous',call_id)); c.commit()
        raise
    with closing(store.connect()) as c: c.execute('UPDATE short_x_calls SET status=? WHERE id=?',('http_'+str(status),call_id)); c.commit()
    if status in (401,402,403): store.state('halt','x_http_'+str(status))
    return status,data

def account_check(store,ledger,env,cfg,at,send):
    # One user-context check per JST day, tied to the exact credential set.
    fingerprint=digest({'credentials':{k:env.get(k) for k in ('API_KEY','API_KEY_SECRET','ACCESS_TOKEN','ACCESS_TOKEN_SECRET')},
                        'expected_id':cfg['expected_user_id'],'expected_username':cfg['expected_username']})
    cache=stamp(at)[:10]+':'+fingerprint
    if store.state('account_verified')==cache: return
    status,data=call_x(store,ledger,env,cfg,'GET','/2/users/me',at,send)
    user=data.get('data') or {}
    if status!=200 or user.get('id')!=cfg['expected_user_id'] or user.get('username','').casefold()!=cfg['expected_username'].casefold():
        if status==200: store.state('halt','account_mismatch')
        raise ValueError('account_check_failed')
    store.state('account_verified',cache)

def publish(store,ledger,env,cfg,item,at,send):
    if not cfg['enabled'] or any(env.get(k,'').lower()!='true' for k in ('POST_ENABLED','X_POST_ENABLED')): return 'disabled'
    identity=item['id']
    item=next((r for r in store.items() if r['id']==identity),None)
    if not item: return 'not_ready_or_expired'
    if item['status']!='ready' or date(item['expires'])<=at: return 'not_ready_or_expired'
    reason=gate(store,cfg,at,env)
    if reason: return reason
    account_check(store,ledger,env,cfg,at,send)
    # Reserve before persisting sending, then never resend after uncertainty.
    call_id=reserve_x(store,ledger,env,'post',cfg,at)
    with closing(store.connect()) as c:
        c.execute('BEGIN IMMEDIATE')
        changed=c.execute("UPDATE short_items SET status='sending',created=? WHERE id=? AND status='ready'",(stamp(at),identity)).rowcount
        c.commit()
    if changed!=1: return 'already_claimed'
    try:
        status,response=send('POST','/2/tweets',env,{'text':item['body']})
        data=response.get('data') or {}
        if status==201 and re.fullmatch(r'\d+',str(data.get('id',''))) and data.get('text')==item['body']:
            store.update(identity,status='published',post_id=data['id'],reason=None); outcome='published'
        elif status in (400,401,402,403,404,422,429):
            store.update(identity,status='rejected',reason='x_http_'+str(status)); outcome='rejected'
            if status in (401,402,403): store.state('halt','x_http_'+str(status))
        else:
            store.update(identity,status='ambiguous',reason='x_http_'+str(status)); outcome='ambiguous'
        with closing(store.connect()) as c: c.execute('UPDATE short_x_calls SET status=? WHERE id=?',('http_'+str(status),call_id)); c.commit()
        return outcome
    except Exception as exc:
        store.update(identity,status='ambiguous',reason=type(exc).__name__)
        return 'ambiguous'

def generate(store,client,cfg,item,at,folder,source_fetch=fetch):
    previous=next((r for r in store.items() if r['url']==item['url']),None)
    revision=previous is not None
    if revision:
        if previous['status']!='needs_research' or not previous['reason'] or not set(previous['reason'].split(',')) <= {'claim_not_in_text','source_copy_too_long','length','internal_process_in_post'}:
            return 'not_repairable'
        identity=previous['id']
        if any(r['stage']=='short_revision' for r in client.ledger.rows('short_'+identity)): return 'revision_limit'
        store.update(identity,status='generating',reason='revision_started')
    else: identity=store.put(item,at,cfg)
    run='short_'+identity
    folder=Path(folder)/identity; folder.mkdir(parents=True,exist_ok=True)
    try:
        source=load(folder/'source.json') if revision else source_fetch(item['url'],1,max_chars=cfg['source_max_chars']); save(folder/'source.json',source)
        if source.get('error'): raise ValueError('source_failed')
        policy=(ROOT/'config/short_editorial.md').read_text(encoding='utf-8')
        packet=dict(title=item['title'],published_at=item['published_at'],source_url=item['url'],segments=segments(source))
        if revision: packet.update(previous_draft=load(folder/'draft.json'),repair_errors=previous['reason'],task='問題箇所を修正。同じ根拠だけを使う。claims.textは出力textの完全一致の部分文字列。資料文そのものではない。')
        draft=client.call(run,'short_revision' if revision else 'short_draft',json.dumps(packet,ensure_ascii=False),DRAFT,cfg['generation_budget_usd'],policy)
        save(folder/'draft.json',draft)
        review=client.call(run,'short_revision_review' if revision else 'short_review',json.dumps(dict(article=draft,source=packet,task='独立検証。全主要主張が列挙され、提示原文の段落番号で裏付けられる場合だけ承認。報道の帰属、政治・司法の段階、条件、数字、引用の文脈、誇張、全文の網羅を検証。反論未確認を自認扱いしない。claim_indexは0始まり。問題はissuesへ。'),ensure_ascii=False),REVIEW,cfg['generation_budget_usd'],policy)
        save(folder/'review.json',review)
        errors=validate_post(draft,review,source)
        previous=[r for r in store.items() if r['status'] in ('ready','published','sending','ambiguous') and r['body']]
        if any(SequenceMatcher(None,draft['text'],r['body']).ratio()>.8 for r in previous): errors.append('similar_text')
        if errors:
            store.update(identity,status='needs_research',reason=','.join(errors)); return 'needs_research'
        save(folder/'evidence.json',[{'claim':c['text'],'quotes':[segments(source)[str(n)] for n in c['segment_ids']]} for c in draft['claims']])
        store.update(identity,status='ready',body=draft['text'],reason=None); return 'ready'
    except (BudgetExceeded,AmbiguousCall,AuthenticationError) as exc:
        store.update(identity,status='generation_failed',reason=type(exc).__name__)
        if isinstance(exc,AuthenticationError): store.state('halt','xai_authentication')
        return type(exc).__name__
    except Exception as exc:
        store.update(identity,status='generation_failed',reason=type(exc).__name__); return 'generation_failed'
    finally: save(folder/'usage.json',client.ledger.summary(run))

def report(store,cfg,at,outcome):
    today=stamp(at)[:10]; yesterday=(at.astimezone(JST)-timedelta(days=1)).date().isoformat()
    rows=store.items()
    def daily(day):
        selected=[r for r in rows if r['created'].startswith(day)]
        count=sum(r['status']=='published' for r in selected)
        return dict(date_jst=day,published=count,target=cfg['daily_target'],missing=max(0,cfg['daily_target']-count),
                    reasons=[{'status':r['status'],'reason':r['reason']} for r in selected if r['status']!='published'])
    return dict(at=stamp(at),outcome=outcome,today=daily(today),yesterday=daily(yesterday),halt=store.state('halt'),
                posts=[{'text':r['body'],'url':f'https://x.com/{cfg["expected_username"]}/status/{r["post_id"]}'} for r in rows if r['status']=='published'][:10])

def run_cycle(root=ROOT,send=x_request,clock=timestamp,collect_fn=collect,source_fetch=fetch,client_factory=Client,mode='run'):
    cfg=load(root/'config/short_posts.json'); at=clock()
    if mode=='dry-run': return {'outcome':'dry_run','target':cfg['daily_target'],'enabled':cfg['enabled'],'publishes':False}
    with process_lock(root/'outputs/articles/generation.lock'):
        store=Store(root/'data/bot_metrics.db')
        if mode=='status': return report(store,cfg,at,'status')
        env=x_environment(root/'.env'); env.update(read_settings_env(root/'.env'))
        outcome='disabled'
        if not cfg['enabled'] or any(env.get(k,'').lower()!='true' for k in ('POST_ENABLED','X_POST_ENABLED')):
            return report(store,cfg,at,outcome)
        model_cfg=load(root/'config/article_generation.json'); model_cfg['max_output_tokens']=cfg['max_output_tokens']
        ledger=Ledger(store.path,model_cfg,env,clock); client=client_factory(model_cfg,ledger,env)
        try:
            reason=gate(store,cfg,at,env)
            if mode=='run' and reason: outcome=reason
            else:
                account_check(store,ledger,env,cfg,at,send) if mode=='run' else None
                ready=next((r for r in store.items() if r['status']=='ready' and date(r['expires'])>at and r['config_hash']==digest(cfg)),None)
                if not ready:
                    day=stamp(at)[:10]
                    if sum(r['created'].startswith(day) for r in store.items())>=cfg['max_generation_attempts_per_day']:
                        outcome='generation_attempt_limit'
                    else:
                        rss_cfg=load(root/'config/rss_sources.json')
                        news=collect_fn(rss_cfg['feeds'],root/'data/rss_candidates.json',at=at,hours=rss_cfg['max_age_hours'])
                        previous=store.items()
                        repairs=[r for r in previous if r['status']=='needs_research' and r['reason'] and set(r['reason'].split(','))<={'claim_not_in_text','source_copy_too_long','length','internal_process_in_post'} and not any(c['stage']=='short_revision' for c in ledger.rows('short_'+r['id']))]
                        candidates=rank(news['candidates'],[{'urls':[r['url']],'theme':r['title']} for r in previous if r not in repairs],at)
                        outcome='no_eligible_candidates'
                        for candidate in [c for c in candidates if c['eligible'] and '動静' not in c['title'] and c['url'].startswith('https://news.web.nhk/')][:cfg['max_candidates_per_run']]:
                            item=next(i for i in news['candidates'] if i['url']==candidate['url'])
                            # Initial auto-publication is limited to the verified NHK political feed.
                            if not item['url'].startswith('https://news.web.nhk/'):
                                continue
                            outcome=generate(store,client,cfg,item,clock(),root/'outputs/short_posts',source_fetch)
                            if outcome in ('ready','BudgetExceeded','AmbiguousCall','AuthenticationError'): break
                        ready=next((r for r in store.items() if r['status']=='ready' and date(r['expires'])>clock() and r['config_hash']==digest(cfg)),None)
                if ready:
                    artifact=root/'outputs/short_posts'/ready['id']
                    errors=validate_post(load(artifact/'draft.json'),load(artifact/'review.json'),load(artifact/'source.json'))
                    if errors:
                        store.update(ready['id'],status='needs_research',reason=','.join(errors)); outcome='needs_research'
                    else:
                        outcome=publish(store,ledger,env,cfg,ready,clock(),send) if mode=='run' else 'ready_not_published'
        except Exception as exc: outcome=type(exc).__name__
        store.event(outcome,clock()); result=report(store,cfg,clock(),outcome)
        save(root/'data/short_posts/latest.json',result); return result

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('run','prepare','status','dry-run'),default='status',nargs='?')
    args=parser.parse_args(argv)
    print(json.dumps(run_cycle(mode=args.mode),ensure_ascii=False))
    return 0

if __name__=='__main__': raise SystemExit(main())
