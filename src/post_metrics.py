"""Collect each published post at Phase A growth evaluation windows."""

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
WINDOWS = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "72h": timedelta(hours=72),
}
TWEET_METRIC_FIELDS = [
    "public_metrics", "non_public_metrics", "organic_metrics", "created_at",
]


def _mapping(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return dict(value or {})
    except (TypeError, ValueError):
        return {}


def _integer(mapping: dict, name: str) -> int | None:
    value = mapping.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_tweet_metrics(tweet) -> dict:
    """Extract measured values without turning unavailable fields into zero."""
    public = _mapping(getattr(tweet, "public_metrics", None))
    non_public = _mapping(getattr(tweet, "non_public_metrics", None))
    organic = _mapping(getattr(tweet, "organic_metrics", None))

    def first(name: str, sources: tuple[tuple[str, dict], ...]) -> tuple[int | None, str | None]:
        for source_name, source in sources:
            value = _integer(source, name)
            if value is not None:
                return value, source_name
        return None, None

    impressions, impressions_source = first(
        "impression_count", (
            ("non_public_metrics", non_public),
            ("organic_metrics", organic),
            ("public_metrics", public),
        ))
    profile_clicks, profile_source = first(
        "user_profile_clicks", (
            ("non_public_metrics", non_public),
            ("organic_metrics", organic),
        ))
    url_clicks, url_source = first(
        "url_link_clicks", (
            ("non_public_metrics", non_public),
            ("organic_metrics", organic),
        ))
    return {
        "impressions": impressions,
        "likes": _integer(public, "like_count"),
        "reposts": _integer(public, "retweet_count"),
        "replies": _integer(public, "reply_count"),
        "quotes": _integer(public, "quote_count"),
        "bookmarks": _integer(public, "bookmark_count"),
        "profile_clicks": profile_clicks,
        "url_clicks": url_clicks,
        "impressions_source": impressions_source,
        "profile_clicks_source": profile_source,
        "url_clicks_source": url_source,
        "public_metrics_available": bool(public),
        "private_metrics_available": bool(non_public or organic),
        "metric_fields_json": json.dumps({
            "public_metrics": sorted(public),
            "non_public_metrics": sorted(non_public),
            "organic_metrics": sorted(organic),
        }, ensure_ascii=False, sort_keys=True),
    }


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
        response = client.get_tweets(
            ids=ids, tweet_fields=TWEET_METRIC_FIELDS, user_auth=True)
        by_id = {str(tweet.id): tweet for tweet in (response.data or [])}
        collected = 0; missing = 0
        for post, window in due:
            tweet = by_id.get(str(post["tweet_id"]))
            if not tweet:
                missing += 1; continue
            measured = extract_tweet_metrics(tweet)
            impressions = measured["impressions"]
            engagement_parts = [
                measured[name] for name in (
                    "likes", "reposts", "replies", "quotes", "bookmarks")
                if measured[name] is not None
            ]
            engagement = sum(engagement_parts) if engagement_parts else None
            hours = max(WINDOWS[window].total_seconds() / 3600, .25)
            row = {"tweet_id": str(post["tweet_id"]), "measurement_window": window,
                   "measured_at": now.isoformat(), **measured,
                   "engagement_rate": (
                       engagement / impressions
                       if engagement is not None and impressions else None),
                   "impressions_per_hour": (
                       impressions / hours if impressions is not None else None)}
            if upsert_metric(row, path) is not None: collected += 1
        finalize(reservation, estimate_x("owned_read_per_resource", len(due)) or 0, success=True, path=path)
        return {"collected": collected, "missing": missing}
    except Exception as exc:
        finalize(reservation, 0, success=False, error_type=type(exc).__name__, path=path)
        return {"collected": 0, "skipped": type(exc).__name__}
