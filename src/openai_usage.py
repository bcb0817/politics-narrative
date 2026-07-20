"""OpenAI token accounting and append-only usage history."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")


def _obj_value(obj, name: str, default=0):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def load_pricing(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def calculate_cost(
    pricing: dict, model: str, input_tokens: int, cached_tokens: int, output_tokens: int
) -> float:
    rates = pricing.get(model) or {}
    cached_tokens = max(0, min(int(cached_tokens or 0), int(input_tokens or 0)))
    uncached_tokens = max(0, int(input_tokens or 0) - cached_tokens)
    return round(
        uncached_tokens * float(rates.get("input_per_million", 0.0)) / 1_000_000
        + cached_tokens * float(rates.get("cached_input_per_million", 0.0)) / 1_000_000
        + int(output_tokens or 0) * float(rates.get("output_per_million", 0.0)) / 1_000_000,
        8,
    )


def load_usage_state(path: Path, now_jst: datetime | None = None) -> dict:
    now_jst = now_jst or datetime.now(JST)
    month = now_jst.strftime("%Y-%m")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict) or state.get("month") != month:
        state = {}
    state.setdefault("month", month)
    state.setdefault("estimated_cost_usd", 0.0)
    state.setdefault("input_tokens", 0)
    state.setdefault("cached_input_tokens", 0)
    state.setdefault("output_tokens", 0)
    state.setdefault("calls", 0)
    state.setdefault("important_calls", 0)
    state.setdefault("days", {})
    state.setdefault("models", {})
    return state


def today_usage(state: dict, now_jst: datetime | None = None) -> dict:
    now_jst = now_jst or datetime.now(JST)
    day = now_jst.date().isoformat()
    current = state.setdefault("days", {}).setdefault(day, {})
    current.setdefault("calls", 0)
    current.setdefault("important_calls", 0)
    current.setdefault("daily_review_calls", 0)
    current.setdefault("weekly_report_calls", 0)
    current.setdefault("premium_calls", 0)
    current.setdefault("estimated_cost_usd", 0.0)
    return current


def usage_from_response(response) -> tuple[int, int, int]:
    usage = getattr(response, "usage", None)
    input_tokens = int(_obj_value(usage, "input_tokens", 0) or 0)
    output_tokens = int(_obj_value(usage, "output_tokens", 0) or 0)
    details = _obj_value(usage, "input_tokens_details", {}) or {}
    cached_tokens = int(_obj_value(details, "cached_tokens", 0) or 0)
    return input_tokens, max(0, min(cached_tokens, input_tokens)), output_tokens


def record_usage(
    *,
    response,
    model: str,
    task_type: str,
    pricing: dict,
    state_path: Path,
    history_dir: Path,
    success: bool = True,
    fallback_used: bool = False,
    now_jst: datetime | None = None,
) -> dict:
    now_jst = now_jst or datetime.now(JST)
    input_tokens, cached_tokens, output_tokens = usage_from_response(response)
    cost = calculate_cost(pricing, model, input_tokens, cached_tokens, output_tokens)
    state = load_usage_state(state_path, now_jst)
    today = today_usage(state, now_jst)

    state["estimated_cost_usd"] = round(float(state["estimated_cost_usd"]) + cost, 8)
    state["input_tokens"] = int(state["input_tokens"]) + input_tokens
    state["cached_input_tokens"] = int(state["cached_input_tokens"]) + cached_tokens
    state["output_tokens"] = int(state["output_tokens"]) + output_tokens
    state["calls"] = int(state["calls"]) + 1
    today["calls"] = int(today["calls"]) + 1
    today["estimated_cost_usd"] = round(float(today["estimated_cost_usd"]) + cost, 8)

    if task_type == "post_generation" and model in {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"}:
        state["important_calls"] = int(state["important_calls"]) + 1
        today["important_calls"] = int(today["important_calls"]) + 1
    counter = {
        "daily_review": "daily_review_calls",
        "weekly_report": "weekly_report_calls",
        "premium_report": "premium_calls",
    }.get(task_type)
    if counter:
        today[counter] = int(today[counter]) + 1

    model_rec = state["models"].setdefault(model, {
        "calls": 0, "input_tokens": 0, "cached_input_tokens": 0,
        "output_tokens": 0, "estimated_cost_usd": 0.0,
    })
    model_rec["calls"] += 1
    model_rec["input_tokens"] += input_tokens
    model_rec["cached_input_tokens"] += cached_tokens
    model_rec["output_tokens"] += output_tokens
    model_rec["estimated_cost_usd"] = round(float(model_rec["estimated_cost_usd"]) + cost, 8)

    event = {
        "timestamp": now_jst.isoformat(),
        "task_type": task_type,
        "model": model,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": cost,
        "success": bool(success),
        "fallback_used": bool(fallback_used),
    }
    state["last_event"] = event
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"{now_jst:%Y-%m}.jsonl"
    with open(history_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event
