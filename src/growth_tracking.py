"""Follower snapshots, external conversions, and human-review persistence."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from api_budget import estimate_x, finalize, reserve
from metrics_db import apply_additive_migrations, connect, db_path, init_db, write

JST = ZoneInfo("Asia/Tokyo")
CONVERSION_TYPES = {
    "profile_visit", "note_click", "note_follow", "note_purchase",
    "note_view", "note_like", "note_comment",
    "youtube_click", "youtube_view", "youtube_subscribe",
    "newsletter_click", "newsletter_signup", "paid_purchase",
}


def capture_follower_snapshot(client_factory=None, path: Path | None = None,
                              now: datetime | None = None) -> dict:
    """Capture only owned-account counts. It never modifies the X account."""
    now = now or datetime.now(JST)
    path = path or db_path()
    apply_additive_migrations(path)
    maximum = estimate_x("owned_read_per_resource", 1)
    reservation, reason = reserve(
        "x", "owned_read", "users_me", maximum, 1,
        {"purpose": "follower_snapshot"}, path=path,
    )
    if not reservation:
        return {"captured": False, "reason": reason}
    try:
        if client_factory is None:
            import tweepy
            client_factory = tweepy.Client
        client = client_factory(
            consumer_key=os.environ.get("API_KEY", ""),
            consumer_secret=os.environ.get("API_KEY_SECRET", ""),
            access_token=os.environ.get("ACCESS_TOKEN", ""),
            access_token_secret=os.environ.get("ACCESS_TOKEN_SECRET", ""),
            wait_on_rate_limit=False,
        )
        response = client.get_me(user_auth=True, user_fields=["public_metrics"])
        metrics = dict(getattr(response.data, "public_metrics", None) or {})
        row = {
            "timestamp": now.isoformat(),
            "followers_count": int(metrics.get("followers_count", 0) or 0),
            "following_count": int(metrics.get("following_count", 0) or 0),
            "posts_count": int(metrics.get("tweet_count", 0) or 0),
            "source": "x_owned_read",
            "estimated": False,
        }
        write("""INSERT OR IGNORE INTO follower_snapshots
          (captured_at,followers_count,following_count,posts_count,source,estimated)
          VALUES (?,?,?,?,?,?)""", (
            row["timestamp"], row["followers_count"], row["following_count"],
            row["posts_count"], row["source"], 0,
        ), path)
        finalize(reservation, maximum or 0, success=True, resource_count=1, path=path)
        return {"captured": True, **row}
    except Exception as exc:
        finalize(reservation, 0, success=False, error_type=type(exc).__name__,
                 resource_count=0, path=path)
        return {"captured": False, "reason": type(exc).__name__}


def follower_status(path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    try:
        with closing(connect(path)) as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM follower_snapshots ORDER BY captured_at DESC LIMIT 2"
            )]
    except Exception:
        rows = []
    change = None
    if len(rows) == 2:
        change = int(rows[0]["followers_count"] or 0) - int(rows[1]["followers_count"] or 0)
    return {"latest": rows[0] if rows else None, "previous": rows[1] if len(rows) > 1 else None,
            "follower_change": change, "estimate_note": "time-window estimate; not post-level attribution"}


def import_conversions(source: Path, path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    inserted = 0
    rejected = 0
    duplicates = 0
    quarantined = 0
    with open(source, "r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            event_type = str(row.get("event_type", "")).strip()
            canonical = "|".join(str(row.get(key, "")).strip() for key in (
                "occurred_at", "source", "campaign", "content_id",
                "event_type", "value"))
            event_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if event_type not in CONVERSION_TYPES:
                rejected += 1
                result = write("""INSERT OR IGNORE INTO conversion_event_quarantine
                  (imported_at,source_file,row_json,rejection_reason,event_key)
                  VALUES (?,?,?,?,?)""", (
                    datetime.now(JST).isoformat(), str(source),
                    json.dumps(row, ensure_ascii=False), "invalid_event_type",
                    event_key,
                ), path)
                quarantined += int(result is not None)
                continue
            metadata = row.get("metadata_json", "")
            try:
                metadata_obj = json.loads(metadata) if metadata else {}
            except json.JSONDecodeError:
                metadata_obj = {"raw": metadata}
            try:
                with closing(connect(path)) as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM conversion_events WHERE event_key=?",
                        (event_key,)).fetchone()
                    if exists:
                        duplicates += 1
                        continue
                    conn.execute("""INSERT INTO conversion_events
                      (occurred_at,source,campaign,content_id,event_type,value,
                       metadata_json,event_key) VALUES (?,?,?,?,?,?,?,?)""", (
                        row.get("occurred_at") or datetime.now(JST).isoformat(),
                        row.get("source", ""), row.get("campaign", ""),
                        row.get("content_id", ""), event_type,
                        float(row.get("value", 1) or 0),
                        json.dumps(metadata_obj, ensure_ascii=False), event_key,
                    ))
                    conn.commit()
                    inserted += 1
            except Exception:
                rejected += 1
    return {
        "inserted": inserted, "duplicates": duplicates, "rejected": rejected,
        "quarantined": quarantined, "source_unchanged": True,
    }


def import_human_reviews(source: Path, path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    suffix = source.suffix.lower()
    if suffix == ".json":
        payload = json.loads(source.read_text(encoding="utf-8"))
        rows = payload if isinstance(payload, list) else payload.get("reviews", [])
    else:
        with open(source, "r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    inserted = 0
    for row in rows:
        scores = {}
        for key in ("factuality", "relevance", "logic", "originality",
                    "natural_japanese", "brand_fit"):
            value = row.get(key)
            if value not in (None, ""):
                scores[key] = max(1, min(5, int(float(value))))
        reviewed_at = row.get("reviewed_at") or datetime.now(JST).isoformat()
        result = write("""INSERT OR IGNORE INTO human_reviews
          (content_type,content_id,reviewed_at,scores_json,should_post,notes,reviewer)
          VALUES (?,?,?,?,?,?,?)""", (
            row.get("content_type", "published"),
            str(row.get("content_id") or row.get("tweet_id") or ""),
            reviewed_at, json.dumps(scores, ensure_ascii=False),
            int(str(row.get("should_post", "")).strip().lower() in {"1", "true", "yes", "y"}),
            row.get("notes", ""), row.get("reviewer", "human"),
        ), path)
        inserted += int(result is not None)
    return {"inserted": inserted, "source": str(source)}
