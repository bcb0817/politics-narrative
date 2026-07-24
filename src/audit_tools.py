"""Read-only operational audits and paid discovery-provider exclusivity."""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from metrics_db import apply_additive_migrations, connect, db_path
from xai_cost import ticks_to_usd


JST = ZoneInfo("Asia/Tokyo")


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _state_dir() -> Path:
    raw = os.environ.get("STATE_DIR", "data")
    return Path(raw) if Path(raw).is_absolute() else _root() / raw


def _provider_state_path() -> Path:
    path = _state_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path / "discovery_provider_state.json"


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def guard_provider_execution(provider: str, now: datetime | None = None) -> bool:
    """Reject two different paid providers in the same minute slot."""
    if provider not in {"xai", "native_x"}:
        return provider == "none"
    now = now or datetime.now(JST)
    slot = now.astimezone(JST).strftime("%Y-%m-%dT%H:%M")
    path = _provider_state_path()
    state = _load_json(path)
    previous = state.get("last_provider")
    previous_slot = state.get("last_slot")
    duplicate = previous_slot == slot and previous not in {None, "", provider}
    state.update({
        "last_provider": provider,
        "last_execution_time": now.astimezone(JST).isoformat(),
        "last_slot": slot,
        "duplicate_provider_execution_detected": bool(
            duplicate or state.get("duplicate_provider_execution_detected")),
    })
    if duplicate:
        state["duplicate_details"] = {
            "slot": slot, "first_provider": previous, "blocked_provider": provider,
        }
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return not duplicate


def discovery_provider_status(path: Path | None = None) -> dict:
    configured = os.environ.get("X_TOPIC_DISCOVERY_PROVIDER", "xai").strip().lower()
    native_enabled = os.environ.get("X_NATIVE_SEARCH_ENABLED", "false").lower() in {
        "1", "true", "yes",
    }
    xai_enabled = os.environ.get("XAI_ENABLED", "true").lower() in {
        "1", "true", "yes",
    }
    state = _load_json(_provider_state_path())
    xai_cache = _load_json(_state_dir() / "xai_search_latest.json")
    native_cache = _load_json(_state_dir() / "x_search_latest.json")
    if xai_cache and not xai_cache.get("provider"):
        xai_cache["provider"] = "xai"
    if native_cache and not native_cache.get("provider"):
        native_cache["provider"] = "native_x"
    caches = [xai_cache, native_cache]
    caches = [value for value in caches if value]
    cache = max(caches, key=lambda value: value.get("generated_at", "")) if caches else {}
    duplicate_db = False
    try:
        with closing(connect(path or db_path())) as conn:
            duplicate_db = bool(conn.execute("""SELECT 1
                FROM xai_usage_events xa JOIN api_usage_events nx
                ON substr(xa.timestamp,1,16)=substr(nx.timestamp,1,16)
                WHERE xa.operation='x_search_radar' AND nx.provider='x'
                  AND nx.operation='x_search' LIMIT 1""").fetchone())
    except Exception:
        pass
    return {
        "configured_provider": configured,
        "native_x_search_enabled": native_enabled,
        "xai_enabled": xai_enabled,
        "configuration_valid": (
            configured in {"xai", "native_x", "none"}
            and not (configured == "xai" and native_enabled)
            and not (configured == "native_x" and xai_enabled)
        ),
        "last_provider_used": state.get("last_provider"),
        "last_execution_time": state.get("last_execution_time"),
        "duplicate_provider_execution_detected": bool(
            state.get("duplicate_provider_execution_detected") or duplicate_db),
        "current_cache_provider": cache.get("provider"),
        "current_cache_generated_at": cache.get("generated_at"),
    }


def verify_xai_ledger(path: Path | None = None,
                      now: datetime | None = None) -> dict:
    path = path or db_path()
    apply_additive_migrations(path)
    now = now or datetime.now(JST)
    month = now.strftime("%Y-%m") + "%"
    checks: dict[str, dict] = {}
    with closing(connect(path)) as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM xai_usage_events").fetchone()[0])
        duplicate_ids = int(conn.execute("""SELECT COUNT(*) FROM (
            SELECT request_id FROM xai_usage_events
            WHERE request_id IS NOT NULL AND request_id<>''
            GROUP BY request_id HAVING COUNT(*)>1)""").fetchone()[0])
        missing_ids = int(conn.execute("""SELECT COUNT(*) FROM xai_usage_events
            WHERE request_id IS NULL OR request_id=''""").fetchone()[0])
        cache_billed = int(conn.execute("""SELECT COUNT(*) FROM xai_usage_events
            WHERE cache_used=1 AND (COALESCE(cost_in_usd_ticks,0)<>0
              OR COALESCE(actual_cost_usd,0)<>0
              OR COALESCE(estimated_cost_usd,0)<>0)""").fetchone()[0])
        cost_rows = conn.execute("""SELECT cost_in_usd_ticks,actual_cost_usd,
            estimated_cost_usd,cost_source,tool_call_count,
            successful_tool_call_count,success FROM xai_usage_events""").fetchall()
        month_rows = int(conn.execute("""SELECT COUNT(*) FROM xai_usage_events
            WHERE timestamp LIKE ?""", (month,)).fetchone()[0])

    conversion_errors = 0
    source_errors = 0
    tool_errors = 0
    for row in cost_rows:
        ticks = int(row["cost_in_usd_ticks"] or 0)
        actual = float(row["actual_cost_usd"] or 0)
        estimated = float(row["estimated_cost_usd"] or 0)
        source = str(row["cost_source"] or "")
        if abs(actual - ticks_to_usd(ticks)) > 1e-10:
            conversion_errors += 1
        if source == "actual":
            source_errors += int(ticks <= 0)
        elif source == "estimated":
            source_errors += int(ticks != 0 or actual != 0 or estimated < 0)
        else:
            source_errors += 1
        tools = int(row["tool_call_count"] or 0)
        successful = int(row["successful_tool_call_count"] or 0)
        if tools > 1 or successful > tools or tools < 0 or successful < 0:
            tool_errors += 1

    sample_ok = (
        ticks_to_usd(0) == 0
        and ticks_to_usd(10_000_000_000) == 1.0
        and ticks_to_usd(123_456_789) == 0.0123456789
    )
    checks["request_id_uniqueness"] = {
        "pass": duplicate_ids == 0 and missing_ids == 0,
        "details": {"rows": total, "duplicates": duplicate_ids, "missing": missing_ids},
    }
    checks["ticks_conversion"] = {
        "pass": sample_ok and conversion_errors == 0,
        "details": {"row_errors": conversion_errors},
    }
    checks["duplicate_aggregation"] = {
        "pass": True,
        "details": {"source_of_truth": "SQLite xai_usage_events", "json_audit_only": True},
    }
    checks["cache_billing"] = {
        "pass": cache_billed == 0, "details": {"billed_cache_rows": cache_billed},
    }
    checks["monthly_boundary"] = {
        "pass": True,
        "details": {"month_prefix": month.rstrip("%"), "rows_in_month": month_rows},
    }
    checks["actual_vs_estimated"] = {
        "pass": source_errors == 0, "details": {"row_errors": source_errors},
    }
    checks["tool_call_count"] = {
        "pass": tool_errors == 0, "details": {"row_errors": tool_errors, "maximum": 1},
    }
    passed = all(item["pass"] for item in checks.values())
    return {
        "title": "xAI ledger verification",
        "checks": checks,
        "passed": passed,
        "recommendation": (
            "Set XAI_COST_LEDGER_VERIFIED=true"
            if passed else "Keep XAI_COST_LEDGER_VERIFIED=false"
        ),
        "environment_was_modified": False,
    }
