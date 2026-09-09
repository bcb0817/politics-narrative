"""Budgeted official interval analytics. No cumulative-count subtraction."""
import json
import math
import os
from contextlib import closing
from datetime import timedelta, timezone

from api_budget import reserve, finalize
from metrics_db import connect
from reach_storage import ensure


def _request(ids, start, end):
    import requests
    # Only the documented, fixed API host. Never log tokens or response bodies.
    with requests.get('https://api.x.com/2/tweets/analytics',
            headers={'Authorization':'Bearer '+os.environ['X_ANALYTICS_OAUTH2_ACCESS_TOKEN']},
            params={'ids':','.join(ids), 'start_time':start.astimezone(timezone.utc).isoformat(),
                    'end_time':end.astimezone(timezone.utc).isoformat(), 'granularity':'total',
                    'analytics.fields':'id,impressions,bookmarks,quote_tweets,user_profile_clicks'},
            timeout=(5,20), allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            raise RuntimeError('analytics_http_'+str(response.status_code))
        body=bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body)>5_000_000: raise ValueError('analytics_response_too_large')
        return json.loads(body)


def collect_day(path, now, config, request=None, day=None):
    ensure(path)
    cfg=config['calendar_analytics']
    end=now.replace(hour=0,minute=0,second=0,microsecond=0)
    if day is not None:
        end=day+timedelta(days=1)
        if end>now: raise ValueError('calendar_interval_not_closed')
    start=end-timedelta(days=1); date=start.date().isoformat()
    reason=''
    if not cfg['enabled']: reason='disabled'
    elif os.environ.get('X_ANALYTICS_ENTITLED','false').lower()!='true': reason='analytics_entitlement_unconfirmed'
    elif not os.environ.get('X_ANALYTICS_OAUTH2_ACCESS_TOKEN'): reason='analytics_oauth2_missing'
    try: price=float(os.environ.get('X_ANALYTICS_PRICE_PER_POST_USD','nan'))
    except ValueError: price=float('nan')
    if not reason and (not math.isfinite(price) or price<0 or os.environ.get('X_ANALYTICS_PRICE_VERIFIED','false').lower()!='true'):
        reason='analytics_pricing_unconfirmed'
    with closing(connect(path)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        expected=[r[0] for r in conn.execute('SELECT tweet_id FROM published_posts WHERE julianday(posted_at)<julianday(?) ORDER BY julianday(posted_at) DESC',(end.isoformat(),))]
        existing=conn.execute('SELECT * FROM reach_daily_runs WHERE day=?',(date,)).fetchone()
        if existing and existing['status'] in ('complete','permission_denied'):
            return dict(existing)
        if existing and existing['status']=='fetching' and conn.execute('SELECT julianday(?)-julianday(?)',(now.isoformat(),existing['updated_at'])).fetchone()[0]*1440<10:
            return {'status':'in_progress'}
        measured={r[0] for r in conn.execute('SELECT tweet_id FROM reach_daily_posts WHERE day=? AND impressions IS NOT NULL',(date,))}
        used=int(conn.execute("SELECT COALESCE(SUM(resource_count),0) FROM api_usage_events WHERE provider='x' AND operation='owned_read' AND timestamp LIKE ?",(now.date().isoformat()+'%',)).fetchone()[0])
        remaining=max(0,int(os.environ.get('X_OWNED_READ_MAX_PER_DAY','24'))-used-cfg['minimum_24h_read_reserve'])
        ids=[i for i in expected if i not in measured][:min(100,cfg['daily_max_ids'],remaining)]
        if not reason and not ids: reason='no_inventory' if not expected else '24h_priority_budget_hold'
        conn.execute('''INSERT INTO reach_daily_runs VALUES(?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET
            updated_at=excluded.updated_at,status=excluded.status,expected_ids_json=excluded.expected_ids_json,reason=excluded.reason,inventory_complete=excluded.inventory_complete''',
            (date,now.isoformat(),'blocked' if reason else 'fetching',json.dumps(expected),reason,int(cfg['account_inventory_complete'])))
        conn.commit()
    if reason: return {'status':'blocked','reason':reason,'expected':len(expected),'measured':len(measured)}
    reservation,reason=reserve('x','owned_read','tweets_analytics',price*len(ids),len(ids),
                               {'tweet_ids':ids,'analytics_day':date,'cost_basis':'configured_estimate'},path=path)
    status='blocked'; count=0
    if reservation:
        try:
            response=(request or _request)(ids,start,end)
            data=response.get('data',[])
            seen=set()
            with closing(connect(path)) as conn:
                for row in data:
                    ident=str(row.get('id',''))
                    if ident not in ids or ident in seen: raise ValueError('unexpected_analytics_id')
                    seen.add(ident)
                    values=[]
                    for key in ('impressions','bookmarks','quote_tweets','user_profile_clicks'):
                        value=row.get(key)
                        if value is not None and (type(value) is not int or value<0): raise ValueError('invalid_analytics_value')
                        values.append(value)
                    conn.execute('INSERT OR REPLACE INTO reach_daily_posts VALUES(?,?,?,?,?,?,?,?)',
                                 (date,ident,now.isoformat(),*values,'x_interval_analytics'))
                    if values[0] is not None: count+=1
                conn.commit()
            finalize(reservation,price*len(ids),success=True,path=path)
            status='complete' if len(measured)+count==len(expected) else 'partial'
            reason='' if status=='complete' else 'missing_or_budget_limited_ids'
        except Exception as exc:
            # Unknown billing retains maximum reservation; never assume free errors.
            reason=str(exc) if str(exc) in ('analytics_http_401','analytics_http_403','analytics_http_429') else type(exc).__name__
            status='permission_denied' if reason in ('analytics_http_401','analytics_http_403') else 'failed'
    with closing(connect(path)) as conn:
        conn.execute('UPDATE reach_daily_runs SET status=?,reason=?,updated_at=? WHERE day=?',(status,reason,now.isoformat(),date)); conn.commit()
    return {'status':status,'reason':reason,'collected':count,'expected':len(expected)}


def day_summary(conn,start,end):
    tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    results={}
    for i in range((end-start).days):
        day=start+timedelta(days=i); label=day.date().isoformat()
        summary={'calendar_day_total_impressions':None,'known_posts_calendar_impressions':None,
                 'expected_posts':None,'measured_posts':0,'missing_rate':None,'reason':'not_collected',
                 'daily_net_followers':None,'observed_follower_change':None,'follower_window':None}
        if 'reach_daily_runs' in tables:
            run=conn.execute('SELECT * FROM reach_daily_runs WHERE day=?',(label,)).fetchone()
            if run:
                expected=set(json.loads(run['expected_ids_json']))
                measured=[r for r in conn.execute('SELECT * FROM reach_daily_posts WHERE day=? AND impressions IS NOT NULL',(label,)) if r['tweet_id'] in expected]
                total=sum(r['impressions'] for r in measured) if measured else None
                summary.update(known_posts_calendar_impressions=total,expected_posts=len(expected),measured_posts=len(measured),
                               missing_rate=1-len(measured)/len(expected) if expected else None,reason=run['reason'] or run['status'])
                if run['inventory_complete'] and expected and len(expected)==len(measured):
                    summary['calendar_day_total_impressions']=total
        endpoints=[]
        for boundary in (day,day+timedelta(days=1)):
            row=conn.execute('''SELECT * FROM follower_snapshots WHERE estimated=0 AND followers_count IS NOT NULL
                AND ABS(julianday(captured_at)-julianday(?))<=10.0/1440
                ORDER BY ABS(julianday(captured_at)-julianday(?)) LIMIT 1''',(boundary.isoformat(),boundary.isoformat())).fetchone()
            endpoints.append(row)
        if all(endpoints):
            change=endpoints[1]['followers_count']-endpoints[0]['followers_count']
            summary['observed_follower_change']=change
            summary['follower_window']=[r['captured_at'] for r in endpoints]
            from reach_features import parse_time
            if parse_time(endpoints[0]['captured_at'])==day and parse_time(endpoints[1]['captured_at'])==day+timedelta(days=1):
                summary['daily_net_followers']=change
        results[label]=summary
    return results
