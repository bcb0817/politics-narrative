"""Bounded stock for the existing collector/selector; first expiry is immutable."""
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime, format_datetime
from metrics_db import connect, init_db

SCHEMA='''CREATE TABLE IF NOT EXISTS reach_inventory (
 source_url TEXT PRIMARY KEY, first_seen_at TEXT, expires_at TEXT,
 payload_json TEXT, last_seen_at TEXT, status TEXT);
'''


def published_time(item):
    raw=item.get('pub_date') or item.get('published_at')
    if not raw: return None
    try: result=datetime.fromisoformat(raw)
    except ValueError:
        try: result=parsedate_to_datetime(raw)
        except (ValueError,TypeError): return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def refresh(items, *, path=None, now=None, max_age_hours=24):
    now=now or datetime.now(timezone.utc)
    init_db(path)
    with closing(connect(path)) as conn:
        conn.executescript(SCHEMA)
        for item in items[:500]:
            source=item.get('url')
            if not source: continue
            published=published_time(item)
            expiry=(published or now)+timedelta(hours=max_age_hours)
            status='ready' if published and published<=now and expiry>=now else 'unknown_or_expired'
            payload=dict(item)
            if published: payload['pub_date']=format_datetime(published)
            conn.execute('''INSERT INTO reach_inventory VALUES(?,?,?,?,?,?)
                ON CONFLICT(source_url) DO UPDATE SET payload_json=excluded.payload_json,
                last_seen_at=excluded.last_seen_at,
                expires_at=MIN(reach_inventory.expires_at,excluded.expires_at),
                status=excluded.status''',
                (source,now.isoformat(),expiry.astimezone(timezone.utc).isoformat(),json.dumps(payload,ensure_ascii=False),now.isoformat(),status))
        conn.commit()
    return available(path=path,now=now)


def available(*, path=None, now=None):
    now=now or datetime.now(timezone.utc)
    init_db(path)
    with closing(connect(path)) as conn:
        conn.executescript(SCHEMA)
        return [json.loads(r[0]) for r in conn.execute('''SELECT payload_json FROM reach_inventory
            WHERE status='ready' AND julianday(expires_at)>=julianday(?)
            ORDER BY first_seen_at DESC LIMIT 500''',(now.isoformat(),))]
