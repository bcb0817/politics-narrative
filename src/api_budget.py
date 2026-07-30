"""Transactional reservations for OpenAI and X API costs."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from calendar import monthrange
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from metrics_db import apply_additive_migrations, connect, db_path, init_db

JST = ZoneInfo("Asia/Tokyo")

DEFAULT_BUDGETS = {
    "OPENAI_MONTHLY_BUDGET_USD": 15.0,
    "XAI_MONTHLY_BUDGET_USD": 30.0,
    "X_MONTHLY_BUDGET_USD": 16.0,
    "TOTAL_MONTHLY_API_BUDGET_USD": 61.0,
    "OPENAI_BUDGET_RESERVE_USD": 1.0,
    "XAI_BUDGET_RESERVE_USD": 1.5,
    "X_BUDGET_RESERVE_USD": 0.75,
    "TOTAL_BUDGET_RESERVE_USD": 3.25,
    "BUDGET_USD_JPY_RATE": 165.0,
    "BUDGET_WARNING_RATIO": 0.85,
    "BUDGET_RESTRICT_RATIO": 0.93,
    "BUDGET_HARD_STOP_RATIO": 1.0,
}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def budget_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "api_budget.json"


def _budget_file_values() -> dict:
    try:
        payload = json.loads(budget_config_path().read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _budget_float(name: str) -> float:
    """Resolve a budget value from env, then config, then safe defaults."""
    default = DEFAULT_BUDGETS[name]
    raw = os.environ.get(name)
    if raw is None:
        raw = _budget_file_values().get(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def budget_configuration() -> dict:
    providers = {
        "openai": _budget_float("OPENAI_MONTHLY_BUDGET_USD"),
        "xai": _budget_float("XAI_MONTHLY_BUDGET_USD"),
        "x": _budget_float("X_MONTHLY_BUDGET_USD"),
    }
    configured_total = _budget_float("TOTAL_MONTHLY_API_BUDGET_USD")
    provider_sum = round(sum(providers.values()), 8)
    effective_total = min(configured_total, provider_sum)
    total_reserve = min(
        max(0.0, _budget_float("TOTAL_BUDGET_RESERVE_USD")),
        effective_total,
    )
    return {
        "providers": providers,
        "configured_total": configured_total,
        "provider_sum": provider_sum,
        "consistent": abs(provider_sum - configured_total) < 0.000001,
        "effective_total_limit": effective_total,
        "provider_reserves": {
            "openai": _budget_float("OPENAI_BUDGET_RESERVE_USD"),
            "xai": _budget_float("XAI_BUDGET_RESERVE_USD"),
            "x": _budget_float("X_BUDGET_RESERVE_USD"),
        },
        "total_reserve": total_reserve,
        "effective_spendable": max(0.0, effective_total - total_reserve),
        "usd_jpy_rate": _budget_float("BUDGET_USD_JPY_RATE"),
        "jpy_budget_display": configured_total * _budget_float("BUDGET_USD_JPY_RATE"),
        "ratios": {
            "warning": _budget_float("BUDGET_WARNING_RATIO"),
            "restrict": _budget_float("BUDGET_RESTRICT_RATIO"),
            "hard_stop": _budget_float("BUDGET_HARD_STOP_RATIO"),
        },
    }


def budget_stage(usage_ratio: float) -> str:
    ratios = budget_configuration()["ratios"]
    if usage_ratio >= ratios["hard_stop"]:
        return "hard_stop"
    if usage_ratio >= ratios["restrict"]:
        return "restrict"
    if usage_ratio >= ratios["warning"]:
        return "warning"
    return "normal"


def restriction_level(usage_ratio: float) -> int:
    """Return the ordered degradation step between 93% and 100%."""
    ratios = budget_configuration()["ratios"]
    if usage_ratio < ratios["restrict"]:
        return 0
    if usage_ratio >= ratios["hard_stop"]:
        return 8
    width = max(0.000001, ratios["hard_stop"] - ratios["restrict"])
    return min(7, 1 + int((usage_ratio - ratios["restrict"]) / (width / 7)))


def startup_budget_lines() -> list[str]:
    cfg = budget_configuration()
    effective_xai = effective_xai_limit()
    lines = [
        f"OpenAI monthly budget : ${cfg['providers']['openai']:.2f}",
        f"xAI configured budget : ${cfg['providers']['xai']:.2f}",
        f"xAI effective budget  : ${effective_xai:.2f}",
        f"X API monthly budget  : ${cfg['providers']['x']:.2f}",
        f"Total monthly budget  : ${cfg['configured_total']:.2f}",
        f"Total reserve         : ${cfg['total_reserve']:.2f}",
        f"Effective spendable   : ${cfg['effective_spendable']:.2f}",
        f"JPY rate              : {cfg['usd_jpy_rate']:g}",
        f"JPY budget display    : JPY {cfg['jpy_budget_display']:,.0f}",
    ]
    if not cfg["consistent"]:
        lines.append(
            "WARNING: provider budget sum does not match total; "
            f"effective total capped at ${cfg['effective_total_limit']:.2f}"
        )
    return lines


def pricing_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "api_pricing.json"


def load_pricing() -> dict:
    try:
        data = json.loads(pricing_path().read_text(encoding="utf-8"))
        if not isinstance(data.get("openai"), dict) or not isinstance(data.get("x"), dict):
            raise ValueError("invalid pricing")
        return data
    except Exception:
        return {}


def estimate_openai(model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float | None:
    rates = load_pricing().get("openai", {}).get(model)
    if not rates:
        return None
    cached = max(0, min(cached_tokens, input_tokens))
    return round((input_tokens - cached) * rates["input_per_million"] / 1_000_000
                 + cached * rates["cached_input_per_million"] / 1_000_000
                 + output_tokens * rates["output_per_million"] / 1_000_000, 8)


def estimate_x(endpoint: str, resources: int = 1) -> float | None:
    rate = load_pricing().get("x", {}).get(endpoint)
    return None if rate is None else round(float(rate) * max(0, resources), 8)


def month_prefix(now: datetime | None = None) -> str:
    return (now or datetime.now(JST)).strftime("%Y-%m")


def xai_ledger_verified() -> bool:
    return os.environ.get("XAI_COST_LEDGER_VERIFIED", "false").lower() in {
        "1", "true", "yes"
    }


def effective_xai_limit() -> float:
    """Apply the configured operator cap while the xAI ledger is unverified."""
    configured = _budget_float("XAI_MONTHLY_BUDGET_USD")
    verified_cap = max(
        0.0, _float("XAI_VERIFIED_EFFECTIVE_LIMIT_USD", configured))
    unverified_cap = max(
        0.0, _float("XAI_UNVERIFIED_EFFECTIVE_LIMIT_USD", 7.5))
    allow_unverified_full = os.environ.get(
        "XAI_ALLOW_UNVERIFIED_FULL_BUDGET", "false").lower() in {
            "1", "true", "yes"
        }
    if xai_ledger_verified():
        return min(configured, verified_cap)
    return configured if allow_unverified_full else min(
        configured, unverified_cap)


def usage_totals(path: Path | None = None, now: datetime | None = None) -> dict:
    apply_additive_migrations(path)
    totals = {"openai": 0.0, "xai": 0.0, "x": 0.0, "total": 0.0, "monitor_runs": 0,
              "x_search_runs": 0, "x_search_reads": 0, "x_post_creates": 0,
              "x_owned_reads": 0, "xai_search_runs": 0, "xai_tool_calls": 0,
              "xai_successful_tool_calls": 0,
              "models": {}, "xai_models": {}}
    totals["xai_requests"] = 0
    try:
        with closing(connect(path)) as conn:
            rows = conn.execute("""SELECT provider,operation,model_or_endpoint,resource_count,
                estimated_cost_usd,success FROM api_usage_events
                WHERE timestamp LIKE ? AND provider<>'xai'""",
                (month_prefix(now) + "%",)).fetchall()
            xai_rows = conn.execute("""SELECT model,operation,tool_call_count,
                successful_tool_call_count,actual_cost_usd,estimated_cost_usd,cost_source,
                success FROM xai_usage_events WHERE timestamp LIKE ?""",
                (month_prefix(now) + "%",)).fetchall()
        for row in rows:
            provider = row["provider"]
            cost = float(row["estimated_cost_usd"] or 0)
            totals[provider] = totals.get(provider, 0) + cost
            if row["operation"] == "news_monitor" and int(row["resource_count"] or 0) > 0:
                totals["monitor_runs"] += 1
            if provider == "openai":
                model = row["model_or_endpoint"] or "unknown"
                totals["models"][model] = totals["models"].get(model, 0) + cost
            elif row["operation"] == "x_search":
                if int(row["success"] or 0) == 1:
                    totals["x_search_runs"] += 1
                totals["x_search_reads"] += int(row["resource_count"] or 0)
            elif row["operation"] == "post_create":
                totals["x_post_creates"] += int(row["resource_count"] or 0)
            elif row["operation"] == "owned_read":
                totals["x_owned_reads"] += int(row["resource_count"] or 0)
        for row in xai_rows:
            totals["xai_requests"] += 1
            cost = (float(row["actual_cost_usd"] or 0)
                    if row["cost_source"] == "actual"
                    else float(row["estimated_cost_usd"] or 0))
            totals["xai"] += cost
            model = row["model"] or "unknown"
            totals["xai_models"][model] = totals["xai_models"].get(model, 0) + cost
            if row["operation"] == "x_search_radar":
                totals["xai_search_runs"] += 1
                totals["xai_tool_calls"] += int(row["tool_call_count"] or 0)
                totals["xai_successful_tool_calls"] += int(
                    row["successful_tool_call_count"] or 0)
        totals["total"] = totals["openai"] + totals["xai"] + totals["x"]
    except sqlite3.Error:
        pass
    return totals


def record_local_event(operation: str, resource_count: int = 1, metadata: dict | None = None,
                       path: Path | None = None) -> None:
    """Record a zero-cost local operation for forecast volume reporting."""
    init_db(path)
    try:
        with closing(connect(path)) as conn:
            conn.execute("""INSERT INTO api_usage_events
              (timestamp,provider,operation,model_or_endpoint,resource_count,input_tokens,cached_input_tokens,
               output_tokens,estimated_cost_usd,success,fallback_used,error_type,metadata_json)
               VALUES (?,?,?,?,?,0,0,0,0,1,0,'',?)""", (
                datetime.now(JST).isoformat(), "local", operation, "local", max(0, resource_count),
                json.dumps(metadata or {}, ensure_ascii=False)))
            conn.commit()
    except sqlite3.Error:
        pass


def _operation_usage(conn, provider: str, operation: str, prefix: str) -> tuple[float, int, int]:
    row = conn.execute("""SELECT COALESCE(SUM(estimated_cost_usd),0),
        COALESCE(SUM(resource_count),0),COUNT(*) FROM api_usage_events
        WHERE provider=? AND operation=? AND timestamp LIKE ?""",
        (provider, operation, prefix + "%")).fetchone()
    return float(row[0] or 0), int(row[1] or 0), int(row[2] or 0)


def _post_generation_usage(conn, prefix: str, is_breaking: bool) -> float:
    """Keep normal and important post allocations independent."""
    predicate = (
        "metadata_json LIKE '%\"is_breaking\": true%'"
        if is_breaking
        else "(metadata_json IS NULL OR metadata_json NOT LIKE '%\"is_breaking\": true%')"
    )
    row = conn.execute(
        f"""SELECT COALESCE(SUM(estimated_cost_usd),0)
        FROM api_usage_events
        WHERE provider='openai' AND operation='post_generation'
        AND timestamp LIKE ? AND {predicate}""",
        (prefix + "%",),
    ).fetchone()
    return float(row[0] or 0)


def reserve(provider: str, operation: str, model_or_endpoint: str, maximum_cost: float | None,
            resource_count: int = 0, metadata: dict | None = None, path: Path | None = None) -> tuple[int | None, str]:
    """Atomically reserve maximum cost. Missing pricing fails closed."""
    if maximum_cost is None:
        return None, "api_pricing_unavailable"
    path = path or db_path()
    init_db(path)
    now = datetime.now(JST)
    cfg = budget_configuration()
    provider_limit = {
        "openai": cfg["providers"]["openai"],
        "xai": effective_xai_limit(),
        "x": cfg["providers"]["x"],
    }.get(provider, 0.0)
    provider_reserve = cfg["provider_reserves"].get(provider, 0.0)
    total_limit = cfg["effective_total_limit"]
    total_reserve = cfg["total_reserve"]
    metadata = metadata or {}
    is_breaking = bool(metadata.get("is_breaking"))
    forecast_result = forecast(path, now)
    current_stage = forecast_result["current_warning_stage"]
    current_restriction_level = forecast_result["restriction_level"]
    current_totals = usage_totals(path, now)
    try:
        conn = connect(path)
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("""SELECT provider,SUM(estimated_cost_usd) cost
            FROM api_usage_events WHERE timestamp LIKE ? AND provider<>'xai'
            GROUP BY provider""",
                            (month_prefix(now) + "%",)).fetchall()
        spent = {row["provider"]: float(row["cost"] or 0) for row in rows}
        spent["xai"] = float(current_totals["xai"])
        usable_provider_limit = max(0, provider_limit - provider_reserve)
        usable_total_limit = max(0, total_limit - total_reserve)
        if spent.get(provider, 0) + maximum_cost > usable_provider_limit:
            conn.rollback(); conn.close()
            return None, f"{provider}_monthly_api_budget_guard"
        if sum(spent.values()) + maximum_cost > usable_total_limit:
            conn.rollback(); conn.close()
            return None, "total_monthly_api_budget_guard"
        if current_stage == "hard_stop":
            conn.rollback(); conn.close()
            return None, "total_monthly_api_hard_stop"
        degradation_steps = {
            "content_pipeline": 1,
            "preview_extensions": 1,
            "quality_eval": 2,
            "weekly_report": 3,
            "free_note_generation": 4,
            "daily_review": 4,
            "x_search_radar": 5,
            "classifier": 6,
        }
        if current_restriction_level >= degradation_steps.get(operation, 99):
            conn.rollback(); conn.close()
            return None, "monthly_budget_restriction_guard"

        if provider == "openai":
            allocations = {
                "post_generation": _float("OPENAI_POST_BUDGET_USD", 9.0),
                "classifier": _float("OPENAI_CLASSIFIER_BUDGET_USD", .75),
                "daily_review": _float("OPENAI_DAILY_REVIEW_BUDGET_USD", 1.0),
                "weekly_report": _float("OPENAI_WEEKLY_REVIEW_BUDGET_USD", .75),
                "quality_eval": _float("OPENAI_QUALITY_EVAL_BUDGET_USD", .5),
                "content_pipeline": _float("OPENAI_CONTENT_PIPELINE_BUDGET_USD", .5),
                "post_experiment_candidates": _float(
                    "POST_EXPERIMENT_OPENAI_MONTHLY_BUDGET_USD", .5),
                # This is an operation cap inside the existing OpenAI $15
                # provider ceiling, not an addition to the $36 total.
                "free_note_generation": _float(
                    "FREE_NOTE_MONTHLY_BUDGET_USD", 1.5),
                "preview_extensions": 0.0,
            }
            if operation in allocations:
                operation_spent = (
                    _post_generation_usage(conn, month_prefix(now), is_breaking)
                    if operation == "post_generation"
                    else _operation_usage(conn, provider, operation, month_prefix(now))[0]
                )
                allocation = (
                    _float("OPENAI_IMPORTANT_POST_BUDGET_USD", 1.5)
                    if operation == "post_generation" and is_breaking
                    else allocations[operation]
                )
                if operation_spent + maximum_cost > allocation:
                    conn.rollback(); conn.close()
                    return None, f"openai_{operation}_budget_guard"

        if provider == "x":
            day_prefix = now.date().isoformat()
            _, month_resources, _ = _operation_usage(conn, provider, operation, month_prefix(now))
            _, day_resources, day_runs = _operation_usage(conn, provider, operation, day_prefix)
            limits = {
                "x_search": (
                    _float("X_SEARCH_MAX_POST_READS_PER_DAY", 54),
                    _float("X_SEARCH_MAX_POST_READS_PER_MONTH", 1620),
                ),
                "post_create": (
                    _float("X_POST_CREATE_MAX_PER_DAY", 10),
                    _float("X_POST_CREATE_MAX_PER_MONTH", 300),
                ),
                "owned_read": (
                    _float("X_OWNED_READ_MAX_PER_DAY", 36),
                    _float("X_OWNED_READ_MAX_PER_MONTH", 1080),
                ),
            }
            if operation in limits:
                daily_limit, monthly_limit = limits[operation]
                if day_resources + resource_count > daily_limit:
                    conn.rollback(); conn.close()
                    return None, f"{operation}_daily_resource_cap"
                if month_resources + resource_count > monthly_limit:
                    conn.rollback(); conn.close()
                    return None, f"{operation}_monthly_resource_cap"
            if operation == "x_search":
                per_run = int(_float("X_SEARCH_MAX_POST_READS_PER_RUN", 18))
                runs_per_day = int(_float("X_SEARCH_RUNS_PER_DAY", 3))
                if resource_count > per_run:
                    conn.rollback(); conn.close()
                    return None, "x_search_per_run_resource_cap"
                if day_runs >= runs_per_day:
                    conn.rollback(); conn.close()
                    return None, "x_search_daily_run_cap"
        # xAI has a dedicated request ledger. A negative sentinel represents a
        # successful budget reservation without creating a mirrored cost row.
        if provider == "xai":
            conn.commit(); conn.close()
            return -1, ""
        cur = conn.execute("""INSERT INTO api_usage_events
            (timestamp,provider,operation,model_or_endpoint,resource_count,input_tokens,cached_input_tokens,
             output_tokens,estimated_cost_usd,success,fallback_used,error_type,metadata_json,
             task_type_source)
             VALUES (?,?,?,?,?,0,0,0,?,0,0,'reserved',?,'explicit')""",
            (now.isoformat(), provider, operation, model_or_endpoint, resource_count, maximum_cost,
             json.dumps(metadata, ensure_ascii=False)))
        reservation_id = int(cur.lastrowid)
        conn.commit(); conn.close()
        return reservation_id, ""
    except sqlite3.Error:
        return None, "sqlite_budget_reservation_failed"


def finalize(reservation_id: int | None, actual_cost: float, *, success: bool,
             input_tokens: int = 0, cached_tokens: int = 0, output_tokens: int = 0,
             fallback_used: bool = False, error_type: str = "", resource_count: int | None = None,
             path: Path | None = None) -> None:
    if not reservation_id or reservation_id < 0:
        return
    try:
        with closing(connect(path)) as conn:
            conn.execute("""UPDATE api_usage_events SET estimated_cost_usd=?,success=?,input_tokens=?,
              cached_input_tokens=?,output_tokens=?,fallback_used=?,error_type=?,
              resource_count=COALESCE(?,resource_count) WHERE id=?""",
              (actual_cost, int(success), input_tokens, cached_tokens, output_tokens,
               int(fallback_used), error_type, resource_count, reservation_id))
            conn.commit()
    except sqlite3.Error:
        pass


def forecast(path: Path | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(JST)
    totals = usage_totals(path, now)
    start = now - timedelta(days=7)
    recent = {"openai": 0.0, "xai": 0.0, "x": 0.0}
    try:
        with closing(connect(path)) as conn:
            rows = conn.execute("""SELECT provider,SUM(estimated_cost_usd) cost
                FROM api_usage_events WHERE timestamp>=? AND provider<>'xai'
                GROUP BY provider""",
                                (start.isoformat(),)).fetchall()
            xai_recent = conn.execute("""SELECT COALESCE(SUM(CASE WHEN cost_source='actual'
                THEN actual_cost_usd ELSE estimated_cost_usd END),0)
                FROM xai_usage_events WHERE timestamp>=?""",
                (start.isoformat(),)).fetchone()[0]
        for row in rows:
            if row["provider"] in recent:
                recent[row["provider"]] = float(row["cost"] or 0)
        recent["xai"] = float(xai_recent or 0)
    except sqlite3.Error:
        pass
    days_left = monthrange(now.year, now.month)[1] - now.day
    cfg = budget_configuration()
    raw_projected = {
        key: totals[key] + recent[key] / 7.0 * days_left
        for key in ("openai", "xai", "x")
    }
    provider_caps = {
        "openai": cfg["providers"]["openai"],
        "xai": effective_xai_limit(),
        "x": cfg["providers"]["x"],
    }
    projected = {
        key: min(raw_projected[key], provider_caps[key])
        for key in ("openai", "xai", "x")
    }
    projected["total"] = projected["openai"] + projected["xai"] + projected["x"]
    projected["total"] = min(projected["total"], cfg["effective_total_limit"])
    usd_jpy = cfg["usd_jpy_rate"]
    monthly_jpy = cfg["jpy_budget_display"]
    projected_jpy = projected["total"] * usd_jpy
    projected_ratio = (
        projected["total"] / cfg["effective_total_limit"]
        if cfg["effective_total_limit"] > 0 else 1.0
    )
    actual_ratio = (
        totals["total"] / cfg["effective_total_limit"]
        if cfg["effective_total_limit"] > 0 else 1.0
    )
    avg_daily = sum(recent.values()) / 7.0

    def threshold_date(threshold_ratio: float) -> str | None:
        target = cfg["effective_total_limit"] * threshold_ratio
        if totals["total"] >= target:
            return now.date().isoformat()
        if avg_daily <= 0:
            return None
        days = max(0, int((target - totals["total"]) / avg_daily))
        candidate = now.date() + timedelta(days=days)
        month_end = now.date().replace(day=monthrange(now.year, now.month)[1])
        return candidate.isoformat() if candidate <= month_end else None

    return {
        "actual": totals,
        "projected": projected,
        "raw_projected": raw_projected,
        "projected_jpy": projected_jpy,
        "remaining_jpy": monthly_jpy - projected_jpy,
        "budget_configuration": cfg,
        "usage_ratio": actual_ratio,
        "projected_usage_ratio": projected_ratio,
        "current_warning_stage": budget_stage(projected_ratio),
        "restriction_level": restriction_level(projected_ratio),
        "warning": projected_ratio >= cfg["ratios"]["warning"],
        "pause_x_search": projected_ratio >= cfg["ratios"]["restrict"],
        "block_non_breaking": projected_ratio >= cfg["ratios"]["hard_stop"],
        "average_daily_usd_7d": avg_daily,
        "threshold_dates": {
            "warning_85": threshold_date(cfg["ratios"]["warning"]),
            "restrict_93": threshold_date(cfg["ratios"]["restrict"]),
            "hard_stop_100": threshold_date(cfg["ratios"]["hard_stop"]),
        },
    }
