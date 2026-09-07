"""Additive reach telemetry and request-scoped attribution; never publishes."""
import hashlib
import json
from contextlib import closing, contextmanager
from contextvars import ContextVar

from metrics_db import connect, init_db

_scope = ContextVar('reach_cost_scope', default={})
SCHEMA = '''
CREATE TABLE IF NOT EXISTS reach_features (
 tweet_id TEXT PRIMARY KEY, source_key TEXT, recorded_at TEXT, features_json TEXT);
CREATE TABLE IF NOT EXISTS reach_models (
 version TEXT PRIMARY KEY, trained_at TEXT, status TEXT, model_json TEXT);
CREATE TABLE IF NOT EXISTS reach_incidents (
 event_key TEXT PRIMARY KEY, occurred_at TEXT, category TEXT, stage TEXT,
 reason TEXT, platform TEXT, reference_id TEXT, resolved INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reach_daily_posts (
 day TEXT, tweet_id TEXT, observed_at TEXT, impressions INTEGER,
 bookmarks INTEGER, quotes INTEGER, profile_clicks INTEGER,
 source TEXT, PRIMARY KEY(day,tweet_id));
CREATE TABLE IF NOT EXISTS reach_daily_runs (
 day TEXT PRIMARY KEY, updated_at TEXT, status TEXT, expected_ids_json TEXT,
 reason TEXT, inventory_complete INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS reach_runtime_snapshots (
 captured_at TEXT PRIMARY KEY, config_json TEXT);
CREATE TABLE IF NOT EXISTS reach_collection_runs (
 run_key TEXT PRIMARY KEY, started_at TEXT, requested INTEGER, collected INTEGER,
 reason TEXT, stage TEXT);
'''


def ensure(path=None):
    init_db(path)
    with closing(connect(path)) as conn:
        conn.executescript(SCHEMA)


def source_key(item):
    source = item.get('url') or item.get('source_url')
    return hashlib.sha256(source.encode()).hexdigest() if source else None


@contextmanager
def cost_scope(**metadata):
    token = _scope.set({**_scope.get(), **metadata})
    try:
        yield
    finally:
        _scope.reset(token)


def cost_metadata():
    return dict(_scope.get())


def incident(key, at, category, stage, reason, platform='x', reference=None, path=None):
    ensure(path)
    with closing(connect(path)) as conn:
        conn.execute('INSERT OR IGNORE INTO reach_incidents VALUES(?,?,?,?,?,?,?,0)',
                     (key, at, category, stage, reason, platform, reference))
        conn.commit()


def collection_run(now, requested, collected, reason, stage, path=None):
    ensure(path)
    with closing(connect(path)) as conn:
        conn.execute('INSERT OR REPLACE INTO reach_collection_runs VALUES(?,?,?,?,?,?)',
                     (stage+':'+now.isoformat(), now.isoformat(), requested, collected,
                      reason, stage))
        conn.commit()
