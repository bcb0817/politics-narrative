"""Read-first Threads analytics and autonomous official API actions.

Only Meta's graph.threads.net API is used.  Read/sync operations may be
scheduled.  External writes always require an action-specific opt-in flag.
Interactive confirmation is required only when autonomous posting is disabled.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from metrics_db import apply_threads_full_migrations, connect, write
from threads_api import (
    JST, KNOWN_SCOPES, SCOPE_PROFILES, ThreadsClient, _hash_payload,
    _now, _parse_datetime, settings as base_settings, token_status,
)


PROFILE_FIELDS = (
    "id", "username", "name", "is_verified",
    "threads_profile_picture_url", "threads_biography",
    "recently_searched_keywords", "is_eligible_for_geo_gating",
)
POST_METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")
ACCOUNT_METRICS = (
    "views", "likes", "replies", "reposts", "quotes", "clicks",
    "followers_count", "follower_demographics",
)
PERMISSION_FEATURES = {
    "threads_basic": "profile and owned posts",
    "threads_content_publish": "publishing, reply, quote, repost",
    "threads_manage_insights": "post and account insights",
    "threads_read_replies": "reply ingestion",
    "threads_manage_replies": "reply moderation",
    "threads_keyword_search": "keyword and topic-tag search",
    "threads_manage_mentions": "mention ingestion",
    "threads_delete": "owned-post deletion",
    "threads_location_tagging": "location retrieval/tagging",
    "threads_profile_discovery": "public profile discovery",
}
ACTION_ENV = {
    "reply": "THREADS_AUTO_REPLY_ENABLED",
    "quote": "THREADS_AUTO_QUOTE_ENABLED",
    "repost": "THREADS_AUTO_REPOST_ENABLED",
    "delete": "THREADS_AUTO_DELETE_ENABLED",
    "hide": "THREADS_AUTO_HIDE_REPLY_ENABLED",
    "unhide": "THREADS_AUTO_UNHIDE_REPLY_ENABLED",
    "publish_format": "THREADS_AUTO_MEDIA_POST_ENABLED",
}
ACTION_APPROVAL_ENV = {
    "reply": "THREADS_REQUIRE_APPROVAL_FOR_REPLY",
    "quote": "THREADS_REQUIRE_APPROVAL_FOR_QUOTE",
    "repost": "THREADS_REQUIRE_APPROVAL_FOR_REPOST",
    "delete": "THREADS_REQUIRE_APPROVAL_FOR_DELETE",
    "hide": "THREADS_REQUIRE_APPROVAL_FOR_MODERATION",
    "unhide": "THREADS_REQUIRE_APPROVAL_FOR_MODERATION",
    "publish_format": "THREADS_REQUIRE_APPROVAL_FOR_MEDIA",
}
FORMAT_FLAGS = {
    "TEXT": "THREADS_TEXT_POST_ENABLED",
    "IMAGE": "THREADS_IMAGE_POST_ENABLED",
    "VIDEO": "THREADS_VIDEO_POST_ENABLED",
    "CAROUSEL": "THREADS_CAROUSEL_POST_ENABLED",
}
REPLY_CONTROLS = {
    "everyone", "accounts_you_follow", "mentioned_only",
    "parent_post_author_only", "followers_only",
}
CONTAINER_STATES = {"IN_PROGRESS", "FINISHED", "PUBLISHED", "ERROR", "EXPIRED"}


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _int(name: str, default: int, minimum: int = 0,
         maximum: int = 100000) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def settings() -> dict:
    return {
        **base_settings(),
        "sync_posts": _bool("THREADS_SYNC_POSTS_ENABLED", "true"),
        "sync_replies": _bool("THREADS_SYNC_REPLIES_ENABLED", "true"),
        "sync_mentions": _bool("THREADS_SYNC_MENTIONS_ENABLED", "true"),
        "post_insights": _bool("THREADS_POST_INSIGHTS_ENABLED", "true"),
        "account_insights": _bool(
            "THREADS_ACCOUNT_INSIGHTS_ENABLED", "true"),
        "profile_sync": _bool("THREADS_PROFILE_SYNC_ENABLED", "true"),
        "quota_sync": _bool("THREADS_QUOTA_SYNC_ENABLED", "true"),
        "keyword_search": _bool("THREADS_KEYWORD_SEARCH_ENABLED", "false"),
        "topic_tag_search": _bool(
            "THREADS_TOPIC_TAG_SEARCH_ENABLED", "false"),
        "search_type": os.environ.get(
            "THREADS_SEARCH_TYPE_DEFAULT", "RECENT").upper(),
        "search_limit": _int("THREADS_SEARCH_LIMIT", 50, 1, 100),
        "search_lookback_hours": _int(
            "THREADS_SEARCH_LOOKBACK_HOURS", 24, 1, 720),
        "search_cache_minutes": _int(
            "THREADS_SEARCH_CACHE_TTL_MINUTES", 60, 1, 1440),
        "analysis_enabled": _bool("THREADS_ANALYSIS_ENABLED", "true"),
        "discord_research": _bool(
            "THREADS_DISCORD_RESEARCH_ENABLED", "true"),
        "raw_retention_days": _int(
            "THREADS_RAW_RESPONSE_RETENTION_DAYS", 30, 1, 365),
        "search_retention_days": _int(
            "THREADS_SEARCH_RESULT_RETENTION_DAYS", 90, 1, 730),
        "analytics_retention_days": _int(
            "THREADS_ANALYTICS_RETENTION_DAYS", 730, 30, 3650),
        "personal_retention_days": _int(
            "THREADS_PERSONAL_DATA_RETENTION_DAYS", 90, 1, 730),
    }


def _now_text() -> str:
    return _now().isoformat()


def _username_hash(value: str) -> str:
    return hashlib.sha256(
        str(value or "").strip().lower().encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _upsert(sql: str, params: tuple, path: Path | None) -> bool:
    apply_threads_full_migrations(path)
    return write(sql, params, path) is not None


def _discord_result(title: str, fields: dict, level: str = "info") -> bool:
    if not _bool("THREADS_DISCORD_REPORT_ENABLED", "true"):
        return False
    try:
        from discord_notify import notify
        return notify(
            "threads_report", title,
            "Threads公式APIの集計結果だけを通知します。",
            level=level, fields=fields)
    except Exception:
        return False


def _discord_research(report: dict, *, dry_run: bool = False) -> bool:
    if dry_run or not settings()["discord_research"]:
        return False
    try:
        from discord_notify import notify_threads_research
        return notify_threads_research(report)
    except Exception:
        return False


def _granted_scopes(debug_payload: dict | None = None) -> set[str]:
    data = (debug_payload or {}).get("data") or {}
    values = data.get("scopes") or data.get("granular_scopes") or []
    output: set[str] = set()
    for value in values:
        if isinstance(value, str):
            output.add(value)
        elif isinstance(value, dict) and value.get("scope"):
            output.add(str(value["scope"]))
    if not output:
        output.update(base_settings()["scopes"])
    return output.intersection(KNOWN_SCOPES)


def permissions(client: ThreadsClient | None = None, *,
                probe: bool = False) -> dict:
    """Return a secret-free permission matrix.

    ``probe`` performs the official read-only debug_token request.  Offline
    mode reports the scopes last requested/stored locally and labels the
    source so it is never mistaken for a live grant.
    """
    debug: dict = {}
    source = "configured_oauth_scopes"
    error = ""
    if probe and base_settings()["access_token"]:
        try:
            debug = (client or ThreadsClient()).debug_token()
            source = "official_debug_token"
        except Exception as exc:
            error = type(exc).__name__
            source = "configured_oauth_scopes_fallback"
    granted = _granted_scopes(debug)
    required = set(SCOPE_PROFILES["full-analysis"])
    rows = [{
        "permission": scope,
        "granted": scope in granted,
        "required": scope in required,
        "feature": PERMISSION_FEATURES[scope],
    } for scope in KNOWN_SCOPES]
    return {
        "source": source,
        "token_present": bool(base_settings()["access_token"]),
        "permissions": rows,
        "granted": sorted(granted),
        "missing": sorted(required - granted),
        "debug_error": error or None,
        "secrets_returned": False,
    }


def profile_sync(client: ThreadsClient | None = None, *,
                 dry_run: bool = False, path: Path | None = None) -> dict:
    cfg = settings()
    if not cfg["profile_sync"]:
        return {"status": "skipped", "reason": "profile_sync_disabled"}
    if dry_run:
        return {
            "status": "dry_run", "endpoint": "GET /me",
            "fields": list(PROFILE_FIELDS), "api_calls": 0, "writes": 0,
        }
    if not cfg["access_token"]:
        return {"status": "skipped", "reason": "token_missing"}
    row = (client or ThreadsClient(path=path)).profile()
    now = _now_text()
    user_id = str(row.get("id") or cfg["user_id"] or "")
    _upsert("""INSERT INTO threads_profiles
      (threads_user_id,username,name,is_verified,profile_picture_url,biography,
       recently_searched_keywords_json,is_eligible_for_geo_gating,synced_at,
       created_at,updated_at,source,api_version,raw_response_hash)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(threads_user_id) DO UPDATE SET username=excluded.username,
       name=excluded.name,is_verified=excluded.is_verified,
       profile_picture_url=excluded.profile_picture_url,
       biography=excluded.biography,
       recently_searched_keywords_json=excluded.recently_searched_keywords_json,
       is_eligible_for_geo_gating=excluded.is_eligible_for_geo_gating,
       synced_at=excluded.synced_at,updated_at=excluded.updated_at,
       raw_response_hash=excluded.raw_response_hash""", (
        user_id, row.get("username"), row.get("name"),
        int(bool(row.get("is_verified"))),
        row.get("threads_profile_picture_url"),
        row.get("threads_biography"),
        _json(row.get("recently_searched_keywords") or []),
        int(bool(row.get("is_eligible_for_geo_gating"))),
        now, now, now, "meta_official_api", cfg["api_version"],
        _hash_payload(row),
    ), path)
    return {
        "status": "synced", "threads_user_id": user_id,
        "username": row.get("username"), "api_calls": 1, "writes": 1,
    }


def _save_children(post: dict, path: Path | None) -> int:
    children = post.get("children") or {}
    if isinstance(children, dict):
        children = children.get("data") or []
    if not isinstance(children, list):
        return 0
    count = 0
    now = _now_text()
    for position, child in enumerate(children):
        if not isinstance(child, dict) or not child.get("id"):
            continue
        count += int(_upsert("""INSERT INTO threads_post_children
          (parent_post_id,child_post_id,position,media_type,media_url,alt_text,
           created_at,updated_at,source,api_version,raw_response_hash)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(parent_post_id,child_post_id) DO UPDATE SET
           position=excluded.position,media_type=excluded.media_type,
           media_url=excluded.media_url,alt_text=excluded.alt_text,
           updated_at=excluded.updated_at,
           raw_response_hash=excluded.raw_response_hash""", (
            str(post["id"]), str(child["id"]), position,
            child.get("media_type"), child.get("media_url"),
            child.get("alt_text"), now, now, "meta_official_api",
            base_settings()["api_version"], _hash_payload(child),
        ), path))
    return count


def _save_poll(post: dict, path: Path | None) -> int:
    poll = post.get("poll_attachment")
    if not isinstance(poll, dict):
        return 0
    now = _now_text()
    return int(_upsert("""INSERT OR IGNORE INTO threads_poll_snapshots
      (threads_post_id,measured_at,options_json,expiration_timestamp,
       created_at,updated_at,source,api_version,raw_response_hash)
      VALUES (?,?,?,?,?,?,?,?,?)""", (
        str(post["id"]), now, _json(poll), poll.get("expiration_timestamp"),
        now, now, "meta_official_api", base_settings()["api_version"],
        _hash_payload(poll),
    ), path))


def sync_posts(client: ThreadsClient | None = None, *, dry_run: bool = False,
               since: str = "", until: str = "",
               path: Path | None = None) -> dict:
    cfg = settings()
    if not cfg["sync_posts"]:
        return {"status": "skipped", "reason": "post_sync_disabled"}
    if dry_run:
        return {
            "status": "dry_run", "endpoint": "GET /me/threads",
            "since": since or None, "until": until or None,
            "api_calls": 0, "writes": 0,
        }
    if not cfg["access_token"]:
        return {"status": "skipped", "reason": "token_missing"}
    posts = (client or ThreadsClient(path=path)).own_posts(
        since=since or None, until=until or None)
    now = _now_text()
    saved = children = polls = 0
    for post in posts:
        post_id = str(post.get("id") or "")
        if not post_id:
            continue
        owner = post.get("owner") or {}
        text = str(post.get("text") or "")
        existing_key = f"synced:{post_id}"
        saved += int(_upsert("""INSERT INTO threads_posts
          (client_post_key,threads_post_id,threads_user_id,text,status,
           published_at,reply_control,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?)
          ON CONFLICT(threads_post_id) DO UPDATE SET text=excluded.text,
           published_at=COALESCE(excluded.published_at,threads_posts.published_at),
           updated_at=excluded.updated_at""", (
            existing_key, post_id, str(owner.get("id") or cfg["user_id"]),
            text, "published", post.get("timestamp"), None, now, now,
        ), path))
        children += _save_children(post, path)
        polls += _save_poll(post, path)
    _save_cursor("posts", "", max(
        (str(p.get("timestamp") or "") for p in posts), default=""), "", path)
    return {
        "status": "synced", "received": len(posts), "saved": saved,
        "children_saved": children, "poll_snapshots_saved": polls,
    }


def _save_cursor(sync_type: str, cursor: str, timestamp: str,
                 error: str, path: Path | None) -> None:
    now = _now_text()
    _upsert("""INSERT INTO threads_sync_cursors
      (sync_type,after_cursor,last_item_timestamp,last_success_at,last_error_at,
       error_class,created_at,updated_at,source,api_version,raw_response_hash)
      VALUES (?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(sync_type) DO UPDATE SET
       after_cursor=excluded.after_cursor,
       last_item_timestamp=excluded.last_item_timestamp,
       last_success_at=excluded.last_success_at,
       last_error_at=excluded.last_error_at,error_class=excluded.error_class,
       updated_at=excluded.updated_at""", (
        sync_type, cursor, timestamp, now if not error else None,
        now if error else None, error, now, now, "meta_official_api",
        base_settings()["api_version"], "",
    ), path)


def _save_reply(row: dict, path: Path | None) -> bool:
    reply_id = str(row.get("id") or "")
    if not reply_id:
        return False
    root = row.get("root_post") or {}
    parent = row.get("replied_to") or {}
    now = _now_text()
    return _upsert("""INSERT INTO threads_replies
      (reply_id,root_post_id,parent_reply_id,username_hash,text,timestamp,
       media_type,permalink,is_owned_by_me,hide_status,reply_audience,
       reply_approval_status,synced_at,created_at,updated_at,source,
       api_version,raw_response_hash)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(reply_id) DO UPDATE SET text=excluded.text,
       hide_status=excluded.hide_status,
       reply_approval_status=excluded.reply_approval_status,
       synced_at=excluded.synced_at,updated_at=excluded.updated_at,
       raw_response_hash=excluded.raw_response_hash""", (
        reply_id, str(root.get("id") or ""),
        str(parent.get("id") or ""), _username_hash(row.get("username", "")),
        row.get("text"), row.get("timestamp"), row.get("media_type"),
        row.get("permalink"), int(bool(row.get("is_reply_owned_by_me"))),
        row.get("hide_status"), row.get("reply_audience"),
        row.get("reply_approval_status"), now, now, now,
        "meta_official_api", base_settings()["api_version"],
        _hash_payload(row),
    ), path)


def sync_replies(client: ThreadsClient | None = None, *,
                 dry_run: bool = False, path: Path | None = None) -> dict:
    cfg = settings()
    if not cfg["sync_replies"]:
        return {"status": "skipped", "reason": "reply_sync_disabled"}
    if dry_run:
        return {
            "status": "dry_run",
            "endpoints": ["GET /{post-id}/conversation", "GET /me/replies"],
            "api_calls": 0, "writes": 0,
        }
    if "threads_read_replies" not in _granted_scopes():
        return {"status": "skipped", "reason": "permission_missing",
                "permission": "threads_read_replies"}
    api = client or ThreadsClient(path=path)
    apply_threads_full_migrations(path)
    with closing(connect(path)) as conn:
        post_ids = [
            str(row[0]) for row in conn.execute(
                """SELECT threads_post_id FROM threads_posts
                   WHERE threads_post_id IS NOT NULL AND status='published'
                   ORDER BY published_at DESC LIMIT 100""")
        ]
    received: list[dict] = []
    for post_id in post_ids:
        received.extend(api.replies(post_id))
    received.extend(api.own_replies())
    saved = sum(int(_save_reply(row, path)) for row in received)
    _save_cursor("replies", "", max(
        (str(r.get("timestamp") or "") for r in received), default=""), "", path)
    return {"status": "synced", "received": len(received), "saved": saved}


def sync_mentions(client: ThreadsClient | None = None, *,
                  dry_run: bool = False, path: Path | None = None) -> dict:
    cfg = settings()
    if not cfg["sync_mentions"]:
        return {"status": "skipped", "reason": "mention_sync_disabled"}
    if dry_run:
        return {"status": "dry_run", "endpoint": "GET /me/mentions",
                "api_calls": 0, "writes": 0}
    if "threads_manage_mentions" not in _granted_scopes():
        return {"status": "skipped", "reason": "permission_missing",
                "permission": "threads_manage_mentions"}
    rows = (client or ThreadsClient(path=path)).mentions()
    now = _now_text()
    saved = 0
    for row in rows:
        mention_id = str(row.get("id") or "")
        if not mention_id:
            continue
        saved += int(_upsert("""INSERT INTO threads_mentions
          (mention_id,username_hash,text,timestamp,media_type,permalink,
           synced_at,created_at,updated_at,source,api_version,raw_response_hash)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(mention_id) DO UPDATE SET text=excluded.text,
           synced_at=excluded.synced_at,updated_at=excluded.updated_at,
           raw_response_hash=excluded.raw_response_hash""", (
            mention_id, _username_hash(row.get("username", "")),
            row.get("text"), row.get("timestamp"), row.get("media_type"),
            row.get("permalink"), now, now, now, "meta_official_api",
            cfg["api_version"], _hash_payload(row),
        ), path))
    _save_cursor("mentions", "", max(
        (str(r.get("timestamp") or "") for r in rows), default=""), "", path)
    return {"status": "synced", "received": len(rows), "saved": saved}


def _metric_value(row: dict) -> float | None:
    values = row.get("values") or []
    if values and isinstance(values[-1], dict):
        value = values[-1].get("value")
    else:
        value = (row.get("total_value") or {}).get("value")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def collect_post_insights(client: ThreadsClient | None = None, *,
                          dry_run: bool = False,
                          path: Path | None = None) -> dict:
    cfg = settings()
    if dry_run:
        return {"status": "dry_run", "endpoint": "GET /{post-id}/insights",
                "api_calls": 0, "writes": 0}
    if not cfg["post_insights"] or "threads_manage_insights" not in _granted_scopes():
        return {"status": "skipped", "reason": "insights_disabled_or_permission"}
    apply_threads_full_migrations(path)
    with closing(connect(path)) as conn:
        posts = [dict(row) for row in conn.execute(
            """SELECT threads_post_id,published_at FROM threads_posts
               WHERE status='published' AND threads_post_id IS NOT NULL""")]
        saved_windows: dict[str, set[str]] = defaultdict(set)
        for row in conn.execute(
                """SELECT threads_post_id,measurement_window
                   FROM threads_post_insights"""):
            saved_windows[str(row["threads_post_id"])].add(
                str(row["measurement_window"]))
    api = client or ThreadsClient(path=path)
    api_calls = saved = skipped = failed = 0
    now = _now()
    windows = list(dict.fromkeys([
        "15m", "1h", "6h",
        *[
            value.strip() for value in os.environ.get(
                "THREADS_POST_METRIC_WINDOWS", "1h,6h,24h,72h,7d").split(",")
            if value.strip()
        ],
        "24h", "72h",
    ]))
    for post in posts:
        post_id = str(post["threads_post_id"])
        published = _parse_datetime(post.get("published_at"))
        if not published:
            skipped += 1
            continue
        age = (now - published.astimezone(JST)).total_seconds() / 3600
        due = [w for w in windows if age >= {
            "15m": .25, "1h": 1, "6h": 6, "24h": 24, "72h": 72, "7d": 168
        }.get(w, math.inf) and w not in saved_windows[post_id]]
        if not due:
            skipped += 1
            continue
        measured = now.replace(
            minute=0, second=0, microsecond=0).isoformat()
        try:
            api_calls += 1
            payload = api.insights(post_id)
            values = {row.get("name"): _metric_value(row)
                      for row in payload.get("data") or []}
            views = values.get("views")
            engagement_values = [
                values.get(name) for name in POST_METRICS[1:]
                if values.get(name) is not None
            ]
            engagement = (
                sum(engagement_values) / views
                if views and views > 0 else None)
            for window in due:
                saved += int(_upsert(
                    """INSERT OR IGNORE INTO threads_post_insights
                      (threads_post_id,measurement_window,measured_at,views,
                       likes,replies,reposts,quotes,shares,engagement_rate,
                       views_per_hour,created_at,updated_at,source,api_version,
                       raw_response_hash)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        post_id, window, measured, views,
                        values.get("likes"), values.get("replies"),
                        values.get("reposts"), values.get("quotes"),
                        values.get("shares"), engagement,
                        views / max(1, {
                            "15m": .25, "1h": 1, "6h": 6, "24h": 24, "72h": 72,
                            "7d": 168}[window]) if views is not None else None,
                        measured, measured, "meta_official_api",
                        cfg["api_version"], _hash_payload(payload),
                    ), path))
        except Exception:
            failed += 1
    return {
        "status": "completed", "api_calls": api_calls, "saved": saved,
        "skipped": skipped, "failed": failed,
    }


def collect_account_insights(client: ThreadsClient | None = None, *,
                             dry_run: bool = False,
                             path: Path | None = None) -> dict:
    cfg = settings()
    if dry_run:
        return {"status": "dry_run", "endpoint": "GET /me/threads_insights",
                "metrics": list(ACCOUNT_METRICS), "api_calls": 0, "writes": 0}
    if not cfg["account_insights"] or "threads_manage_insights" not in _granted_scopes():
        return {"status": "skipped", "reason": "insights_disabled_or_permission"}
    api = client or ThreadsClient(path=path)
    payload = api.account_insights(metrics=",".join(ACCOUNT_METRICS))
    now = _now_text()
    saved = 0
    for row in payload.get("data") or []:
        metric = str(row.get("name") or "")
        values = row.get("values") or []
        if not values:
            values = [{"value": _metric_value(row), "end_time": ""}]
        for point in values:
            saved += int(_upsert("""INSERT OR IGNORE INTO
              threads_account_insights
              (threads_user_id,metric_name,period,measured_at,value,end_time,
               created_at,updated_at,source,api_version,raw_response_hash)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                cfg["user_id"], metric, row.get("period"), now,
                point.get("value"), point.get("end_time"), now, now,
                "meta_official_api", cfg["api_version"], _hash_payload(row),
            ), path))
    demographics = 0
    for breakdown in ("country", "city", "age", "gender"):
        try:
            demo = api.account_insights(
                metrics="follower_demographics", breakdown=breakdown)
        except Exception:
            continue
        for row in demo.get("data") or []:
            results = ((row.get("total_value") or {}).get("breakdowns") or [])
            for group in results:
                keys = group.get("dimension_keys") or [breakdown]
                for item in group.get("results") or []:
                    dimensions = item.get("dimension_values") or []
                    value = item.get("value")
                    for key, dimension in zip(keys, dimensions):
                        demographics += int(_upsert("""INSERT OR IGNORE INTO
                          threads_follower_demographics
                          (threads_user_id,breakdown,dimension_value,measured_at,
                           value,created_at,updated_at,source,api_version,
                           raw_response_hash)
                          VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                            cfg["user_id"], key, str(dimension), now, value,
                            now, now, "meta_official_api", cfg["api_version"],
                            _hash_payload(item),
                        ), path))
    return {
        "status": "completed", "account_metrics_saved": saved,
        "demographics_saved": demographics,
    }


def search(query: str, *, search_type: str = "", search_mode: str = "KEYWORD",
           since: str = "", until: str = "", hours: int = 0,
           dry_run: bool = False,
           client: ThreadsClient | None = None,
           path: Path | None = None) -> dict:
    cfg = settings()
    query = str(query or "").strip()
    mode = search_mode.upper()
    search_type = (search_type or cfg["search_type"]).upper()
    if not query:
        return {"status": "rejected", "reason": "empty_query"}
    if hours and not since:
        since = (_now() - timedelta(
            hours=max(1, min(720, hours)))).isoformat()
    if search_type not in {"TOP", "RECENT"} or mode not in {"KEYWORD", "TAG"}:
        return {"status": "rejected", "reason": "invalid_search_mode"}
    flag = cfg["keyword_search"] if mode == "KEYWORD" else cfg["topic_tag_search"]
    if not flag:
        return {"status": "skipped", "reason": "search_disabled", "mode": mode}
    if not cfg["access_token"]:
        return {"status": "missing_credentials", "reason": "token_missing"}
    if "threads_keyword_search" not in _granted_scopes():
        return {"status": "missing_scope", "reason": "permission_missing",
                "permission": "threads_keyword_search"}
    if dry_run:
        return {
            "status": "dry_run", "endpoint": "GET /keyword_search",
            "query": query, "search_type": search_type, "search_mode": mode,
            "api_calls": 0, "writes": 0,
        }
    apply_threads_full_migrations(path)
    query_hash = hashlib.sha256(
        f"{query}|{search_type}|{mode}|{since}|{until}".encode(
            "utf-8")).hexdigest()
    now = _now()
    with closing(connect(path)) as conn:
        cached = conn.execute("""SELECT id,result_count,cache_expires_at,status
          FROM threads_search_queries
          WHERE query_hash=? AND status IN ('success','empty')
          ORDER BY fetched_at DESC LIMIT 1""", (query_hash,)).fetchone()
    if cached and (_parse_datetime(cached["cache_expires_at"]) or now) > now:
        return {
            "status": "cached", "query_id": cached["id"],
            "result_count": cached["result_count"], "api_calls": 0,
            "cached_result_status": cached["status"],
            "live_or_cached": "cached",
        }
    now_text = now.isoformat()
    active_client = client or ThreadsClient(path=path)
    try:
        response = active_client.keyword_search(
            query, search_type=search_type, search_mode=mode,
            limit=cfg["search_limit"], since=since, until=until,
            return_metadata=True)
        if isinstance(response, dict):
            rows = response.get("data") or []
            page_count = int(response.get("page_count") or 1)
            pagination_truncated = bool(
                response.get("pagination_truncated"))
        else:
            rows = response or []
            page_count = 1
            pagination_truncated = False
        if not isinstance(rows, list):
            raise RuntimeError("threads_api_invalid_data_schema")
    except Exception as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        error_class = (
            "missing_scope" if status_code == 403
            else "authentication_error" if status_code == 401
            else "rate_limited" if status_code == 429
            else type(exc).__name__
        )
        query_id = write("""INSERT INTO threads_search_queries
          (query_hash,query_text,search_type,search_mode,since_at,until_at,
           result_count,status,fetched_at,cache_expires_at,created_at,updated_at,
           source,api_version,raw_response_hash,page_count,error_class,
           live_or_cached)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            query_hash, query, search_type, mode, since, until, None,
            "failed", now_text, None, now_text, now_text,
            "meta_official_api", cfg["api_version"], "", 0, error_class,
            "live",
        ), path)
        return {
            "status": error_class if error_class in {
                "missing_scope", "rate_limited"} else "failing",
            "query_id": query_id, "result_count": None,
            "api_calls": 1, "error_class": error_class,
            "live_or_cached": "live",
        }
    result_status = "success" if rows else "empty"
    query_id = write("""INSERT INTO threads_search_queries
      (query_hash,query_text,search_type,search_mode,since_at,until_at,
       result_count,status,fetched_at,cache_expires_at,created_at,updated_at,
       source,api_version,raw_response_hash,page_count,error_class,
       live_or_cached)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        query_hash, query, search_type, mode, since, until, len(rows),
        result_status, now_text,
        (now + timedelta(minutes=cfg["search_cache_minutes"])).isoformat(),
        now_text, now_text, "meta_official_api", cfg["api_version"],
        _hash_payload(rows), page_count, None, "live",
    ), path)
    saved = 0
    for rank, row in enumerate(rows, 1):
        post_id = str(row.get("id") or "")
        if not post_id:
            continue
        _upsert("""INSERT INTO threads_search_results
          (threads_post_id,username_hash,text,timestamp,permalink,media_type,
           topic_tag,is_verified,has_replies,is_quote_post,has_link,media_url,
           first_seen_at,last_seen_at,created_at,updated_at,source,api_version,
           raw_response_hash)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(threads_post_id) DO UPDATE SET
           text=excluded.text,last_seen_at=excluded.last_seen_at,
           updated_at=excluded.updated_at,
           raw_response_hash=excluded.raw_response_hash""", (
            post_id, _username_hash(row.get("username", "")), row.get("text"),
            row.get("timestamp"), row.get("permalink"), row.get("media_type"),
            row.get("topic_tag"), int(bool(row.get("is_verified"))),
            int(bool(row.get("has_replies"))),
            int(bool(row.get("is_quote_post"))),
            int(bool(row.get("link_attachment_url"))),
            row.get("media_url"), now_text, now_text, now_text, now_text,
            "meta_official_api",
            cfg["api_version"], _hash_payload(row),
        ), path)
        with closing(connect(path)) as conn:
            result_id = conn.execute(
                "SELECT id FROM threads_search_results WHERE threads_post_id=?",
                (post_id,)).fetchone()[0]
        saved += int(_upsert("""INSERT OR IGNORE INTO
          threads_search_result_matches
          (query_id,result_id,rank,created_at,updated_at,source,api_version,
           raw_response_hash) VALUES (?,?,?,?,?,?,?,?)""", (
            query_id, result_id, rank, now_text, now_text,
            "meta_official_api", cfg["api_version"], "",
        ), path))
    return {
        "status": result_status, "query_id": query_id,
        "result_count": len(rows), "matches_saved": saved,
        "api_calls": page_count, "page_count": page_count,
        "pagination_truncated": pagination_truncated,
        "live_or_cached": "live",
        "coverage_note": "API search results only; not all Threads posts.",
    }


def _tokens(text: str) -> list[str]:
    return [
        value for value in re.findall(
            r"[一-龥ぁ-んァ-ヶA-Za-z0-9]{2,}", str(text or "").lower())
        if value not in {"これ", "それ", "ため", "こと", "について"}
    ]


def trends(*, hours: int = 24, dry_run: bool = False,
           path: Path | None = None) -> dict:
    apply_threads_full_migrations(path)
    hours = max(1, min(720, hours))
    cutoff = (_now() - timedelta(hours=hours)).isoformat()
    with closing(connect(path)) as conn:
        previous_snapshot = conn.execute(
            """SELECT snapshot_at FROM threads_trend_snapshots
               ORDER BY snapshot_at DESC LIMIT 1""").fetchone()
        previous_snapshot_at = (
            str(previous_snapshot["snapshot_at"])
            if previous_snapshot else ""
        )
        query_sql = """SELECT id,query_text,result_count,status,fetched_at
          FROM threads_search_queries
          WHERE fetched_at>=? AND status IN ('success','empty')"""
        query_params: list[Any] = [cutoff]
        if previous_snapshot_at:
            query_sql += " AND fetched_at>?"
            query_params.append(previous_snapshot_at)
        query_sql += " ORDER BY fetched_at DESC"
        new_queries = [
            dict(row) for row in conn.execute(query_sql, query_params)
        ]
        rows = [dict(row) for row in conn.execute(
            """SELECT threads_post_id,text,username_hash,timestamp,permalink,
                      topic_tag,is_verified,has_replies,is_quote_post,has_link,
                      media_type
               FROM threads_search_results WHERE last_seen_at>=?""", (cutoff,))]
        news_rows = [dict(row) for row in conn.execute(
            """SELECT title,summary,source_name,source_type,verified
               FROM news_candidates
               WHERE fetched_at>=?""", (cutoff,))]
        representative_rows: list[dict] = []
        unique_new_posts = 0
        if new_queries:
            placeholders = ",".join("?" for _ in new_queries)
            query_ids = [int(row["id"]) for row in new_queries]
            representative_rows = [
                dict(row) for row in conn.execute(
                    f"""SELECT DISTINCT r.threads_post_id,r.text,r.permalink,
                               r.is_verified,m.rank
                        FROM threads_search_result_matches m
                        JOIN threads_search_results r ON r.id=m.result_id
                        WHERE m.query_id IN ({placeholders})
                        ORDER BY r.is_verified DESC,m.rank ASC LIMIT 5""",
                    query_ids,
                )
            ]
            unique_new_posts = int(conn.execute(
                f"""SELECT COUNT(DISTINCT result_id)
                    FROM threads_search_result_matches
                    WHERE query_id IN ({placeholders})""",
                query_ids,
            ).fetchone()[0] or 0)
    research_base = {
        "lookback_hours": hours,
        "search_run_count": len(new_queries),
        "result_count": sum(
            int(row.get("result_count") or 0) for row in new_queries),
        "unique_post_count": unique_new_posts,
        "searches": [
            {
                "query": row.get("query_text"),
                "result_count": row.get("result_count"),
                "status": row.get("status"),
            }
            for row in new_queries
        ],
        "representative_posts": [
            {
                "text": row.get("text"),
                "permalink": row.get("permalink"),
                "is_verified": bool(row.get("is_verified")),
            }
            for row in representative_rows
        ],
    }
    if not rows:
        result = {
            "status": "insufficient_data", "sample_size": 0,
            "scope": "locally collected API search sample",
            "dry_run": dry_run,
            "research": research_base,
        }
        if not dry_run:
            now = _now_text()
            write("""INSERT INTO threads_trend_snapshots
              (snapshot_at,lookback_hours,status,summary_json,created_at,
               updated_at,source,api_version,raw_response_hash)
              VALUES (?,?,?,?,?,?,?,?,?)""", (
                now, hours, result["status"], _json(result), now, now,
                "local_analysis", base_settings()["api_version"],
                _hash_payload(result),
            ), path)
        result["discord_sent"] = bool(
            new_queries and _discord_research({
                **research_base,
                "top_entities": [],
                "eligible_entity_count": 0,
            }, dry_run=dry_run)
        )
        return result
    entities: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        keys = set(_tokens(row.get("text", ""))[:30])
        if row.get("topic_tag"):
            keys.add("#" + str(row["topic_tag"]).lower())
        for key in keys:
            entities[key].append(row)
    try:
        weights = json.loads((
            Path(__file__).resolve().parent.parent
            / "config" / "threads_trend_weights.json"
        ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        weights = {
            "volume_growth": .25, "unique_author_growth": .20,
            "velocity": .15, "cross_source_match": .15,
            "reply_presence": .10, "quote_presence": .05,
            "verified_diversity": .05, "novelty": .05,
        }
    ranked = []
    midpoint = _now() - timedelta(hours=max(1, hours) / 2)
    for key, matches in entities.items():
        if len(matches) < 2:
            continue
        unique = len({m["username_hash"] for m in matches})
        texts = [re.sub(r"\s+", "", m.get("text") or "") for m in matches]
        comparisons = [
            SequenceMatcher(None, a, b).ratio()
            for i, a in enumerate(texts) for b in texts[i + 1:i + 6]
        ]
        similarity = (
            sum(score >= .85 for score in comparisons) / len(comparisons)
            if comparisons else 0.0)
        velocity = len(matches) / max(1, hours)
        current = [
            row for row in matches
            if (_parse_datetime(row.get("timestamp")) or _now()) >= midpoint]
        previous = [row for row in matches if row not in current]
        volume_growth = (
            (len(current) - len(previous)) / max(1, len(previous)))
        current_authors = len({row["username_hash"] for row in current})
        previous_authors = len({row["username_hash"] for row in previous})
        author_growth = (
            (current_authors - previous_authors) / max(1, previous_authors))
        verified_ratio = sum(
            bool(row.get("is_verified")) for row in matches) / len(matches)
        reply_ratio = sum(
            bool(row.get("has_replies")) for row in matches) / len(matches)
        quote_ratio = sum(
            bool(row.get("is_quote_post")) for row in matches) / len(matches)
        link_ratio = sum(
            bool(row.get("has_link")) for row in matches) / len(matches)
        image_ratio = sum(
            str(row.get("media_type") or "").upper() == "IMAGE"
            for row in matches) / len(matches)
        video_ratio = sum(
            str(row.get("media_type") or "").upper() == "VIDEO"
            for row in matches) / len(matches)
        source_matches = {
            str(row.get("source_name") or "")
            for row in news_rows
            if key in str(row.get("title") or "").lower()
            or key in str(row.get("summary") or "").lower()
        }
        cross_source = len(source_matches)
        official_matches = {
            str(row.get("source_name") or "")
            for row in news_rows
            if str(row.get("source_type") or "") in {
                "official", "government", "ministry", "parliament", "party"}
            and (
                key in str(row.get("title") or "").lower()
                or key in str(row.get("summary") or "").lower())
        }
        verified_news = {
            str(row.get("source_name") or "")
            for row in news_rows
            if bool(row.get("verified")) and (
                key in str(row.get("title") or "").lower()
                or key in str(row.get("summary") or "").lower())
        }
        single_author_penalty = 0.5 if unique == 1 else 1.0
        features = {
            "volume_growth": max(0.0, min(1.0, (volume_growth + 1) / 2)),
            "unique_author_growth": max(
                0.0, min(1.0, (author_growth + 1) / 2)),
            "velocity": min(1.0, velocity),
            "cross_source_match": min(1.0, cross_source / 2),
            "reply_presence": reply_ratio,
            "quote_presence": quote_ratio,
            "verified_diversity": verified_ratio if unique >= 2 else 0.0,
            "novelty": 1 - similarity,
        }
        score = round(sum(
            float(weights.get(name, 0)) * value
            for name, value in features.items()
        ) * 100 * single_author_penalty, 4)
        concentration = 1 - (unique / len(matches))
        signal_score = similarity * .5 + concentration * .3 + (
            .2 if velocity >= 1 else 0)
        signal = (
            "high" if signal_score >= .75 and unique >= 3
            else "medium" if signal_score >= .5 else "low")
        state = (
            "spike" if volume_growth >= 2 and velocity >= 1
            else "emerging" if not previous and len(current) >= 2
            else "rising" if volume_growth > .25
            else "cooling" if volume_growth < -.25 else "stable")
        ranked.append({
            "entity": key, "post_count": len(matches),
            "unique_authors": unique, "velocity": round(velocity, 4),
            "similarity_rate": round(similarity, 4),
            "duplicate_text_rate": round(similarity, 4),
            "volume_growth": round(volume_growth, 4),
            "unique_author_growth": round(author_growth, 4),
            "verified_author_ratio": round(verified_ratio, 4),
            "reply_presence_ratio": round(reply_ratio, 4),
            "quote_presence_ratio": round(quote_ratio, 4),
            "link_presence_ratio": round(link_ratio, 4),
            "image_ratio": round(image_ratio, 4),
            "video_ratio": round(video_ratio, 4),
            "cross_source_count": cross_source,
            "trend_detected": True,
            "fact_status": (
                "cross_checked" if official_matches and verified_news
                else "unverified_threads_signal"),
            "official_source_count": len(official_matches),
            "news_source_count": len(verified_news),
            "contradiction_found": False,
            "eligible_for_post": bool(official_matches and verified_news),
            "verification_reason": (
                "matched locally stored official and verified news sources"
                if official_matches and verified_news
                else "Threads signal is not treated as a primary source"),
            "trend_score": score,
            "state": state,
            "coordinated_activity_signal": signal,
        })
    ranked.sort(key=lambda row: row["trend_score"], reverse=True)
    result = {
        "status": "relative_trends", "sample_size": len(rows),
        "scope": "locally collected official API search sample",
        "entities": ranked[:30], "not_official_global_ranking": True,
        "frequent_terms": [
            {"term": term, "count": count}
            for term, count in Counter(
                token for row in rows for token in _tokens(row["text"])
            ).most_common(20)
        ],
        "frequent_people": [],
        "frequent_organizations": [],
        "frequent_url_domains": [
            {"domain": domain, "count": count}
            for domain, count in Counter(
                urlsplit(url).hostname or ""
                for row in rows
                for url in re.findall(r"https?://[^\s]+", row["text"] or "")
            ).most_common(10) if domain
        ],
        "top_recent_difference": "requires paired TOP and RECENT query samples",
        "dry_run": dry_run,
        "research": research_base,
    }
    if not dry_run:
        now = _now_text()
        snapshot_id = write("""INSERT INTO threads_trend_snapshots
          (snapshot_at,lookback_hours,status,summary_json,created_at,updated_at,
           source,api_version,raw_response_hash) VALUES (?,?,?,?,?,?,?,?,?)""", (
            now, hours, result["status"], _json(result), now, now,
            "local_analysis", base_settings()["api_version"],
            _hash_payload(result),
        ), path)
        for entity in ranked[:30]:
            _upsert("""INSERT OR IGNORE INTO threads_trend_entities
              (snapshot_id,entity_key,post_count,unique_authors,velocity,
               similarity_rate,cross_source_count,trend_score,
               coordinated_activity_signal,trend_detected,fact_status,
               official_source_count,news_source_count,contradiction_found,
               eligible_for_post,verification_reason,created_at,updated_at,
               source,api_version,raw_response_hash)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                snapshot_id, entity["entity"], entity["post_count"],
                entity["unique_authors"], entity["velocity"],
                entity["similarity_rate"], entity["cross_source_count"],
                entity["trend_score"],
                entity["coordinated_activity_signal"],
                int(entity["trend_detected"]), entity["fact_status"],
                entity["official_source_count"], entity["news_source_count"],
                int(entity["contradiction_found"]),
                int(entity["eligible_for_post"]),
                entity["verification_reason"], now, now,
                "local_analysis", base_settings()["api_version"], "",
            ), path)
    research_report = {
        **research_base,
        "top_entities": ranked[:5],
        "eligible_entity_count": sum(
            bool(row.get("eligible_for_post")) for row in ranked),
    }
    result["discord_sent"] = bool(
        new_queries and _discord_research(
            research_report, dry_run=dry_run)
    )
    return result


def analyze_reply(reply_id: str, text: str,
                  path: Path | None = None) -> dict:
    normalized = str(text or "").lower()
    spam_terms = ("副業", "稼げる", "dmください", "フォロバ", "プレゼント")
    toxic_terms = ("死ね", "消えろ", "馬鹿", "売国奴", "非国民")
    oppose_terms = ("反対", "違う", "誤り", "納得できない", "批判")
    support_terms = ("賛成", "同意", "その通り", "支持")
    question = "?" in normalized or "？" in normalized or "なぜ" in normalized
    spam = min(1.0, sum(term in normalized for term in spam_terms) / 2)
    toxicity = min(1.0, sum(term in normalized for term in toxic_terms) / 2)
    if question:
        intent = "question"
    elif spam >= .5:
        intent = "spam_or_promotion"
    elif any(term in normalized for term in oppose_terms):
        intent = "criticism"
    elif any(term in normalized for term in support_terms):
        intent = "support"
    else:
        intent = "comment"
    stance = (
        "opposed" if any(term in normalized for term in oppose_terms)
        else "supportive" if any(term in normalized for term in support_terms)
        else "unclear")
    sentiment = (
        "negative" if stance == "opposed" or toxicity > 0
        else "positive" if stance == "supportive" else "neutral")
    confidence = .9 if intent != "comment" else .55
    result = {
        "reply_id": reply_id, "intent": intent, "stance": stance,
        "sentiment": sentiment, "toxicity_score": toxicity,
        "misunderstanding_score": .0, "spam_score": spam,
        "confidence": confidence, "review_required": confidence < .7,
        "analysis_method": "local_rules_v1",
    }
    now = _now_text()
    _upsert("""INSERT INTO threads_reply_analyses
      (reply_id,intent,stance,sentiment,toxicity_score,
       misunderstanding_score,spam_score,confidence,review_required,
       analysis_method,created_at,updated_at,source,api_version,
       raw_response_hash)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(reply_id) DO UPDATE SET intent=excluded.intent,
       stance=excluded.stance,sentiment=excluded.sentiment,
       toxicity_score=excluded.toxicity_score,
       misunderstanding_score=excluded.misunderstanding_score,
       spam_score=excluded.spam_score,confidence=excluded.confidence,
       review_required=excluded.review_required,
       analysis_method=excluded.analysis_method,
       updated_at=excluded.updated_at""", (
        reply_id, intent, stance, sentiment, toxicity, 0.0, spam,
        confidence, int(confidence < .7), "local_rules_v1", now, now,
        "local_analysis", base_settings()["api_version"],
        _hash_payload(result),
    ), path)
    return result


def analyze_pending_replies(path: Path | None = None) -> dict:
    apply_threads_full_migrations(path)
    with closing(connect(path)) as conn:
        rows = [dict(row) for row in conn.execute("""
          SELECT r.reply_id,r.text FROM threads_replies r
          LEFT JOIN threads_reply_analyses a ON a.reply_id=r.reply_id
          WHERE a.reply_id IS NULL""")]
    analyses = [analyze_reply(row["reply_id"], row["text"], path)
                for row in rows]
    return {
        "status": "completed", "analyzed": len(analyses),
        "review_required": sum(a["review_required"] for a in analyses),
    }


def quota_status(client: ThreadsClient | None = None, *,
                 dry_run: bool = False, path: Path | None = None) -> dict:
    cfg = settings()
    if dry_run:
        return {
            "status": "dry_run",
            "endpoint": "GET /me/threads_publishing_limit",
            "api_calls": 0, "writes": 0,
        }
    if not cfg["quota_sync"]:
        return {"status": "skipped", "reason": "quota_sync_disabled"}
    if not cfg["access_token"]:
        return {"status": "skipped", "reason": "token_missing"}
    try:
        payload = (client or ThreadsClient(path=path)).publishing_limit()
    except Exception as exc:
        return {
            "status": "unavailable",
            "reason": "official_quota_endpoint_failed",
            "error_class": type(exc).__name__,
            "external_writes_allowed": False,
            "retrieval_operations_allowed": True,
            "blind_write_retry_performed": False,
        }
    data = (payload.get("data") or [{}])[0]
    mapping = {
        "publishing": ("quota_usage", "config"),
        "reply": ("reply_quota_usage", "reply_config"),
        "delete": ("delete_quota_usage", "delete_config"),
        "location_search": (
            "location_search_quota_usage", "location_search_config"),
    }
    now = _now_text()
    quotas = []
    for name, (usage_key, config_key) in mapping.items():
        config = data.get(config_key) or {}
        usage = data.get(usage_key)
        total = config.get("quota_total")
        if usage is None and total is None:
            continue
        utilization = (
            float(usage or 0) / float(total) if total else None)
        warning = (
            "stop_external_writes" if utilization is not None
            and utilization >= .9 else
            "warning" if utilization is not None and utilization >= .8
            else "normal")
        row = {
            "quota_type": name, "usage": usage, "quota_total": total,
            "quota_duration": config.get("quota_duration"),
            "utilization": utilization, "warning_level": warning,
        }
        quotas.append(row)
        _upsert("""INSERT OR IGNORE INTO threads_api_quotas
          (measured_at,quota_type,usage,quota_total,quota_duration,utilization,
           reset_at,warning_level,created_at,updated_at,source,api_version,
           raw_response_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            now, name, usage, total, config.get("quota_duration"),
            utilization, None, warning, now, now, "meta_official_api",
            cfg["api_version"], _hash_payload(row),
        ), path)
    return {
        "status": "success", "measured_at": now, "quotas": quotas,
        "external_writes_allowed": not any(
            q["warning_level"] == "stop_external_writes" for q in quotas),
    }


def container_status(container_id: str, client: ThreadsClient | None = None,
                     path: Path | None = None) -> dict:
    payload = (client or ThreadsClient(path=path)).container_status(container_id)
    state = str(payload.get("status") or "")
    if state and state not in CONTAINER_STATES:
        state = "UNKNOWN"
    now = _now_text()
    _upsert("""INSERT INTO threads_containers
      (container_id,status,error_message,last_checked_at,created_at,updated_at,
       source,api_version,raw_response_hash)
      VALUES (?,?,?,?,?,?,?,?,?)
      ON CONFLICT(container_id) DO UPDATE SET status=excluded.status,
       error_message=excluded.error_message,
       last_checked_at=excluded.last_checked_at,
       updated_at=excluded.updated_at,
       raw_response_hash=excluded.raw_response_hash""", (
        container_id, state, payload.get("error_message"), now, now, now,
        "meta_official_api", base_settings()["api_version"],
        _hash_payload(payload),
    ), path)
    return {
        "container_id": container_id, "status": state,
        "error_message": payload.get("error_message"),
    }


def _is_public_https(value: str) -> bool:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return host not in {
        "localhost", "127.0.0.1", "::1",
    } and not host.endswith(".local")


def validate_format(spec: dict) -> dict:
    errors: list[str] = []
    media_type = str(spec.get("media_type") or "TEXT").upper()
    if media_type not in FORMAT_FLAGS:
        errors.append("unsupported_media_type")
    if media_type == "TEXT" and not (
        str(spec.get("text") or "").strip()
        or str(spec.get("link_attachment") or "").strip()
    ):
        errors.append("text_or_link_required")
    if media_type == "IMAGE" and not _is_public_https(
        str(spec.get("image_url") or "")
    ):
        errors.append("public_https_image_url_required")
    if media_type == "VIDEO" and not _is_public_https(
        str(spec.get("video_url") or "")
    ):
        errors.append("public_https_video_url_required")
    if media_type == "CAROUSEL":
        children = spec.get("children") or []
        if not isinstance(children, list) or not 2 <= len(children) <= 20:
            errors.append("carousel_requires_2_to_20_children")
    if spec.get("poll_attachment") and spec.get("link_attachment"):
        errors.append("poll_and_link_are_mutually_exclusive")
    if spec.get("poll_attachment") and spec.get("text_attachment"):
        errors.append("poll_and_text_attachment_are_mutually_exclusive")
    reply_control = str(
        spec.get("reply_control") or "everyone").lower()
    if reply_control not in REPLY_CONTROLS:
        errors.append("invalid_reply_control")
    if spec.get("location_id") and not _bool(
        "THREADS_LOCATION_TAGGING_ENABLED", "false"
    ):
        errors.append("location_tagging_disabled")
    if spec.get("is_ghost_post") and not _bool(
        "THREADS_GHOST_POST_ENABLED", "false"
    ):
        errors.append("ghost_post_disabled")
    if spec.get("poll_attachment") and not _bool(
        "THREADS_POLL_POST_ENABLED", "false"
    ):
        errors.append("poll_post_disabled")
    if not _bool(FORMAT_FLAGS.get(media_type, "THREADS_TEXT_POST_ENABLED"),
                 "true" if media_type == "TEXT" else "false"):
        errors.append(f"{media_type.lower()}_post_disabled")
    return {
        "valid": not errors, "media_type": media_type,
        "errors": sorted(set(errors)), "official_api_only": True,
        "local_files_uploaded": False,
    }


def format_preview(spec: dict, path: Path | None = None) -> dict:
    validation = validate_format(spec)
    safe_spec = {
        key: value for key, value in spec.items()
        if key not in {"access_token", "app_secret", "code"}
    }
    if not validation["valid"]:
        return {"status": "rejected", "validation": validation}
    return _create_draft(
        "publish_format", "", safe_spec,
        {"validation": validation, "human_review_required": True}, path)


def _create_draft(action_type: str, target_id: str, payload: dict,
                  safety: dict, path: Path | None) -> dict:
    apply_threads_full_migrations(path)
    now = _now()
    key = hashlib.sha256(_json({
        "action": action_type, "target": target_id, "payload": payload,
        "date": now.date().isoformat(),
    }).encode("utf-8")).hexdigest()
    draft_id = write("""INSERT OR IGNORE INTO threads_action_drafts
      (action_type,target_id,payload_json,status,safety_json,expires_at,
       idempotency_key,created_at,updated_at,source,api_version,
       raw_response_hash)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
        action_type, target_id, _json(payload), "pending", _json(safety),
        (now + timedelta(hours=24)).isoformat(), key, now.isoformat(),
        now.isoformat(), "human_approval_queue",
        base_settings()["api_version"], _hash_payload(payload),
    ), path)
    if not draft_id:
        with closing(connect(path)) as conn:
            draft_id = conn.execute(
                "SELECT id FROM threads_action_drafts WHERE idempotency_key=?",
                (key,)).fetchone()[0]
    return {
        "status": "draft", "draft_id": draft_id,
        "action_type": action_type, "target_id": target_id,
        "requires_confirm": True, "external_writes": 0,
    }


def reply_draft(reply_to_id: str, text: str = "",
                path: Path | None = None) -> dict:
    text = text.strip() or (
        "ご意見ありがとうございます。確認できる一次情報と制度上の論点を"
        "分けて、もう少し丁寧に見ていきます。")
    safety = {
        "personal_attack": any(
            term in text for term in ("死ね", "売国奴", "非国民")),
        "fact_check_required": True, "human_review_required": True,
    }
    if safety["personal_attack"]:
        return {"status": "rejected", "reason": "personal_attack"}
    return _create_draft(
        "reply", reply_to_id, {"text": text}, safety, path)


def quote_draft(post_id: str, text: str = "",
                path: Path | None = None) -> dict:
    text = text.strip() or (
        "この投稿が示す論点は、結論だけでなく一次資料と実施条件を"
        "確認して考える必要があります。")
    return _create_draft(
        "quote", post_id, {"text": text},
        {"source_recheck_required": True, "human_review_required": True}, path)


def moderation_draft(reply_id: str, action: str,
                     path: Path | None = None) -> dict:
    action = action.lower()
    if action not in {"hide", "unhide"}:
        return {"status": "rejected", "reason": "invalid_moderation_action"}
    apply_threads_full_migrations(path)
    with closing(connect(path)) as conn:
        row = conn.execute(
            "SELECT text FROM threads_replies WHERE reply_id=?",
            (reply_id,)).fetchone()
    text = str(row["text"] if row else "")
    analysis = analyze_reply(reply_id, text, path)
    allowed = (
        action == "unhide" or analysis["spam_score"] >= .5
        or analysis["toxicity_score"] >= .5)
    if action == "hide" and not allowed:
        return {
            "status": "rejected",
            "reason": "ordinary_criticism_is_not_moderation_candidate",
        }
    return _create_draft(
        action, reply_id, {"hide": action == "hide"},
        {"analysis": analysis, "human_review_required": True}, path)


def _load_action_draft(draft_id: int, path: Path | None) -> dict:
    apply_threads_full_migrations(path)
    with closing(connect(path)) as conn:
        row = conn.execute(
            "SELECT * FROM threads_action_drafts WHERE id=?",
            (draft_id,)).fetchone()
    if not row:
        raise ValueError("threads action draft not found")
    result = dict(row)
    result["payload"] = json.loads(result.get("payload_json") or "{}")
    return result


def _quota_allows_write(path: Path | None) -> bool:
    apply_threads_full_migrations(path)
    with closing(connect(path)) as conn:
        row = conn.execute("""SELECT warning_level FROM threads_api_quotas
          ORDER BY measured_at DESC LIMIT 1""").fetchone()
    return not row or row["warning_level"] != "stop_external_writes"


def apply_action(draft_id: int, *, confirm: bool = False,
                 client: ThreadsClient | None = None,
                 path: Path | None = None) -> dict:
    draft = _load_action_draft(draft_id, path)
    action = str(draft["action_type"])
    autonomous = _bool("AUTONOMOUS_POSTING_ENABLED", "true")
    approval_required = (
        _bool(ACTION_APPROVAL_ENV.get(action, ""), "false")
        and not autonomous
    )
    if approval_required and not confirm:
        return {"status": "blocked", "reason": "confirm_required",
                "external_writes": 0}
    if draft["status"] != "pending":
        return {"status": "blocked", "reason": "draft_not_pending",
                "external_writes": 0}
    if not _bool("THREADS_POST_ENABLED", "false"):
        return {"status": "blocked", "reason": "threads_posting_disabled",
                "external_writes": 0}
    if not _bool(ACTION_ENV.get(action, ""), "false"):
        return {"status": "blocked", "reason": "action_disabled",
                "external_writes": 0}
    if not _quota_allows_write(path):
        return {"status": "blocked", "reason": "quota_safety_stop",
                "external_writes": 0}
    expires = _parse_datetime(draft.get("expires_at"))
    if expires and expires < _now():
        return {"status": "blocked", "reason": "draft_expired",
                "external_writes": 0}
    api = client or ThreadsClient(path=path)
    payload = draft["payload"]
    result: dict
    calls = 1
    try:
        if action in {"reply", "quote", "publish_format"}:
            options = dict(payload)
            text = str(options.pop("text", ""))
            if action == "reply":
                options["reply_to_id"] = draft["target_id"]
            elif action == "quote":
                options["quote_post_id"] = draft["target_id"]
            validation = (
                validate_format({"media_type": "TEXT", "text": text, **options})
                if action != "publish_format" else validate_format(payload))
            if not validation["valid"]:
                return {"status": "blocked", "reason": "validation_failed",
                        "validation": validation, "external_writes": 0}
            for key in (
                "poll_attachment", "text_attachment", "gif_attachment",
                "text_entities",
            ):
                if key in options and not isinstance(options[key], str):
                    options[key] = _json(options[key])
            if isinstance(options.get("children"), list):
                options["children"] = ",".join(
                    str(value) for value in options["children"])
            for key in ("is_ghost_post", "is_spoiler_media",
                        "enable_reply_approvals", "is_carousel_item"):
                if key in options and isinstance(options[key], bool):
                    options[key] = str(options[key]).lower()
            container = api.create_container(text, **options)
            calls = 1
            creation_id = str(container.get("id") or "")
            if not creation_id:
                raise RuntimeError("threads_creation_id_missing")
            published = api.publish_container(creation_id)
            calls = 2
            result = {
                "id": published.get("id"), "creation_id": creation_id,
            }
        elif action == "repost":
            result = api.repost(str(draft["target_id"]))
        elif action == "delete":
            result = api.delete(str(draft["target_id"]))
        elif action in {"hide", "unhide"}:
            result = api.manage_reply(
                str(draft["target_id"]), action == "hide")
        else:
            return {"status": "blocked", "reason": "unsupported_action",
                    "external_writes": 0}
        now = _now_text()
        write("""UPDATE threads_action_drafts SET status='applied',
          approved_at=?,updated_at=? WHERE id=?""", (now, now, draft_id), path)
        write("""INSERT INTO threads_action_events
          (draft_id,action_type,target_id,status,result_id,ambiguous,event_at,
           created_at,updated_at,source,api_version,raw_response_hash)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            draft_id, action, draft["target_id"], "success",
            str(result.get("id") or result.get("deleted_id") or ""),
            0, now, now, now,
            "autonomous_official_api" if autonomous
            else "human_confirmed_official_api",
            base_settings()["api_version"], _hash_payload(result),
        ), path)
        return {
            "status": "success", "action_type": action,
            "result_id": result.get("id") or result.get("deleted_id"),
            "external_writes": calls,
        }
    except Exception as exc:
        ambiguous = type(exc).__name__ in {"Timeout", "ReadTimeout"}
        now = _now_text()
        write("""INSERT INTO threads_action_events
          (draft_id,action_type,target_id,status,reason,ambiguous,event_at,
           created_at,updated_at,source,api_version,raw_response_hash)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            draft_id, action, draft["target_id"],
            "ambiguous" if ambiguous else "failed", type(exc).__name__,
            int(ambiguous), now, now, now,
            "autonomous_official_api" if autonomous
            else "human_confirmed_official_api",
            base_settings()["api_version"], "",
        ), path)
        return {
            "status": "ambiguous" if ambiguous else "failed",
            "error_class": type(exc).__name__,
            "blind_retry_performed": False, "external_writes": calls,
        }


def direct_action_draft(action: str, target_id: str, *,
                        reason: str = "", path: Path | None = None) -> dict:
    if action not in {"repost", "delete"}:
        return {"status": "rejected", "reason": "unsupported_action"}
    if action == "delete":
        apply_threads_full_migrations(path)
        with closing(connect(path)) as conn:
            row = conn.execute(
                """SELECT text FROM threads_posts
                   WHERE threads_post_id=? AND status='published'""",
                (target_id,)).fetchone()
        if not row:
            return {"status": "rejected", "reason": "not_owned_local_post"}
        payload = {"reason": reason, "pre_delete_text": row["text"]}
    else:
        payload = {}
    return _create_draft(
        action, target_id, payload,
        {"human_review_required": True}, path)


def location_get(location_id: str, client: ThreadsClient | None = None,
                 path: Path | None = None) -> dict:
    if not _bool("THREADS_LOCATION_LOOKUP_ENABLED", "false"):
        return {"status": "skipped", "reason": "location_lookup_disabled"}
    if "threads_location_tagging" not in _granted_scopes():
        return {"status": "skipped", "reason": "permission_missing",
                "permission": "threads_location_tagging"}
    row = (client or ThreadsClient(path=path)).location(location_id)
    now = _now_text()
    _upsert("""INSERT INTO threads_locations
      (location_id,name,address,city,country,latitude,longitude,postal_code,
       fetched_at,created_at,updated_at,source,api_version,raw_response_hash)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(location_id) DO UPDATE SET name=excluded.name,
       address=excluded.address,city=excluded.city,country=excluded.country,
       latitude=excluded.latitude,longitude=excluded.longitude,
       postal_code=excluded.postal_code,fetched_at=excluded.fetched_at,
       updated_at=excluded.updated_at,
       raw_response_hash=excluded.raw_response_hash""", (
        str(row.get("id") or location_id), row.get("name"),
        row.get("address"), row.get("city"), row.get("country"),
        row.get("latitude"), row.get("longitude"), row.get("postal_code"),
        now, now, now, "meta_official_api", base_settings()["api_version"],
        _hash_payload(row),
    ), path)
    return {"status": "success", **{
        key: row.get(key) for key in (
            "id", "name", "address", "city", "country", "latitude",
            "longitude", "postal_code")
    }}


def profile_discovery(username: str, *, dry_run: bool = False,
                      client: ThreadsClient | None = None) -> dict:
    if not _bool("THREADS_PROFILE_DISCOVERY_ENABLED", "false"):
        return {"status": "skipped", "reason": "profile_discovery_disabled"}
    if "threads_profile_discovery" not in _granted_scopes():
        return {"status": "skipped", "reason": "permission_missing",
                "permission": "threads_profile_discovery"}
    if dry_run:
        return {
            "status": "dry_run",
            "endpoints": ["GET /profile_lookup", "GET /profile_posts"],
            "username": username, "api_calls": 0, "writes": 0,
        }
    api = client or ThreadsClient()
    profile = api.public_profile(username)
    posts = api.public_profile_posts(username)
    return {
        "status": "success",
        "profile": {
            key: profile.get(key) for key in (
                "id", "username", "name", "is_verified",
                "threads_profile_picture_url", "threads_biography")
        },
        "posts": posts,
        "result_count": len(posts),
        "coverage_note": "Official API response for one exact public username.",
        "stored": False,
    }


def _report_period(days: int, path: Path | None) -> dict:
    apply_threads_full_migrations(path)
    cutoff = (_now() - timedelta(days=days)).isoformat()
    with closing(connect(path)) as conn:
        posts = conn.execute(
            "SELECT COUNT(*) FROM threads_posts WHERE published_at>=?",
            (cutoff,)).fetchone()[0]
        replies = conn.execute(
            "SELECT COUNT(*) FROM threads_replies WHERE timestamp>=?",
            (cutoff,)).fetchone()[0]
        mentions = conn.execute(
            "SELECT COUNT(*) FROM threads_mentions WHERE timestamp>=?",
            (cutoff,)).fetchone()[0]
        api = conn.execute("""SELECT COUNT(*) calls,
          SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) failures,
          SUM(CASE WHEN rate_limited=1 THEN 1 ELSE 0 END) limited
          FROM threads_api_calls WHERE called_at>=?""", (cutoff,)).fetchone()
        metrics = conn.execute("""SELECT AVG(engagement_rate) engagement,
          SUM(COALESCE(views,0)) views FROM threads_post_insights
          WHERE measured_at>=?""", (cutoff,)).fetchone()
    return {
        "period_days": days, "posts": posts, "replies": replies,
        "mentions": mentions, "api_calls": api["calls"] or 0,
        "api_failures": api["failures"] or 0,
        "rate_limited_calls": api["limited"] or 0,
        "views": metrics["views"] or 0,
        "average_engagement_rate": metrics["engagement"],
        "official_api_cost_usd": None,
        "cost_note": "Threads API pricing is not guessed.",
    }


def daily_report(*, dry_run: bool = False,
                 path: Path | None = None) -> dict:
    report = _report_period(1, path)
    report.update({"report_date": _now().date().isoformat(),
                   "dry_run": dry_run})
    discord_sent = False
    if not dry_run:
        now = _now_text()
        _upsert("""INSERT INTO threads_daily_reports
          (report_date,generated_at,report_json,discord_status,created_at,
           updated_at,source,api_version,raw_response_hash)
          VALUES (?,?,?,?,?,?,?,?,?)
          ON CONFLICT(report_date) DO UPDATE SET generated_at=excluded.generated_at,
           report_json=excluded.report_json,updated_at=excluded.updated_at,
           raw_response_hash=excluded.raw_response_hash""", (
            report["report_date"], now, _json(report), "not_sent",
            now, now, "local_report", base_settings()["api_version"],
            _hash_payload(report),
        ), path)
        discord_sent = _discord_result(
            "📊 Threads日次レポート",
            {
                "投稿数": report["posts"],
                "返信": report["replies"],
                "メンション": report["mentions"],
                "表示": report["views"],
                "API失敗": report["api_failures"],
            },
            "warning" if report["api_failures"] else "success",
        )
        with closing(connect(path)) as conn:
            conn.execute(
                """UPDATE threads_daily_reports SET discord_status=?
                   WHERE report_date=?""",
                ("sent" if discord_sent else "not_sent",
                 report["report_date"]))
            conn.commit()
    report["discord_sent"] = discord_sent
    return report


def weekly_report(*, dry_run: bool = False,
                  path: Path | None = None) -> dict:
    report = _report_period(7, path)
    end = _now().date()
    start = end - timedelta(days=6)
    report.update({
        "week_start": start.isoformat(), "week_end": end.isoformat(),
        "dry_run": dry_run,
    })
    discord_sent = False
    if not dry_run:
        now = _now_text()
        _upsert("""INSERT INTO threads_weekly_reports
          (week_start,week_end,generated_at,report_json,discord_status,
           created_at,updated_at,source,api_version,raw_response_hash)
          VALUES (?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(week_start,week_end) DO UPDATE SET
           generated_at=excluded.generated_at,report_json=excluded.report_json,
           updated_at=excluded.updated_at,
           raw_response_hash=excluded.raw_response_hash""", (
            start.isoformat(), end.isoformat(), now, _json(report), "not_sent",
            now, now, "local_report", base_settings()["api_version"],
            _hash_payload(report),
        ), path)
        discord_sent = _discord_result(
            "📈 Threads週次レポート",
            {
                "期間": f"{start.isoformat()}〜{end.isoformat()}",
                "投稿数": report["posts"],
                "返信": report["replies"],
                "メンション": report["mentions"],
                "表示": report["views"],
            },
            "success",
        )
        with closing(connect(path)) as conn:
            conn.execute(
                """UPDATE threads_weekly_reports SET discord_status=?
                   WHERE week_start=? AND week_end=?""",
                ("sent" if discord_sent else "not_sent",
                 start.isoformat(), end.isoformat()))
            conn.commit()
    report["discord_sent"] = discord_sent
    return report


def x_comparison(days: int = 30, path: Path | None = None) -> dict:
    from threads_api import platform_comparison
    result = platform_comparison(path)
    return {
        **result, "period_days": max(1, min(365, days)),
        "metric_definitions_not_equivalent": True,
    }


def data_retention_run(path: Path | None = None,
                       now: datetime | None = None) -> dict:
    cfg = settings()
    now = now or _now()
    apply_threads_full_migrations(path)
    cutoffs = {
        "threads_api_calls": now - timedelta(days=cfg["raw_retention_days"]),
        "threads_search_results": now - timedelta(
            days=cfg["search_retention_days"]),
        "threads_replies": now - timedelta(days=cfg["personal_retention_days"]),
        "threads_mentions": now - timedelta(days=cfg["personal_retention_days"]),
        "threads_post_insights": now - timedelta(
            days=cfg["analytics_retention_days"]),
        "threads_account_insights": now - timedelta(
            days=cfg["analytics_retention_days"]),
    }
    fields = {
        "threads_api_calls": "called_at",
        "threads_search_results": "last_seen_at",
        "threads_replies": "timestamp",
        "threads_mentions": "timestamp",
        "threads_post_insights": "measured_at",
        "threads_account_insights": "measured_at",
    }
    deleted = {}
    with closing(connect(path)) as conn:
        for table, cutoff in cutoffs.items():
            cur = conn.execute(
                f"DELETE FROM {table} WHERE {fields[table]}<?",
                (cutoff.isoformat(),))
            deleted[table] = cur.rowcount
        conn.execute("""UPDATE threads_profiles SET profile_picture_url=NULL,
          updated_at=? WHERE updated_at<?""", (
            now.isoformat(),
            (now - timedelta(days=cfg["personal_retention_days"])).isoformat()))
        conn.commit()
    return {"status": "completed", "deleted": deleted}


def user_data_delete(user_id: str, *, confirm: bool = False,
                     path: Path | None = None) -> dict:
    if not confirm:
        return {"status": "blocked", "reason": "confirm_required",
                "deleted_rows": 0}
    cfg = base_settings()
    if str(user_id) != str(cfg["user_id"]):
        return {"status": "blocked", "reason": "user_mismatch",
                "deleted_rows": 0}
    from threads_api import clear_user_credentials, create_deletion_receipt
    apply_threads_full_migrations(path)
    deleted = 0
    with closing(connect(path)) as conn:
        for table, field in (
            ("threads_profiles", "threads_user_id"),
            ("threads_account_insights", "threads_user_id"),
            ("threads_follower_demographics", "threads_user_id"),
        ):
            deleted += conn.execute(
                f"DELETE FROM {table} WHERE {field}=?", (user_id,)).rowcount
        conn.execute("""UPDATE threads_posts SET threads_user_id=NULL,
          updated_at=? WHERE threads_user_id=?""", (_now_text(), user_id))
        conn.commit()
    clear = clear_user_credentials(
        user_id, detach_history=True, path=path)
    code = create_deletion_receipt(user_id, path)
    return {
        "status": "completed", "deleted_rows": deleted,
        "credentials_cleared": clear["credentials_cleared"],
        "confirmation_code": code, "threads_posting_disabled": True,
    }


def full_sync(*, dry_run: bool = False,
              path: Path | None = None) -> dict:
    """Run only retrieval and local analysis.  Never perform external writes."""
    def safe(name: str, callback):
        try:
            return callback()
        except Exception as exc:
            return {
                "status": "failed", "operation": name,
                "error_class": type(exc).__name__,
                "x_bot_affected": False, "external_writes": 0,
            }

    operations = {
        "profile": safe("profile", lambda: profile_sync(
            dry_run=dry_run, path=path)),
        "posts": safe("posts", lambda: sync_posts(
            dry_run=dry_run, path=path)),
        "replies": safe("replies", lambda: sync_replies(
            dry_run=dry_run, path=path)),
        "mentions": safe("mentions", lambda: sync_mentions(
            dry_run=dry_run, path=path)),
        "post_insights": safe("post_insights", lambda:
            collect_post_insights(dry_run=dry_run, path=path)),
        "account_insights": safe("account_insights", lambda:
            collect_account_insights(dry_run=dry_run, path=path)),
        "trends": safe("trends", lambda: trends(
            dry_run=dry_run, path=path)),
    }
    if not dry_run:
        operations["reply_analysis"] = safe(
            "reply_analysis", lambda: analyze_pending_replies(path))
    return {
        "status": "completed", "operations": operations,
        "external_write_actions": 0, "x_writes": 0,
        "note_publish_actions": 0, "windows_task_changes": 0,
    }
