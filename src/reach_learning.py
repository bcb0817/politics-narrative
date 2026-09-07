"""Bounded observational learning with temporal holdout, never safety tuning."""
import hashlib
import json
import math
import statistics
from collections import defaultdict
from contextlib import closing
from datetime import timedelta

from metrics_db import connect
from reach_features import parse_time
from reach_storage import ensure


def active_model(path=None):
    ensure(path)
    with closing(connect(path)) as conn:
        row = conn.execute("SELECT model_json FROM reach_models WHERE status='active' ORDER BY trained_at DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None


def _fit(rows, keys):
    # Diagonal ridge avoids unstable inversion with tiny/sparse correlated samples.
    ys = [math.log1p(max(0, r['impressions'])) for r in rows]
    cap = sorted(ys)[max(0, int(len(ys)*.9)-1)]
    ys = [min(y, cap) for y in ys]
    mean_y = statistics.mean(ys)
    means = {}; slopes = {}
    for key in keys:
        measured = [float(r['features']['rank_features'][key]) / 10 for r in rows
                    if r['features']['rank_features'].get(key) is not None]
        means[key] = statistics.mean(measured) if measured else 0
        xs = [float(r['features']['rank_features'].get(key))/10
              if r['features']['rank_features'].get(key) is not None else means[key] for r in rows]
        slopes[key] = max(0, sum((x-means[key])*(y-mean_y) for x,y in zip(xs,ys)) /
                          (10 + sum((x-means[key])**2 for x in xs)))
    return means, slopes, mean_y


def train(rows, config, now, path=None, blocked=False):
    ensure(path)
    cfg = config['learning']
    version = 'reach-model-' + now.date().isoformat() + '-' + config['version']
    with closing(connect(path)) as conn:
        existing = conn.execute('SELECT model_json FROM reach_models WHERE version=?', (version,)).fetchone()
        if existing:
            return json.loads(existing[0])
    total = len(rows)
    rows = [r for r in rows if r.get('age_hours') is not None and 24 <= r['age_hours'] <= 26
            and r.get('impressions') is not None and r.get('features', {}).get('rank_features')]
    cutoff = now - timedelta(days=cfg['holdout_days'])
    training = [r for r in rows if parse_time(r['posted_at']) < cutoff]
    holdout = [r for r in rows if parse_time(r['posted_at']) >= cutoff]
    model = {'version': version, 'trained_at': now.isoformat(), 'status': 'insufficient_data',
             'train_posts': len(training), 'holdout_posts': len(holdout),
             'missing_rate': 1-len(rows)/total if total else None,
             'ranking_weights': config['ranking_weights'], 'format_weights': {},
             'causal_claim': False, 'automatic_full_rollout': False}
    measured_days = {r['posted_at'][:10] for r in training}
    topics = {r.get('topic_key') for r in training if r.get('topic_key')}
    if blocked or not cfg['enabled']:
        model['status'] = 'safety_hold' if blocked else 'disabled'
    elif len(training) >= cfg['minimum_train'] and len(holdout) >= cfg['minimum_holdout'] and len(measured_days) >= 7 and len(topics) >= 5 and model['missing_rate'] <= cfg['maximum_missing_rate']:
        keys = list(config['ranking_weights'])
        means, slopes, intercept = _fit(training, keys)
        def prediction(r):
            f = r['features']['rank_features']
            return intercept + sum(slopes[k]*((float(f[k])/10 if f.get(k) is not None else means[k])-means[k]) for k in keys)
        truth = [math.log1p(max(0,r['impressions'])) for r in holdout]
        baseline = statistics.mean(abs(y-intercept) for y in truth)
        error = statistics.mean(abs(y-prediction(r)) for r,y in zip(holdout,truth))
        improvement = (baseline-error)/baseline if baseline > 0 else 0
        model.update(holdout_mae=error, baseline_mae=baseline, holdout_improvement=improvement)
        if improvement >= cfg['minimum_holdout_improvement'] and sum(slopes.values()) > 0:
            alpha = min(.2, max(0, cfg['maximum_weight_update']))
            learned = {k: slopes[k]/sum(slopes.values()) for k in keys}
            model['ranking_weights'] = {k:(1-alpha)*config['ranking_weights'][k]+alpha*learned[k] for k in keys}
            model['status'] = 'active'
        else:
            model['status'] = 'holdout_rejected'
    # Format evidence is stratified by topic family/hour/freshness and shrunk.
    strata = defaultdict(list)
    for r in rows:
        f = r['features']; posted = parse_time(r['posted_at'])
        key = (r.get('genre') or 'unknown', posted.hour//6,
               int((f.get('news_age_hours') or 0)//12))
        strata[key].append(r)
    residuals = defaultdict(list)
    for group in strata.values():
        if len(group) < 4: continue
        center = statistics.median(math.log1p(max(0,r['impressions'])) for r in group)
        for r in group:
            name = r['features'].get('format')
            if name: residuals[name].append((r, math.log1p(max(0,r['impressions']))-center))
    model['format_evidence'] = {}
    for name, values in residuals.items():
        enough = len(values) >= cfg['minimum_per_format'] and len({r['posted_at'][:10] for r,_ in values}) >= 7 and len({r.get('topic_key') for r,_ in values}) >= 5
        model['format_evidence'][name] = {'posts':len(values), 'eligible':enough}
        if enough and model['status'] == 'active':
            effect = statistics.median(v for _,v in values) * len(values)/(len(values)+60)
            model['format_weights'][name] = min(1.5, max(.67, math.exp(effect)))
    model['dataset_hash'] = hashlib.sha256(json.dumps([(r['tweet_id'],r['impressions'],r['measured_at']) for r in rows],sort_keys=True).encode()).hexdigest()
    with closing(connect(path)) as conn:
        conn.execute('INSERT OR IGNORE INTO reach_models VALUES(?,?,?,?)',
                     (version, now.isoformat(), model['status'], json.dumps(model,sort_keys=True)))
        conn.commit()
    return model
