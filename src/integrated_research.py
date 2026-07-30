"""Build auditable cross-source research records and safe post candidates.

Official/RSS material remains the factual authority. X and Threads observations
are stored as public-reaction signals, never as standalone factual proof.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

from metrics_db import (
    apply_additive_migrations,
    apply_threads_full_migrations,
    connect,
    db_path,
)
from publishing_policy import normalize_topic_key

JST = ZoneInfo("Asia/Tokyo")
PROMPT_VERSION = "integrated-research-v1"


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


def _clean(value: object, limit: int = 1200) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _json_list(value: object, limit: int = 5) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [_clean(item, 240) for item in value[:limit] if _clean(item, 240)]


def _latest_xai_run(conn: sqlite3.Connection) -> dict:
    try:
        row = conn.execute(
            """SELECT * FROM xai_discovery_runs
               WHERE status='success' ORDER BY completed_at DESC,id DESC
               LIMIT 1"""
        ).fetchone()
        return dict(row) if row else {}
    except sqlite3.Error:
        return {}


def _threads_matches(
    conn: sqlite3.Connection,
    topic_key: str,
    title: str,
    now: datetime,
) -> list[dict]:
    try:
        rows = [
            dict(row) for row in conn.execute(
                """SELECT threads_post_id,text,permalink,timestamp,is_verified
                   FROM threads_search_results
                   WHERE last_seen_at>=? ORDER BY last_seen_at DESC LIMIT 100""",
                ((now - timedelta(hours=24)).isoformat(),),
            )
        ]
    except sqlite3.Error:
        return []
    terms = {
        token.lower()
        for token in re.findall(
            r"[A-Za-z0-9一-龯ぁ-んァ-ヶー]{3,}",
            f"{topic_key} {title}",
        )
        if token not in {"ニュース", "政治", "政府", "国会", "統合リサーチ"}
    }
    for token in list(terms):
        if len(token) >= 7:
            terms.update(
                token[index:index + 5]
                for index in range(0, len(token) - 4)
            )
    if not terms:
        return []
    return [
        row for row in rows
        if any(term in str(row.get("text") or "").lower() for term in terms)
    ][:5]


def _previous_topic(
    conn: sqlite3.Connection,
    topic_key: str,
    run_id: str,
) -> dict:
    row = conn.execute(
        """SELECT * FROM integrated_research_topics
           WHERE topic_key=? AND run_id<>?
           ORDER BY created_at DESC,id DESC LIMIT 1""",
        (topic_key, run_id),
    ).fetchone()
    return dict(row) if row else {}


def _change_status(previous: dict, fact_summary: str,
                   main_claims: list[str]) -> str:
    if not previous:
        return "new"
    old = " ".join([
        str(previous.get("fact_summary") or ""),
        str(previous.get("main_claims_json") or ""),
    ])
    new = " ".join([fact_summary, *main_claims])
    ratio = SequenceMatcher(None, old, new).ratio()
    return "unchanged" if ratio >= _float(
        "INTEGRATED_RESEARCH_UNCHANGED_SIMILARITY", 0.82
    ) else "changed"


def _recently_posted(conn: sqlite3.Connection, topic_key: str,
                     now: datetime) -> bool:
    hours = max(1, _int("SEMANTIC_TOPIC_COOLDOWN_HOURS", 72))
    row = conn.execute(
        """SELECT 1 FROM integrated_research_topics
           WHERE topic_key=? AND x_post_id IS NOT NULL AND x_post_id<>''
           AND updated_at>=? LIMIT 1""",
        (topic_key, (now - timedelta(hours=hours)).isoformat()),
    ).fetchone()
    return bool(row)


def _run_id(xai_run: dict, items: list[dict], now: datetime) -> str:
    if xai_run.get("run_id"):
        seed = str(xai_run["run_id"])
    else:
        seed = "|".join(sorted(
            f"{item.get('link','')}:{item.get('xai_discovered_at','')}"
            for item in items if item.get("xai_topic_match")
        )) or now.strftime("%Y-%m-%dT%H")
    return "integrated-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def _candidate(row: dict, item: dict) -> dict:
    main_claims = row["main_claims"]
    counterclaims = row["counterclaims"]
    fact = row["fact_summary"]
    main = "／".join(main_claims[:3]) or "大きな反応はまだ確認できません"
    counter = "／".join(counterclaims[:3]) or "明確な反対論はまだ確認できません"
    summary = (
        f"【公式情報】{fact}\n"
        f"【X上の主な論点】{main}\n"
        f"【反対論・留意点】{counter}\n"
        "【統合分析】公式資料で確認できる事実と、SNS上で観測された反応を"
        "区別して検討する必要があります。"
    )
    return {
        "title": f"統合リサーチ｜{row['title']}"[:180],
        "summary": summary[:1800],
        "link": str(item.get("link") or item.get("url") or ""),
        "source": "統合リサーチ（公式情報＋xAI X Search）",
        "source_type": "integrated_research",
        "pub_date": str(item.get("pub_date") or row["generated_at"]),
        "discovered_via": list(dict.fromkeys(
            (item.get("discovered_via") or ["rss"])
            + ["integrated_research", "xai"]
            + (["threads"] if row["threads_matches"] else [])
        )),
        "verified": True,
        "verification_reason": "official_or_rss_fact_base_with_social_signals",
        "source_reliability_score": max(
            8.0, float(item.get("source_reliability_score") or 0)),
        "freshness_score": 8.0,
        "final_news_score": min(
            10.0, 7.0 + float(item.get("xai_discovery_bonus") or 0)),
        "x_attention_score": float(item.get("x_attention_score") or 0),
        "x_velocity_score": float(item.get("x_velocity_score") or 0),
        "xai_topic_match": True,
        "xai_attention_score": float(item.get("xai_attention_score") or 0),
        "xai_velocity_score": float(item.get("xai_velocity_score") or 0),
        "xai_discovered_at": str(item.get("xai_discovered_at") or ""),
        "xai_cost_allocated_usd": float(
            item.get("xai_cost_allocated_usd") or 0),
        "integrated_research_topic_id": row["topic_id"],
        "integrated_research_run_id": row["run_id"],
        "integrated_research_confidence": row["confidence"],
        "has_counter_claims": bool(counterclaims),
        "post_type_hint": "steelman_counterargument",
        "topic_key": row["topic_key"],
        "metadata_json": {
            "prompt_version": PROMPT_VERSION,
            "source_family_count": row["source_family_count"],
            "evidence_count": row["evidence_count"],
            "change_status": row["change_status"],
        },
    }


def build_integrated_research_candidates(
    items: list[dict],
    topics: list[dict],
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Persist integrated results and return at most one safe post candidate."""
    if not _bool("INTEGRATED_RESEARCH_ENABLED", "true"):
        return []
    path = path or db_path()
    now = now or datetime.now(JST)
    apply_additive_migrations(path)
    apply_threads_full_migrations(path)
    matched_items = [
        item for item in items
        if item.get("xai_topic_match") and item.get("link")
    ]
    if not matched_items:
        return []
    topic_lookup = {
        str(topic.get("topic_key") or ""): topic
        for topic in topics if topic.get("topic_key")
    }
    saved: list[dict] = []
    with closing(connect(path)) as conn:
        xai_run = _latest_xai_run(conn)
        run_id = _run_id(xai_run, matched_items, now)
        provider_status = {
            "official_rss": "available",
            "xai_x_search": "available",
            "native_x_search": (
                "available" if any(
                    "x_search" in (item.get("discovered_via") or [])
                    for item in items
                ) else "not_used"
            ),
            "threads_search": "checked",
        }
        conn.execute(
            """INSERT OR IGNORE INTO integrated_research_runs
               (run_id,xai_run_id,generated_at,research_window_start,
                research_window_end,provider_status_json,source_family_count,
                topic_count,eligible_topic_count,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, xai_run.get("run_id"), now.isoformat(),
                xai_run.get("requested_from_at"),
                xai_run.get("requested_to_at"),
                json.dumps(provider_status, ensure_ascii=False),
                2, 0, 0, "building", now.isoformat(),
            ),
        )
        seen_topics: set[str] = set()
        for item in matched_items:
            metadata = dict(item.get("xai_topic_metadata") or {})
            raw_key = str(metadata.get("topic_key") or "")
            xai_topic = topic_lookup.get(raw_key, {})
            topic_key = normalize_topic_key(
                f"{item.get('title', '')} {raw_key}")
            if not topic_key or topic_key in seen_topics:
                continue
            seen_topics.add(topic_key)
            title = _clean(item.get("title"), 180)
            fact_summary = _clean(
                item.get("summary") or title, 1000)
            main_claims = _json_list(
                metadata.get("stance_summary")
                or xai_topic.get("main_claims"))
            counterclaims = _json_list(
                metadata.get("counterargument_summary")
                or xai_topic.get("counter_claims"))
            representative_ids = _json_list(
                metadata.get("representative_post_ids")
                or xai_topic.get("representative_post_ids"), 10)
            threads_matches = _threads_matches(
                conn, topic_key, title, now)
            source_families = {"official_rss", "xai_x_search"}
            if threads_matches:
                source_families.add("threads_search")
            if "x_search" in (item.get("discovered_via") or []):
                source_families.add("native_x_search")
            evidence_count = (
                1 + len(representative_ids) + len(threads_matches))
            confidence = min(
                1.0,
                max(
                    float(metadata.get("search_confidence") or 0),
                    float(xai_topic.get("search_confidence") or 0),
                )
                * max(0.0, min(1.0, float(
                    item.get("xai_news_match_confidence") or 1.0))),
            )
            previous = _previous_topic(conn, topic_key, run_id)
            change_status = _change_status(
                previous, fact_summary, main_claims)
            reasons = []
            if len(source_families) < _int(
                    "INTEGRATED_RESEARCH_MIN_SOURCE_FAMILIES", 2):
                reasons.append("insufficient_source_families")
            if evidence_count < _int(
                    "INTEGRATED_RESEARCH_MIN_EVIDENCE", 2):
                reasons.append("insufficient_evidence")
            if confidence < _float(
                    "INTEGRATED_RESEARCH_MIN_CONFIDENCE", 0.65):
                reasons.append("low_confidence")
            if change_status == "unchanged":
                reasons.append("no_material_change")
            if _recently_posted(conn, topic_key, now):
                reasons.append("semantic_topic_cooldown")
            post_eligible = not reasons and _bool(
                "INTEGRATED_RESEARCH_POST_ENABLED", "true")
            decision_reason = (
                "eligible_for_standard_pipeline"
                if post_eligible else ",".join(reasons)
                or "posting_disabled"
            )
            reaction_summary = _clean(
                " / ".join([*main_claims, *counterclaims]), 1000)
            conn.execute(
                """INSERT INTO integrated_research_topics
                   (run_id,topic_key,title,fact_summary,main_claims_json,
                    counterclaims_json,reaction_summary,confidence,
                    source_family_count,evidence_count,change_status,
                    post_eligible,decision_reason,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id,topic_key) DO UPDATE SET
                    fact_summary=excluded.fact_summary,
                    main_claims_json=excluded.main_claims_json,
                    counterclaims_json=excluded.counterclaims_json,
                    reaction_summary=excluded.reaction_summary,
                    confidence=excluded.confidence,
                    source_family_count=excluded.source_family_count,
                    evidence_count=excluded.evidence_count,
                    change_status=excluded.change_status,
                    post_eligible=excluded.post_eligible,
                    decision_reason=excluded.decision_reason,
                    updated_at=excluded.updated_at""",
                (
                    run_id, topic_key, title, fact_summary,
                    json.dumps(main_claims, ensure_ascii=False),
                    json.dumps(counterclaims, ensure_ascii=False),
                    reaction_summary, confidence, len(source_families),
                    evidence_count, change_status, int(post_eligible),
                    decision_reason, now.isoformat(), now.isoformat(),
                ),
            )
            topic_row = conn.execute(
                """SELECT id,candidate_news_id,x_post_id
                   FROM integrated_research_topics
                   WHERE run_id=? AND topic_key=?""",
                (run_id, topic_key),
            ).fetchone()
            topic_id = int(topic_row["id"])
            evidence = [{
                "provider": "official_rss",
                "evidence_type": "fact_base",
                "source_id": str(item.get("link")),
                "source_url": str(item.get("link")),
                "title": title,
                "summary": fact_summary,
                "reliability": max(
                    8.0, float(item.get("source_reliability_score") or 0)),
                "observed_at": str(item.get("pub_date") or now.isoformat()),
            }]
            evidence.extend({
                "provider": "xai_x_search",
                "evidence_type": "representative_public_post",
                "source_id": post_id,
                "source_url": f"https://x.com/i/web/status/{post_id}",
                "title": raw_key,
                "summary": reaction_summary,
                "reliability": confidence,
                "observed_at": str(item.get("xai_discovered_at") or now.isoformat()),
            } for post_id in representative_ids)
            evidence.extend({
                "provider": "threads_search",
                "evidence_type": "public_reaction",
                "source_id": str(row.get("threads_post_id") or ""),
                "source_url": str(row.get("permalink") or ""),
                "title": "Threads上の反応",
                "summary": _clean(row.get("text"), 600),
                "reliability": 0.6 if row.get("is_verified") else 0.4,
                "observed_at": str(row.get("timestamp") or now.isoformat()),
            } for row in threads_matches)
            for source in evidence:
                conn.execute(
                    """INSERT OR IGNORE INTO integrated_research_evidence
                       (topic_id,provider,evidence_type,source_id,source_url,
                        title,summary,reliability,observed_at,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        topic_id, source["provider"],
                        source["evidence_type"], source["source_id"],
                        source["source_url"], source["title"],
                        source["summary"], source["reliability"],
                        source["observed_at"], now.isoformat(),
                    ),
                )
            saved.append({
                "topic_id": topic_id,
                "run_id": run_id,
                "topic_key": topic_key,
                "title": title,
                "fact_summary": fact_summary,
                "main_claims": main_claims,
                "counterclaims": counterclaims,
                "threads_matches": threads_matches,
                "confidence": round(confidence, 4),
                "source_family_count": len(source_families),
                "evidence_count": evidence_count,
                "change_status": change_status,
                "post_eligible": post_eligible,
                "decision_reason": decision_reason,
                "generated_at": now.isoformat(),
                # A generated news row may lose a normal posting competition.
                # Keep offering the integrated candidate until it is actually
                # published; all standard cooldown and daily-limit gates still
                # run on every attempt.
                "already_linked": bool(topic_row["x_post_id"]),
                "source_item": item,
            })
        eligible_count = sum(row["post_eligible"] for row in saved)
        conn.execute(
            """UPDATE integrated_research_runs SET
               source_family_count=?,topic_count=?,eligible_topic_count=?,
               status='success' WHERE run_id=?""",
            (
                max((row["source_family_count"] for row in saved), default=2),
                len(saved), eligible_count, run_id,
            ),
        )
        conn.commit()
    maximum = max(0, _int(
        "INTEGRATED_RESEARCH_MAX_POST_CANDIDATES_PER_RUN", 1))
    ranked = sorted(
        (
            row for row in saved
            if row["post_eligible"] and not row["already_linked"]
        ),
        key=lambda row: (
            row["confidence"],
            row["evidence_count"],
            float(row["source_item"].get("xai_attention_score") or 0),
        ),
        reverse=True,
    )
    return [_candidate(row, row["source_item"]) for row in ranked[:maximum]]


def status(path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        counts = {
            name: int(conn.execute(
                f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for name in (
                "integrated_research_runs",
                "integrated_research_topics",
                "integrated_research_evidence",
            )
        }
        latest = conn.execute(
            """SELECT * FROM integrated_research_runs
               ORDER BY generated_at DESC,id DESC LIMIT 1"""
        ).fetchone()
    return {
        "enabled": _bool("INTEGRATED_RESEARCH_ENABLED", "true"),
        "post_enabled": _bool(
            "INTEGRATED_RESEARCH_POST_ENABLED", "true"),
        "counts": counts,
        "latest_run": dict(latest) if latest else None,
    }
