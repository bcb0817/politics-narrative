"""Read-only growth, conversion, and digest analysis."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from metrics_db import apply_additive_migrations, connect, db_path


JST = ZoneInfo("Asia/Tokyo")
CONVERSION_TYPES = (
    "note_click", "note_follow", "note_purchase", "youtube_click",
    "youtube_view", "youtube_subscribe", "newsletter_click",
    "newsletter_signup", "paid_purchase",
    "amazon_link_click", "amazon_purchase", "amazon_commission",
)


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        return parsed.astimezone(JST)
    except (TypeError, ValueError):
        return None


def follower_conversion_analysis(path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    minimum = int(os.environ.get("FOLLOW_CONVERSION_MIN_SAMPLE_SIZE", "10"))
    with closing(connect(path)) as conn:
        posts = [dict(row) for row in conn.execute("""SELECT p.*,
          COALESCE(m.profile_clicks,0) profile_clicks,
          COALESCE(m.impressions,0) impressions FROM published_posts p
          LEFT JOIN post_metrics m ON m.id=(SELECT id FROM post_metrics
            WHERE tweet_id=p.tweet_id ORDER BY
            CASE measurement_window WHEN '72h' THEN 3 WHEN '24h' THEN 2
            WHEN '1h' THEN 1 ELSE 0 END DESC,id DESC LIMIT 1)
          ORDER BY p.posted_at""")]
        snapshots = [dict(row) for row in conn.execute(
            "SELECT * FROM follower_snapshots ORDER BY captured_at")]

    snapshot_times = [(_parse(row["captured_at"]), row) for row in snapshots]
    post_times = [_parse(row["posted_at"]) for row in posts]
    samples = []
    for index, post in enumerate(posts):
        posted = post_times[index]
        if not posted:
            continue
        before_candidates = [
            row for stamp, row in snapshot_times
            if stamp and posted - timedelta(minutes=30) <= stamp <= posted
        ]
        after_candidates = [
            row for stamp, row in snapshot_times
            if stamp and posted <= stamp <= posted + timedelta(minutes=120)
        ]
        before = before_candidates[-1] if before_candidates else None
        after = after_candidates[0] if after_candidates else None
        overlap = sum(
            1 for stamp in post_times if stamp and posted < stamp <= posted + timedelta(minutes=120))
        confidence = "unattributable"
        delta = None
        if before and after:
            delta = int(after["followers_count"] or 0) - int(
                before["followers_count"] or 0)
            confidence = "high" if overlap == 0 else ("medium" if overlap == 1 else "low")
        sample = {
            **post,
            "posted_hour_jst": posted.hour,
            "followers_before_window": (
                int(before["followers_count"]) if before else None),
            "followers_after_window": (
                int(after["followers_count"]) if after else None),
            "estimated_follower_delta": delta,
            "conversion_confidence": confidence,
        }
        samples.append(sample)
        with closing(connect(path)) as conn:
            conn.execute("""UPDATE published_posts SET posted_hour_jst=?,
              followers_before_window=?,followers_after_window=?,
              estimated_follower_delta=?,conversion_confidence=? WHERE id=?""", (
                posted.hour, sample["followers_before_window"],
                sample["followers_after_window"], delta, confidence, post["id"]))
            conn.commit()

    attributable = [row for row in samples if row["estimated_follower_delta"] is not None]

    def group(key):
        out = {}
        for row in attributable:
            label = str(row.get(key) if row.get(key) not in {None, ""} else "unknown")
            rec = out.setdefault(label, {
                "samples": 0, "estimated_follower_delta": 0.0,
                "profile_clicks": 0, "impressions": 0,
            })
            rec["samples"] += 1
            rec["estimated_follower_delta"] += float(
                row["estimated_follower_delta"] or 0)
            rec["profile_clicks"] += int(row.get("profile_clicks") or 0)
            rec["impressions"] += int(row.get("impressions") or 0)
        for rec in out.values():
            rec["average_estimated_follower_delta"] = round(
                rec["estimated_follower_delta"] / rec["samples"], 4)
            rec["profile_click_rate"] = round(
                rec["profile_clicks"] / max(1, rec["impressions"]), 8)
        return out

    by_type = group("post_type")
    eligible = {
        key: value for key, value in by_type.items()
        if value["samples"] >= minimum
    }
    ordered = sorted(
        eligible, key=lambda key: eligible[key]["average_estimated_follower_delta"],
        reverse=True)
    confidence_counts = defaultdict(int)
    for row in samples:
        confidence_counts[row["conversion_confidence"]] += 1
    return {
        "window": {"before_minutes": 30, "after_minutes": 120},
        "minimum_sample_size": minimum,
        "attributable_samples": len(attributable),
        "by_post_type": by_type,
        "by_hook_type": group("hook_type"),
        "by_critique_axis": group("critique_axis"),
        "by_posted_hour_jst": group("posted_hour_jst"),
        "confidence_sample_counts": dict(confidence_counts),
        "strongest_post_type": ordered[0] if ordered else None,
        "weakest_post_type": ordered[-1] if ordered else None,
        "decision": "measured" if ordered else "insufficient_data",
        "attribution_note": "Time-window estimate; not strict post-level causation.",
    }


def conversion_dashboard(path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM conversion_events ORDER BY occurred_at")]
        posts = {
            str(row["tweet_id"]): dict(row) for row in conn.execute(
                "SELECT * FROM published_posts")
        }
        metrics = {
            str(row["tweet_id"]): dict(row) for row in conn.execute("""SELECT *
                FROM post_metrics WHERE id IN (SELECT MAX(id) FROM post_metrics
                GROUP BY tweet_id)""")
        }
        notes = {
            str(row["content_id"]): dict(row) for row in conn.execute(
                "SELECT * FROM note_drafts")
        }
        amazon_items = {
            (str(row["content_id"]), str(row["item_id"])): dict(row)
            for row in conn.execute("SELECT * FROM amazon_associate_items")
        }
    totals = {event: 0.0 for event in CONVERSION_TYPES}
    by_campaign = defaultdict(float)
    by_content = defaultdict(float)
    by_post_type = defaultdict(float)
    by_digest = defaultdict(float)
    note_performance = {}
    amazon_by_content = defaultdict(lambda: defaultdict(float))
    amazon_by_item = defaultdict(lambda: defaultdict(float))
    for row in rows:
        value = float(row["value"] or 0)
        if row["event_type"] in totals:
            totals[row["event_type"]] += value
        by_campaign[row["campaign"] or "unknown"] += value
        content = str(row["content_id"] or "")
        by_content[content or "unknown"] += value
        tweet_id = content.removeprefix("tweet_")
        post = posts.get(tweet_id)
        if post:
            by_post_type[post.get("post_type") or "unknown"] += value
            by_digest[post.get("digest_type") or "non_digest"] += value
        note = notes.get(content)
        if note:
            published = _parse(note.get("published_at"))
            summary = note_performance.setdefault(content, {
                "content_id": content,
                "article_type": note.get("article_type"),
                "character_count": note.get("character_count"),
                "published_weekday": (
                    published.strftime("%A") if published else None),
                "published_hour_jst": published.hour if published else None,
                "events": defaultdict(float),
            })
            summary["events"][row["event_type"]] += value
        if str(row["event_type"]).startswith("amazon_"):
            try:
                event_metadata = json.loads(row.get("metadata_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                event_metadata = {}
            item_id = str(event_metadata.get("item_id") or "unknown")
            amazon_by_content[content][row["event_type"]] += value
            amazon_by_item[f"{content}:{item_id}"][row["event_type"]] += value
    impressions = sum(int(row.get("impressions") or 0) for row in metrics.values())
    profile_clicks = sum(int(row.get("profile_clicks") or 0) for row in metrics.values())
    conversions = sum(totals.values())
    def has_related_books(row):
        try:
            return bool(json.loads(row.get("related_books_json") or "[]"))
        except (TypeError, json.JSONDecodeError):
            return False
    return {
        "event_totals": totals,
        "by_post_type": dict(by_post_type),
        "by_digest_type": dict(by_digest),
        "by_campaign": dict(by_campaign),
        "by_content_id": dict(by_content),
        "note_performance": [
            {**row, "events": dict(row["events"])}
            for row in note_performance.values()
        ],
        "amazon_performance": {
            "by_content_id": {
                key: dict(value) for key, value in amazon_by_content.items()
            },
            "by_item": [
                {
                    "content_id": key.split(":", 1)[0],
                    "item_id": key.split(":", 1)[1],
                    "title": (
                        amazon_items.get(tuple(key.split(":", 1)), {})
                        .get("title")
                    ),
                    "events": dict(value),
                }
                for key, value in amazon_by_item.items()
            ],
            "articles_with_related_books": sum(
                has_related_books(row)
                for row in notes.values()
            ),
            "articles_without_related_books": sum(
                not has_related_books(row)
                for row in notes.values()
            ),
        },
        "conversions_per_1000_impressions": round(
            conversions / max(1, impressions) * 1000, 6),
        "conversion_from_profile_click_rate": round(
            conversions / max(1, profile_clicks), 6),
        "sample_count": len(rows),
        "decision": "measured" if len(rows) >= 10 else "insufficient_data",
    }


def digest_comparison(path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    minimum = int(os.environ.get("DIGEST_COMPARISON_MIN_SAMPLE_SIZE", "10"))
    with closing(connect(path)) as conn:
        rows = [dict(row) for row in conn.execute("""SELECT p.*,
          COALESCE(m.impressions,0) impressions,
          COALESCE(m.impressions_per_hour,0) impressions_per_hour,
          COALESCE(m.engagement_rate,0) engagement_rate,
          COALESCE(m.bookmarks,0) bookmarks,COALESCE(m.quotes,0) quotes,
          COALESCE(m.profile_clicks,0) profile_clicks,
          COALESCE(q.follow_conversion_estimate,0) follow_conversion_estimate,
          COALESCE(q.correction_required,0) correction_required,
          COALESCE(g.quality_score,0) quality_score
          FROM published_posts p
          LEFT JOIN post_metrics m ON m.id=(SELECT id FROM post_metrics
            WHERE tweet_id=p.tweet_id ORDER BY
            CASE measurement_window WHEN '72h' THEN 3 WHEN '24h' THEN 2
            WHEN '1h' THEN 1 ELSE 0 END DESC,id DESC LIMIT 1)
          LEFT JOIN post_quality_dimensions q ON q.tweet_id=p.tweet_id
          LEFT JOIN generated_posts g ON g.id=p.generated_post_id
          WHERE p.post_type='morning_evening_digest' OR p.digest_type IS NOT NULL""")]
        conversions = [dict(row) for row in conn.execute(
            "SELECT * FROM conversion_events")]
    for row in rows:
        if not row.get("digest_type"):
            stamp = _parse(row.get("posted_at"))
            if stamp and stamp.hour in {5, 6}:
                row["digest_type"] = "morning"
            elif stamp and stamp.hour in {17, 18}:
                row["digest_type"] = "evening"

    def summarize(kind):
        selected = [row for row in rows if row.get("digest_type") == kind]
        n = len(selected)
        impressions = sum(int(row["impressions"] or 0) for row in selected)
        ids = {str(row["tweet_id"]) for row in selected}
        external = defaultdict(float)
        for event in conversions:
            if str(event.get("content_id") or "").removeprefix("tweet_") in ids:
                external[event["event_type"]] += float(event["value"] or 0)
        return {
            "post_count": n,
            "average_impressions": round(impressions / n, 3) if n else None,
            "average_impressions_per_hour": round(sum(
                float(row["impressions_per_hour"] or 0) for row in selected
            ) / n, 4) if n else None,
            "average_engagement_rate": round(sum(
                float(row["engagement_rate"] or 0) for row in selected
            ) / n, 8) if n else None,
            "bookmark_rate": round(sum(int(row["bookmarks"] or 0) for row in selected)
                                   / max(1, impressions), 8) if n else None,
            "quote_rate": round(sum(int(row["quotes"] or 0) for row in selected)
                                / max(1, impressions), 8) if n else None,
            "profile_click_rate": round(sum(
                int(row["profile_clicks"] or 0) for row in selected
            ) / max(1, impressions), 8) if n else None,
            "follow_conversion_estimate": round(sum(
                float(row["follow_conversion_estimate"] or 0)
                for row in selected), 4) if n else None,
            "average_quality_score": round(sum(
                float(row["quality_score"] or 0) for row in selected
            ) / n, 4) if n else None,
            "correction_rate": round(sum(
                int(row["correction_required"] or 0) for row in selected
            ) / n, 4) if n else None,
            "external_conversions": dict(external),
        }

    morning = summarize("morning")
    evening = summarize("evening")
    decision = "insufficient_data"
    if morning["post_count"] >= minimum and evening["post_count"] >= minimum:
        m_score = float(morning["profile_click_rate"] or 0)
        e_score = float(evening["profile_click_rate"] or 0)
        decision = "tie" if abs(m_score - e_score) < 0.000001 else (
            "morning" if m_score > e_score else "evening")
    return {
        "morning": morning, "evening": evening,
        "minimum_sample_size_per_type": minimum, "decision": decision,
    }
