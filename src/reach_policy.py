"""Small persisted experiments on existing eligible candidates, no extra calls."""
import hashlib
import json
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from metrics_db import connect, init_db
from reach_features import evidence_features, eligible_formats
from reach_storage import ensure

CONFIG = Path(__file__).resolve().parents[1] / 'config' / 'reach_experiments.json'
JST = ZoneInfo('Asia/Tokyo')
SCHEMA = '''CREATE TABLE IF NOT EXISTS reach_assignments (
 source_key TEXT PRIMARY KEY, experiment TEXT, arm TEXT, stratum TEXT,
 assigned_at TEXT, config_json TEXT, tweet_id TEXT, posted_at TEXT);
'''


def settings():
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def assign(item, *, now=None, path=None, config=None):
    cfg = config or settings()
    cfg = json.loads(json.dumps(cfg))
    now = now or datetime.now(JST)
    if not cfg['enabled'] or os.environ.get('REACH_POLICY_ENABLED','true').lower() == 'false':
        return None
    if not datetime.fromisoformat(cfg['start']) <= now < datetime.fromisoformat(cfg['end']):
        return None
    source = item.get('url') or item.get('source_url')
    if not source:
        return None
    key = hashlib.sha256(source.encode()).hexdigest()
    topic = str(item.get('topic_key') or item.get('title') or source)
    index = int(hashlib.sha256(topic.encode()).hexdigest()[:8],16) % len(cfg['experiments'])
    exp = cfg['experiments'][index]
    from reach_learning import active_model
    model = active_model(path) if cfg.get('learning',{}).get('enabled') else None
    model_version = model['version'] if model else 'initial'
    stratum = f"{item.get('genre','unknown')}:{now.hour//6}:{model_version}"
    ensure(path)
    with closing(connect(path)) as conn:
        conn.executescript(SCHEMA)
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT 1 FROM reach_incidents WHERE category IN ('correction','duplicate','ambiguous','budget_overrun') AND resolved=0 AND julianday(occurred_at)>=julianday(?) LIMIT 1", (cfg['start'],)).fetchone():
            return None
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='x_delivery'").fetchone():
            if conn.execute("SELECT 1 FROM x_delivery WHERE status='ambiguous' AND julianday(created_at)>=julianday(?) LIMIT 1",(cfg['start'],)).fetchone():
                return None
        row = conn.execute('SELECT * FROM reach_assignments WHERE source_key=?',(key,)).fetchone()
        if row:
            old_cfg = json.loads(row['config_json'] or '{}')
            if not cfg.get('generation_version') or old_cfg.get('generation_version') == cfg['generation_version']:
                return dict(row)
            # Retain historical publication assignments; never recycle them.
            if row['tweet_id']:
                return None
            conn.execute('DELETE FROM reach_assignments WHERE source_key=?', (key,))
        if conn.execute('SELECT COUNT(*) FROM reach_assignments WHERE julianday(assigned_at)>=julianday(?) AND (? IS NULL OR json_extract(config_json,\'$.generation_version\')=?)',
                        (cfg['start'],cfg.get('generation_version'),cfg.get('generation_version'))).fetchone()[0] >= cfg['max_assignments']:
            return None
        count = conn.execute('SELECT COUNT(*) FROM reach_assignments WHERE experiment=? AND stratum=? AND (? IS NULL OR json_extract(config_json,\'$.generation_version\')=?)',
                             (exp['id'],stratum,cfg.get('generation_version'),cfg.get('generation_version'))).fetchone()[0]
        seed = int(hashlib.sha256((exp['id']+stratum).encode()).hexdigest()[:8],16)
        arm = ('control','treatment')[(count+seed)%2]
        decision = {'format': None, 'exploration': False, 'probability': None,
                    'rank_features': item.get('reach_priority',{}).get('features') or evidence_features(item), 'model_version': model_version}
        if exp['id'] == 'format_selection':
            names = eligible_formats(item, cfg)
            if not names:
                return None
            # The hash is stable across restarts; exploration never adds a call.
            fraction = min(1, max(0, float(cfg['exploration_fraction'])))
            draw = int(hashlib.sha256((key+cfg['version']+'explore').encode()).hexdigest()[:12],16)/16**12
            explore = draw < fraction
            weights = {n: (model or {}).get('format_weights',{}).get(n,1) for n in names}
            published_formats = []
            for feature in conn.execute('SELECT features_json FROM reach_features WHERE (? IS NULL OR json_extract(features_json,\'$.generation_version\')=?) ORDER BY recorded_at DESC LIMIT ?',
                                        (cfg.get('generation_version'),cfg.get('generation_version'),cfg.get('diversity',{}).get('history_posts',20))):
                published_formats.append(json.loads(feature[0]).get('format'))
            for n in names:
                weights[n] /= 1 + cfg.get('diversity',{}).get('format_penalty',.08)*published_formats.count(n)
            distribution = {n: 1/len(names) if explore else weights[n]/sum(weights.values()) for n in names}
            sample = int(hashlib.sha256((key+cfg['version']+'format').encode()).hexdigest()[:12],16)/16**12
            selected = names[-1]
            for name, probability in distribution.items():
                sample -= probability
                if sample < 0:
                    selected = name
                    break
            decision.update(format=selected, exploration=explore,
                            probability=fraction/len(names)+(1-fraction)*weights[selected]/sum(weights.values()),
                            eligible_formats=names)
            if arm == 'control':
                decision.update(format=None, exploration=False, probability=None)
        cfg['decision'] = decision
        conn.execute('INSERT INTO reach_assignments VALUES(?,?,?,?,?,?,NULL,NULL)',
                     (key,exp['id'],arm,stratum,now.isoformat(),json.dumps(cfg,ensure_ascii=False,sort_keys=True)))
        conn.commit()
        return dict(conn.execute('SELECT * FROM reach_assignments WHERE source_key=?',(key,)).fetchone())


def instruction(assignment):
    if not assignment or assignment['arm'] != 'treatment':
        return ''
    cfg=json.loads(assignment['config_json'])
    exp=next(e for e in cfg['experiments'] if e['id']==assignment['experiment'])
    selected = cfg.get('decision',{}).get('format')
    form = (' 説明形式：'+cfg['formats'][selected]) if selected else ''
    return ('\n編集実験（安全・事実性・品質基準は同一）：'+exp['treatment']+form+
            ' 中心メッセージは一つ。条件や例外を削らない。定型質問、反応要求、抽象的な締めは付けない。'+
            ' 資料にないことを資料作成者が公開していないと決めつけない。')


def record_publication(assignment, tweet_id, posted_at, *, path=None):
    if not assignment:
        return
    with closing(connect(path)) as conn:
        conn.execute('UPDATE reach_assignments SET tweet_id=?,posted_at=? WHERE source_key=? AND tweet_id IS NULL',
                     (str(tweet_id),posted_at,assignment['source_key']))
        conn.commit()


def rank_eligible(items, history, *, path=None):
    """Only called AFTER the existing relevance/freshness gates."""
    cfg=settings()
    if not cfg['enabled'] or os.environ.get('REACH_POLICY_ENABLED','true').lower()=='false':
        return items
    from reach_learning import active_model
    model = active_model(path) if cfg.get('learning',{}).get('enabled') else None
    weights=(model or {}).get('ranking_weights', cfg['ranking_weights'])
    def score(item):
        topic=item.get('topic_key') or item.get('title')
        repeat=sum((r.get('topic_key') or r.get('title'))==topic for r in history[-20:])
        values=evidence_features(item, history)
        active={k:v for k,v in values.items() if v is not None}
        priority=sum(weights[k]*float(v) for k,v in active.items())/sum(weights[k] for k in active)
        priority /= 1+cfg.get('diversity',{}).get('topic_penalty',.12)*repeat
        item['reach_priority']={'version':cfg['version'],'model_version':(model or {}).get('version','initial'),'features':values,'score':priority,'hypothesis':True}
        return priority
    return sorted(items,key=score,reverse=True)
