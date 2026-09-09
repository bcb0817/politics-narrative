"""Durable X send barrier shared by text, video and disaster publications.

A crashed/uncertain request is never automatically resent. Reconciliation can
only confirm a matching owned publication; absence is not proof of failure.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing
from datetime import datetime, timezone, timedelta

from metrics_db import connect, init_db
from api_budget import reserve, finalize, estimate_x


class DeliveryBlocked(RuntimeError):
    pass


SCHEMA = """CREATE TABLE IF NOT EXISTS x_delivery (
 delivery_key TEXT PRIMARY KEY, payload_hash TEXT UNIQUE NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 external_id TEXT, reservation_id INTEGER, error_type TEXT);
"""


def publish(payload, send, *, key=None, path=None, now=None):
    """send returns a confirmed nonempty X ID; all uncertain errors fail closed."""
    now = now or datetime.now(timezone.utc)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    key = key or digest
    init_db(path)
    with closing(connect(path)) as conn:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status,external_id FROM x_delivery WHERE delivery_key=? OR payload_hash=?", (key, digest)).fetchone()
        if row:
            raise DeliveryBlocked("x_delivery_" + row["status"])
        conn.execute("INSERT INTO x_delivery VALUES(?,?,'prepared',?,?,NULL,NULL,NULL)",
                     (key, digest, now.isoformat(), now.isoformat()))
        conn.commit()
    endpoint = 'post_create_with_url_per_request' if re.search(r'https?://|www\.', payload.get('text',''), re.I) else 'post_create_per_request'
    cost = estimate_x(endpoint, 1)
    reservation, reason = reserve("x", "post_create", "post_create", cost, 1,
                                  {"delivery_key": key}, path=path)
    if not reservation:
        with closing(connect(path)) as conn:
            conn.execute("DELETE FROM x_delivery WHERE delivery_key=? AND status='prepared'", (key,))
            conn.commit()
        raise DeliveryBlocked(reason)
    with closing(connect(path)) as conn:
        conn.execute("UPDATE x_delivery SET status='sending',reservation_id=? WHERE delivery_key=?", (reservation,key))
        conn.commit()
    try:
        external_id = send()
        if not external_id or str(external_id) in {"None", "null"}:
            raise ValueError("x_response_id_missing")
        with closing(connect(path)) as conn:
            conn.execute("UPDATE x_delivery SET status='published',external_id=?,updated_at=? WHERE delivery_key=?",
                         (str(external_id),now.isoformat(),key))
            conn.commit()
        finalize(reservation, cost, success=True, path=path)
        return str(external_id)
    except Exception as exc:
        with closing(connect(path)) as conn:
            conn.execute("UPDATE x_delivery SET status='ambiguous',error_type=?,updated_at=? WHERE delivery_key=?",
                         (type(exc).__name__,now.isoformat(),key))
            conn.commit()
        # Keep maximum reservation: the server may have accepted and charged.
        raise DeliveryBlocked("x_delivery_ambiguous") from exc


def reconcile(payload, external_id, *, published_at=None, path=None, now=None):
    """Call only with payload/ID observed in the authenticated owner's timeline."""
    if not external_id or published_at is None:
        return False
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with closing(connect(path)) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT * FROM x_delivery WHERE payload_hash=? AND status IN ('sending','ambiguous')", (digest,)).fetchone()
        if not row:
            return False
        observed = datetime.fromisoformat(str(published_at)) if not isinstance(published_at, datetime) else published_at
        if observed.tzinfo is None or observed < datetime.fromisoformat(row['created_at'])-timedelta(minutes=1):
            return False
        result = conn.execute("UPDATE x_delivery SET status='published',external_id=?,updated_at=? WHERE payload_hash=? AND status IN ('sending','ambiguous')",
                              (str(external_id),(now or datetime.now(timezone.utc)).isoformat(),digest))
        conn.execute("INSERT OR IGNORE INTO published_posts(tweet_id,text,posted_at,post_type,prompt_version) VALUES(?,?,?,'reconciled','unknown')",
                     (str(external_id),payload.get('text',''),observed.isoformat()))
        conn.commit()
        return result.rowcount == 1
