"""Read-only cost and attribution reports backed by SQLite."""

from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from metrics_db import apply_additive_migrations, connect, db_path

JST = ZoneInfo("Asia/Tokyo")


def openai_usage_breakdown(path: Path | None = None,
                           now: datetime | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    now = now or datetime.now(JST)
    prefix = now.strftime("%Y-%m") + "%"
    with closing(connect(path)) as conn:
        rows = conn.execute("""SELECT operation,model_or_endpoint,resource_count,
            input_tokens,cached_input_tokens,output_tokens,estimated_cost_usd,
            success,error_type,metadata_json,task_type_source
            FROM api_usage_events WHERE provider='openai' AND timestamp LIKE ?
            ORDER BY id""", (prefix,)).fetchall()
        posts = int(conn.execute("""SELECT COUNT(*) FROM published_posts
            WHERE posted_at LIKE ?""", (prefix,)).fetchone()[0])
    mapping = {
        "post_generation": "post_generation",
        "important_post_generation": "important_post_generation",
        "classifier": "classification",
        "classification": "classification",
        "regeneration": "regeneration",
        "quality_review": "quality_review",
        "daily_review": "daily_review",
        "weekly_report": "weekly_review",
        "weekly_review": "weekly_review",
        "batch_submit": "batch_submit",
        "batch_collect": "batch_collect",
        "engagement_queue": "engagement_queue",
        "quality_eval": "eval_quality",
        "eval_quality": "eval_quality",
        "shorts_generation": "shorts_generation",
        "note_generation": "note_generation",
        "threads_generation": "threads_generation",
        "threads_regeneration": "threads_regeneration",
        "threads_daily_review": "threads_daily_review",
        "failed_request": "failed_request",
    }
    grouped: dict[str, dict] = {}
    for row in rows:
        operation = str(row["operation"] or "")
        if operation in mapping:
            task_type = mapping[operation]
            source = str(row["task_type_source"] or "inferred")
            if operation == "post_generation" and "5.6-luna" in str(
                    row["model_or_endpoint"] or ""):
                task_type = "important_post_generation"
                source = "inferred"
        else:
            task_type = "unknown"
            source = "unknown"
        rec = grouped.setdefault(task_type, {
            "calls": 0, "successes": 0, "failures": 0,
            "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0,
            "task_type_source": {"explicit": 0, "inferred": 0, "unknown": 0},
            "operations": {},
        })
        rec["calls"] += 1
        rec["successes"] += int(bool(row["success"]))
        rec["failures"] += int(not bool(row["success"]))
        rec["input_tokens"] += int(row["input_tokens"] or 0)
        rec["cached_input_tokens"] += int(row["cached_input_tokens"] or 0)
        rec["output_tokens"] += int(row["output_tokens"] or 0)
        rec["cost_usd"] += float(row["estimated_cost_usd"] or 0)
        rec["task_type_source"][source if source in {
            "explicit", "inferred", "unknown"} else "unknown"] += 1
        rec["operations"][operation or "(blank)"] = (
            rec["operations"].get(operation or "(blank)", 0) + 1)
    for rec in grouped.values():
        rec["cost_usd"] = round(rec["cost_usd"], 8)
    total_calls = sum(v["calls"] for v in grouped.values())
    total_cost = sum(v["cost_usd"] for v in grouped.values())
    regeneration = grouped.get("regeneration", {}).get("calls", 0)
    classifications = grouped.get("classification", {}).get("calls", 0)
    failures = sum(v["failures"] for v in grouped.values())
    unknown = grouped.get("unknown", {}).get("calls", 0)
    return {
        "month": now.strftime("%Y-%m"),
        "task_types": grouped,
        "published_posts": posts,
        "total_calls": total_calls,
        "total_cost_usd": round(total_cost, 8),
        "api_calls_per_post": round(total_calls / posts, 4) if posts else None,
        "openai_cost_per_post_usd": round(total_cost / posts, 8) if posts else None,
        "failed_requests": failures,
        "failure_rate": round(failures / total_calls, 6) if total_calls else 0.0,
        "regeneration_calls": regeneration,
        "regeneration_rate": round(regeneration / max(1, posts), 6),
        "classification_calls": classifications,
        "classification_addition_rate": round(
            classifications / max(1, posts), 6),
        "unknown_task_type_calls": unknown,
        "task_type_taxonomy": [
            "post_generation", "important_post_generation", "classification",
            "regeneration", "quality_review", "daily_review", "weekly_review",
            "batch_submit", "batch_collect", "engagement_queue", "eval_quality",
            "shorts_generation", "note_generation", "failed_request", "other",
            "threads_generation", "threads_regeneration",
            "threads_daily_review", "unknown",
        ],
    }


def xai_roi(days: int = 31, path: Path | None = None,
            now: datetime | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    now = now or datetime.now(JST)
    since = (now - timedelta(days=max(1, days))).isoformat()
    with closing(connect(path)) as conn:
        usage = conn.execute("""SELECT COUNT(*) requests,
            COALESCE(SUM(CASE WHEN operation='x_search_radar' THEN 1 ELSE 0 END),0)
                search_requests,
            COALESCE(SUM(CASE WHEN cost_source='actual' THEN actual_cost_usd
                ELSE estimated_cost_usd END),0) cost,
            COALESCE(SUM(tool_call_count),0) tool_calls,
            COALESCE(SUM(successful_tool_call_count),0) successful_tool_calls
            FROM xai_usage_events WHERE timestamp>=?""", (since,)).fetchone()
        discovered = int(conn.execute("""SELECT COUNT(*) FROM news_candidates
            WHERE fetched_at>=? AND xai_topic_match=1""", (since,)).fetchone()[0])
        verified = int(conn.execute("""SELECT COUNT(*) FROM news_candidates
            WHERE fetched_at>=? AND xai_topic_match=1 AND verified=1""",
            (since,)).fetchone()[0])
        rows = conn.execute("""SELECT p.tweet_id,p.xai_topic_match,
            COALESCE(m.impressions,0) impressions,
            COALESCE(m.impressions_per_hour,0) impressions_per_hour,
            COALESCE(m.engagement_rate,0) engagement_rate,
            COALESCE(m.profile_clicks,0) profile_clicks,
            COALESCE(m.bookmarks,0) bookmarks,COALESCE(m.quotes,0) quotes,
            COALESCE(q.trust_score,0) trust_score,
            COALESCE(q.follow_conversion_estimate,0) follow_conversion_estimate,
            COALESCE(g.quality_score,0) quality_score,
            COALESCE(q.correction_required,0) correction_required,
            COALESCE(q.manual_delete_required,0) deletion_required,
            COALESCE(p.xai_cost_allocated_usd,0) xai_cost
            FROM published_posts p
            LEFT JOIN post_metrics m ON m.id=(SELECT id FROM post_metrics
                WHERE tweet_id=p.tweet_id ORDER BY
                CASE measurement_window WHEN '72h' THEN 3 WHEN '24h' THEN 2
                WHEN '1h' THEN 1 ELSE 0 END DESC,id DESC LIMIT 1)
            LEFT JOIN post_quality_dimensions q ON q.tweet_id=p.tweet_id
            LEFT JOIN generated_posts g ON g.id=p.generated_post_id
            WHERE p.posted_at>=?""", (since,)).fetchall()

    def summarize(selected) -> dict:
        values = list(selected)
        count = len(values)
        impressions = sum(int(v["impressions"] or 0) for v in values)
        bookmarks = sum(int(v["bookmarks"] or 0) for v in values)
        quotes = sum(int(v["quotes"] or 0) for v in values)
        profile_clicks = sum(int(v["profile_clicks"] or 0) for v in values)
        follow_estimate = sum(
            float(v["follow_conversion_estimate"] or 0) for v in values)
        return {
            "count": count,
            "average_impressions": round(impressions / count, 3) if count else None,
            "average_impressions_per_hour": round(sum(
                float(v["impressions_per_hour"] or 0) for v in values
            ) / count, 4) if count else None,
            "average_engagement_rate": round(sum(
                float(v["engagement_rate"] or 0) for v in values
            ) / count, 8) if count else None,
            "average_profile_clicks": round(
                profile_clicks / count, 4
            ) if count else None,
            "profile_click_rate": round(
                profile_clicks / max(1, impressions), 8
            ) if count else None,
            "bookmark_rate": round(bookmarks / max(1, impressions), 8) if count else None,
            "quote_rate": round(quotes / max(1, impressions), 8) if count else None,
            "average_bookmarks_quotes": round(
                (bookmarks + quotes) / count, 4
            ) if count else None,
            "follow_conversion_estimate": round(follow_estimate, 6) if count else None,
            "average_quality_score": round(sum(
                float(v["quality_score"] or 0) for v in values
            ) / count, 4) if count else None,
            "average_trust_score": round(
                sum(float(v["trust_score"] or 0) for v in values) / count, 4
            ) if count else None,
            "correction_rate": round(
                sum(int(v["correction_required"] or 0) for v in values) / count, 4
            ) if count else None,
            "deletion_rate": round(
                sum(int(v["deletion_required"] or 0) for v in values) / count, 4
            ) if count else None,
        }

    xai_posts = [row for row in rows if int(row["xai_topic_match"] or 0)]
    other_posts = [row for row in rows if not int(row["xai_topic_match"] or 0)]
    xai_summary = summarize(xai_posts)
    other_summary = summarize(other_posts)
    minimum = int(os.environ.get("XAI_ATTRIBUTION_MIN_SAMPLE_SIZE", "10"))
    decision = "insufficient_data"
    if xai_summary["count"] >= minimum and other_summary["count"] >= minimum:
        x_rate = float(xai_summary["profile_click_rate"] or 0)
        o_rate = float(other_summary["profile_click_rate"] or 0)
        x_follow = float(xai_summary["follow_conversion_estimate"] or 0)
        o_follow = float(other_summary["follow_conversion_estimate"] or 0)
        allocated_cost = sum(float(row["xai_cost"] or 0) for row in xai_posts)
        cost_per_post = allocated_cost / max(1, len(xai_posts))
        allowed_cost = float(os.environ.get(
            "XAI_MAX_COST_PER_ATTRIBUTED_POST_USD", "0.50"))
        quality_ok = (
            float(xai_summary["average_trust_score"] or 0)
            >= float(other_summary["average_trust_score"] or 0)
            and float(xai_summary["average_quality_score"] or 0)
            >= float(other_summary["average_quality_score"] or 0)
            and float(xai_summary["correction_rate"] or 0)
            <= float(other_summary["correction_rate"] or 0)
            and float(xai_summary["deletion_rate"] or 0)
            <= float(other_summary["deletion_rate"] or 0)
        )
        conversion_improved = (
            x_rate >= o_rate * 1.20
            or x_follow > o_follow
        )
        if conversion_improved and quality_ok and cost_per_post <= allowed_cost:
            decision = "increase"
        elif not conversion_improved or not quality_ok:
            decision = "decrease"
        else:
            decision = "maintain"
    cost = float(usage["cost"] or 0)
    xai_profile_clicks = sum(int(row["profile_clicks"] or 0) for row in xai_posts)
    xai_follow_estimate = sum(
        float(row["follow_conversion_estimate"] or 0) for row in xai_posts)
    return {
        "period_days": days,
        "xai_actual_or_estimated_cost_usd": round(cost, 8),
        "xai_requests": int(usage["requests"] or 0),
        "xai_search_requests": int(usage["search_requests"] or 0),
        "tool_call_count": int(usage["tool_calls"] or 0),
        "successful_tool_calls": int(usage["successful_tool_calls"] or 0),
        "average_cost_per_request_usd": round(
            cost / int(usage["requests"]), 8) if usage["requests"] else 0.0,
        "xai_discovered_candidates": discovered,
        "independently_verified_candidates": verified,
        "xai_posts": xai_summary,
        "non_xai_posts": other_summary,
        "xai_cost_per_published_post_usd": round(
            cost / len(xai_posts), 8) if xai_posts else None,
        "xai_cost_per_profile_click_usd": round(
            cost / xai_profile_clicks, 8) if xai_profile_clicks else None,
        "xai_cost_per_estimated_follow_usd": round(
            cost / xai_follow_estimate, 8) if xai_follow_estimate > 0 else None,
        "minimum_sample_size": minimum,
        "decision": decision,
        "decision_rule": (
            "Increase requires at least 10 posts in each group, profile-click-rate "
            "or follow-conversion improvement, no quality/trust decline, no correction "
            "or deletion increase, and acceptable attributed cost. "
            "Impressions alone never justify an increase."
        ),
    }
