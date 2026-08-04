"""Build auditable cross-source research records and safe post candidates.

Official/RSS material remains the factual authority. X and Threads observations
are stored as public-reaction signals, never as standalone factual proof.
"""

from __future__ import annotations

import hashlib
import html
import csv
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from metrics_db import (
    apply_additive_migrations,
    apply_threads_full_migrations,
    connect,
    db_path,
)
from publishing_policy import normalize_topic_key

JST = ZoneInfo("Asia/Tokyo")
PROMPT_VERSION = "integrated-research-v2"


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


def _json(value: object, fallback: object) -> object:
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(str(value or ""))
        return parsed if isinstance(parsed, type(fallback)) else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _canonical_url(value: object) -> str:
    """Normalize a source URL without retaining common tracking parameters."""
    raw = str(value or "").strip()
    if not raw.startswith(("https://", "http://")):
        return raw
    parts = urlsplit(raw)
    blocked = {
        "fbclid", "gclid", "ref", "source",
        "utm_campaign", "utm_content", "utm_medium", "utm_source", "utm_term",
    }
    query = urlencode(sorted(
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in blocked
    ))
    return urlunsplit((
        parts.scheme.lower(), parts.netloc.lower(),
        parts.path.rstrip("/") or "/", query, "",
    ))


def _content_hash(*values: object) -> str:
    normalized = "|".join(_clean(value, 2000).lower() for value in values)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _claim_classification(
    fact_summary: str,
    main_claims: list[str],
    counterclaims: list[str],
) -> dict:
    speculation_terms = ("かもしれ", "可能性", "との見方", "憶測", "予想", "推測")

    def classify(text: str, authority: str) -> dict:
        kind = (
            "speculation"
            if any(term in text for term in speculation_terms)
            else "fact" if authority == "official_rss"
            else "opinion"
        )
        return {"text": text, "class": kind, "authority": authority}

    return {
        "fact_base": [classify(fact_summary, "official_rss")],
        "main_claims": [
            classify(text, "public_reaction") for text in main_claims
        ],
        "counterclaims": [
            classify(text, "public_reaction") for text in counterclaims
        ],
    }


def _contradictions(main_claims: list[str],
                    counterclaims: list[str]) -> list[dict]:
    """Separate normal disagreement from explicit factual contradiction."""
    denial_terms = ("誤り", "事実ではない", "否定", "撤回", "訂正", "虚偽", "デマ")
    results = []
    for left in main_claims:
        for right in counterclaims:
            explicit = any(term in f"{left} {right}" for term in denial_terms)
            similarity = SequenceMatcher(None, left, right).ratio()
            relation = (
                "source_agreement" if similarity >= 0.72
                else "factual_contradiction" if explicit
                else "policy_disagreement"
            )
            results.append({
                "claim": left,
                "counterclaim": right,
                "type": relation,
                "similarity": round(similarity, 3),
                "requires_primary_source_check": (
                    relation == "factual_contradiction"),
            })
    return results[:10]


def _anger_summary(claims: list[str]) -> str:
    terms = ("怒", "不満", "負担", "納得", "説明責任", "不公平", "困窮", "反発")
    matched = [claim for claim in claims if any(term in claim for term in terms)]
    if not matched:
        return "収集標本内で明確な怒り・負担表現は限定的です。"
    return _clean(
        "収集標本では、負担・公平性・説明責任への不満が確認されました: "
        + " / ".join(matched[:3]),
        700,
    )


def _posting_value(
    confidence: float,
    source_family_count: int,
    evidence_count: int,
    change_status: str,
    has_counterclaims: bool,
) -> float:
    score = (
        min(4.0, max(0.0, confidence) * 4.0)
        + min(2.0, source_family_count * 0.75)
        + min(1.5, evidence_count * 0.3)
        + (1.5 if change_status in {"new", "changed"} else 0.0)
        + (1.0 if has_counterclaims else 0.0)
    )
    return round(min(10.0, score), 2)


def _change_summary(previous: dict, fact_summary: str,
                    main_claims: list[str], status_name: str) -> str:
    if not previous:
        return "初回観測"
    if status_name == "unchanged":
        return "前回観測から重要な事実・論点の変化なし"
    old_fact = _clean(previous.get("fact_summary"), 240)
    new_fact = _clean(fact_summary, 240)
    if old_fact != new_fact:
        return _clean(f"事実要約更新: {old_fact} → {new_fact}", 600)
    return _clean(
        "社会反応の論点更新: " + " / ".join(main_claims[:3]), 600)


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
            10.0,
            max(
                float(row.get("posting_value_score") or 0),
                7.0 + float(item.get("xai_discovery_bonus") or 0),
            ),
        ),
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
            "posting_value_score": row.get("posting_value_score", 0),
            "anger_summary": row.get("anger_summary", ""),
            "contradictions": row.get("contradictions", []),
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
            change_summary = _change_summary(
                previous, fact_summary, main_claims, change_status)
            claim_classes = _claim_classification(
                fact_summary, main_claims, counterclaims)
            contradictions = _contradictions(main_claims, counterclaims)
            anger_summary = _anger_summary(
                [*main_claims, *counterclaims])
            posting_value = _posting_value(
                confidence, len(source_families), evidence_count,
                change_status, bool(counterclaims))
            missing_sources = [
                name for name, state in provider_status.items()
                if state not in {"available", "checked"}
            ]
            discovered_at = str(item.get("xai_discovered_at") or "")
            cache_ttl = max(
                1, _int("X_SEARCH_CACHE_TTL_MINUTES", 360))
            cache_status = "fresh"
            if discovered_at:
                try:
                    discovered = datetime.fromisoformat(
                        discovered_at.replace("Z", "+00:00"))
                    if discovered.tzinfo is None:
                        discovered = discovered.replace(tzinfo=JST)
                    cache_status = (
                        "stale" if now - discovered > timedelta(
                            minutes=cache_ttl) else "fresh"
                    )
                except ValueError:
                    cache_status = "unknown"
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
            if posting_value < _float(
                    "INTEGRATED_RESEARCH_MIN_POSTING_VALUE_SCORE", 6.0):
                reasons.append("low_posting_value")
            if cache_status == "stale":
                reasons.append("stale_social_signal")
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
                    post_eligible,decision_reason,previous_topic_id,
                    change_summary,claim_classification_json,
                    contradictions_json,anger_summary,posting_value_score,
                    missing_sources_json,cache_status,correction_status,
                    deleted_source_count,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    previous_topic_id=excluded.previous_topic_id,
                    change_summary=excluded.change_summary,
                    claim_classification_json=excluded.claim_classification_json,
                    contradictions_json=excluded.contradictions_json,
                    anger_summary=excluded.anger_summary,
                    posting_value_score=excluded.posting_value_score,
                    missing_sources_json=excluded.missing_sources_json,
                    cache_status=excluded.cache_status,
                    updated_at=excluded.updated_at""",
                (
                    run_id, topic_key, title, fact_summary,
                    json.dumps(main_claims, ensure_ascii=False),
                    json.dumps(counterclaims, ensure_ascii=False),
                    reaction_summary, confidence, len(source_families),
                    evidence_count, change_status, int(post_eligible),
                    decision_reason, previous.get("id"),
                    change_summary,
                    json.dumps(claim_classes, ensure_ascii=False),
                    json.dumps(contradictions, ensure_ascii=False),
                    anger_summary, posting_value,
                    json.dumps(missing_sources, ensure_ascii=False),
                    cache_status, "current", 0,
                    now.isoformat(), now.isoformat(),
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
                canonical = _canonical_url(source["source_url"])
                digest = _content_hash(
                    source["provider"], source["source_id"],
                    source["title"], source["summary"])
                conn.execute(
                    """INSERT OR IGNORE INTO integrated_research_evidence
                       (topic_id,provider,evidence_type,source_id,source_url,
                        title,summary,reliability,observed_at,canonical_url,
                        content_hash,freshness_status,is_deleted,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        topic_id, source["provider"],
                        source["evidence_type"], source["source_id"],
                        source["source_url"], source["title"],
                        source["summary"], source["reliability"],
                        source["observed_at"], canonical, digest,
                        cache_status if source["provider"] != "official_rss"
                        else "authoritative", 0, now.isoformat(),
                    ),
                )
            conn.execute(
                """INSERT OR IGNORE INTO integrated_research_decisions
                   (topic_id,run_id,stage,decision,reason,scores_json,actor,
                    decided_at) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    topic_id, run_id, "candidate_gate",
                    "eligible" if post_eligible else "skip",
                    decision_reason,
                    json.dumps({
                        "confidence": round(confidence, 4),
                        "posting_value_score": posting_value,
                        "source_family_count": len(source_families),
                        "evidence_count": evidence_count,
                    }, ensure_ascii=False),
                    "integrated_research_v2", now.isoformat(),
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
                "posting_value_score": posting_value,
                "anger_summary": anger_summary,
                "contradictions": contradictions,
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
    if saved and _bool("INTEGRATED_RESEARCH_DISCORD_ENABLED", "true"):
        try:
            with closing(connect(path)) as conn:
                notification_row = conn.execute(
                    """SELECT discord_notified_at FROM integrated_research_runs
                       WHERE run_id=?""",
                    (run_id,),
                ).fetchone()
            if notification_row and not notification_row["discord_notified_at"]:
                from discord_notify import notify_integrated_research
                sent = notify_integrated_research({
                    "run_id": run_id,
                    "topic_count": len(saved),
                    "eligible_count": sum(
                        bool(row["post_eligible"]) for row in saved),
                    "skipped_count": sum(
                        not row["post_eligible"] for row in saved),
                    "topics": saved,
                })
                if sent:
                    with closing(connect(path)) as conn:
                        conn.execute(
                            """UPDATE integrated_research_runs
                               SET discord_notified_at=? WHERE run_id=?""",
                            (datetime.now(JST).isoformat(), run_id),
                        )
                        conn.commit()
        except Exception:
            # Research persistence and posting decisions must not fail because
            # an operational notification endpoint is unavailable.
            pass
    if saved and _bool("INTEGRATED_RESEARCH_DASHBOARD_ENABLED", "true"):
        try:
            render_dashboard(days=30, path=path)
        except Exception:
            # Visualization is derived output and must never stop research.
            pass
    maximum = max(0, _int(
        "INTEGRATED_RESEARCH_MAX_POST_CANDIDATES_PER_RUN", 1))
    ranked = sorted(
        (
            row for row in saved
            if row["post_eligible"] and not row["already_linked"]
        ),
        key=lambda row: (
            row["posting_value_score"],
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
                "integrated_research_decisions",
                "integrated_research_corrections",
                "integrated_research_audits",
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


def history(topic_key: str = "", limit: int = 20,
            path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    where = "WHERE t.topic_key=?" if topic_key else ""
    params: tuple = (topic_key, max(1, min(200, limit))) if topic_key else (
        max(1, min(200, limit)),)
    with closing(connect(path)) as conn:
        rows = [
            dict(row) for row in conn.execute(
                f"""SELECT t.*,COUNT(e.id) AS stored_evidence_count
                    FROM integrated_research_topics t
                    LEFT JOIN integrated_research_evidence e
                      ON e.topic_id=t.id
                    {where}
                    GROUP BY t.id ORDER BY t.created_at DESC,t.id DESC
                    LIMIT ?""",
                params,
            )
        ]
        for row in rows:
            row["decisions"] = [
                dict(item) for item in conn.execute(
                    """SELECT stage,decision,reason,scores_json,actor,decided_at
                       FROM integrated_research_decisions
                       WHERE topic_id=? ORDER BY decided_at,id""",
                    (row["id"],),
                )
            ]
    return {"topic_key": topic_key, "count": len(rows), "topics": rows}


def outcomes(days: int = 30, path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    since = (datetime.now(JST) - timedelta(days=max(1, days))).isoformat()
    with closing(connect(path)) as conn:
        rows = [
            dict(row) for row in conn.execute(
                """SELECT t.id,t.topic_key,t.title,t.posting_value_score,
                          t.confidence,t.x_post_id,t.threads_post_id,t.created_at,
                          COALESCE(MAX(pm.impressions),0) AS x_impressions,
                          COALESCE(MAX(pm.likes),0) AS x_likes,
                          COALESCE(MAX(pm.reposts),0) AS x_reposts,
                          COALESCE(MAX(tm.views),0) AS threads_views,
                          COALESCE(MAX(tm.likes),0) AS threads_likes,
                          COALESCE(MAX(tm.reposts),0) AS threads_reposts
                   FROM integrated_research_topics t
                   LEFT JOIN post_metrics pm ON pm.tweet_id=t.x_post_id
                   LEFT JOIN threads_metrics tm
                     ON tm.threads_post_id=t.threads_post_id
                   WHERE t.created_at>=?
                   GROUP BY t.id ORDER BY t.created_at DESC""",
                (since,),
            )
        ]
    return {
        "days": max(1, days),
        "topics": len(rows),
        "published_topics": sum(
            bool(row["x_post_id"] or row["threads_post_id"]) for row in rows),
        "x_impressions": sum(int(row["x_impressions"] or 0) for row in rows),
        "threads_views": sum(int(row["threads_views"] or 0) for row in rows),
        "rows": rows,
    }


def source_contribution(days: int = 30,
                        path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    since = (datetime.now(JST) - timedelta(days=max(1, days))).isoformat()
    with closing(connect(path)) as conn:
        providers = [
            dict(row) for row in conn.execute(
                """SELECT e.provider,COUNT(*) AS evidence_count,
                          COUNT(DISTINCT e.topic_id) AS topic_count,
                          SUM(CASE WHEN e.is_deleted=1 THEN 1 ELSE 0 END)
                              AS deleted_count,
                          AVG(e.reliability) AS average_reliability
                   FROM integrated_research_evidence e
                   JOIN integrated_research_topics t ON t.id=e.topic_id
                   WHERE t.created_at>=?
                   GROUP BY e.provider ORDER BY evidence_count DESC""",
                (since,),
            )
        ]
        eligible_topics = int(conn.execute(
            """SELECT COUNT(*) FROM integrated_research_topics
               WHERE created_at>=? AND post_eligible=1""",
            (since,),
        ).fetchone()[0])
    result = outcomes(days, path)
    return {
        "days": max(1, days),
        "providers": providers,
        "eligible_topics": eligible_topics,
        "published_topics": result["published_topics"],
        "x_impressions": result["x_impressions"],
        "threads_views": result["threads_views"],
    }


def daily_review_summary(days: int = 1,
                         path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    since = (datetime.now(JST) - timedelta(days=max(1, days))).isoformat()
    with closing(connect(path)) as conn:
        aggregate = dict(conn.execute(
            """SELECT COUNT(*) AS topics,
                      SUM(CASE WHEN post_eligible=1 THEN 1 ELSE 0 END)
                          AS eligible_topics,
                      SUM(CASE WHEN x_post_id IS NOT NULL AND x_post_id<>''
                               THEN 1 ELSE 0 END) AS x_posts,
                      SUM(CASE WHEN threads_post_id IS NOT NULL
                                    AND threads_post_id<>''
                               THEN 1 ELSE 0 END) AS threads_posts,
                      AVG(confidence) AS average_confidence,
                      AVG(posting_value_score) AS average_posting_value
               FROM integrated_research_topics WHERE created_at>=?""",
            (since,),
        ).fetchone())
        decisions = [
            dict(row) for row in conn.execute(
                """SELECT decision,reason,COUNT(*) AS count
                   FROM integrated_research_decisions
                   WHERE decided_at>=?
                   GROUP BY decision,reason ORDER BY count DESC LIMIT 10""",
                (since,),
            )
        ]
    result = outcomes(days, path)
    return {
        "window_days": max(1, days),
        **aggregate,
        "decisions": decisions,
        "outcomes": {
            "x_impressions": result["x_impressions"],
            "threads_views": result["threads_views"],
        },
        "top_topics": result["rows"][:5],
        "interpretation_limit": (
            "検索で取得できた標本の分析であり、世論全体の推計ではありません。"
        ),
    }


def export_results(
    output_format: str = "json",
    days: int = 30,
    output: Path | None = None,
    path: Path | None = None,
) -> dict:
    path = path or db_path()
    data = history(limit=200, path=path)
    since = datetime.now(JST) - timedelta(days=max(1, days))
    rows = [
        row for row in data["topics"]
        if datetime.fromisoformat(row["created_at"]) >= since
    ]
    output_format = output_format.lower()
    if output_format not in {"json", "csv", "markdown"}:
        raise ValueError("format must be json, csv, or markdown")
    default_dir = Path(os.environ.get(
        "INTEGRATED_RESEARCH_EXPORT_DIR",
        str(path.parent.parent / "outputs" / "integrated_research"),
    ))
    suffix = {"json": ".json", "csv": ".csv", "markdown": ".md"}[output_format]
    output = output or default_dir / (
        f"integrated-research-{datetime.now(JST):%Y%m%d-%H%M%S}{suffix}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        output.write_text(json.dumps(
            {"days": days, "topics": rows}, ensure_ascii=False, indent=2
        ), encoding="utf-8")
    elif output_format == "csv":
        fields = [
            "id", "run_id", "topic_key", "title", "fact_summary",
            "confidence", "posting_value_score", "change_status",
            "post_eligible", "decision_reason", "x_post_id",
            "threads_post_id", "created_at",
        ]
        with output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({
                key: row.get(key, "") for key in fields
            } for row in rows)
    else:
        lines = [
            "# 統合リサーチ結果",
            "",
            f"- 対象期間: 直近{max(1, days)}日",
            f"- 件数: {len(rows)}",
            "",
        ]
        for row in rows:
            lines.extend([
                f"## {row['title']}",
                "",
                f"- topic_key: `{row['topic_key']}`",
                f"- 信頼度: {row.get('confidence')}",
                f"- 投稿価値: {row.get('posting_value_score')}",
                f"- 判断: {row.get('decision_reason')}",
                "",
                str(row.get("fact_summary") or ""),
                "",
            ])
        output.write_text("\n".join(lines), encoding="utf-8")
    return {"format": output_format, "rows": len(rows), "output": str(output)}


def render_dashboard(days: int = 30, output: Path | None = None,
                     path: Path | None = None) -> dict:
    path = path or db_path()
    report = daily_review_summary(days, path)
    rows = outcomes(days, path)["rows"]
    output = output or Path(os.environ.get(
        "INTEGRATED_RESEARCH_EXPORT_DIR",
        str(path.parent.parent / "outputs" / "integrated_research"),
    )) / "dashboard.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    cards = "".join(
        f"<article><h2>{html.escape(str(row['title']))}</h2>"
        f"<p>{html.escape(str(row.get('fact_summary') or ''))}</p>"
        f"<dl><dt>信頼度</dt><dd>{row.get('confidence') or 0:.2f}</dd>"
        f"<dt>投稿価値</dt><dd>{row.get('posting_value_score') or 0:.2f}</dd>"
        f"<dt>X表示</dt><dd>{int(row.get('x_impressions') or 0):,}</dd>"
        f"<dt>Threads表示</dt><dd>{int(row.get('threads_views') or 0):,}</dd>"
        f"</dl></article>" for row in rows[:50]
    ) or "<article><p>統合リサーチ結果はまだありません。</p></article>"
    document = f"""<!doctype html>
<html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>統合リサーチ・ダッシュボード</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f4f6f8;color:#17202a;margin:0}}
main{{max-width:1100px;margin:auto;padding:28px}} header,article{{background:white;border-radius:14px;
padding:20px;margin:14px 0;box-shadow:0 2px 12px #0001}} .grid{{display:grid;
grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}} dl{{display:grid;
grid-template-columns:1fr 1fr}} dt,dd{{padding:5px;margin:0}} small{{color:#59636e}}
</style><main><header><h1>🔎 統合リサーチ</h1>
<p>直近{max(1, days)}日・{int(report.get('topics') or 0)}テーマ</p>
<small>検索標本の相対分析です。世論全体を表すものではありません。</small>
</header><section class="grid">{cards}</section></main></html>"""
    output.write_text(document, encoding="utf-8")
    return {"output": str(output), "topics": len(rows), "days": max(1, days)}


def audit(output: Path | None = None, path: Path | None = None,
          persist: bool = True) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        checks = {
            "orphan_evidence": conn.execute(
                """SELECT COUNT(*) FROM integrated_research_evidence e
                   LEFT JOIN integrated_research_topics t ON t.id=e.topic_id
                   WHERE t.id IS NULL""").fetchone()[0],
            "invalid_confidence": conn.execute(
                """SELECT COUNT(*) FROM integrated_research_topics
                   WHERE confidence<0 OR confidence>1""").fetchone()[0],
            "missing_fact_base": conn.execute(
                """SELECT COUNT(*) FROM integrated_research_topics t
                   WHERE t.change_status<>'historical' AND NOT EXISTS (
                     SELECT 1 FROM integrated_research_evidence e
                     WHERE e.topic_id=t.id AND e.evidence_type='fact_base'
                       AND e.is_deleted=0)""").fetchone()[0],
            "duplicate_canonical_urls": conn.execute(
                """SELECT COUNT(*) FROM (
                     SELECT topic_id,canonical_url,COUNT(*) AS n
                     FROM integrated_research_evidence
                     WHERE canonical_url IS NOT NULL AND canonical_url<>''
                     GROUP BY topic_id,canonical_url HAVING n>1)""").fetchone()[0],
            "missing_decisions": conn.execute(
                """SELECT COUNT(*) FROM integrated_research_topics t
                   WHERE NOT EXISTS (
                     SELECT 1 FROM integrated_research_decisions d
                     WHERE d.topic_id=t.id)""").fetchone()[0],
            "deleted_sources": conn.execute(
                """SELECT COUNT(*) FROM integrated_research_evidence
                   WHERE is_deleted=1""").fetchone()[0],
        }
        status_name = (
            "ok" if integrity == "ok" and not any(
                checks[key] for key in (
                    "orphan_evidence", "invalid_confidence",
                    "missing_fact_base", "duplicate_canonical_urls",
                )
            ) else "warning"
        )
        report = {
            "audited_at": datetime.now(JST).isoformat(),
            "status": status_name,
            "integrity_check": integrity,
            "checks": checks,
        }
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                "# 統合リサーチ監査\n\n"
                + "\n".join(f"- {key}: {value}" for key, value in report.items()),
                encoding="utf-8",
            )
        if persist:
            conn.execute(
                """INSERT INTO integrated_research_audits
                   (audited_at,status,summary_json,report_path,created_at)
                   VALUES(?,?,?,?,?)""",
                (
                    report["audited_at"], status_name,
                    json.dumps(report, ensure_ascii=False),
                    str(output or ""), report["audited_at"],
                ),
            )
            conn.commit()
    return report


def backfill(limit: int = 500, apply: bool = False,
             path: Path | None = None) -> dict:
    """Import available xAI, native X, and Threads history as audit-only data."""
    path = path or db_path()
    apply_additive_migrations(path)
    maximum = max(1, min(5000, limit))
    native_rows: list[dict] = []
    for history_file in sorted(
            (path.parent / "x_search_history").glob("*.jsonl"),
            reverse=True):
        try:
            for line in history_file.read_text(encoding="utf-8").splitlines():
                payload = json.loads(line)
                for topic in payload.get("topics") or []:
                    native_rows.append({
                        "run_id": "native-x-" + _content_hash(
                            payload.get("generated_at"), history_file.name)[:20],
                        "topic_key": topic.get("topic_key") or topic.get("title"),
                        "summary": topic.get("summary") or "X API検索の過去観測",
                        "source_id": str(
                            topic.get("post_id") or topic.get("tweet_id") or ""),
                        "created_at": payload.get("generated_at"),
                    })
                    if len(native_rows) >= maximum:
                        break
                if len(native_rows) >= maximum:
                    break
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if len(native_rows) >= maximum:
            break
    with closing(connect(path)) as conn:
        before_topic_count = int(conn.execute(
            "SELECT COUNT(*) FROM integrated_research_topics"
        ).fetchone()[0])
        rows = [
            dict(row) for row in conn.execute(
                """SELECT x.*,r.completed_at
                   FROM xai_discovery_topics x
                   LEFT JOIN xai_discovery_runs r ON r.run_id=x.run_id
                   WHERE NOT EXISTS (
                     SELECT 1 FROM integrated_research_runs i
                     WHERE i.xai_run_id=x.run_id)
                   ORDER BY x.id DESC LIMIT ?""",
                (maximum,),
            )
        ]
        try:
            thread_rows = [
                dict(row) for row in conn.execute(
                    """SELECT threads_post_id,text,permalink,last_seen_at
                       FROM threads_search_results
                       ORDER BY last_seen_at DESC LIMIT ?""",
                    (maximum,),
                )
            ]
        except sqlite3.Error:
            thread_rows = []
        if apply:
            for row in rows:
                created = row.get("completed_at") or row.get(
                    "created_at") or datetime.now(JST).isoformat()
                run_id = f"backfill-{row['run_id']}"
                conn.execute(
                    """INSERT OR IGNORE INTO integrated_research_runs
                       (run_id,xai_run_id,generated_at,provider_status_json,
                        source_family_count,topic_count,eligible_topic_count,
                        status,created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, row["run_id"], created,
                        json.dumps({
                            "xai_x_search": "historical",
                            "official_rss": "not_reconstructable",
                            "threads_search": "not_reconstructable",
                        }, ensure_ascii=False),
                        1, 1, 0, "historical_backfill", created,
                    ),
                )
                topic_key = normalize_topic_key(
                    str(row.get("topic_key") or row.get("content_id") or ""))
                conn.execute(
                    """INSERT OR IGNORE INTO integrated_research_topics
                       (run_id,topic_key,title,fact_summary,main_claims_json,
                        counterclaims_json,reaction_summary,confidence,
                        source_family_count,evidence_count,change_status,
                        post_eligible,decision_reason,posting_value_score,
                        missing_sources_json,cache_status,correction_status,
                        deleted_source_count,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, topic_key, topic_key,
                        "過去の一次資料は完全には再構築できません。",
                        row.get("stance_summary_json") or "[]",
                        row.get("counterargument_summary_json") or "[]",
                        "xAIの過去観測記録", row.get("search_confidence") or 0,
                        1, row.get("evidence_count") or 0, "historical",
                        0, "historical_backfill_no_auto_post", 0,
                        json.dumps(
                            ["official_rss", "threads_search"],
                            ensure_ascii=False),
                        "historical_unknown", "current", 0, created, created,
                    ),
                )
                topic = conn.execute(
                    """SELECT id FROM integrated_research_topics
                       WHERE run_id=? AND topic_key=?""",
                    (run_id, topic_key),
                ).fetchone()
                if topic:
                    for post_id in _json(
                            row.get("representative_post_ids_json"), []):
                        conn.execute(
                            """INSERT OR IGNORE INTO integrated_research_evidence
                               (topic_id,provider,evidence_type,source_id,
                                source_url,title,summary,reliability,observed_at,
                                canonical_url,content_hash,freshness_status,
                                is_deleted,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                topic["id"], "xai_x_search",
                                "historical_public_post", str(post_id),
                                f"https://x.com/i/web/status/{post_id}",
                                topic_key, "過去xAI観測", row.get(
                                    "search_confidence") or 0,
                                created,
                                f"https://x.com/i/web/status/{post_id}",
                                _content_hash("xai", post_id),
                                "historical_unknown", 0, created,
                            ),
                        )
                    conn.execute(
                        """INSERT OR IGNORE INTO integrated_research_decisions
                           (topic_id,run_id,stage,decision,reason,scores_json,
                            actor,decided_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            topic["id"], run_id, "historical_backfill", "skip",
                            "historical_backfill_no_auto_post", "{}",
                            "migration", created,
                        ),
                    )
            historical_sources = [
                *[{
                    "provider": "native_x_search",
                    "run_id": row["run_id"],
                    "topic_key": row["topic_key"],
                    "summary": row["summary"],
                    "source_id": row["source_id"],
                    "source_url": (
                        f"https://x.com/i/web/status/{row['source_id']}"
                        if row["source_id"] else ""),
                    "created_at": row["created_at"],
                } for row in native_rows],
                *[{
                    "provider": "threads_search",
                    "run_id": "threads-" + _content_hash(
                        row.get("threads_post_id"))[:20],
                    "topic_key": _clean(row.get("text"), 80),
                    "summary": _clean(row.get("text"), 600),
                    "source_id": row.get("threads_post_id"),
                    "source_url": row.get("permalink"),
                    "created_at": row.get("last_seen_at"),
                } for row in thread_rows],
            ][:maximum]
            for source in historical_sources:
                created = source.get("created_at") or datetime.now(
                    JST).isoformat()
                run_id = f"backfill-{source['run_id']}"
                topic_key = normalize_topic_key(
                    str(source.get("topic_key") or source.get("source_id") or ""))
                if not topic_key:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO integrated_research_runs
                       (run_id,generated_at,provider_status_json,
                        source_family_count,topic_count,eligible_topic_count,
                        status,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        run_id, created,
                        json.dumps({
                            source["provider"]: "historical",
                            "official_rss": "not_reconstructable",
                        }, ensure_ascii=False),
                        1, 1, 0, "historical_backfill", created,
                    ),
                )
                conn.execute(
                    """INSERT OR IGNORE INTO integrated_research_topics
                       (run_id,topic_key,title,fact_summary,main_claims_json,
                        counterclaims_json,reaction_summary,confidence,
                        source_family_count,evidence_count,change_status,
                        post_eligible,decision_reason,posting_value_score,
                        missing_sources_json,cache_status,correction_status,
                        deleted_source_count,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, topic_key, _clean(source["topic_key"], 180),
                        "過去の一次資料は完全には再構築できません。",
                        json.dumps([source["summary"]], ensure_ascii=False),
                        "[]", source["summary"], 0.4, 1, 1, "historical", 0,
                        "historical_backfill_no_auto_post", 0,
                        json.dumps(["official_rss"], ensure_ascii=False),
                        "historical_unknown", "current", 0, created, created,
                    ),
                )
                topic = conn.execute(
                    """SELECT id FROM integrated_research_topics
                       WHERE run_id=? AND topic_key=?""",
                    (run_id, topic_key),
                ).fetchone()
                if topic:
                    source_url = _canonical_url(source["source_url"])
                    conn.execute(
                        """INSERT OR IGNORE INTO integrated_research_evidence
                           (topic_id,provider,evidence_type,source_id,source_url,
                            title,summary,reliability,observed_at,canonical_url,
                            content_hash,freshness_status,is_deleted,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            topic["id"], source["provider"],
                            "historical_public_post", source["source_id"],
                            source["source_url"], topic_key, source["summary"],
                            0.4, created, source_url,
                            _content_hash(
                                source["provider"], source["source_id"],
                                source["summary"]),
                            "historical_unknown", 0, created,
                        ),
                    )
                    conn.execute(
                        """INSERT OR IGNORE INTO integrated_research_decisions
                           (topic_id,run_id,stage,decision,reason,scores_json,
                            actor,decided_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            topic["id"], run_id, "historical_backfill", "skip",
                            "historical_backfill_no_auto_post", "{}",
                            "migration", created,
                        ),
                    )
            conn.commit()
        after_topic_count = int(conn.execute(
            "SELECT COUNT(*) FROM integrated_research_topics"
        ).fetchone()[0])
    return {
        "apply": apply,
        "available": {
            "xai_topics": len(rows),
            "native_x_topics": len(native_rows),
            "threads_posts": len(thread_rows),
        },
        "eligible_historical_topics": (
            len(rows) + len(native_rows) + len(thread_rows)),
        "imported": max(0, after_topic_count - before_topic_count)
        if apply else 0,
        "external_posts": 0,
        "limitation": "削除済み投稿と未保存の一次資料は復元できません。",
    }


def retention(days: int | None = None, apply: bool = False,
              path: Path | None = None) -> dict:
    """Redact old raw social evidence while retaining hashes and audit links."""
    path = path or db_path()
    apply_additive_migrations(path)
    days = max(30, days or _int(
        "INTEGRATED_RESEARCH_EVIDENCE_RETENTION_DAYS", 180))
    cutoff = (datetime.now(JST) - timedelta(days=days)).isoformat()
    with closing(connect(path)) as conn:
        count = int(conn.execute(
            """SELECT COUNT(*) FROM integrated_research_evidence
               WHERE provider IN ('xai_x_search','native_x_search',
                                  'threads_search')
                 AND created_at<? AND is_deleted=0""",
            (cutoff,),
        ).fetchone()[0])
        if apply:
            conn.execute(
                """UPDATE integrated_research_evidence
                   SET summary='[retention-redacted]',source_url='',
                       is_deleted=1,deleted_at=?
                   WHERE provider IN ('xai_x_search','native_x_search',
                                      'threads_search')
                     AND created_at<? AND is_deleted=0""",
                (datetime.now(JST).isoformat(), cutoff),
            )
            conn.execute(
                """UPDATE integrated_research_topics SET
                   deleted_source_count=(
                     SELECT COUNT(*) FROM integrated_research_evidence e
                     WHERE e.topic_id=integrated_research_topics.id
                       AND e.is_deleted=1)"""
            )
            conn.commit()
    return {
        "apply": apply, "retention_days": days,
        "eligible_for_redaction": count,
        "redacted": count if apply else 0,
        "audit_records_deleted": 0,
    }


def mark_source_deleted(provider: str, source_id: str, apply: bool = False,
                        path: Path | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    now = datetime.now(JST).isoformat()
    with closing(connect(path)) as conn:
        rows = conn.execute(
            """SELECT id,topic_id,evidence_type FROM integrated_research_evidence
               WHERE provider=? AND source_id=? AND is_deleted=0""",
            (provider, source_id),
        ).fetchall()
        if apply and rows:
            conn.execute(
                """UPDATE integrated_research_evidence
                   SET is_deleted=1,deleted_at=?,summary='[source-deleted]',
                       source_url='' WHERE provider=? AND source_id=?""",
                (now, provider, source_id),
            )
            topic_ids = sorted({int(row["topic_id"]) for row in rows})
            for topic_id in topic_ids:
                official_deleted = any(
                    row["evidence_type"] == "fact_base"
                    and int(row["topic_id"]) == topic_id for row in rows)
                conn.execute(
                    """UPDATE integrated_research_topics SET
                       deleted_source_count=deleted_source_count+?,
                       post_eligible=CASE WHEN ? THEN 0 ELSE post_eligible END,
                       decision_reason=CASE WHEN ? THEN
                         'deleted_fact_base_requires_reverification'
                         ELSE decision_reason END,updated_at=? WHERE id=?""",
                    (
                        sum(int(row["topic_id"]) == topic_id for row in rows),
                        official_deleted, official_deleted, now, topic_id,
                    ),
                )
            conn.commit()
    return {
        "provider": provider, "source_id": source_id, "matches": len(rows),
        "apply": apply, "marked_deleted": len(rows) if apply else 0,
    }


def record_correction(
    topic_id: int,
    fact_summary: str,
    reason: str,
    apply: bool = False,
    path: Path | None = None,
) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    now = datetime.now(JST).isoformat()
    with closing(connect(path)) as conn:
        row = conn.execute(
            "SELECT * FROM integrated_research_topics WHERE id=?",
            (topic_id,),
        ).fetchone()
        if not row:
            return {"found": False, "topic_id": topic_id, "apply": apply}
        previous = {"fact_summary": row["fact_summary"]}
        corrected = {"fact_summary": _clean(fact_summary, 1000)}
        status_name = "applied" if apply else "preview"
        conn.execute(
            """INSERT INTO integrated_research_corrections
               (topic_id,correction_type,previous_json,corrected_json,reason,
                detected_at,applied_at,status) VALUES(?,?,?,?,?,?,?,?)""",
            (
                topic_id, "fact_summary",
                json.dumps(previous, ensure_ascii=False),
                json.dumps(corrected, ensure_ascii=False), _clean(reason, 500),
                now, now if apply else None, status_name,
            ),
        )
        if apply:
            conn.execute(
                """UPDATE integrated_research_topics SET fact_summary=?,
                   correction_status='corrected',post_eligible=0,
                   decision_reason='correction_requires_reverification',
                   updated_at=? WHERE id=?""",
                (corrected["fact_summary"], now, topic_id),
            )
        conn.commit()
    return {
        "found": True, "topic_id": topic_id, "apply": apply,
        "status": status_name, "previous": previous, "corrected": corrected,
    }


def validate_backup_restore(path: Path | None = None) -> dict:
    """Verify an online SQLite backup by restoring it into a temporary DB."""
    path = path or db_path()
    apply_additive_migrations(path)
    with tempfile.TemporaryDirectory(prefix="integrated-research-restore-") as tmp:
        target = Path(tmp) / "restored.db"
        with closing(connect(path)) as source, closing(
                sqlite3.connect(target)) as restored:
            source.backup(restored)
        with closing(sqlite3.connect(target)) as restored:
            integrity = str(restored.execute(
                "PRAGMA integrity_check").fetchone()[0])
            restored_count = int(restored.execute(
                "SELECT COUNT(*) FROM integrated_research_topics").fetchone()[0])
        with closing(connect(path)) as source:
            source_count = int(source.execute(
                "SELECT COUNT(*) FROM integrated_research_topics").fetchone()[0])
    return {
        "ok": integrity == "ok" and restored_count == source_count,
        "integrity_check": integrity,
        "source_topics": source_count,
        "restored_topics": restored_count,
        "temporary_copy_removed": True,
    }


def reuse_topic(topic_id: int, path: Path | None = None) -> dict:
    """Create a verified reusable content packet for note/video production."""
    path = path or db_path()
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        topic = conn.execute(
            "SELECT * FROM integrated_research_topics WHERE id=?",
            (topic_id,),
        ).fetchone()
        if not topic:
            return {"found": False, "topic_id": topic_id}
        if topic["content_packet_id"]:
            return {
                "found": True, "created": False, "already_exists": True,
                "topic_id": topic_id,
                "content_packet_id": topic["content_packet_id"],
                "note_ready": True, "video_ready": True,
                "external_posts": 0,
            }
        source = conn.execute(
            """SELECT source_url FROM integrated_research_evidence
               WHERE topic_id=? AND evidence_type='fact_base'
                 AND is_deleted=0 ORDER BY reliability DESC,id LIMIT 1""",
            (topic_id,),
        ).fetchone()
        if not source:
            return {
                "found": True, "created": False,
                "reason": "verified_fact_base_missing",
            }
        cursor = conn.execute(
            """INSERT INTO news_candidates
               (source_type,source_name,source_url,title,summary,published_at,
                fetched_at,topic_key,genre,source_reliability_score,
                freshness_score,final_news_score,verified,
                verification_reason,is_major_update,metadata_json,
                discovered_via_json,xai_topic_match)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "integrated_research", "統合リサーチ", source["source_url"],
                topic["title"], topic["fact_summary"], topic["created_at"],
                datetime.now(JST).isoformat(), topic["topic_key"], "politics",
                8.0, 7.0, topic["posting_value_score"] or 7.0, 1,
                "integrated_research_verified_fact_base", 0,
                json.dumps({"integrated_topic_id": topic_id},
                           ensure_ascii=False),
                json.dumps(["integrated_research"], ensure_ascii=False), 1,
            ),
        )
        news_id = int(cursor.lastrowid)
        conn.commit()
    from social_content_factory import generate_packet
    packet = generate_packet(topic_key=topic["topic_key"], persist=True, path=path)
    with closing(connect(path)) as conn:
        conn.execute(
            """UPDATE integrated_research_topics
               SET candidate_news_id=?,content_packet_id=?,updated_at=?
               WHERE id=?""",
            (
                news_id, packet.get("content_id"),
                datetime.now(JST).isoformat(), topic_id,
            ),
        )
        conn.commit()
    return {
        "found": True, "created": True, "topic_id": topic_id,
        "news_candidate_id": news_id,
        "content_packet_id": packet.get("content_id"),
        "note_ready": True, "video_ready": True,
        "external_posts": 0,
    }


def mitigation_report() -> dict:
    return {
        "threads_keyword_search": {
            "constraint": "Metaの権限審査・API応答に依存",
            "mitigation": "未取得を明記し、公式資料+xAIのみで継続。Threads単独で事実認定しない。",
        },
        "deleted_social_posts": {
            "constraint": "削除済みX/Threads本文は復元不可",
            "mitigation": "本文を再取得せず、ID・ハッシュ・削除日時の墓標を保持。",
        },
        "public_sentiment": {
            "constraint": "検索標本から世論全体は推定不可",
            "mitigation": "標本内の相対分析と明記し、賛否・矛盾を分離。",
        },
        "historical_backfill": {
            "constraint": "未保存の一次資料・検索レスポンスは再構築不可",
            "mitigation": "信頼度を下げた監査専用レコードとして取り込み、自動投稿禁止。",
        },
        "note_publish": {
            "constraint": "noteへの公式自動投稿APIを利用していない",
            "mitigation": "ドラフト・画像・根拠・関連書籍を生成し、Discord/Gmail経由で手動確認。",
        },
        "youtube_instagram_publish": {
            "constraint": "有効なOAuth、権限、公開可能な動画が必要",
            "mitigation": "統合テーマをコンテンツパケット化し、資格情報が揃うまで候補生成に限定。",
        },
    }
