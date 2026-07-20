"""Bounded OpenAI analysis for local daily/weekly/premium reports."""

from __future__ import annotations

import json
import os
from pathlib import Path

from model_router import ModelRouter, is_auth_error
from openai_usage import load_usage_state, record_usage, today_usage


ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "timing_findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "strengths", "weaknesses", "recommendations", "timing_findings"],
    "additionalProperties": False,
}


def compact_daily_payload(payload: dict) -> dict:
    """Keep only bounded, deduplicated evidence needed for trend analysis."""
    seen = set()
    samples = []
    for key in ("top_impressions_3", "top_growth_3", "bottom_3"):
        for row in payload.get(key, [])[:3]:
            tweet_id = str(row.get("tweet_id", ""))
            if tweet_id and tweet_id in seen:
                continue
            seen.add(tweet_id)
            samples.append({
                "bucket": key,
                "tweet_id": tweet_id,
                "text": str(row.get("text", ""))[:280],
                "post_type": row.get("post_type", ""),
                "hook_type": row.get("hook_type", ""),
                "critique_axis": row.get("critique_axis", ""),
                "impressions": row.get("impressions", 0),
                "growth_score": row.get("growth_score", 0),
                "posted_hour_jst": row.get("posted_hour_jst", 0),
            })
    return {
        "reviewed_count": payload.get("reviewed_count", 0),
        "samples": samples[:9],
        "quality_errors": payload.get("quality_errors", [])[-5:],
        "performance_breakdown": payload.get("performance_breakdown", {}),
        "x_timing_analysis": payload.get("x_timing_analysis", {}),
        "repeated_structures": payload.get("repeated_structures", [])[:5],
    }


def analyze_report(*, task_type: str, payload: dict, root_dir: Path, state_dir: Path,
                   premium_requested: bool = False, client_factory=None) -> dict:
    router = ModelRouter(root_dir / "config" / "openai_model_pricing.json")
    state_path = state_dir / "openai_usage.json"
    state = load_usage_state(state_path)
    route = router.select_model(
        task_type,
        budget_state=state,
        daily_usage=today_usage(state),
        premium_requested=premium_requested,
    )
    result = {"route": route, "analysis": None, "error": ""}
    if not route.get("model"):
        result["error"] = route.get("skip_reason", "model_route_skip")
        return result
    try:
        if client_factory is None:
            from openai import OpenAI
            client_factory = OpenAI
        client = client_factory(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            timeout=max(15.0, float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "90"))),
            max_retries=0,
        )
        system = (
            "You analyze performance data for a Japanese political-news X bot. "
            "Use only supplied metrics. Separate observations from recommendations. "
            "Do not invent political facts, model details, prices, or credentials."
        )
        user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        model = route["model"]
        fallback_used = bool(route.get("fallback_used"))

        def call(chosen_model: str):
            kwargs = {
                "model": chosen_model,
                "instructions": system,
                "input": user,
                "max_output_tokens": int(route["max_output_tokens"]),
                "text": {"format": {"type": "json_schema", "name": "performance_analysis",
                                      "strict": True, "schema": ANALYSIS_SCHEMA}},
                "store": False,
            }
            effort = route.get("reasoning_effort", "none")
            if effort not in {"none", "off", "false", "minimal"}:
                kwargs["reasoning"] = {"effort": effort}
            return client.responses.create(**kwargs)

        try:
            response = call(model)
        except Exception as exc:
            fallbacks = route.get("fallback_models", [])
            if is_auth_error(exc) or not fallbacks or int(os.environ.get("OPENAI_MAX_RETRIES", "1")) < 1:
                raise
            model = fallbacks[0]
            fallback_used = True
            response = call(model)
        result["analysis"] = json.loads((getattr(response, "output_text", "") or "{}").strip())
        route["used_model"] = model
        route["fallback_used"] = fallback_used
        record_usage(
            response=response,
            model=model,
            task_type=task_type,
            pricing=router.pricing,
            state_path=state_path,
            history_dir=state_dir / "openai_usage_history",
            fallback_used=fallback_used,
        )
    except Exception as exc:
        result["error"] = "authentication_error_no_retry" if is_auth_error(exc) else type(exc).__name__
    return result
