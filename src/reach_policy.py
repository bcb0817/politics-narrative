"""Small persisted experiments on existing eligible candidates, no extra calls."""
import hashlib
import json
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from metrics_db import connect, init_db

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
    stratum = f"{item.get('genre','unknown')}:{now.hour//6}"
    init_db(path)
    with closing(connect(path)) as conn:
        conn.executescript(SCHEMA)
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='x_delivery'").fetchone():
            if conn.execute("SELECT 1 FROM x_delivery WHERE status='ambiguous' AND created_at>=? LIMIT 1",(cfg['start'],)).fetchone():
                return None
        row = conn.execute('SELECT * FROM reach_assignments WHERE source_key=?',(key,)).fetchone()
        if row:
            return dict(row)
        if conn.execute('SELECT COUNT(*) FROM reach_assignments').fetchone()[0] >= cfg['max_assignments']:
            return None
        count = conn.execute('SELECT COUNT(*) FROM reach_assignments WHERE experiment=? AND stratum=?',(exp['id'],stratum)).fetchone()[0]
        seed = int(hashlib.sha256((exp['id']+stratum).encode()).hexdigest()[:8],16)
        arm = ('control','treatment')[(count+seed)%2]
        conn.execute('INSERT INTO reach_assignments VALUES(?,?,?,?,?,?,NULL,NULL)',
                     (key,exp['id'],arm,stratum,now.isoformat(),json.dumps(cfg,ensure_ascii=False,sort_keys=True)))
        conn.commit()
        return dict(conn.execute('SELECT * FROM reach_assignments WHERE source_key=?',(key,)).fetchone())


def instruction(assignment):
    if not assignment or assignment['arm'] != 'treatment':
        return ''
    cfg=json.loads(assignment['config_json'])
    exp=next(e for e in cfg['experiments'] if e['id']==assignment['experiment'])
    return ('\n編集実験（安全・事実性・品質基準は同一）：'+exp['treatment']+
            ' 中心メッセージは一つ。条件や例外を削らない。定型質問、反応要求、抽象的な締めは付けない。'+
            ' 資料にないことを資料作成者が公開していないと決めつけない。')


def record_publication(assignment, tweet_id, posted_at, *, path=None):
    if not assignment:
        return
    with closing(connect(path)) as conn:
        conn.execute('UPDATE reach_assignments SET tweet_id=?,posted_at=? WHERE source_key=? AND tweet_id IS NULL',
                     (str(tweet_id),posted_at,assignment['source_key']))
        conn.commit()


def rank_eligible(items, history):
    """Only called AFTER the existing relevance/freshness gates."""
    cfg=settings()
    if not cfg['enabled'] or os.environ.get('REACH_POLICY_ENABLED','true').lower()=='false':
        return items
    weights=cfg['ranking_weights']
    def score(item):
        topic=item.get('topic_key') or item.get('title')
        repeat=sum((r.get('topic_key') or r.get('title'))==topic for r in history[-20:])
        values={'reader_impact':float(item.get('news_relevance_score') or 0),
                'freshness':float(item.get('freshness_score') or 0),
                'observed_demand':item.get('x_attention_score') if item.get('x_post_count',0)>0 and not item.get('xai_topic_match') else None,
                'explanation_value':None, 'difference':10/(1+repeat)}
        active={k:v for k,v in values.items() if v is not None}
        priority=sum(weights[k]*float(v) for k,v in active.items())/sum(weights[k] for k in active)
        item['reach_priority']={'version':cfg['version'],'features':values,'score':priority,'hypothesis':True}
        return priority
    return sorted(items,key=score,reverse=True)
