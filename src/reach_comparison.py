"""Descriptive feature comparisons under matched observation conditions."""
import statistics
from collections import defaultdict
from reach_features import parse_time

DIMENSIONS = ('topic','opening','paragraphs','url_count','hashtag_count','media_type',
              'angle','format','generation_version','policy_version','model_version',
              'followers_at_publish','news_age_hours','interval_minutes',
              'same_topic_last_24h','opening_repeat_last_30')


def bucket(name,value):
    if value is None: return 'unknown'
    if name=='opening': return 'concrete_number' if any(c.isdigit() for c in value) else 'no_number'
    if name=='news_age_hours': return str(int(value//12)*12)+'h'
    if name=='interval_minutes': return str(int(value//30)*30)+'m'
    if name=='followers_at_publish': return str(int(value//50)*50)+'-49'
    return str(value)


def comparisons(rows, summarize):
    groups={}; matched={}; missing={}
    valid=[r for r in rows if r.get('impressions') is not None and r.get('age_hours') is not None and 24<=r['age_hours']<=26]
    for dimension in DIMENSIONS:
        cells=defaultdict(list)
        for row in rows: cells[bucket(dimension,row['features'].get(dimension))].append(row)
        groups[dimension]={k:summarize(v) for k,v in cells.items()}
        missing[dimension]=len(cells.get('unknown',[]))/len(rows) if rows else None
        strata=defaultdict(lambda:defaultdict(list))
        for row in valid:
            f=row['features']; value=bucket(dimension,f.get(dimension))
            if value=='unknown': continue
            key=(row.get('genre') or 'unknown',parse_time(row['posted_at']).hour//6,
                 bucket('news_age_hours',f.get('news_age_hours')),
                 bucket('followers_at_publish',f.get('followers_at_publish')))
            strata[key][value].append(row['impressions'])
        pairs=defaultdict(list)
        for levels in strata.values():
            names=sorted(levels)
            for i,left in enumerate(names):
                for right in names[i+1:]:
                    a,b=levels[left],levels[right]
                    if min(len(a),len(b))<5: continue
                    pairs[(left,right)].append((min(len(a),len(b)),statistics.median(b)-statistics.median(a)))
        matched[dimension]=[{'reference':k[0],'comparison':k[1],'matched_strata':len(v),
                            'minimum_cell_posts':5,'weighted_median_difference':sum(n*d for n,d in v)/sum(n for n,d in v)} for k,v in pairs.items()]
    return {'groups':groups,'feature_missing_rates':missing,'matched_comparisons':matched,
            'matching':'genre, 6h posting band, 12h freshness band, 50-follower band; unknown strata retained and labeled',
            'causal_claim':False,'limitations':'observational selection, missing covariates and multiple comparisons; no p-value fishing or automatic full rollout'}
