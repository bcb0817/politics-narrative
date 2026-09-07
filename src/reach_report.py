"""Read-only, observation-age-aware reach report. No API calls, no .env load."""
import argparse
import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST=ZoneInfo('Asia/Tokyo')


def percentile(values, p):
    if not values: return None
    values=sorted(values); index=(len(values)-1)*p
    lo=math.floor(index); hi=math.ceil(index)
    return values[lo]+(values[hi]-values[lo])*(index-lo)


def summarize(rows):
    valid=[r for r in rows if r.get('age_hours') is not None and 24<=r['age_hours']<=26 and r['impressions'] is not None]
    values=[r['impressions'] for r in valid]
    return {'posts':len(rows),'measured_24_to_26h':len(valid),
            'missing_rate':1-len(valid)/len(rows) if rows else None,
            'median_24h':statistics.median(values) if values else None,
            'p75_24h':percentile(values,.75),
            'cohort_24h_impressions_sum':sum(values) if values else None,
            'calendar_day_total_impressions':None,
            'api_cost_per_1000_calendar_impressions':None,
            'daily_net_followers':None,
            'bookmark_per_impression':ratio(valid,'bookmarks'),
            'quote_per_impression':ratio(valid,'quotes'),
            'profile_click_per_impression':ratio(valid,'profile_clicks')}


def ratio(rows,field):
    measured=[r for r in rows if r.get(field) is not None and r['impressions']>0]
    return sum(r[field] for r in measured)/sum(r['impressions'] for r in measured) if measured else None


def report(path, end, days=28):
    start=end-timedelta(days=days)
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        rows=[dict(r) for r in conn.execute('''SELECT p.tweet_id,p.text,p.posted_at,p.topic_key,p.post_type,p.hook_type,
            m.impressions,m.measured_at,m.bookmarks,m.quotes,m.profile_clicks,
            (julianday(m.measured_at)-julianday(p.posted_at))*24 age_hours,
            n.source_url,n.published_at news_published_at
            FROM published_posts p LEFT JOIN post_metrics m ON m.tweet_id=p.tweet_id AND m.measurement_window='24h'
            LEFT JOIN generated_posts g ON g.id=p.generated_post_id
            LEFT JOIN news_candidates n ON n.id=g.news_candidate_id
            WHERE julianday(p.posted_at)>=julianday(?) AND julianday(p.posted_at)<julianday(?)
            ORDER BY p.posted_at''',(start.isoformat(),end.isoformat()))]
        experiments=[]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='reach_assignments'").fetchone():
            lookup={r['tweet_id']:r for r in rows}
            groups=defaultdict(list)
            for r in conn.execute('SELECT experiment,arm,tweet_id FROM reach_assignments'):
                if r['tweet_id'] in lookup: groups[(r['experiment'],r['arm'])].append(lookup[r['tweet_id']])
            experiments=[{'experiment':k[0],'arm':k[1],**summarize(v)} for k,v in groups.items()]
        followers=[dict(r) for r in conn.execute('SELECT captured_at,followers_count FROM follower_snapshots WHERE captured_at>=? AND captured_at<? ORDER BY captured_at',(start.isoformat(),end.isoformat()))]
    groups={}
    for dimension in ['post_type','hook_type','hour','weekday','length_band']:
        cells=defaultdict(list)
        for r in rows:
            posted=datetime.fromisoformat(r['posted_at']).astimezone(JST)
            value={'hour':posted.hour,'weekday':posted.weekday(),'length_band':len(r['text'] or '')//100}.get(dimension,r.get(dimension))
            cells[str(value)].append(r)
        groups[dimension]={k:summarize(v) for k,v in cells.items()}
    valid=sorted([r for r in rows if r['age_hours'] is not None and 24<=r['age_hours']<=26 and r['impressions'] is not None],key=lambda r:r['impressions'])
    daily=defaultdict(list)
    for r in rows: daily[r['posted_at'][:10]].append(r)
    return {'period_start':start.isoformat(),'period_end_exclusive':end.isoformat(),
        'observation_rule':'24 <= measured_age_hours <= 26; not exact 24h or calendar-day totals',
        'summary':summarize(rows),'late_24h_records':sum(r['age_hours'] is not None and r['age_hours']>26 for r in rows),
        'by_publish_day':{(start+timedelta(days=i)).date().isoformat():summarize(daily[(start+timedelta(days=i)).date().isoformat()]) for i in range(days)},
        'groups':groups,'examples':{'low':valid[:3],'high':valid[-3:]},
        'follower_snapshots':followers,'experiments':experiments,
        'causal_conclusion':None,'automatic_adoption':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--end',required=True); parser.add_argument('--days',type=int,default=28)
    args=parser.parse_args()
    print(json.dumps(report(args.db,datetime.fromisoformat(args.end),args.days),ensure_ascii=False,indent=2))
