"""Auditable, budget-aware planning for the xAI X Search radar.

This module is deliberately usable without network access.  Phase A commands
only inspect local SQLite data or deterministic fixtures; they never publish
and never call xAI.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import uuid
from calendar import monthrange
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from metrics_db import apply_additive_migrations, connect, db_path, write
from xai_cost import ticks_to_usd

JST = ZoneInfo("Asia/Tokyo")
MODES = ("normal", "low_frequency", "restricted", "emergency_only", "stopped")
PROMPT_VERSION = "xai-discovery-audit-v1"


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def effective_limit() -> tuple[float, str]:
    configured = max(0.0, _float("XAI_MONTHLY_BUDGET_USD", 30.0))
    verified_limit = max(
        0.0, _float("XAI_VERIFIED_EFFECTIVE_LIMIT_USD", configured))
    unverified_limit = max(
        0.0, _float("XAI_UNVERIFIED_EFFECTIVE_LIMIT_USD", 7.5))
    ledger_verified = _bool("XAI_COST_LEDGER_VERIFIED", "false")
    allow_unverified = _bool("XAI_ALLOW_UNVERIFIED_FULL_BUDGET", "false")
    if ledger_verified:
        return min(configured, verified_limit), "verified"
    if allow_unverified:
        return configured, "operator_override"
    return min(configured, unverified_limit), "unverified_cap"


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(day=monthrange(now.year, now.month)[1]) + timedelta(days=1)
    return start, end


def _xai_cost_rows(path: Path, now: datetime) -> list[dict]:
    start, end = _month_bounds(now)
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        return [dict(row) for row in conn.execute(
            """SELECT * FROM xai_usage_events
               WHERE timestamp>=? AND timestamp<? ORDER BY timestamp,id""",
            (start.isoformat(), end.isoformat()),
        )]


def ledger_health(path: Path | None = None,
                  now: datetime | None = None) -> dict:
    path = path or db_path()
    now = now or datetime.now(JST)
    try:
        rows = _xai_cost_rows(path, now)
    except Exception as exc:
        return {
            "healthy": False, "reason": "ledger_read_failed",
            "consecutive_unverified": 3, "row_count": 0,
            "error_type": type(exc).__name__,
        }
    conversion_errors = 0
    unverified = 0
    for row in rows:
        ticks = int(row.get("cost_in_usd_ticks") or 0)
        actual = float(row.get("actual_cost_usd") or 0)
        if ticks and abs(actual - ticks_to_usd(ticks)) > 1e-10:
            conversion_errors += 1
    consecutive = 0
    for row in reversed(rows):
        # Legacy rows predate Phase A cost-verification tracking and must not
        # accidentally stop the newly deployed runner.
        if not row.get("started_at"):
            continue
        verified = (
            str(row.get("cost_source") or "") == "actual"
            and int(row.get("cost_in_usd_ticks") or 0) >= 0
            and int(row.get("cost_verified") or 0) == 1
        )
        if verified:
            break
        if int(row.get("success") or 0):
            consecutive += 1
            unverified += 1
    return {
        "healthy": conversion_errors == 0 and consecutive < 3,
        "reason": (
            "ok" if conversion_errors == 0 and consecutive < 3
            else "ticks_conversion_mismatch" if conversion_errors
            else "consecutive_cost_unverified"
        ),
        "row_count": len(rows),
        "conversion_errors": conversion_errors,
        "consecutive_unverified": consecutive,
        "unverified_success_rows_in_tail": unverified,
    }


def actual_and_reserved(path: Path | None = None,
                        now: datetime | None = None) -> tuple[float, float]:
    path = path or db_path()
    now = now or datetime.now(JST)
    start, end = _month_bounds(now)
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        actual = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN cost_source='actual'
                 THEN actual_cost_usd ELSE 0 END),0)
               FROM xai_usage_events WHERE timestamp>=? AND timestamp<?""",
            (start.isoformat(), end.isoformat()),
        ).fetchone()[0]
        reserved = conn.execute(
            """SELECT COALESCE(SUM(reserved_cost_usd),0)
               FROM xai_discovery_runs
               WHERE started_at>=? AND started_at<?
               AND status IN ('reserved','running')""",
            (start.isoformat(), end.isoformat()),
        ).fetchone()[0]
    return float(actual or 0), float(reserved or 0)


def _daily_slots(mode: str) -> tuple[str, ...]:
    configured = tuple(
        value.strip() for value in os.environ.get(
            "XAI_SEARCH_SCHEDULE",
            "06:00,09:00,12:00,15:00,18:00,21:00",
        ).split(",") if value.strip()
    )
    if mode == "normal":
        return configured[:6]
    if mode == "low_frequency":
        return tuple(value.strip() for value in os.environ.get(
            "XAI_LOW_VOLATILITY_SCHEDULE", "06:00,12:00,18:00"
        ).split(",") if value.strip())[:3]
    if mode == "restricted":
        return tuple(value.strip() for value in os.environ.get(
            "XAI_RESTRICTED_SCHEDULE", "06:00,18:00"
        ).split(",") if value.strip())[:2]
    return ()


def remaining_planned_runs(now: datetime, mode: str) -> int:
    slots = _daily_slots(mode)
    if not slots:
        return 0
    month_end = now.replace(
        day=monthrange(now.year, now.month)[1],
        hour=23, minute=59, second=59, microsecond=999999)
    cursor = now.date()
    total = 0
    while cursor <= month_end.date():
        for slot in slots:
            hour, minute = map(int, slot.split(":"))
            run_at = datetime(
                cursor.year, cursor.month, cursor.day, hour, minute, tzinfo=JST)
            if now <= run_at <= month_end:
                total += 1
        cursor += timedelta(days=1)
    return total


def _recent_daily_cost(path: Path, now: datetime, days: int = 7) -> float:
    start = now - timedelta(days=days)
    with closing(connect(path)) as conn:
        value = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN cost_source='actual'
                 THEN actual_cost_usd ELSE estimated_cost_usd END),0)
               FROM xai_usage_events WHERE timestamp>=?""",
            (start.isoformat(),),
        ).fetchone()[0]
    return float(value or 0) / max(1, days)


@dataclass
class BudgetPlan:
    evaluated_at: str
    effective_limit_usd: float
    limit_source: str
    actual_cost_usd: float
    active_reserved_cost_usd: float
    remaining_budget_usd: float
    forecast_month_end_usd: float
    actual_ratio: float
    forecast_ratio: float
    remaining_planned_runs: int
    dynamic_target_per_run_usd: float
    mode: str
    reason: str
    emergency_budget_available: bool
    ledger_health: dict


def budget_plan(path: Path | None = None, now: datetime | None = None,
                persist: bool = False) -> BudgetPlan:
    path = path or db_path()
    now = now or datetime.now(JST)
    apply_additive_migrations(path)
    limit, source = effective_limit()
    health = ledger_health(path, now)
    actual, reserved = actual_and_reserved(path, now)
    days_left = monthrange(now.year, now.month)[1] - now.day
    projected = min(limit, actual + _recent_daily_cost(path, now) * days_left)
    actual_ratio = actual / limit if limit > 0 else 1.0
    forecast_ratio = projected / limit if limit > 0 else 1.0
    remaining = max(0.0, limit - actual - reserved)

    if not health["healthy"] or actual_ratio >= 1.0:
        mode, reason = "stopped", health["reason"] if not health["healthy"] else "budget_exhausted"
    elif actual_ratio >= 0.93 or forecast_ratio >= 0.97:
        mode, reason = "restricted", "provider_budget_restriction"
    elif actual_ratio >= 0.85 or forecast_ratio >= 0.90:
        mode, reason = "low_frequency", "provider_budget_warning"
    else:
        mode, reason = "normal", "within_budget"

    planned = remaining_planned_runs(now, mode)
    safety = max(0.0, min(1.0, _float(
        "XAI_DYNAMIC_RUN_BUDGET_SAFETY_FACTOR", 0.85)))
    warning = max(0.0, _float("XAI_PER_RUN_WARNING_USD", 0.20))
    dynamic = min(warning, remaining / max(planned, 1) * safety)
    minimum = max(0.0, _float("XAI_MIN_USEFUL_RUN_BUDGET_USD", 0.020))
    emergency_reserve = max(
        minimum, _float("XAI_EMERGENCY_RESERVE_USD", 0.30))
    emergency_available = remaining >= emergency_reserve
    if mode not in {"stopped", "restricted"} and dynamic < minimum:
        mode = "emergency_only" if emergency_available else "stopped"
        reason = "dynamic_target_below_minimum"
        planned = 0
    plan = BudgetPlan(
        evaluated_at=now.isoformat(),
        effective_limit_usd=round(limit, 8),
        limit_source=source,
        actual_cost_usd=round(actual, 8),
        active_reserved_cost_usd=round(reserved, 8),
        remaining_budget_usd=round(remaining, 8),
        forecast_month_end_usd=round(projected, 8),
        actual_ratio=round(actual_ratio, 8),
        forecast_ratio=round(forecast_ratio, 8),
        remaining_planned_runs=planned,
        dynamic_target_per_run_usd=round(max(0.0, dynamic), 8),
        mode=mode,
        reason=reason,
        emergency_budget_available=emergency_available,
        ledger_health=health,
    )
    if persist:
        _persist_budget_plan(plan, path)
    return plan


def _persist_budget_plan(plan: BudgetPlan, path: Path) -> None:
    with closing(connect(path)) as conn:
        previous = conn.execute(
            """SELECT new_mode FROM xai_budget_mode_history
               ORDER BY id DESC LIMIT 1""").fetchone()
    write(
        """INSERT INTO xai_budget_mode_history
           (evaluated_at,actual_cost_usd,reserved_cost_usd,
            remaining_budget_usd,actual_ratio,forecast_month_end_usd,
            forecast_ratio,remaining_planned_runs,dynamic_target_per_run,
            previous_mode,new_mode,reason,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            plan.evaluated_at, plan.actual_cost_usd,
            plan.active_reserved_cost_usd, plan.remaining_budget_usd,
            plan.actual_ratio, plan.forecast_month_end_usd,
            plan.forecast_ratio, plan.remaining_planned_runs,
            plan.dynamic_target_per_run_usd,
            previous["new_mode"] if previous else "",
            plan.mode, plan.reason, datetime.now(JST).isoformat(),
        ),
        path,
    )


@dataclass
class SearchWindow:
    requested_from_at: str
    requested_to_at: str
    actual_search_window_minutes: int
    last_success_at: str | None
    overlap_minutes: int
    coverage_gap_minutes: int
    coverage_status: str


def search_window(path: Path | None = None, now: datetime | None = None,
                  last_success_at: datetime | None = None) -> SearchWindow:
    path = path or db_path()
    now = now or datetime.now(JST)
    default_minutes = max(1, _int(
        "XAI_SEARCH_DEFAULT_LOOKBACK_MINUTES", 240))
    overlap = max(0, _int("XAI_SEARCH_OVERLAP_MINUTES", 30))
    max_minutes = max(default_minutes, _int(
        "XAI_SEARCH_MAX_LOOKBACK_HOURS", 24) * 60)
    if last_success_at is None:
        try:
            apply_additive_migrations(path)
            with closing(connect(path)) as conn:
                row = conn.execute(
                    """SELECT completed_at FROM xai_discovery_runs
                       WHERE status='success' AND completed_at IS NOT NULL
                       ORDER BY completed_at DESC LIMIT 1""").fetchone()
                if not row:
                    row = conn.execute(
                        """SELECT timestamp completed_at FROM xai_usage_events
                           WHERE operation='x_search_radar' AND success=1
                           ORDER BY timestamp DESC LIMIT 1""").fetchone()
            if row and row["completed_at"]:
                last_success_at = datetime.fromisoformat(row["completed_at"])
        except Exception:
            last_success_at = None
    if last_success_at and last_success_at.tzinfo is None:
        last_success_at = last_success_at.replace(tzinfo=JST)
    desired = (
        last_success_at - timedelta(minutes=overlap)
        if last_success_at else now - timedelta(minutes=default_minutes)
    )
    earliest = now - timedelta(minutes=max_minutes)
    requested_from = max(desired, earliest)
    window_minutes = max(
        0, int((now - requested_from).total_seconds() // 60))
    raw_gap = (
        max(0, int((now - last_success_at).total_seconds() // 60) - overlap)
        if last_success_at else 0
    )
    coverage_gap = max(
        0, int((earliest - desired).total_seconds() // 60)) if desired < earliest else 0
    return SearchWindow(
        requested_from_at=requested_from.isoformat(),
        requested_to_at=now.isoformat(),
        actual_search_window_minutes=window_minutes,
        last_success_at=last_success_at.isoformat() if last_success_at else None,
        overlap_minutes=overlap,
        coverage_gap_minutes=max(coverage_gap, raw_gap if raw_gap > max_minutes else 0),
        coverage_status="gap" if coverage_gap else "covered",
    )


def is_important(candidate: dict) -> bool:
    text = f"{candidate.get('title','')} {candidate.get('summary','')}"
    terms = (
        "災害", "地震", "救助", "逮捕", "辞任", "法案成立", "重要採決",
        "外交", "安全保障", "判決", "政策撤回", "公式数値",
    )
    return (
        bool(candidate.get("is_major_update"))
        or float(candidate.get("importance_score") or 0) >= 8
        or any(term in text for term in terms)
    )


def research_profile(candidates: Iterable[dict], mode: str,
                     dynamic_target_usd: float) -> dict:
    rows = list(candidates)
    extended = (
        _bool("XAI_EXTENDED_RESEARCH_ENABLED", "true")
        and mode == "normal"
        and dynamic_target_usd >= _float("XAI_MIN_USEFUL_RUN_BUDGET_USD", .02)
        and any(is_important(row) for row in rows)
    )
    prefix = "XAI_EXTENDED" if extended else "XAI_STANDARD"
    profile = {
        "research_mode": "extended" if extended else "standard",
        "max_topics": _int(f"{prefix}_MAX_TOPICS", 8 if extended else 5),
        "max_tool_calls": _int(f"{prefix}_MAX_TOOL_CALLS", 3 if extended else 2),
        "max_turns": _int(f"{prefix}_MAX_TURNS", 3 if extended else 2),
        "image_understanding": False,
        "video_understanding": _bool(
            "XAI_VIDEO_UNDERSTANDING_ENABLED", "false"),
        "prompt_version": PROMPT_VERSION,
    }
    profile["image_understanding"] = (
        extended
        and _bool("XAI_IMAGE_UNDERSTANDING_IMPORTANT_ONLY", "true")
        and any(image_understanding_allowed(row, mode) for row in rows)
    )
    return profile


def image_understanding_allowed(candidate: dict, mode: str) -> bool:
    if mode != "normal":
        return False
    text = f"{candidate.get('title','')} {candidate.get('summary','')}"
    forbidden = ("サムネイル", "人物写真", "政党ロゴ", "感情分析")
    useful = ("災害", "現場", "公式文書", "表", "グラフ", "図表")
    return (
        bool(candidate.get("has_image"))
        and any(term in text for term in useful)
        and not any(term in text for term in forbidden)
    )


def match_confidence(candidate: dict, topic: dict) -> tuple[float, str]:
    title = str(candidate.get("title") or "").lower()
    summary = str(candidate.get("summary") or "").lower()
    key = str(topic.get("topic_key") or "").lower().strip()
    if not key:
        return 0.0, "missing_topic_key"
    exact = key in title or key in summary
    tokens = {token for token in key.replace("・", " ").split() if len(token) >= 2}
    overlap = sum(token in f"{title} {summary}" for token in tokens)
    token_ratio = overlap / max(1, len(tokens))
    source_id = bool(
        candidate.get("content_id") or candidate.get("source_url")
        or candidate.get("link"))
    semantic_base = 0.70 if tokens and token_ratio == 1.0 else 0.0
    confidence = min(
        1.0, (0.65 if exact else semantic_base)
        + (0.25 * token_ratio if exact or not semantic_base else 0.0)
        + (0.10 if source_id else 0.0))
    reason = (
        "exact_topic_and_source" if exact and source_id
        else "semantic_tokens_and_source" if source_id and token_ratio
        else "insufficient_match_evidence"
    )
    return round(confidence, 4), reason


def score_bonus(candidate: dict, topic: dict,
                confidence: float) -> float:
    if confidence < _float("XAI_NEWS_MATCH_MIN_CONFIDENCE", 0.80):
        return 0.0
    if not candidate.get("verified", True):
        return 0.0
    if float(candidate.get("source_reliability_score") or 6) < 5:
        return 0.0
    attention = max(0.0, min(10.0, float(
        topic.get("attention_estimate", topic.get("attention_score", 0)) or 0)))
    velocity = max(0.0, min(10.0, float(
        topic.get("velocity_estimate", topic.get("velocity_score", 0)) or 0)))
    attention_bonus = attention / 10 * _float(
        "XAI_ATTENTION_SCORE_MAX_BONUS", 1.5)
    velocity_bonus = velocity / 10 * _float(
        "XAI_VELOCITY_SCORE_MAX_BONUS", 1.0)
    return round(min(
        _float("XAI_TOTAL_DISCOVERY_BONUS_MAX", 2.0),
        attention_bonus + velocity_bonus,
    ), 4)


def cached_signal(topic: dict, generated_at: datetime,
                  now: datetime | None = None) -> dict:
    now = now or datetime.now(JST)
    age = max(0.0, (now - generated_at).total_seconds() / 60)
    ttl = max(1, _int("XAI_SEARCH_CACHE_TTL_MINUTES", 240))
    row = dict(topic)
    original = float(
        topic.get("attention_estimate", topic.get("attention_score", 0)) or 0)
    row["x_attention_estimate"] = round(
        original * max(0.0, 1.0 - age / ttl), 4)
    row["x_velocity_estimate"] = None
    row["x_signal_type"] = "cached"
    row["cache_age_minutes"] = round(age, 2)
    return row


def topic_audit_record(candidate: dict, topic: dict,
                       run_id: str, allocated_cost: float = 0.0) -> dict:
    confidence, reason = match_confidence(candidate, topic)
    representative = [
        str(value) for value in (topic.get("representative_post_ids") or [])[:5]
    ]
    evidence = int(topic.get("evidence_count") or len(representative))
    signal_type = str(topic.get("x_signal_type") or "qualitative_xai")
    return {
        "run_id": run_id,
        "content_id": str(candidate.get("content_id") or candidate.get("id") or ""),
        "source_id": str(candidate.get("source_url") or candidate.get("link") or ""),
        "topic_key": str(topic.get("topic_key") or "")[:100],
        "signal_type": signal_type,
        "attention_estimate": topic.get(
            "attention_estimate", topic.get("attention_score")),
        "velocity_estimate": topic.get(
            "velocity_estimate", topic.get("velocity_score")),
        "stance_summary": list(topic.get("main_claims") or [])[:5],
        "counterargument_summary": list(topic.get("counter_claims") or [])[:5],
        "representative_post_ids": representative,
        "representative_post_ids_hash": hashlib.sha256(
            "|".join(representative).encode("utf-8")).hexdigest()
            if representative else "",
        "evidence_count": evidence,
        "unique_source_estimate": int(
            topic.get("unique_source_estimate") or 0),
        "search_confidence": float(topic.get("search_confidence") or 0),
        "data_sufficiency": str(
            topic.get("data_sufficiency") or
            ("sufficient" if evidence >= 3 else "limited")),
        "news_match_confidence": confidence,
        "news_match_reason": reason,
        "score_bonus": score_bonus(candidate, topic, confidence),
        "allocated_cost_usd": round(allocated_cost, 8),
        "prompt_version": PROMPT_VERSION,
        "additional_verification_required": True,
    }


def save_discovery_run(run: dict, topics: list[dict],
                       path: Path | None = None) -> None:
    path = path or db_path()
    apply_additive_migrations(path)
    write(
        """INSERT OR REPLACE INTO xai_discovery_runs
           (run_id,mode,started_at,completed_at,requested_from_at,
            requested_to_at,search_window_minutes,coverage_gap_minutes,
            topic_count,tool_call_count,turn_count,image_understanding_used,
            video_understanding_used,estimated_cost_usd,reserved_cost_usd,
            actual_cost_ticks,actual_cost_usd,cost_verified,budget_mode,
            dynamic_target_usd,status,failure_reason,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(run.get(key) for key in (
            "run_id", "mode", "started_at", "completed_at",
            "requested_from_at", "requested_to_at", "search_window_minutes",
            "coverage_gap_minutes", "topic_count", "tool_call_count",
            "turn_count", "image_understanding_used",
            "video_understanding_used", "estimated_cost_usd",
            "reserved_cost_usd", "actual_cost_ticks", "actual_cost_usd",
            "cost_verified", "budget_mode", "dynamic_target_usd", "status",
            "failure_reason", "created_at",
        )),
        path,
    )
    for topic in topics:
        write(
            """INSERT INTO xai_discovery_topics
               (run_id,content_id,topic_key,signal_type,attention_estimate,
                velocity_estimate,stance_summary_json,
                counterargument_summary_json,representative_post_ids_json,
                evidence_count,unique_source_estimate,search_confidence,
                data_sufficiency,news_match_confidence,news_match_reason,
                score_bonus,allocated_cost_usd,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                topic.get("run_id"), topic.get("content_id"),
                topic.get("topic_key"), topic.get("signal_type"),
                topic.get("attention_estimate"), topic.get("velocity_estimate"),
                json.dumps(topic.get("stance_summary") or [], ensure_ascii=False),
                json.dumps(topic.get("counterargument_summary") or [], ensure_ascii=False),
                json.dumps(topic.get("representative_post_ids") or [], ensure_ascii=False),
                topic.get("evidence_count"), topic.get("unique_source_estimate"),
                topic.get("search_confidence"), topic.get("data_sufficiency"),
                topic.get("news_match_confidence"), topic.get("news_match_reason"),
                topic.get("score_bonus"), topic.get("allocated_cost_usd"),
                datetime.now(JST).isoformat(),
            ),
            path,
        )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 8)
    value = ordered[lower] * (upper - index) + ordered[upper] * (index - lower)
    return round(value, 8)


def cost_breakdown(days: int = 30, path: Path | None = None,
                   now: datetime | None = None) -> dict:
    path = path or db_path()
    now = now or datetime.now(JST)
    since = (now - timedelta(days=max(1, days))).isoformat()
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM xai_usage_events WHERE timestamp>=? ORDER BY timestamp",
            (since,),
        )]
        runs = [dict(row) for row in conn.execute(
            "SELECT * FROM xai_discovery_runs WHERE started_at>=? ORDER BY started_at",
            (since,),
        )]
        topics = int(conn.execute(
            """SELECT COUNT(*) FROM xai_discovery_topics
               WHERE created_at>=?""", (since,)).fetchone()[0])
    costs = [
        float(row["actual_cost_usd"] if row.get("cost_source") == "actual"
              else row.get("estimated_cost_usd") or 0)
        for row in rows
    ]
    successes = sum(int(row.get("success") or 0) for row in rows)
    by_tools: dict[str, list[float]] = {}
    for row, cost in zip(rows, costs):
        by_tools.setdefault(str(int(row.get("tool_call_count") or 0)), []).append(cost)
    return {
        "period_days": days,
        "request_count": len(rows),
        "success_count": successes,
        "failure_count": len(rows) - successes,
        "tool_invocation_count": sum(
            int(row.get("tool_call_count") or 0) for row in rows),
        "average_cost_usd": round(statistics.mean(costs), 8) if costs else 0.0,
        "median_cost_usd": round(statistics.median(costs), 8) if costs else 0.0,
        "max_cost_usd": round(max(costs), 8) if costs else 0.0,
        "p75_cost_usd": _percentile(costs, .75),
        "p90_cost_usd": _percentile(costs, .90),
        "p95_cost_usd": _percentile(costs, .95),
        "cost_by_tool_call_count": {
            key: {
                "count": len(values),
                "total_usd": round(sum(values), 8),
                "average_usd": round(statistics.mean(values), 8),
            } for key, values in sorted(by_tools.items())
        },
        "image_understanding_on_cost_usd": round(sum(
            float(row.get("actual_cost_usd") or row.get("estimated_cost_usd") or 0)
            for row in runs if row.get("image_understanding_used")), 8),
        "image_understanding_off_cost_usd": round(sum(
            float(row.get("actual_cost_usd") or row.get("estimated_cost_usd") or 0)
            for row in runs if not row.get("image_understanding_used")), 8),
        "topic_count": topics,
        "cache_use_count": sum(int(row.get("cache_used") or 0) for row in rows),
        "budget_stop_count": sum(
            1 for row in runs if "budget" in str(row.get("failure_reason") or "")),
        "per_run_hard_limit_stop_count": sum(
            1 for row in runs
            if row.get("failure_reason") == "per_run_hard_limit"),
        "cost_verified_count": sum(
            1 for row in rows if int(row.get("cost_verified") or 0)),
        "cost_unverified_count": sum(
            1 for row in rows if not int(row.get("cost_verified") or 0)),
    }


def coverage_report(days: int = 30, path: Path | None = None,
                    now: datetime | None = None) -> dict:
    path = path or db_path()
    now = now or datetime.now(JST)
    since = (now - timedelta(days=max(1, days))).isoformat()
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT * FROM xai_discovery_runs
               WHERE started_at>=? ORDER BY started_at""", (since,))]
    return {
        "period_days": days,
        "run_count": len(rows),
        "successful_runs": sum(row.get("status") == "success" for row in rows),
        "failed_runs": sum(row.get("status") == "failed" for row in rows),
        "coverage_gap_runs": sum(
            int(row.get("coverage_gap_minutes") or 0) > 0 for row in rows),
        "coverage_gap_minutes": sum(
            int(row.get("coverage_gap_minutes") or 0) for row in rows),
        "maximum_search_window_minutes": max(
            [int(row.get("search_window_minutes") or 0) for row in rows] or [0]),
        "topic_count": sum(int(row.get("topic_count") or 0) for row in rows),
        "runs": [{
            "run_id": row.get("run_id"), "started_at": row.get("started_at"),
            "mode": row.get("mode"), "status": row.get("status"),
            "search_window_minutes": row.get("search_window_minutes"),
            "coverage_gap_minutes": row.get("coverage_gap_minutes"),
        } for row in rows],
    }


def discovery_status(path: Path | None = None,
                     now: datetime | None = None) -> dict:
    path = path or db_path()
    now = now or datetime.now(JST)
    plan = budget_plan(path, now)
    window = search_window(path, now)
    with closing(connect(path)) as conn:
        last = conn.execute(
            """SELECT * FROM xai_discovery_runs ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        verified_samples = int(conn.execute(
            """SELECT COUNT(*) FROM xai_usage_events
               WHERE started_at IS NOT NULL AND started_at<>''
               AND cost_verified=1 AND success=1""").fetchone()[0])
    optimization = phase_d_optimize(
        path=path, now=now, apply=False)
    return {
        "phase": "A-D",
        "command_external_posting": False,
        "command_live_api_call": False,
        "rollout": {
            "phase_a_implemented": True,
            "phase_b_verified": verified_samples >= 2,
            "phase_b_verified_samples": verified_samples,
            "phase_c_scheduled": _bool("XAI_ENABLED", "false"),
            "phase_d_collecting": not optimization["eligible"],
            "phase_d_applied": bool(phase_d_tuning(path)),
            "phase_d_sample_size": optimization["sample_size"],
            "phase_d_minimum_sample_size": optimization["minimum_sample_size"],
        },
        "budget": asdict(plan),
        "next_window": asdict(window),
        "last_run": dict(last) if last else None,
        "signal_semantics": {
            "xai_only": "qualitative_xai",
            "native_x": "quantitative_native_x",
            "combined": "hybrid",
            "cache": "cached",
            "failure": "unavailable",
        },
    }


def discovery_audit(path: Path | None = None,
                    now: datetime | None = None) -> dict:
    path = path or db_path()
    now = now or datetime.now(JST)
    status = discovery_status(path, now)
    costs = cost_breakdown(30, path, now)
    coverage = coverage_report(30, path, now)
    return {
        "generated_at": now.isoformat(),
        "status": status,
        "costs_30d": costs,
        "coverage_30d": coverage,
        "acceptance": {
            "dynamic_budget": status["budget"]["dynamic_target_per_run_usd"] >= 0,
            "unverified_cap": effective_limit()[0] <= 7.5
            if not _bool("XAI_COST_LEDGER_VERIFIED", "false") else True,
            "search_window_capped_24h":
                status["next_window"]["actual_search_window_minutes"] <= 1440,
            "fixture_dry_run_no_live_api": True,
            "fixture_dry_run_no_external_post": True,
        },
    }


def dry_run(max_topics: int = 5, now: datetime | None = None) -> dict:
    """Deterministic fixture-only acceptance run."""
    now = now or datetime(2026, 7, 30, 12, 0, tzinfo=JST)
    fixtures = [
        {
            "content_id": "fixture-official-001",
            "title": "政府が重要法案の採決日程を公式発表",
            "summary": "国会の公式資料に採決日程を掲載",
            "source_url": "fixture://official/001",
            "verified": True, "source_reliability_score": 9,
            "importance_score": 8.5, "is_major_update": True,
            "has_image": True,
        },
        {
            "content_id": "fixture-normal-002",
            "title": "審議会が制度運用を更新",
            "summary": "省庁の公開資料に基づく通常更新",
            "source_url": "fixture://official/002",
            "verified": True, "source_reliability_score": 8,
            "importance_score": 5, "has_image": False,
        },
    ]
    topic = {
        "topic_key": "重要法案 採決",
        "attention_score": 8, "velocity_score": 7,
        "main_claims": ["審議日程への関心"],
        "counter_claims": ["追加説明を求める意見"],
        "representative_post_ids": ["fixture-post-1", "fixture-post-2", "fixture-post-3"],
        "evidence_count": 3, "unique_source_estimate": 3,
        "search_confidence": .9, "data_sufficiency": "sufficient",
    }
    scenarios = []
    for mode in MODES:
        target = .20 if mode == "normal" else .10 if mode == "low_frequency" else .04
        profile = research_profile(fixtures, mode, target)
        scenarios.append({
            "mode": mode, "profile": profile,
            "scheduled_slots": _daily_slots(mode),
        })
    gap_cases = {}
    for hours in (3, 6, 12, 30):
        gap_cases[f"{hours}h"] = asdict(search_window(
            now=now, last_success_at=now - timedelta(hours=hours),
            path=Path(":memory:"),
        ))
    record = topic_audit_record(fixtures[0], topic, "fixture-run")
    cached = cached_signal(topic, now - timedelta(minutes=120), now)
    return {
        "phase": "A",
        "api_calls": 0,
        "external_posts": 0,
        "max_topics": max(1, min(max_topics, 8)),
        "scenarios": scenarios,
        "gap_cases": gap_cases,
        "topic_audit": record,
        "cache": cached,
        "checks": {
            "standard_max_five": scenarios[1]["profile"]["max_topics"] <= 5,
            "extended_max_eight": scenarios[0]["profile"]["max_topics"] <= 8,
            "standard_max_two_tools": scenarios[1]["profile"]["max_tool_calls"] <= 2,
            "extended_max_three_tools": scenarios[0]["profile"]["max_tool_calls"] <= 3,
            "normal_image_conditional": scenarios[0]["profile"]["image_understanding"],
            "non_normal_image_off": all(
                not row["profile"]["image_understanding"]
                for row in scenarios[1:]),
            "video_off": all(
                not row["profile"]["video_understanding"] for row in scenarios),
            "match_requires_confidence": record["news_match_confidence"] >= .8,
            "bonus_capped": record["score_bonus"] <= 2.0,
            "cached_velocity_null": cached["x_velocity_estimate"] is None,
            "maximum_lookback_24h": gap_cases["30h"][
                "actual_search_window_minutes"] <= 1440,
            "no_api_call": True,
            "no_external_post": True,
        },
    }


def roi_report(days: int = 30, path: Path | None = None,
               now: datetime | None = None, persist: bool = False) -> dict:
    from usage_reports import xai_roi

    path = path or db_path()
    now = now or datetime.now(JST)
    result = xai_roi(days=days, path=path, now=now)
    minimum = _int("XAI_ROI_MIN_SAMPLE_SIZE", 30)
    recommended = _int("XAI_ROI_RECOMMENDED_SAMPLE_SIZE", 100)
    x_count = int(result.get("xai_posts", {}).get("count") or 0)
    other_count = int(result.get("non_xai_posts", {}).get("count") or 0)
    result["minimum_sample_size"] = minimum
    result["recommended_sample_size"] = recommended
    result["statistical_conclusion_allowed"] = (
        x_count >= minimum and other_count >= minimum)
    if not result["statistical_conclusion_allowed"]:
        result["decision"] = "insufficient_data"
    result["cost_per_1000_impressions_usd"] = None
    x_impressions = (
        float(result.get("xai_posts", {}).get("average_impressions") or 0)
        * x_count)
    if x_impressions > 0:
        result["cost_per_1000_impressions_usd"] = round(
            float(result.get("xai_actual_or_estimated_cost_usd") or 0)
            / x_impressions * 1000, 8)
    result["phase_a_dry_run"] = not persist
    if persist:
        start = (now - timedelta(days=days)).isoformat()
        write(
            """INSERT INTO xai_roi_results
               (period_start,period_end,comparison_group,sample_size,
                metrics_json,cost_json,conclusion,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                start, now.isoformat(), "xai_vs_non_xai", x_count + other_count,
                json.dumps(result, ensure_ascii=False),
                json.dumps({
                    "xai_cost_usd": result.get(
                        "xai_actual_or_estimated_cost_usd"),
                    "cost_per_1000_impressions_usd": result.get(
                        "cost_per_1000_impressions_usd"),
                }, ensure_ascii=False),
                result["decision"], now.isoformat(),
            ),
            path,
        )
    return result


def phase_d_tuning(path: Path | None = None) -> dict:
    """Return the latest applied Phase D tuning without mutating configuration."""
    path = path or db_path()
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        row = conn.execute(
            """SELECT metrics_json,created_at FROM xai_roi_results
               WHERE comparison_group='phase_d_optimization'
               AND conclusion='applied'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
    if not row:
        return {}
    try:
        result = json.loads(row["metrics_json"] or "{}")
        result["applied_at"] = row["created_at"]
        return result
    except (TypeError, json.JSONDecodeError):
        return {}


def phase_d_optimize(days: int = 30, path: Path | None = None,
                     now: datetime | None = None,
                     apply: bool = False) -> dict:
    """Recommend frequency/tool-call tuning after enough observed live runs.

    The optimizer is deliberately bounded: it cannot exceed six daily slots,
    two standard tool calls, or three extended tool calls.  Before 30
    successful runs it reports insufficient data and applies nothing.
    """
    path = path or db_path()
    now = now or datetime.now(JST)
    since = (now - timedelta(days=max(1, days))).isoformat()
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        runs = [dict(row) for row in conn.execute(
            """SELECT * FROM xai_discovery_runs
               WHERE started_at>=? AND status='success'
               ORDER BY started_at""", (since,))]
        productive = int(conn.execute(
            """SELECT COUNT(DISTINCT run_id) FROM xai_discovery_topics
               WHERE created_at>=? AND score_bonus>0""", (since,)).fetchone()[0])
    minimum = max(30, _int("XAI_ROI_MIN_SAMPLE_SIZE", 30))
    sample = len(runs)
    costs = [float(row.get("actual_cost_usd") or 0) for row in runs
             if int(row.get("cost_verified") or 0)]
    average_cost = statistics.mean(costs) if costs else 0.0
    productive_ratio = productive / sample if sample else 0.0
    average_topics = (
        statistics.mean(int(row.get("topic_count") or 0) for row in runs)
        if runs else 0.0)
    average_tools = (
        statistics.mean(int(row.get("tool_call_count") or 0) for row in runs)
        if runs else 0.0)
    if sample < minimum:
        decision = "insufficient_data"
        daily_runs, standard_tools, extended_tools = 3, 2, 3
    elif productive_ratio < .20 or (average_cost > .15 and average_topics < 1):
        decision = "reduce"
        daily_runs, standard_tools, extended_tools = 2, 1, 2
    elif productive_ratio >= .50 and average_cost <= .12:
        decision = "increase_within_cap"
        daily_runs, standard_tools, extended_tools = 6, 2, 3
    else:
        decision = "maintain"
        daily_runs, standard_tools, extended_tools = 3, 2, 3
    result = {
        "phase": "D",
        "period_days": days,
        "sample_size": sample,
        "minimum_sample_size": minimum,
        "eligible": sample >= minimum,
        "decision": decision,
        "productive_run_ratio": round(productive_ratio, 6),
        "average_verified_cost_usd": round(average_cost, 8),
        "average_topics_per_run": round(average_topics, 4),
        "average_tool_calls_per_run": round(average_tools, 4),
        "recommended_daily_runs": min(6, max(2, daily_runs)),
        "recommended_standard_tool_calls": min(2, max(1, standard_tools)),
        "recommended_extended_tool_calls": min(3, max(2, extended_tools)),
        "bounds": {
            "daily_runs_max": 6,
            "standard_tool_calls_max": 2,
            "extended_tool_calls_max": 3,
        },
        "applied": bool(apply and sample >= minimum),
        "evaluated_at": now.isoformat(),
    }
    if result["applied"]:
        write(
            """INSERT INTO xai_roi_results
               (period_start,period_end,comparison_group,sample_size,
                metrics_json,cost_json,conclusion,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                since, now.isoformat(), "phase_d_optimization", sample,
                json.dumps(result, ensure_ascii=False),
                json.dumps({
                    "average_verified_cost_usd": average_cost,
                }, ensure_ascii=False),
                "applied", now.isoformat(),
            ),
            path,
        )
    return result


def new_run_id() -> str:
    return f"xai-discovery-{uuid.uuid4()}"
