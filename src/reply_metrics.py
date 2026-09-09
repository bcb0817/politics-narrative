"""Read-only reply collection for post-performance experiments."""

from __future__ import annotations

import hashlib
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from api_budget import estimate_x, finalize, reserve
from metrics_db import connect as metrics_connect, db_path
from post_experiments import apply_migrations, classify_reply, connect


JST = ZoneInfo("Asia/Tokyo")


def _author_hash(value: object) -> str:
    return hashlib.sha256(
        str(value or "").strip().encode("utf-8")).hexdigest()


def collect_x_replies(
        *, path: Path | None = None, post_limit: int = 5,
        replies_per_post: int = 20, client_factory=None,
        now: datetime | None = None, max_pages_per_post: int = 5) -> dict:
    """Collect replies to recent owned posts without any X write operation."""
    path = Path(path or db_path())
    now = now or datetime.now(JST)
    post_limit = max(1, min(20, int(post_limit)))
    replies_per_post = max(10, min(500, int(replies_per_post)))
    max_pages_per_post = max(1, min(5, int(max_pages_per_post)))
    apply_migrations(path)
    with closing(metrics_connect(path)) as connection:
        posts = [
            dict(row) for row in connection.execute(
                """SELECT tweet_id,posted_at FROM published_posts
                   WHERE tweet_id IS NOT NULL AND tweet_id<>''
                   ORDER BY posted_at DESC LIMIT ?""",
                (post_limit,),
            )
        ]
    if not posts:
        return {"status": "empty", "posts_checked": 0, "saved": 0}
    bearer = os.environ.get("BEARER_TOKEN", "").strip()
    if not bearer:
        return {
            "status": "missing_credentials",
            "required_environment": "BEARER_TOKEN",
            "saved": 0,
        }
    maximum_rows = len(posts) * min(
        replies_per_post, max_pages_per_post * 100)
    estimated = estimate_x("post_read_per_resource", maximum_rows)
    reservation, reason = reserve(
        "x", "reply_read", "recent_search", estimated, maximum_rows,
        {"purpose": "post_experiment_reply_classification"},
        path=path,
    )
    if not reservation:
        return {"status": "budget_blocked", "reason": reason, "saved": 0}
    try:
        if client_factory is None:
            import tweepy
            client_factory = tweepy.Client
        client = client_factory(
            bearer_token=bearer, wait_on_rate_limit=False)
        saved = 0
        received = 0
        pages_requested = 0
        for post in posts:
            pagination_token = None
            post_received = 0
            for _page in range(max_pages_per_post):
                remaining = replies_per_post - post_received
                if remaining <= 0:
                    break
                request = {
                    "query": (
                        f"conversation_id:{post['tweet_id']} is:reply"),
                    # Recent Search requires 10..100. On the final page we
                    # may request 10 and discard rows beyond the caller's cap.
                    "max_results": max(10, min(100, remaining)),
                    "tweet_fields": [
                        "id", "author_id", "conversation_id",
                        "created_at", "text",
                    ],
                }
                if pagination_token:
                    request["pagination_token"] = pagination_token
                response = client.search_recent_tweets(**request)
                pages_requested += 1
                page_rows = list(response.data or [])
                accepted = page_rows[:remaining]
                with closing(connect(path)) as connection:
                    for reply in accepted:
                        text = str(getattr(reply, "text", "") or "")
                        author_id = getattr(reply, "author_id", "")
                        reply_id = str(getattr(reply, "id", "") or "")
                        if not reply_id:
                            continue
                        received += 1
                        post_received += 1
                        cursor = connection.execute(
                            """INSERT OR IGNORE INTO post_reply_events
                               (platform,root_post_id,reply_id,author_hash,
                                reply_text,reply_classification,replied_at,
                                source,collected_at)
                               VALUES (?,?,?,?,?,?,?,?,?)""",
                            (
                                "x", str(post["tweet_id"]), reply_id,
                                _author_hash(author_id), text,
                                classify_reply(text),
                                str(getattr(reply, "created_at", "") or ""),
                                "x_official_recent_search", now.isoformat(),
                            ),
                        )
                        saved += int(cursor.rowcount > 0)
                    connection.commit()
                meta = getattr(response, "meta", None) or {}
                if isinstance(meta, dict):
                    pagination_token = meta.get("next_token")
                else:
                    pagination_token = getattr(meta, "next_token", None)
                if not pagination_token:
                    break
        finalize(
            reservation,
            estimate_x("post_read_per_resource", received) or 0,
            success=True, resource_count=received, path=path,
        )
        return {
            "status": "working_live",
            "posts_checked": len(posts),
            "replies_received": received,
            "saved": saved,
            "pages_requested": pages_requested,
            "max_pages_per_post": max_pages_per_post,
            "external_writes": 0,
        }
    except Exception as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        status = (
            "permission_error" if status_code in {401, 403}
            else "rate_limited" if status_code == 429 else "failing"
        )
        finalize(
            reservation, 0, success=False,
            error_type=type(exc).__name__, resource_count=0, path=path,
        )
        return {
            "status": status,
            "error_class": type(exc).__name__,
            "saved": 0,
            "external_writes": 0,
        }
