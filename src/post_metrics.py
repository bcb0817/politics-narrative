"""Collect each published post once at 1h, 24h and 72h."""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from api_budget import estimate_x, finalize, reserve
from metrics_db import connect, db_path, init_db, upsert_metric

JST = ZoneInfo("Asia/Tokyo")
WINDOWS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "72h": timedelta(hours=72)}


def due_measurements(history: list[dict], now: datetime, path: Path | None = None) -> list[tuple[dict, str]]:
    init_db(path)
    existing = set()
    try:
        with closing(connect(path)) as conn:
            existing = {(str(row[0]), row[1]) for row in conn.execute("SELECT tweet_id,measurement_window FROM post_metrics")}
    except Exception:
        pass
    due = []
    for post in history:
        tweet_id = str(post.get("tweet_id", ""))
        try:
            posted = datetime.fromisoformat(post.get("posted_at_jst", ""))
            if posted.tzinfo is None: posted = posted.replace(tzinfo=JST)
        except Exception:
            continue
        age = now - posted.astimezone(JST)
        for window, delta in WINDOWS.items():
            if age >= delta and (tweet_id, window) not in existing:
                due.append((post, window))
    return due


def collect(history: list[dict], now: datetime | None = None, client_factory=None,
            path: Path | None = None) -> dict:
    now = now or datetime.now(JST)
    if os.environ.get("POST_METRICS_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return {"collected": 0, "skipped": "disabled"}
    daily_cap = int(os.environ.get("X_OWNED_READ_MAX_PER_DAY", "24"))
    used_today = 0
    try:
        with closing(connect(path)) as conn:
            used_today = int(conn.execute("""SELECT COALESCE(SUM(resource_count),0) FROM api_usage_events
              WHERE provider='x' AND operation='owned_read' AND timestamp LIKE ?""",
              (now.date().isoformat() + "%",)).fetchone()[0])
    except Exception:
        pass
    due = due_measurements(history, now, path)[:max(0, daily_cap - used_today)]
    if not due:
        return {"collected": 0, "missing": 0}
    cost = estimate_x("owned_read_per_resource", len(due))
    reservation, reason = reserve("x", "owned_read", "tweets_lookup", cost, len(due), path=path)
    if not reservation:
        return {"collected": 0, "skipped": reason}
    try:
        if client_factory is None:
            import tweepy
            client_factory = tweepy.Client
        client = client_factory(consumer_key=os.environ.get("API_KEY"), consumer_secret=os.environ.get("API_KEY_SECRET"),
                                access_token=os.environ.get("ACCESS_TOKEN"), access_token_secret=os.environ.get("ACCESS_TOKEN_SECRET"))
        ids = list(dict.fromkeys(str(post["tweet_id"]) for post, _ in due))
        response = client.get_tweets(ids=ids, tweet_fields=["public_metrics", "created_at"], user_auth=True)
        by_id = {str(tweet.id): tweet for tweet in (response.data or [])}
        collected = 0; missing = 0
        for post, window in due:
            tweet = by_id.get(str(post["tweet_id"]))
            if not tweet:
                missing += 1; continue
            pm = tweet.public_metrics or {}
            impressions = int(pm.get("impression_count", 0) or 0)
            engagement = sum(int(pm.get(k, 0) or 0) for k in ("like_count", "retweet_count", "reply_count", "quote_count", "bookmark_count"))
            hours = max(WINDOWS[window].total_seconds() / 3600, .25)
            row = {"tweet_id": str(post["tweet_id"]), "measurement_window": window,
                   "measured_at": now.isoformat(), "impressions": impressions,
                   "likes": pm.get("like_count", 0), "reposts": pm.get("retweet_count", 0),
                   "replies": pm.get("reply_count", 0), "quotes": pm.get("quote_count", 0),
                   "bookmarks": pm.get("bookmark_count", 0), "profile_clicks": pm.get("user_profile_clicks", 0),
                   "url_clicks": pm.get("url_link_clicks", 0),
                   "engagement_rate": engagement / impressions if impressions else 0,
                   "impressions_per_hour": impressions / hours}
            if upsert_metric(row, path) is not None: collected += 1
        finalize(reservation, estimate_x("owned_read_per_resource", len(due)) or 0, success=True, path=path)
        return {"collected": collected, "missing": missing}
    except Exception as exc:
        finalize(reservation, 0, success=False, error_type=type(exc).__name__, path=path)
        return {"collected": 0, "skipped": type(exc).__name__}
