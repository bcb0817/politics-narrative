"""Bounded OpenAI analysis for local daily/weekly/premium reports."""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from model_router import ModelRouter, is_auth_error
from openai_usage import load_usage_state, record_usage, today_usage
from api_budget import estimate_openai, finalize as finalize_budget, reserve as reserve_budget
from metrics_db import connect, init_db

JST = ZoneInfo("Asia/Tokyo")


ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "weaknesses": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "timing_findings": {"type": "array", "items": {"type": "string"}},
        "operational_findings": {
            "type": "array", "items": {"type": "string"},
        },
        "impression_strategy": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding": {"type": "string"},
                            "tweet_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "metric": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                        "required": [
                            "finding", "tweet_ids", "metric", "confidence",
                        ],
                        "additionalProperties": False,
                    },
                },
                "next_day_policy": {
                    "type": "object",
                    "properties": {
                        "post_type_priority": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "issue_diagram", "strong_opinion",
                                    "comparison_factcheck",
                                    "steelman_counterargument",
                                ],
                            },
                        },
                        "hook_type_priority": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": [
                                    "fact_reversal", "issue_redefinition",
                                    "number", "contrast", "question",
                                    "conclusion_first",
                                ],
                            },
                        },
                        "preferred_hours_jst": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "target_text_min": {"type": "integer"},
                        "target_text_max": {"type": "integer"},
                        "body_structure": {
                            "type": "string",
                            "enum": [
                                "fact_impact_accountability_improvement",
                                "claim_evidence_conclusion",
                                "before_after_comparison",
                                "timeline_cause_response",
                            ],
                        },
                        "cta_style": {
                            "type": "string",
                            "enum": [
                                "specific_accountability_question",
                                "improvement_request", "source_check",
                                "no_question",
                            ],
                        },
                        "experiment_name": {"type": "string"},
                    },
                    "required": [
                        "post_type_priority", "hook_type_priority",
                        "preferred_hours_jst", "target_text_min",
                        "target_text_max", "body_structure", "cta_style",
                        "experiment_name",
                    ],
                    "additionalProperties": False,
                },
            },
            "required": ["summary", "evidence", "next_day_policy"],
            "additionalProperties": False,
        },
    },
    "required": [
        "summary", "strengths", "weaknesses", "recommendations",
        "timing_findings", "operational_findings",
        "impression_strategy",
    ],
    "additionalProperties": False,
}


def compact_daily_payload(payload: dict) -> dict:
    """Keep only bounded, deduplicated evidence needed for trend analysis."""
    seen = set()
    samples = []
    for key in ("top_impressions_3", "top_trust_3", "top_conversation_3",
                "top_growth_3", "bottom_3"):
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
                "review_strategy_experiment": row.get(
                    "review_strategy_experiment", ""),
                "impressions": row.get("impressions", 0),
                "growth_score": row.get("growth_score", 0),
                "posted_hour_jst": row.get("posted_hour_jst", 0),
                "text_length": row.get("text_length", 0),
                "impressions_per_hour": row.get(
                    "impressions_per_hour", 0),
                "engagement_rate": row.get("engagement_rate", 0),
                "likes": row.get("likes", 0),
                "reposts": row.get("reposts", 0),
                "replies": row.get("replies", 0),
                "quotes": row.get("quotes", 0),
                "bookmarks": row.get("bookmarks", 0),
                "profile_clicks": row.get("profile_clicks"),
                "four_axes": row.get("four_axes", {}),
            })
    return {
        "reviewed_count": payload.get("reviewed_count", 0),
        "samples": samples[:12],
        "quality_errors": payload.get("quality_errors", [])[-5:],
        "performance_breakdown": payload.get("performance_breakdown", {}),
        "x_timing_analysis": payload.get("x_timing_analysis", {}),
        "repeated_structures": payload.get("repeated_structures", [])[:5],
        "operational_log_summary": payload.get(
            "operational_log_summary", {}),
        "current_active_strategy": payload.get(
            "current_active_strategy", {}),
    }


def analyze_report(*, task_type: str, payload: dict, root_dir: Path, state_dir: Path,
                   premium_requested: bool = False, client_factory=None,
                   target_json_path: str = "", batch_metadata: dict | None = None,
                   batch_dedupe_key: str = "") -> dict:
    if task_type == "weekly_report" and os.environ.get("FORCE_REPORT", "false").lower() not in {"1", "true", "yes"}:
        db_file = state_dir / "bot_metrics.db"
        init_db(db_file)
        now = datetime.now()
        week_start = (now - timedelta(days=now.weekday())).date().isoformat()
        try:
            with closing(connect(db_file)) as conn:
                count = int(conn.execute("""SELECT COUNT(*) FROM api_usage_events
                  WHERE provider='openai' AND operation='weekly_report' AND timestamp>=? AND success=1""",
                  (week_start,)).fetchone()[0])
            if count >= int(os.environ.get("OPENAI_WEEKLY_REVIEW_MAX_CALLS_PER_WEEK", "1")):
                return {"route": {}, "analysis": None, "error": "weekly_review_model_limit"}
        except Exception:
            pass
    # The wall-clock deadline is a scheduler guard. Direct/manual calls and
    # unit tests have no scheduled review_date metadata and must remain usable.
    if (task_type == "daily_review" and batch_metadata
            and batch_metadata.get("review_date")):
        deadline_raw = os.environ.get("DAILY_REVIEW_DEADLINE", "04:55")
        try:
            deadline_hour, deadline_minute = (int(value) for value in deadline_raw.split(":", 1))
            now_local = datetime.now(JST)
            deadline = now_local.replace(
                hour=deadline_hour, minute=deadline_minute, second=0, microsecond=0)
            if now_local > deadline and os.environ.get(
                "FORCE_REPORT", "false").lower() not in {"1", "true", "yes"}:
                return {"route": {"processing_mode": "local_only"}, "analysis": None,
                        "error": "daily_review_deadline_exceeded", "usage_event": {},
                        "pending": False, "batch_job": {}}
        except (TypeError, ValueError):
            pass
    router = ModelRouter(root_dir / "config" / "openai_model_pricing.json")
    state_path = state_dir / "openai_usage.json"
    state = load_usage_state(state_path)
    route = router.select_model(
        task_type,
        budget_state=state,
        daily_usage=today_usage(state),
        premium_requested=premium_requested,
    )
    result = {"route": route, "analysis": None, "error": "", "usage_event": {},
              "pending": False, "batch_job": {}}
    if not route.get("model"):
        result["error"] = route.get("skip_reason", "model_route_skip")
        return result
    from openai_batch import enabled as batch_enabled, submit_analysis
    if batch_enabled(task_type):
        submission = submit_analysis(
            task_type=task_type, payload=payload, model=route["model"],
            max_output_tokens=int(route["max_output_tokens"]), schema=ANALYSIS_SCHEMA,
            state_dir=state_dir, pricing=router.pricing,
            reasoning_effort=route.get("reasoning_effort", "none"),
            target_json_path=target_json_path, metadata=batch_metadata,
            dedupe_key=batch_dedupe_key, client_factory=client_factory,
        )
        result["pending"] = bool(submission.get("pending"))
        result["batch_job"] = submission.get("job") or {}
        result["analysis"] = submission.get("analysis")
        result["error"] = submission.get("error", "")
        route["processing_mode"] = "batch"
        route["used_model"] = route["model"]
        return result
    try:
        if client_factory is None:
            from openai import OpenAI
            client_factory = OpenAI
        timeout = max(15.0, float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "90")))
        if task_type == "daily_review":
            timeout = min(timeout, float(os.environ.get("DAILY_REVIEW_TIMEOUT_SECONDS", "600")))
        client = client_factory(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            timeout=timeout,
            max_retries=0,
        )
        system = (
            "You analyze performance data for a Japanese political-news X bot. "
            "Your goal is to maximize next-day impressions without weakening "
            "factuality, safety, trust, or account health. Use only supplied metrics "
            "and operational counts. Separate observations from recommendations. "
            "Every strategy finding must cite supplied tweet IDs and a measured "
            "metric; use confidence 0 to 1 and lower it for small samples. "
            "Recommend presentation choices only: post type, hook type, posting "
            "hour, text length, body structure, and closing style. Never propose "
            "changes to political positions, factual standards, safety thresholds, "
            "budgets, posting limits, credentials, or external publishing controls. "
            "Do not invent political facts, model details, prices, or credentials."
        )
        user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        model = route["model"]
        fallback_used = bool(route.get("fallback_used"))
        reservation_id = None
        budget_reason = ""
        for candidate in [model] + [m for m in route.get("fallback_models", []) if m != model]:
            reservation_id, budget_reason = reserve_budget(
                "openai", task_type, candidate,
                estimate_openai(candidate, 6000, int(route["max_output_tokens"])),
                path=state_dir / "bot_metrics.db",
            )
            if reservation_id:
                if candidate != model:
                    print(f"Budget downgrade: {model} -> {candidate}")
                    fallback_used = True
                    model = candidate
                break
        if not reservation_id:
            result["error"] = budget_reason
            return result

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
        event = record_usage(
            response=response,
            model=model,
            task_type=task_type,
            pricing=router.pricing,
            state_path=state_path,
            history_dir=state_dir / "openai_usage_history",
            fallback_used=fallback_used,
        )
        result["usage_event"] = event
        finalize_budget(
            reservation_id, float(event["estimated_cost_usd"]), success=True,
            input_tokens=event["input_tokens"], cached_tokens=event["cached_input_tokens"],
            output_tokens=event["output_tokens"], fallback_used=fallback_used,
            path=state_dir / "bot_metrics.db",
        )
    except Exception as exc:
        if "reservation_id" in locals():
            finalize_budget(reservation_id, 0, success=False, error_type=type(exc).__name__,
                            path=state_dir / "bot_metrics.db")
        result["error"] = "authentication_error_no_retry" if is_auth_error(exc) else type(exc).__name__
    return result
