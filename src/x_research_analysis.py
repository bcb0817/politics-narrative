"""Publish verified X Search analysis as a Premium long post or safe thread."""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from api_budget import estimate_x, finalize, reserve
from metrics_db import connect, db_path, init_db, insert_published


JST = ZoneInfo("Asia/Tokyo")


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _clean(value: object, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _json_list(value: object, limit: int = 5) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = []
    if not isinstance(parsed, list):
        return []
    return [_clean(item, 300) for item in parsed[:limit] if _clean(item, 300)]


def settings() -> dict:
    return {
        "enabled": _bool("X_RESEARCH_ANALYSIS_ENABLED", "false"),
        "auto_publish": _bool(
            "X_RESEARCH_ANALYSIS_AUTO_PUBLISH_ENABLED", "false"),
        "premium_long_post": _bool(
            "X_RESEARCH_PREMIUM_LONG_POST_ENABLED", "true"),
        "thread_fallback": _bool(
            "X_RESEARCH_THREAD_FALLBACK_ENABLED", "true"),
        "max_chars": max(500, min(
            10_000, _int("X_RESEARCH_LONG_POST_MAX_CHARS", 4000))),
        "thread_chars": max(180, min(
            270, _int("X_RESEARCH_THREAD_CHARS", 260))),
        "thread_max_posts": max(2, min(
            12, _int("X_RESEARCH_THREAD_MAX_POSTS", 8))),
        "daily_limit": max(0, min(
            4, _int("X_RESEARCH_ANALYSIS_DAILY_LIMIT", 2))),
        "min_interval_minutes": max(
            60, _int("X_RESEARCH_ANALYSIS_MIN_INTERVAL_MINUTES", 180)),
        "min_confidence": max(0.0, min(
            1.0, _float("X_RESEARCH_ANALYSIS_MIN_CONFIDENCE", 0.65))),
        "min_evidence": max(
            2, _int("X_RESEARCH_ANALYSIS_MIN_EVIDENCE", 2)),
        "min_posting_value": max(0.0, min(
            10.0, _float(
                "X_RESEARCH_ANALYSIS_MIN_POSTING_VALUE", 6.0))),
        "max_topics": max(1, min(
            3, _int("X_RESEARCH_ANALYSIS_MAX_TOPICS", 2))),
        "dedup_hours": max(
            24, _int("X_RESEARCH_ANALYSIS_DEDUP_HOURS", 72)),
    }


def migrate(path: Path | None = None) -> None:
    path = path or db_path()
    init_db(path)
    with closing(connect(path)) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS x_research_analysis_posts (
          id INTEGER PRIMARY KEY,
          source_run_id TEXT UNIQUE,
          content_hash TEXT UNIQUE,
          text TEXT,
          status TEXT,
          delivery_mode TEXT,
          tweet_ids_json TEXT,
          included_topic_ids_json TEXT,
          failure_reason TEXT,
          created_at TEXT,
          published_at TEXT,
          updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_x_research_analysis_status
          ON x_research_analysis_posts(status,created_at);
        """)
        conn.commit()


def _latest_run(conn) -> dict | None:
    row = conn.execute(
        """SELECT * FROM integrated_research_runs
           WHERE status='success' AND eligible_topic_count>0
           ORDER BY generated_at DESC,id DESC LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def _eligible_topics(conn, run_id: str, cfg: dict) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM integrated_research_topics
           WHERE run_id=? AND post_eligible=1
             AND correction_status='current'
             AND deleted_source_count=0
             AND source_family_count>=?
             AND evidence_count>=?
             AND confidence>=?
             AND posting_value_score>=?
           ORDER BY posting_value_score DESC,confidence DESC,id ASC
           LIMIT ?""",
        (
            run_id,
            2,
            cfg["min_evidence"],
            cfg["min_confidence"],
            cfg["min_posting_value"],
            cfg["max_topics"],
        ),
    ).fetchall()
    topics = []
    for source in rows:
        row = dict(source)
        evidence = conn.execute(
            """SELECT provider,title,source_url,reliability
               FROM integrated_research_evidence
               WHERE topic_id=? AND is_deleted=0
                 AND provider IN ('official_rss','official','rss')
               ORDER BY reliability DESC,id ASC LIMIT 2""",
            (row["id"],),
        ).fetchall()
        row["primary_sources"] = [dict(item) for item in evidence]
        if row["primary_sources"]:
            topics.append(row)
    return topics


def _content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", "", text).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _render(topics: list[dict]) -> str:
    lines = [
        "🔎 X Searchリサーチ・分析",
        "",
        "X上で注目された論点を、一次資料・報道と照合しました。",
        "X上の声は世論全体ではなく、検索で確認できた範囲の反応です。",
    ]
    for index, row in enumerate(topics, 1):
        main_claims = _json_list(row.get("main_claims_json"), 2)
        counterclaims = _json_list(row.get("counterclaims_json"), 2)
        lines.extend([
            "",
            f"【{index}. {_clean(row.get('title'), 140)}】",
            f"確認できた事実：{_clean(row.get('fact_summary'), 300)}",
        ])
        if main_claims:
            lines.append("X上の主な見方（反応・意見）：")
            lines.extend(f"・{claim}" for claim in main_claims)
        if counterclaims:
            lines.append("反対・補足の見方：")
            lines.extend(f"・{claim}" for claim in counterclaims)
        lines.append(
            "検証状態："
            f"信頼度{float(row.get('confidence') or 0):.2f}／"
            f"根拠{int(row.get('evidence_count') or 0)}件")
        urls = []
        for source in row.get("primary_sources") or []:
            url = str(source.get("source_url") or "").strip()
            if url.startswith("https://") and url not in urls:
                urls.append(url)
        if urls:
            lines.append("一次資料：" + " ".join(urls[:2]))
    lines.extend([
        "",
        "🧭 判断",
        "確認できた事実と、X上の評価・批判は分けて読む必要があります。",
        "新しい事実や公式発表が出た場合は、次回の定点観測で更新します。",
    ])
    return "\n".join(lines).strip()


def _latest_any_post_at(conn) -> datetime | None:
    value = conn.execute(
        """SELECT posted_at FROM published_posts
           WHERE posted_at IS NOT NULL AND posted_at<>''
           ORDER BY posted_at DESC,id DESC LIMIT 1"""
    ).fetchone()
    if not value or not value[0]:
        return None
    try:
        parsed = datetime.fromisoformat(str(value[0]))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)
    except ValueError:
        return None


def prepare(path: Path | None = None, *,
            now: datetime | None = None,
            ignore_timing: bool = False) -> dict:
    path = path or db_path()
    now = now or datetime.now(JST)
    cfg = settings()
    migrate(path)
    if not cfg["enabled"]:
        return {"status": "blocked", "reason": "feature_disabled"}
    with closing(connect(path)) as conn:
        run = _latest_run(conn)
        if not run:
            return {"status": "blocked", "reason": "no_eligible_research_run"}
        prior = conn.execute(
            """SELECT status,tweet_ids_json FROM x_research_analysis_posts
               WHERE source_run_id=? LIMIT 1""",
            (run["run_id"],),
        ).fetchone()
        if prior:
            return {
                "status": "already_processed",
                "run_id": run["run_id"],
                "publication_status": prior["status"],
            }
        topics = _eligible_topics(conn, run["run_id"], cfg)
        if not topics:
            return {
                "status": "blocked", "reason": "no_verified_topics",
                "run_id": run["run_id"],
            }
        text = _render(topics)
        digest = _content_hash(text)
        duplicate = conn.execute(
            """SELECT id FROM x_research_analysis_posts
               WHERE content_hash=? AND created_at>=? LIMIT 1""",
            (
                digest,
                (now - timedelta(hours=cfg["dedup_hours"])).isoformat(),
            ),
        ).fetchone()
        if duplicate:
            return {
                "status": "blocked", "reason": "duplicate_analysis",
                "run_id": run["run_id"],
            }
        daily_count = int(conn.execute(
            """SELECT COUNT(*) FROM x_research_analysis_posts
               WHERE status='published' AND published_at LIKE ?""",
            (now.date().isoformat() + "%",),
        ).fetchone()[0])
        if not ignore_timing and daily_count >= cfg["daily_limit"]:
            return {
                "status": "blocked", "reason": "daily_limit",
                "daily_count": daily_count,
            }
        latest_post = _latest_any_post_at(conn)
        if not ignore_timing and latest_post and (
            now - latest_post.astimezone(JST)
        ).total_seconds() < cfg["min_interval_minutes"] * 60:
            return {
                "status": "blocked", "reason": "minimum_interval",
                "latest_posted_at": latest_post.isoformat(),
            }
    if len(text) > cfg["max_chars"]:
        return {
            "status": "blocked", "reason": "long_post_too_long",
            "character_count": len(text),
        }
    return {
        "status": "ready",
        "run_id": run["run_id"],
        "text": text,
        "content_hash": digest,
        "character_count": len(text),
        "topics": [
            {
                "id": row["id"],
                "title": row["title"],
                "confidence": row["confidence"],
                "evidence_count": row["evidence_count"],
            }
            for row in topics
        ],
    }


def _split_thread(text: str, limit: int) -> list[str]:
    content_limit = max(100, limit - 8)
    paragraphs = [part.strip() for part in text.split("\n") if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        parts = [paragraph[index:index + content_limit]
                 for index in range(0, len(paragraph), content_limit)]
        for part in parts:
            candidate = f"{current}\n{part}".strip() if current else part
            if len(candidate) <= content_limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = part
    if current:
        chunks.append(current)
    total = len(chunks)
    return [
        f"{index}/{total}\n{chunk}" for index, chunk in enumerate(chunks, 1)
    ]


def _client():
    from post import _x_client
    return _x_client()


def _known_long_post_rejection(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status in {400, 403}


def _reserve_posts(count: int, path: Path, run_id: str):
    cost = estimate_x("post_create_per_request", count)
    return reserve(
        "x", "post_create", "post_create", cost, count,
        {"post_type": "x_research_analysis", "run_id": run_id},
        path=path,
    )


def publish(*, confirm: bool = False, automatic: bool = False,
            dry_run: bool = True, path: Path | None = None,
            now: datetime | None = None, client=None) -> dict:
    path = path or db_path()
    now = now or datetime.now(JST)
    cfg = settings()
    candidate = prepare(path, now=now)
    if candidate.get("status") != "ready" or dry_run:
        return {
            **candidate,
            "dry_run": bool(dry_run),
            "external_writes": 0,
        }
    if not _bool("POST_ENABLED", "false"):
        return {
            **candidate, "status": "blocked", "reason": "post_disabled",
            "external_writes": 0,
        }
    if automatic and not cfg["auto_publish"]:
        return {
            **candidate, "status": "blocked",
            "reason": "auto_publish_disabled", "external_writes": 0,
        }
    if not automatic and not confirm:
        return {
            **candidate, "status": "blocked",
            "reason": "explicit_confirmation_required", "external_writes": 0,
        }

    migrate(path)
    topic_ids = [row["id"] for row in candidate["topics"]]
    with closing(connect(path)) as conn:
        conn.execute(
            """INSERT INTO x_research_analysis_posts
               (source_run_id,content_hash,text,status,delivery_mode,
                tweet_ids_json,included_topic_ids_json,failure_reason,
                created_at,published_at,updated_at)
               VALUES (?,?,?,'publishing','',?,?,'',?,NULL,?)""",
            (
                candidate["run_id"], candidate["content_hash"],
                candidate["text"], "[]",
                json.dumps(topic_ids, ensure_ascii=False),
                now.isoformat(), now.isoformat(),
            ),
        )
        conn.commit()

    active_client = client or _client()
    tweet_ids: list[str] = []
    delivery_mode = "premium_long_post"
    reservation, reason = _reserve_posts(1, path, candidate["run_id"])
    if not reservation:
        _finish(
            path, candidate["run_id"], "blocked", "", [],
            reason, now)
        return {
            **candidate, "status": "blocked", "reason": reason,
            "external_writes": 0,
        }
    try:
        response = active_client.create_tweet(text=candidate["text"])
        tweet_ids.append(str(response.data["id"]))
        finalize(
            reservation,
            estimate_x("post_create_per_request", 1) or 0,
            success=True, resource_count=1, path=path)
    except Exception as exc:
        finalize(
            reservation, 0, success=False,
            error_type=type(exc).__name__, resource_count=1, path=path)
        if not (
            cfg["premium_long_post"]
            and cfg["thread_fallback"]
            and len(candidate["text"]) > 280
            and _known_long_post_rejection(exc)
        ):
            _finish(
                path, candidate["run_id"], "unknown", delivery_mode, [],
                type(exc).__name__, now)
            return {
                **candidate, "status": "unknown",
                "reason": "ambiguous_publish_failure",
                "error_type": type(exc).__name__, "external_writes": 0,
            }
        chunks = _split_thread(candidate["text"], cfg["thread_chars"])
        if len(chunks) > cfg["thread_max_posts"]:
            _finish(
                path, candidate["run_id"], "blocked", "thread", [],
                "thread_post_limit", now)
            return {
                **candidate, "status": "blocked",
                "reason": "thread_post_limit", "external_writes": 0,
            }
        thread_reservation, thread_reason = _reserve_posts(
            len(chunks), path, candidate["run_id"])
        if not thread_reservation:
            _finish(
                path, candidate["run_id"], "blocked", "thread", [],
                thread_reason, now)
            return {
                **candidate, "status": "blocked", "reason": thread_reason,
                "external_writes": 0,
            }
        delivery_mode = "thread"
        try:
            previous_id = ""
            for chunk in chunks:
                kwargs = {"text": chunk}
                if previous_id:
                    kwargs["in_reply_to_tweet_id"] = previous_id
                response = active_client.create_tweet(**kwargs)
                previous_id = str(response.data["id"])
                tweet_ids.append(previous_id)
            finalize(
                thread_reservation,
                estimate_x("post_create_per_request", len(tweet_ids)) or 0,
                success=True, fallback_used=True,
                resource_count=len(tweet_ids), path=path)
        except Exception as thread_exc:
            finalize(
                thread_reservation,
                estimate_x(
                    "post_create_per_request", len(tweet_ids)) or 0,
                success=False, fallback_used=True,
                error_type=type(thread_exc).__name__,
                resource_count=len(tweet_ids), path=path)
            _finish(
                path, candidate["run_id"],
                "partial" if tweet_ids else "failed",
                delivery_mode, tweet_ids, type(thread_exc).__name__, now)
            return {
                **candidate,
                "status": "partial" if tweet_ids else "failed",
                "reason": "thread_publish_failed",
                "tweet_ids": tweet_ids,
                "external_writes": len(tweet_ids),
            }

    _finish(
        path, candidate["run_id"], "published", delivery_mode,
        tweet_ids, "", now)
    parent_id = tweet_ids[0]
    insert_published(None, {
        "tweet_id": parent_id,
        "tweet_text": candidate["text"],
        "posted_at_jst": now.isoformat(),
        "topic_key": "x-search-research-analysis",
        "post_type": "x_research_analysis",
        "hook_type": "research_summary",
        "critique_axis": "evidence_and_public_reaction",
        "discovered_via": ["official", "rss", "xai_x_search"],
        "xai_topic_match": True,
        "included_topic_keys": [
            row["title"] for row in candidate["topics"]],
        "primary_topic_key": candidate["topics"][0]["title"],
        "posted_hour_jst": now.hour,
    }, path)
    with closing(connect(path)) as conn:
        placeholders = ",".join("?" for _ in topic_ids)
        conn.execute(
            f"""UPDATE integrated_research_topics
                SET x_post_id=?,updated_at=? WHERE id IN ({placeholders})""",
            (parent_id, now.isoformat(), *topic_ids),
        )
        for topic_id in topic_ids:
            conn.execute(
                """INSERT OR IGNORE INTO integrated_research_decisions
                   (topic_id,run_id,stage,decision,reason,scores_json,actor,
                    decided_at) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    topic_id, candidate["run_id"],
                    "x_research_analysis_publish", "published",
                    delivery_mode, "{}", "x_research_analysis_v1",
                    now.isoformat(),
                ),
            )
        conn.commit()
    try:
        from discord_notify import notify_post_success
        notify_post_success({
            "tweet_id": parent_id,
            "tweet_text": candidate["text"],
            "post_type": "x_research_analysis",
        })
    except Exception:
        pass
    return {
        **candidate,
        "status": "published",
        "delivery_mode": delivery_mode,
        "tweet_ids": tweet_ids,
        "external_writes": len(tweet_ids),
    }


def _finish(path: Path, run_id: str, status: str, delivery_mode: str,
            tweet_ids: list[str], failure_reason: str,
            now: datetime) -> None:
    with closing(connect(path)) as conn:
        conn.execute(
            """UPDATE x_research_analysis_posts
               SET status=?,delivery_mode=?,tweet_ids_json=?,
                   failure_reason=?,published_at=?,updated_at=?
               WHERE source_run_id=?""",
            (
                status, delivery_mode,
                json.dumps(tweet_ids, ensure_ascii=False),
                failure_reason,
                now.isoformat() if status == "published" else None,
                now.isoformat(), run_id,
            ),
        )
        conn.commit()


def status(path: Path | None = None) -> dict:
    path = path or db_path()
    migrate(path)
    with closing(connect(path)) as conn:
        rows = conn.execute(
            """SELECT status,COUNT(*) count
               FROM x_research_analysis_posts GROUP BY status"""
        ).fetchall()
        latest = conn.execute(
            """SELECT source_run_id,status,delivery_mode,tweet_ids_json,
                      failure_reason,created_at,published_at
               FROM x_research_analysis_posts
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    return {
        "settings": settings(),
        "counts": {row["status"]: int(row["count"]) for row in rows},
        "latest": dict(latest) if latest else None,
    }
