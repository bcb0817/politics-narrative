"""Idempotent incident/skip audit and ledger-derived expense attribution."""
import hashlib
import json
import re
import os
from collections import Counter, defaultdict
from contextlib import closing
from datetime import timedelta

from metrics_db import connect
from reach_features import parse_time
from reach_storage import ensure, incident


def sync_publications(path, now):
    """Import only confirmed IDs from existing paths; never send or infer text."""
    ensure(path)
    candidates=[]
    with closing(connect(path)) as conn:
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        known={r[0] for r in conn.execute('SELECT tweet_id FROM published_posts')}
        if 'short_video_publications' in tables:
            for r in conn.execute("SELECT * FROM short_video_publications WHERE platform='x' AND status='published' AND external_post_id IS NOT NULL"):
                candidates.append((r['external_post_id'],r['published_at'],'','short_video','video'))
        if 'disaster_update_publications' in tables:
            for r in conn.execute("SELECT * FROM disaster_update_publications WHERE platform='x' AND status='published' AND external_post_id IS NOT NULL"):
                candidates.append((r['external_post_id'],r['published_at'],r['candidate_text'],'disaster_update','text'))
        if 'x_research_analysis_posts' in tables:
            for r in conn.execute("SELECT * FROM x_research_analysis_posts WHERE status='published'"):
                for ident in json.loads(r['tweet_ids_json'] or '[]'):
                    candidates.append((str(ident),r['published_at'],'','x_research_analysis','text'))
        if 'x_delivery' in tables:
            for r in conn.execute("SELECT * FROM x_delivery WHERE status='published' AND external_id IS NOT NULL"):
                candidates.append((r['external_id'],r['updated_at'],'','delivery_recovered',None))
    from metrics_db import insert_published
    count=0
    for ident,at,text,kind,media in candidates:
        posted=parse_time(at)
        if not ident or ident in known or not posted or now-posted<timedelta(minutes=10): continue
        inserted=insert_published(None,{'tweet_id':ident,'tweet_text':text,'posted_at_jst':at,
                                       'post_type':kind,'media_type':media,'prompt_version':'unknown'},path)
        if inserted is not None: known.add(ident); count+=1
    return count


def classify(reason):
    if any(s in reason for s in ('budget','daily_cap','monthly_cap')): return 'budget_stop', 'budget'
    if any(s in reason for s in ('ambiguous','publish_unknown')): return 'ambiguous', 'publish'
    if any(s in reason for s in ('generation_failed','api_call','timeout')): return 'technical_loss', 'generation'
    if any(s in reason for s in ('duplicate','cooldown','interval','expiry','expired')): return 'quality_hold', 'eligibility'
    if any(s in reason for s in ('quality','score','risk','safe','relevance','qualified','limit')): return 'quality_hold', 'selection'
    return 'unclassified_skip', 'unknown'


def audit(path, now, log_path=None):
    ensure(path)
    sync_publications(path,now)
    since = now.replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=28)
    if log_path and log_path.is_file():
        with log_path.open(encoding='utf-8') as handle:
            for line in handle:
                if len(line) > 65536: continue
                try: entry = json.loads(line)
                except ValueError: continue
                at = parse_time(entry.get('ts_jst'))
                if not at or not since <= at <= now or entry.get('decision') != 'skip': continue
                raw = str(entry.get('reason') or 'unknown')
                reason = raw if re.fullmatch(r'[A-Za-z0-9_]{1,100}',raw) else 'unclassified'
                category, stage = classify(reason)
                key = hashlib.sha256((str(entry.get('slot_key'))+at.isoformat()+reason).encode()).hexdigest()
                incident('attempt:'+key, at.isoformat(), category, stage, reason, path=path)
    with closing(connect(path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        pending = []
        for r in conn.execute('SELECT id,detected_at,status FROM integrated_research_corrections WHERE julianday(detected_at)>=julianday(?)',(since.isoformat(),)):
            pending.append(('correction:'+str(r['id']),r['detected_at'],'correction','evidence','source_correction','x',str(r['id']),r['status'] in ('resolved','applied')))
        if 'x_delivery' in tables:
            for r in conn.execute("SELECT * FROM x_delivery WHERE status IN ('sending','ambiguous')"):
                at=parse_time(r['created_at'])
                if r['status']=='sending' and at and now-at < timedelta(minutes=10): continue
                pending.append(('delivery:'+r['delivery_key'],r['created_at'],'ambiguous','publish',r['status'],'x',r['delivery_key'],False))
            conn.execute("UPDATE reach_incidents SET resolved=1 WHERE category='ambiguous' AND platform='x' AND reference_id IN (SELECT delivery_key FROM x_delivery WHERE status='published')")
        if 'x_research_analysis_posts' in tables:
            for r in conn.execute("SELECT * FROM x_research_analysis_posts WHERE status IN ('unknown','publishing','partial')"):
                at=parse_time(r['updated_at'])
                if r['status']=='publishing' and at and now-at<timedelta(minutes=10): continue
                pending.append(('research:'+r['source_run_id'],r['updated_at'],'ambiguous','publish',r['status'],'x_research',r['source_run_id'],False))
            conn.execute("UPDATE reach_incidents SET resolved=1 WHERE platform='x_research' AND reference_id IN (SELECT source_run_id FROM x_research_analysis_posts WHERE status='published')")
        for r in conn.execute("SELECT id,status,updated_at FROM threads_posts WHERE status IN ('ambiguous','publish_unknown','publishing')"):
            at=parse_time(r['updated_at'])
            if r['status']=='publishing' and at and now-at < timedelta(minutes=10): continue
            pending.append(('threads:'+str(r['id']),r['updated_at'],'ambiguous','publish',r['status'],'threads',str(r['id']),False))
        conn.execute("UPDATE reach_incidents SET resolved=1 WHERE platform='threads' AND reference_id IN (SELECT CAST(id AS TEXT) FROM threads_posts WHERE status='published')")
        seen={}
        for r in conn.execute('SELECT tweet_id,text,posted_at FROM published_posts ORDER BY julianday(posted_at)'):
            digest=hashlib.sha256(re.sub(r'\s+','',r['text'] or '').encode()).hexdigest()
            if r['text'] and digest in seen:
                pending.append(('duplicate:'+r['tweet_id'],r['posted_at'],'duplicate','publish','identical_published_text','x',r['tweet_id'],False))
            seen[digest]=r['tweet_id']
        for provider, table, time_field, error_field in [('api','api_usage_events','timestamp','error_type'),('threads','threads_api_calls','called_at','error_class')]:
            if table not in tables: continue
            for r in conn.execute(f'SELECT id,{time_field} at,{error_field} reason FROM {table} WHERE success=0 AND julianday({time_field})>=julianday(?)',(since.isoformat(),)):
                if r['reason'] in ('reserved','',None): continue
                reason=r['reason'] if re.fullmatch(r'[A-Za-z0-9_]{1,100}',r['reason']) else 'api_error'
                pending.append((provider+':'+str(r['id']),r['at'],'api_error','api',reason,provider,str(r['id']),False))
        for key,at,category,stage,reason,platform,reference,resolved in pending:
            conn.execute('''INSERT INTO reach_incidents VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(event_key) DO UPDATE SET resolved=excluded.resolved''',
                (key,at,category,stage,reason,platform,reference,int(resolved)))
        conn.commit()
    from api_budget import budget_configuration
    cfg=budget_configuration()
    with closing(connect(path)) as conn:
        costs=expense_report(conn,now.replace(day=1,hour=0,minute=0,second=0,microsecond=0),now)
        completed=costs['completed_ledger_cost_usd']
        for provider, limit in {**cfg['providers'],'total':cfg['effective_total_limit']}.items():
            value=completed if provider=='total' else costs['by_provider'].get(provider,0)
            if value > limit:
                key='budget:'+now.strftime('%Y-%m')+':'+provider
                conn.execute('INSERT OR IGNORE INTO reach_incidents VALUES(?,?,?, ?,?,?,?,0)',
                             (key,now.isoformat(),'budget_overrun','budget','completed_ledger_exceeds_limit',provider,provider))
        safe={'policy_version': 'reach-v2', 'budget':cfg,
              'operating_settings': {name:os.environ.get(name) for name in (
                  'POST_ENABLED','MAX_DAILY_AUTOMATED_POSTS','MONITOR_INTERVAL_MINUTES',
                  'ACTIVE_HOURS','PROMPT_VERSION','REACH_POLICY_ENABLED','REACH_COLLECTION_ENABLED',
                  'POST_METRIC_WINDOWS','X_OWNED_READ_MAX_PER_DAY')}}
        serialized=json.dumps(safe,sort_keys=True)
        previous=conn.execute('SELECT captured_at,config_json FROM reach_runtime_snapshots ORDER BY captured_at DESC LIMIT 1').fetchone()
        if not previous or previous['config_json']!=serialized or previous['captured_at'][:10]!=now.date().isoformat():
            conn.execute('INSERT OR IGNORE INTO reach_runtime_snapshots VALUES(?,?)',(now.isoformat(),serialized))
        conn.commit()


def expense_report(conn, start, end):
    rows=[dict(r) for r in conn.execute('SELECT * FROM api_usage_events WHERE julianday(timestamp)>=julianday(?) AND julianday(timestamp)<julianday(?)',(start.isoformat(),end.isoformat()))]
    dedicated=[dict(r) for r in conn.execute('SELECT * FROM xai_usage_events WHERE julianday(timestamp)>=julianday(?) AND julianday(timestamp)<julianday(?)',(start.isoformat(),end.isoformat()))]
    providers=defaultdict(float); by_post=defaultdict(float)
    reserved=0.; verified=0.; estimated=0.; unattributed=0.; unposted=0.; linked_events=0
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    sources={}; deliveries={}; videos=defaultdict(list); research={}
    if 'reach_assignments' in tables:
        sources={r['source_key']:[r['tweet_id']] for r in conn.execute('SELECT source_key,tweet_id FROM reach_assignments WHERE tweet_id IS NOT NULL')}
    if 'reach_features' in tables:
        for r in conn.execute('SELECT source_key,tweet_id FROM reach_features WHERE source_key IS NOT NULL'):
            sources.setdefault(r['source_key'],[])
            if r['tweet_id'] not in sources[r['source_key']]: sources[r['source_key']].append(r['tweet_id'])
    if 'x_delivery' in tables:
        deliveries={r['delivery_key']:r['external_id'] for r in conn.execute("SELECT * FROM x_delivery WHERE status='published'")}
    if 'short_video_publications' in tables:
        for r in conn.execute("SELECT * FROM short_video_publications WHERE status='published' AND external_post_id IS NOT NULL"):
            videos[r['video_id']].append(str(r['external_post_id']) if r['platform']=='x' else r['platform']+':'+str(r['external_post_id']))
    if 'x_research_analysis_posts' in tables:
        research={r['source_run_id']:json.loads(r['tweet_ids_json'] or '[]') for r in conn.execute("SELECT * FROM x_research_analysis_posts WHERE status='published'")}
    events=[]
    for r in rows:
        cost=float(r['estimated_cost_usd'] or 0)
        if r['error_type']=='reserved': reserved+=cost; continue
        if r['provider']=='xai': continue
        events.append((r['provider'],cost,False,json.loads(r['metadata_json'] or '{}')))
    for r in dedicated:
        actual=r['cost_source']=='actual' and bool(r['cost_verified']) and r['actual_cost_usd'] is not None
        cost=float(r['actual_cost_usd'] if actual else r['estimated_cost_usd'] or 0)
        events.append(('xai',cost,actual,json.loads(r['metadata_json'] or '{}')))
    for provider,cost,actual,metadata in events:
        providers[provider]+=cost
        if actual: verified+=cost
        else: estimated+=cost
        research_ids=research.get(metadata.get('run_id')) if metadata.get('post_type')=='x_research_analysis' else None
        ids=metadata.get('tweet_ids') or sources.get(metadata.get('source_key')) or videos.get(metadata.get('video_id')) or research_ids or [metadata.get('tweet_id') or deliveries.get(metadata.get('delivery_key'))]
        ids=list(dict.fromkeys(str(i) for i in ids if i))
        if ids:
            linked_events+=1
            for tweet_id in ids: by_post[tweet_id]+=cost/len(ids)
        else:
            unattributed+=cost
            if metadata.get('source_key'): unposted+=cost
    return {'completed_ledger_cost_usd':sum(providers.values()),'verified_actual_usd':verified,
            'estimated_usd':estimated,'in_flight_reserved_usd':reserved,
            'unattributed_usd':unattributed,'unpublished_candidate_cost_usd':unposted,
            'shared_or_unlinked_cost_usd':unattributed-unposted,
            'by_provider':dict(providers),'by_post_allocated_usd':dict(by_post),
            'attribution_rule':'direct source/delivery ID; multi-post reads equal allocation; shared discovery remains unattributed',
            'linked_event_fraction':linked_events/len(events) if events else None,
            'all_costs_actual': bool(events) and estimated==0 and reserved==0}


def incident_report(conn,start,end):
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'reach_incidents' not in tables: return {'available':False,'counts':None}
    rows=[dict(r) for r in conn.execute('SELECT * FROM reach_incidents WHERE julianday(occurred_at)>=julianday(?) AND julianday(occurred_at)<julianday(?)',(start.isoformat(),end.isoformat()))]
    return {'available':True,'events':len(rows),'counts':dict(Counter(r['category'] for r in rows)),
            'by_stage_reason':dict(Counter(r['stage']+':'+r['reason'] for r in rows)),
            'by_platform':dict(Counter(r['platform'] for r in rows)),
            'unresolved_safety_events':sum(not r['resolved'] and r['category'] in ('ambiguous','duplicate','correction','budget_overrun') for r in rows)}
