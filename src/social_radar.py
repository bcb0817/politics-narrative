"""Opt-in social topic research. Existing article drafts only; no publication import."""
import argparse
import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from .article_api import Client, BudgetExceeded, AmbiguousCall, process_lock, read_settings_env
from .article_generation import run_article, save, load
from .article_sources import canonical, fetch
from .social_radar_api import RadarLedger, SearchClient

ROOT=Path(__file__).resolve().parent.parent
JST=timezone(timedelta(hours=9))
VERSION='radar-observation-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def instant(value):
    try:
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError,TypeError,AttributeError): return None


def url_key(url):
    p=urlsplit(canonical(url))
    host=p.hostname.lower()
    if host in ('twitter.com','www.twitter.com','www.x.com'): host='x.com'
    path=p.path.rstrip('/') or '/'
    if host=='x.com':
        match=re.fullmatch(r'/[^/]+/status/(\d+)',path)
        if match: return 'https://x.com/i/status/'+match[1]
    query=urlencode([(k,v) for k,v in parse_qsl(p.query) if not k.startswith('utm_') and k not in ('s','t')])
    return urlunsplit((p.scheme,host+((':'+str(p.port)) if p.port else ''),path,query,''))


def metrics(records,target,available=True):
    """Only adapter-verified measurement records. Model-search summaries never enter."""
    empty=dict(unique_critics=None,reactions=None,classifiable=None,unknown=None,
               criticism_ratio=None,unique_authors=None,version=VERSION,
               note='無作為抽出ではない観測サンプル。X全体の割合ではない。')
    if not available: return empty
    groups={}
    for r in records:
        if r.get('purpose')!='measurement' or r.get('provenance')!='verified_post' or not r.get('post_id'): continue
        if r.get('target_id')!=target: continue
        groups.setdefault(r['post_id'],[]).append(r)
    rows=[]
    for group in groups.values():
        r=dict(group[0])
        if len({g.get('stance') for g in group})>1: r['stance']='unknown'
        if len({g.get('author_id') for g in group})>1: r['author_id']=None
        if not r.get('evidence_url') or not r.get('reason') or not r.get('context_sufficient'): r['stance']='unknown'
        rows.append(r)
    eligible=[r for r in rows if r.get('stance') in ('support','criticism','neutral')]
    critics=[r for r in eligible if r['stance']=='criticism']
    authors=lambda rs: len({r['author_id'] for r in rs}) if all(r.get('author_id') for r in rs) else None
    return dict(empty,unique_critics=authors(critics),reactions=len(rows),classifiable=len(eligible),
                unknown=len(rows)-len(eligible),criticism_ratio=len(critics)/len(eligible) if eligible else None,
                unique_authors=authors(rows))


def compare(previous,current):
    if previous is None: return {'state':'初回','new_critics':None}
    fields=('query','window_seconds','limit','classifier','event_id')
    if (any(previous.get(k)!=current.get(k) for k in fields) or
        any(not o.get('complete') or o.get('limit_reached') or not o.get('executed_conditions_verified') for o in (previous,current))):
        return {'state':'比較不能','new_critics':None}
    cutoff=instant(previous.get('at'))
    rows=current.get('records',[])+previous.get('records',[])
    if not cutoff or any(not instant(r.get('created_at')) or not r.get('author_id') for r in rows):
        return {'state':'比較不能','new_critics':None}
    def critics(obs,after=None):
        return {r['author_id'] for r in obs.get('records',[]) if
                r.get('provenance')=='verified_post' and r.get('purpose')=='measurement' and
                r.get('target_id')==obs['event_id'] and r.get('stance')=='criticism' and
                r.get('context_sufficient') and r.get('evidence_url') and r.get('reason') and
                (after is None or instant(r['created_at'])>after)}
    added=critics(current,cutoff)-critics(previous)
    return {'state':'観測上増加' if added else '継続','new_critics':len(added),
            'note':'観測できた新規批判者。X全体の増加ではない。'}


def validate_config(cfg):
    if any(cfg[k] for k in ('auto_publish','external_notifications','score_enabled','retain_raw_social_text')):
        raise ValueError('unsupported_publication_notification_score_or_raw_retention')
    for key,lo,hi in [('max_candidates',1,5),('max_deep_dives',0,2),('max_followups',0,2),
                      ('followup_minutes',30,60),('discoveries_per_day',1,6),('max_tool_calls',1,2),
                      ('max_calls',1,20),('max_retries',0,0),('tracking_hours',1,72),
                      ('lookback_hours',1,72),('model_observation_retention_days',1,30)]:
        if type(cfg.get(key)) is not int or not lo<=cfg[key]<=hi: raise ValueError('invalid_'+key)
    if not cfg['categories'] or cfg['budget_timezone']!='Asia/Tokyo': raise ValueError('invalid_schedule')
    for k in ('run_budget_usd','daily_budget_usd','monthly_budget_usd'):
        if cfg[k] is not None and (type(cfg[k]) not in (int,float) or not math.isfinite(cfg[k]) or cfg[k]<=0): raise ValueError('invalid_'+k)


class Store:
    def __init__(self,path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS radar_topics(id TEXT PRIMARY KEY, anchor TEXT UNIQUE, payload TEXT, first_seen TEXT, deadline TEXT, state TEXT, reason TEXT);
            CREATE TABLE IF NOT EXISTS radar_history(topic TEXT, at TEXT, state TEXT, reason TEXT);
            CREATE TABLE IF NOT EXISTS radar_observations(id TEXT PRIMARY KEY, topic TEXT, purpose TEXT, at TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS radar_jobs(id TEXT PRIMARY KEY, kind TEXT, topic TEXT, due TEXT, state TEXT, reason TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS radar_drafts(topic TEXT PRIMARY KEY, folder TEXT, state TEXT);
            CREATE TABLE IF NOT EXISTS radar_evaluations(topic TEXT PRIMARY KEY, accurate INTEGER, adopted INTEGER, duplicate INTEGER, reason TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS radar_posts(id TEXT PRIMARY KEY, url TEXT, author_id TEXT, created_at TEXT, reply_to TEXT, quote_of TEXT, thread_id TEXT);
            CREATE TABLE IF NOT EXISTS radar_acquisitions(observation TEXT, post TEXT, purpose TEXT, fetched_at TEXT, PRIMARY KEY(observation,post,purpose));
            CREATE TABLE IF NOT EXISTS radar_classifications(observation TEXT, post TEXT, target TEXT, version TEXT, payload TEXT, PRIMARY KEY(observation,post,target));
            CREATE TABLE IF NOT EXISTS radar_engagement(post TEXT, at TEXT, payload TEXT, PRIMARY KEY(post,at));
            '''); c.commit()
    def connect(self):
        c=sqlite3.connect(self.path,timeout=20); c.row_factory=sqlite3.Row; return c
    def rows(self,sql,args=()):
        with closing(self.connect()) as c: return [dict(r) for r in c.execute(sql,args)]
    def execute(self,sql,args=()):
        with closing(self.connect()) as c: c.execute(sql,args); c.commit()
    def transition(self,topic,state,reason,at):
        with closing(self.connect()) as c:
            c.execute('UPDATE radar_topics SET state=?,reason=? WHERE id=?',(state,reason,topic))
            c.execute('INSERT INTO radar_history VALUES(?,?,?,?)',(topic,at.isoformat(),state,reason)); c.commit()
    def ingest(self,item,result,job,at,cfg):
        item=dict(item)
        if item.get('target_kind') not in ('organization','policy','public_figure'):
            return None,False
        cited=set()
        for u in result['citations']:
            try: cited.add(url_key(u))
            except ValueError: pass
        try: original=url_key(item['original_url']) if item.get('original_url') else None
        except ValueError: original=None
        sources=[]
        for u in item['source_urls']:
            try:
                normalized=url_key(u)
                if normalized in cited: sources.append(normalized)
            except ValueError: pass
        # No entity-only merges. Missing original means a held, non-exportable candidate.
        anchor=original if original in cited else digest([item['target'],item['event'],item.get('event_at')])
        topic=digest(anchor)
        item.update(source_urls=sorted(set(sources)),original_url=original if original in cited else None,
                    provenance='model_synthesis',event_at=None,reported_event_at=item.get('event_at'),
                    reaction_confirmation={'status':'unverified','reason':'X Search原文非取得。モデル整理の根拠リンクのみ','urls':sources},
                    fact_confirmation={'status':'unverified','reason':'原典取得・既存記事審査が必要','urls':sources},
                    metrics=metrics([],topic,False),priority='中' if original in cited else '低',
                    priority_reason='原典リンクあり・反応規模未確認' if original in cited else '原典未確認',score=None,discovery_job=job)
        new=False
        with closing(self.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM radar_topics WHERE id=?',(topic,)).fetchone()
            if not old:
                c.execute('INSERT INTO radar_topics VALUES(?,?,?,?,?,?,?)',(topic,anchor,json.dumps(item,ensure_ascii=False),at.isoformat(),(at+timedelta(hours=cfg['tracking_hours'])).isoformat(),'candidate','source_links_require_verification'))
                new=True
            else:
                previous=json.loads(old['payload'])
                additions=set(sources)-set(previous['source_urls'])
                # Model change claims alone never authorize a second draft.
                if additions:
                    previous['source_urls']=sorted(set(previous['source_urls'])|set(sources))
                    previous['pending_update']={'reported_change':item.get('change'),'new_urls':sorted(additions),'verified':False}
                    c.execute('UPDATE radar_topics SET payload=? WHERE id=?',(json.dumps(previous,ensure_ascii=False),topic))
            obs=digest([job,topic,result['purpose']])
            payload=dict(result,topics=[item],observed_at=at.isoformat())
            c.execute('INSERT OR IGNORE INTO radar_observations VALUES(?,?,?,?,?)',(obs,topic,result['purpose'],at.isoformat(),json.dumps(payload,ensure_ascii=False)))
            c.commit()
        return topic,new
    def record_verified(self,observation,records,at):
        """Adapter boundary; not exposed to model output or an untrusted file importer.

        No production direct-post adapter is enabled. Tests use marked synthetic data.
        Text is deliberately not retained; labels require evidence links and reasons.
        """
        with closing(self.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            for r in records:
                if r.get('provenance')!='verified_post' or r.get('purpose') not in ('discovery','measurement','context'):
                    raise ValueError('unverified_record')
                if not re.fullmatch(r'\d+',r['post_id']): raise ValueError('invalid_post_id')
                url=url_key(r['evidence_url'])
                if url!='https://x.com/i/status/'+r['post_id']: raise ValueError('post_url_mismatch')
                created=instant(r.get('created_at'))
                c.execute('INSERT OR IGNORE INTO radar_posts VALUES(?,?,?,?,?,?,?)',(r['post_id'],url,r.get('author_id'),created.isoformat() if created else None,r.get('reply_to'),r.get('quote_of'),r.get('thread_id')))
                c.execute('INSERT OR IGNORE INTO radar_acquisitions VALUES(?,?,?,?)',(observation,r['post_id'],r['purpose'],at.isoformat()))
                fields=('target_id','target_type','stance','expression','action_demands','danger','reason','context_sufficient')
                labels={k:r.get(k) for k in fields}
                c.execute('INSERT OR IGNORE INTO radar_classifications VALUES(?,?,?,?,?)',(observation,r['post_id'],r.get('target_id'),VERSION,json.dumps(labels,ensure_ascii=False)))
                if r.get('engagement') is not None:
                    c.execute('INSERT OR IGNORE INTO radar_engagement VALUES(?,?,?)',(r['post_id'],at.isoformat(),json.dumps(r['engagement'])))
            c.commit()

    def prune(self,at,cfg):
        cutoff=(at-timedelta(days=cfg['model_observation_retention_days'])).isoformat()
        # Keep IDs/usage/dedup tombstones, drop old model-search narratives.
        with closing(self.connect()) as c:
            c.execute("UPDATE radar_observations SET payload=? WHERE at<?",(json.dumps({'expired':True,'limitations':['source_revalidation_required']}),cutoff))
            for row in c.execute('SELECT id,payload FROM radar_topics WHERE first_seen<?',(cutoff,)).fetchall():
                data=json.loads(row['payload'])
                for key in ('target','event','reason','angle','counterarguments','unknowns','pending_update','context_research'):
                    data.pop(key,None)
                data['retention_expired']=True
                c.execute("UPDATE radar_topics SET payload=?,state='ended',reason='retention_expired' WHERE id=?",(json.dumps(data,ensure_ascii=False),row['id']))
            # Cached search response cannot be reused after retention expiry.
            c.execute("UPDATE article_calls SET response_json=NULL WHERE EXISTS (SELECT 1 FROM radar_call_scope s WHERE s.run_id=article_calls.run_id AND s.stage=article_calls.stage AND s.attempt=article_calls.attempt AND s.day<?) AND (stage='discovery' OR stage LIKE 'measurement%' OR stage LIKE 'context%')",(instant(cutoff).astimezone(JST).date().isoformat(),))
            c.commit()
    def reserve_job(self,key,kind,topic,due,payload):
        self.execute('INSERT OR IGNORE INTO radar_jobs VALUES(?,?,?,?,?,?,?)',(key,kind,topic,due.isoformat(),'pending','',json.dumps(payload,ensure_ascii=False)))
    def claim(self,key):
        with closing(self.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            changed=c.execute("UPDATE radar_jobs SET state='running' WHERE id=? AND state IN ('pending','budget_exceeded')",(key,)).rowcount
            c.commit(); return bool(changed)
    def finish(self,key,state,reason=''):
        self.execute('UPDATE radar_jobs SET state=?,reason=? WHERE id=?',(state,reason,key))
    def report(self,ledger=None):
        topics=[]
        for row in self.rows('SELECT * FROM radar_topics ORDER BY first_seen DESC'):
            item=json.loads(row.pop('payload'))
            row['first_seen_jst']=instant(row['first_seen']).astimezone(JST).isoformat()
            topics.append(dict(row,**item))
        ev=self.rows('SELECT * FROM radar_evaluations')
        def ratio(key):
            values=[e[key] for e in ev if e[key] is not None]
            return sum(values)/len(values) if values else None
        adopted=sum(e['adopted']==1 for e in ev)
        costs=None
        if ledger:
            with closing(ledger.connect()) as c:
                costs=c.execute('SELECT COALESCE(SUM(a.charged),0) FROM article_calls a JOIN radar_call_scope s USING(run_id,stage,attempt)').fetchone()[0]
            calls=self.rows('SELECT a.run_id,a.stage,a.attempt,a.status,a.reserved,a.charged,a.usage_json,s.model,s.day,s.batch FROM article_calls a JOIN radar_call_scope s USING(run_id,stage,attempt)')
        else: calls=[]
        return dict(topics=topics,jobs=self.rows('SELECT * FROM radar_jobs ORDER BY due'),
                    evaluations=dict(evaluated=len(ev),accuracy=ratio('accurate'),adoption=ratio('adopted'),duplicate=ratio('duplicate'),
                                     accounted_cost_per_adopted=costs/adopted if adopted and costs is not None else None),
                    accounted_usd=costs,api_calls=calls,auto_publish=False,external_notifications=False)


def political(item,cfg): return bool(set(item['categories']) & set(cfg['political_categories']))


def confirm_update(store,parent,url,quote,event_at,description,at,cfg,reader=fetch):
    """Human-confirmed significance + fetched evidence; not inferred from rewording."""
    rows=store.rows('SELECT * FROM radar_topics WHERE id=?',(parent,))
    event=instant(event_at)
    if not rows or not event or event>at or not quote.strip() or not description.strip():
        raise ValueError('update_needs_existing_topic_timestamp_quote_description')
    if event<instant(rows[0]['first_seen']): raise ValueError('update_predates_initial_observation')
    source=reader(url,1,max_chars=10000)
    if source.get('error') or quote not in source['text']: raise ValueError('update_evidence_unverified')
    item=json.loads(rows[0]['payload'])
    if item.get('retention_expired'): raise ValueError('original_expired_research_again')
    update={'source_url':canonical(url),'evidence_quote':quote,'event_date':event.isoformat(),'description':description}
    anchor=rows[0]['anchor']+'#update='+digest([url_key(url),quote,event.isoformat()])
    key=digest(anchor)
    item.update(title=item['title']+'（確認済み続報）',substantive_update=update,parent_topic=parent,
                source_urls=list(dict.fromkeys([canonical(url)]+item['source_urls'])),
                change=description,discovery_job='draft-'+key)
    with closing(store.connect()) as c:
        c.execute('INSERT OR IGNORE INTO radar_topics VALUES(?,?,?,?,?,?,?)',(key,anchor,json.dumps(item,ensure_ascii=False),at.isoformat(),(at+timedelta(hours=cfg['tracking_hours'])).isoformat(),'candidate','human_confirmed_update_with_fetched_quote'))
        c.commit()
    store.reserve_job('draft-'+key,'draft',key,at,{})
    return key


def export_draft(store,topic,root,cfg,article_cfg,client,at):
    row=store.rows('SELECT * FROM radar_topics WHERE id=?',(topic,))[0]
    item=json.loads(row['payload'])
    if item.get('retention_expired') or not political(item,cfg) or not item.get('original_url') or not item['source_urls']:
        store.transition(topic,'held','not_political_or_original_missing',at); return None
    folder=root/'outputs/articles'/('radar-'+topic[:24])
    store.execute('INSERT OR IGNORE INTO radar_drafts VALUES(?,?,?)',(topic,str(folder),'pending'))
    data=dict(theme=item['title'],urls=list(dict.fromkeys([item['original_url']]+item['source_urls']))[:article_cfg['max_sources']],
              region='日本',audience='政治に詳しくない一般読者',format='ニュースの背景解説',
              editorial='既存の編集方針。反発の存在と前提の真偽を分離。検索要約を事実の根拠にしない。',
              research_mode='provided',budget_usd=min(article_cfg['budget_usd'],cfg['run_budget_usd']),min_chars=2500,max_chars=4000)
    if item.get('substantive_update'): data['substantive_update']=item['substantive_update']
    if (folder/'run.json').exists():
        prior=load(folder/'run.json'); data=prior['input']; article_cfg=prior['config']
    result=run_article(folder,data,article_cfg,client=client)
    store.execute('UPDATE radar_drafts SET state=? WHERE topic=?',(result['status'],topic))
    state='drafted' if result['status']=='ready_for_review' else 'held'
    store.transition(topic,state,'article_'+result['status'],at)
    item['fact_confirmation']={'status':'reviewed_draft' if state=='drafted' else 'unverified',
        'reason':'既存記事の資料取得・独立審査を通過。公開前に人間確認' if state=='drafted' else '既存記事審査が未完了または根拠不足',
        'urls':data['urls'],'review_file':str(folder/'review.json')}
    store.execute('UPDATE radar_topics SET payload=? WHERE id=?',(json.dumps(item,ensure_ascii=False),topic))
    return result


def run_job(store,job,at,cfg,search,article_client=None,root=ROOT,article_cfg=None):
    if not store.claim(job['id']): return 'already_claimed'
    search.ledger.batch=job['id']
    payload=json.loads(job['payload'])
    start=at-timedelta(hours=cfg['lookback_hours'])
    try:
        if job['kind']=='discovery':
            query=payload['category']+'で新しい出来事への批判・謝罪要求・反発の根拠。最大5話題、弱ければ0件。'
            result=search.search(job['id'],'discovery',query,start,at,'discovery')
            fresh=[]
            for item in result['topics']:
                topic,new=store.ingest(item,result,job['id'],at,cfg)
                if topic:
                    row=store.rows('SELECT state,payload FROM radar_topics WHERE id=?',(topic,))[0]
                    owned=json.loads(row['payload']).get('discovery_job')==job['id']
                    if (new or owned) and row['state'] not in ('drafted','ended'): fresh.append(topic)
            # Prefer identifiable original, then diversity. Never pretend author counts known.
            candidates=[store.rows('SELECT * FROM radar_topics WHERE id=?',(t,))[0] for t in fresh]
            candidates.sort(key=lambda r:(not bool(json.loads(r['payload']).get('original_url')),r['id']))
            chosen=[]; seen=set()
            for row in candidates:
                item=json.loads(row['payload']); category=tuple(item['categories'])
                if len(chosen)<cfg['max_deep_dives'] and item.get('original_url') and category not in seen:
                    chosen.append(row); seen.add(category)
                else: store.transition(row['id'],'held','missing_original_or_depth_diversity_limit',at)
            for row in chosen:
                topic=row['id']; item=json.loads(row['payload'])
                store.transition(topic,'verifying','identifiable_original; critic_authors_unknown',at)
                neutral=search.search(job['id'],'measurement_'+topic[:16],item['event']+'に対する反応。賛否を指定せず取得。',start,at,'measurement')
                context=search.search(job['id'],'context_'+topic[:16],item['event']+'の原典・当事者の説明・有力な反論を確認。',start,at,'context')
                for observation in (neutral,context):
                    store.execute('INSERT OR IGNORE INTO radar_observations VALUES(?,?,?,?,?)',(digest([job['id'],topic,observation['purpose']]),topic,observation['purpose'],at.isoformat(),json.dumps(observation,ensure_ascii=False)))
                item['context_research']=context
                item['measurement_status']='unavailable; model_synthesis_not_raw_posts'
                item['comparison']={'state':'初回','new_critics':None}
                store.execute('UPDATE radar_topics SET payload=? WHERE id=?',(json.dumps(item,ensure_ascii=False),topic))
                # Need a direct measurement adapter before claiming observed growth.
                store.transition(topic,'tracking','unmeasured; source_link_research_only',at)
                if article_client and political(item,cfg):
                    draft=export_draft(store,topic,root,cfg,article_cfg,article_client,at)
                    if draft and draft['status']=='budget_exceeded': raise BudgetExceeded('article_budget_exceeded')
                    if draft and draft['status']=='failed': raise AmbiguousCall('article_failed; inspect_run_and_cost_before_retry')
                if cfg['max_followups']:
                    store.reserve_job('follow-'+topic+'-1','followup',topic,at+timedelta(minutes=cfg['followup_minutes']),{'index':1,'query':item['event']+'に対する反応。賛否を指定せず取得。'})
        elif job['kind']=='draft':
            if not article_client: raise ValueError('article_client_required')
            result=export_draft(store,job['topic'],root,cfg,article_cfg,article_client,at)
            if result and result['status']=='budget_exceeded': raise BudgetExceeded('article_budget_exceeded')
            if result and result['status']=='failed': raise AmbiguousCall('article_failed_review_required')
        else:
            row=store.rows('SELECT * FROM radar_topics WHERE id=?',(job['topic'],))[0]
            if instant(row['deadline'])<=at or json.loads(row['payload']).get('retention_expired'):
                store.transition(row['id'],'ended','tracking_deadline',at)
            else:
                result=search.search(job['id'],'measurement',payload['query'],start,at,'measurement')
                store.execute('INSERT OR IGNORE INTO radar_observations VALUES(?,?,?,?,?)',(job['id'],row['id'],'measurement',at.isoformat(),json.dumps(result,ensure_ascii=False)))
                # Search cannot demonstrate comparable complete retrieval. Stop, not "accelerating".
                store.transition(row['id'],'held','comparison_unavailable; raw_results_not_returned',at)
        store.finish(job['id'],'completed'); return 'completed'
    except (BudgetExceeded,AmbiguousCall) as exc:
        state='budget_exceeded' if isinstance(exc,BudgetExceeded) else 'ambiguous'
        store.finish(job['id'],state,str(exc))
        for row in store.rows("SELECT id FROM radar_topics WHERE state IN ('verifying','tracking')"):
            store.transition(row['id'],'held',state,at)
        return state
    except Exception as exc:
        store.finish(job['id'],'failed',type(exc).__name__); return 'failed'


def main(argv=None):
    p=argparse.ArgumentParser(description='Social radar: research and reviewed article drafts only')
    p.add_argument('command',choices=['run','tick','report','evaluate','confirm-update','dry-run'])
    p.add_argument('--config',type=Path,default=ROOT/'config/social_radar.json')
    p.add_argument('--topic'); p.add_argument('--accurate',choices=['yes','no']); p.add_argument('--adopted',choices=['yes','no'])
    p.add_argument('--job',help='Resume an existing budget-exceeded job; ambiguous/running jobs are never reset')
    p.add_argument('--duplicate',choices=['yes','no']); p.add_argument('--reason')
    p.add_argument('--source'); p.add_argument('--quote'); p.add_argument('--event-at')
    args=p.parse_args(argv); cfg=load(args.config); validate_config(cfg)
    if args.job and args.command!='run': p.error('--job requires run')
    at=datetime.now(timezone.utc)
    if not cfg['enabled']:
        print(json.dumps({'status':'disabled','api_calls':0})); return 0
    if args.command=='dry-run':
        print(json.dumps({'status':'dry_run','api_calls':0,'auto_publish':False,'budget_configured':all(cfg[k] for k in ('run_budget_usd','daily_budget_usd','monthly_budget_usd'))})); return 0
    with process_lock(ROOT/'outputs/articles/generation.lock'):
        store=Store(ROOT/'data/bot_metrics.db')
        if args.command=='confirm-update':
            if not all((args.topic,args.source,args.quote,args.event_at,args.reason)): p.error('--topic --source --quote --event-at --reason required')
            confirm_update(store,args.topic,args.source,args.quote,args.event_at,args.reason,at,cfg)
        if args.command=='evaluate':
            if not args.topic or not args.reason or not store.rows('SELECT id FROM radar_topics WHERE id=?',(args.topic,)): p.error('existing --topic and --reason required')
            vals=[None if v is None else int(v=='yes') for v in (args.accurate,args.adopted,args.duplicate)]
            store.execute('INSERT OR REPLACE INTO radar_evaluations VALUES(?,?,?,?,?,?)',(args.topic,*vals,args.reason,at.isoformat()))
        article_cfg=load(ROOT/'config/article_generation.json')
        env=read_settings_env(ROOT/'.env') if args.command in ('run','tick') else {}
        ledger=RadarLedger(store.path,article_cfg,env,cfg,lambda:at)
        store.prune(at,cfg)
        search=SearchClient(cfg,ledger,env)
        if args.command in ('run','tick'):
            if not all(cfg[k] for k in ('run_budget_usd','daily_budget_usd','monthly_budget_usd')) or (args.command=='tick' and not cfg['paid_schedule_enabled']):
                print(json.dumps({'status':'paid_execution_disabled','api_calls':0})); return 0
            local=at.astimezone(JST); slot=int(local.hour*cfg['discoveries_per_day']/24)
            category=cfg['categories'][(local.toordinal()*cfg['discoveries_per_day']+slot)%len(cfg['categories'])]
            key='radar-'+local.strftime('%Y%m%d')+'-'+str(slot)
            if not args.job: store.reserve_job(key,'discovery',None,at,{'category':category})
            # No sleeping and no modification of the normal-post scheduler.
            jobs=store.rows("SELECT * FROM radar_jobs WHERE state IN ('pending','budget_exceeded') AND due<=? ORDER BY due LIMIT 3",(at.isoformat(),))
            if args.job:
                jobs=store.rows("SELECT * FROM radar_jobs WHERE id=? AND state IN ('pending','budget_exceeded')",(args.job,))
                if not jobs: p.error('job_missing_or_not_resumable')
            outcomes=[]
            for job in jobs:
                if not args.job and job['kind']=='discovery' and job['id']!=key:
                    store.finish(job['id'],'ended','stale_discovery_slot'); continue
                client=Client(article_cfg,ledger,env)
                outcomes.append(run_job(store,job,at,cfg,search,client,ROOT,article_cfg))
        report=store.report(ledger)
        save(ROOT/'outputs/social_radar/latest.json',report)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 2 if args.command in ('run','tick') and any(s in ('failed','ambiguous','budget_exceeded') for s in outcomes) else 0


if __name__=='__main__': raise SystemExit(main())
