"""Validate and activate bounded ChatGPT daily-review strategies.

The model may optimize presentation choices only. It cannot change safety
thresholds, budgets, posting limits, account identity, political positions, or
external publishing settings.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
POST_TYPES = {
    "issue_diagram", "strong_opinion", "comparison_factcheck",
    "steelman_counterargument",
}
HOOK_TYPES = {
    "fact_reversal", "issue_redefinition", "number", "contrast",
    "question", "conclusion_first",
}
BODY_STRUCTURES = {
    "fact_impact_accountability_improvement",
    "claim_evidence_conclusion",
    "before_after_comparison",
    "timeline_cause_response",
}
CTA_STYLES = {
    "specific_accountability_question", "improvement_request",
    "source_check", "no_question",
}
STRATEGY_FILE = Path("knowledge") / "viral_patterns" / "chatgpt_strategy.json"
STRATEGY_MARKDOWN = (
    Path("knowledge") / "viral_patterns" / "chatgpt_strategy.md"
)
STRATEGY_HISTORY = Path("data") / "chatgpt_strategy_history.jsonl"


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _strategy_id(generated_at: str, experiment_name: str) -> str:
    raw = f"{generated_at}|{experiment_name}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _append_history(root_dir: Path, event: dict) -> None:
    """Append a bounded, credential-free strategy lifecycle event."""
    path = root_dir / STRATEGY_HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        key: value for key, value in event.items()
        if key in {
            "event", "at", "strategy_id", "experiment_name", "reason",
            "status", "treatment_count", "control_count",
            "treatment_impressions_per_hour",
            "control_impressions_per_hour", "performance_ratio",
        }
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False) + "\n")


def strategy_history(root_dir: Path, limit: int = 20) -> list[dict]:
    try:
        lines = (root_dir / STRATEGY_HISTORY).read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return []
    output = []
    for line in lines[-max(1, min(100, int(limit))):]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            output.append(row)
    return output


def assignment_for(
    item: dict,
    *,
    strategy: dict | None = None,
    root_dir: Path | None = None,
    now: datetime | None = None,
) -> str:
    """Deterministically assign an eligible item to treatment or control."""
    if strategy is None:
        strategy = load_active_strategy(root_dir or Path.cwd(), now=now)
    if not strategy:
        return "inactive"
    ratio = _float(
        "CHATGPT_DAILY_STRATEGY_TREATMENT_RATIO",
        float(strategy.get("treatment_ratio", 0.8) or 0.8),
        0.5,
        0.9,
    )
    identity = "|".join([
        str(strategy.get("strategy_id") or ""),
        str(item.get("topic_key") or ""),
        str(item.get("title") or item.get("tweet_id") or ""),
    ])
    bucket = int(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8], 16)
    return "treatment" if (bucket % 10000) < int(ratio * 10000) else "control"


def alignment_bonus(
    strategy: dict,
    *,
    variant: str,
    post_type: str,
    hook_type: str,
    posted_hour_jst: int,
) -> float:
    """Return a small presentation-only ranking bonus capped at 0.25."""
    if not strategy or variant != "treatment":
        return 0.0
    bonus = 0.0
    post_types = strategy.get("post_type_priority") or []
    hooks = strategy.get("hook_type_priority") or []
    if post_type in post_types:
        bonus += max(0.02, 0.10 - post_types.index(post_type) * 0.02)
    if hook_type in hooks:
        bonus += max(0.02, 0.08 - hooks.index(hook_type) * 0.015)
    if int(posted_hour_jst) in (strategy.get("preferred_hours_jst") or []):
        bonus += 0.07
    return round(min(0.25, bonus), 3)


def summarize_operational_logs(
    log_dir: Path, start_at: datetime, end_at: datetime
) -> dict:
    """Return bounded counts only; raw log text and credentials are excluded."""
    decisions: Counter[str] = Counter()
    skip_reasons: Counter[str] = Counter()
    warnings: Counter[str] = Counter()
    attempts_path = log_dir / "post_attempts.jsonl"
    try:
        lines = attempts_path.read_text(encoding="utf-8").splitlines()[-1000:]
    except (FileNotFoundError, OSError):
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
            when = datetime.fromisoformat(str(row.get("ts_jst") or ""))
            if when.tzinfo is None:
                when = when.replace(tzinfo=JST)
            when = when.astimezone(JST)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not start_at <= when <= end_at:
            continue
        decision = str(row.get("decision") or "unknown")
        reason = str(row.get("reason") or "unknown")
        decisions[decision] += 1
        if decision == "skip":
            skip_reasons[reason] += 1

    bot_path = log_dir / "bot.log"
    try:
        bot_lines = bot_path.read_text(
            encoding="utf-8", errors="replace").splitlines()[-5000:]
    except (FileNotFoundError, OSError):
        bot_lines = []
    categories = {
        "source_fetch_error": re.compile(r"取得エラー|fetch_all_items failed"),
        "openai_generation_error": re.compile(
            r"OpenAI.*(?:failed|unavailable|incomplete|no output)",
            re.IGNORECASE,
        ),
        "candidate_quality_rejection": re.compile(
            r"Candidate rejected|quality rejection", re.IGNORECASE),
        "fallback_activation": re.compile(
            r"fallback.*active=true|fallback applied", re.IGNORECASE),
        "x_post_failure": re.compile(r"post_to_x_failed|X posting failed"),
        "threads_failure": re.compile(r"Threads.*(?:failed|error)", re.IGNORECASE),
    }
    for line in bot_lines:
        timestamp = re.match(
            r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
        if not timestamp:
            continue
        try:
            when = datetime.fromisoformat(timestamp.group(1)).replace(
                tzinfo=JST)
        except ValueError:
            continue
        if not start_at <= when <= end_at:
            continue
        for category, pattern in categories.items():
            if pattern.search(line):
                warnings[category] += 1
    return {
        "decisions": dict(decisions.most_common(10)),
        "skip_reasons": dict(skip_reasons.most_common(10)),
        "operational_signals": dict(warnings.most_common(10)),
        "raw_log_text_shared_with_model": False,
        "credentials_shared_with_model": False,
    }


def _unique_allowed(values: Iterable, allowed: set[str], limit: int) -> list[str]:
    out: list[str] = []
    for value in values or []:
        value = str(value)
        if value in allowed and value not in out:
            out.append(value)
    return out[:limit]


def _safe_evidence(strategy: dict, valid_tweet_ids: set[str]) -> list[dict]:
    output = []
    for row in strategy.get("evidence", [])[:8]:
        if not isinstance(row, dict):
            continue
        ids = [
            str(value) for value in row.get("tweet_ids", [])
            if str(value) in valid_tweet_ids
        ][:5]
        try:
            confidence = max(0.0, min(1.0, float(row.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        if not ids or confidence < 0.55:
            continue
        output.append({
            "finding": str(row.get("finding") or "")[:180],
            "tweet_ids": ids,
            "metric": str(row.get("metric") or "")[:80],
            "confidence": round(confidence, 3),
        })
    return output


def activate_strategy(
    analysis: dict | None,
    review_payload: dict,
    *,
    root_dir: Path,
    now: datetime | None = None,
) -> dict:
    """Validate model output and atomically activate presentation-only rules."""
    now = now or datetime.now(JST)
    enabled = _bool("CHATGPT_DAILY_STRATEGY_ENABLED", True)
    auto_apply = _bool("CHATGPT_DAILY_STRATEGY_AUTO_APPLY", True)
    reviewed_count = int(review_payload.get("reviewed_count", 0) or 0)
    minimum_samples = _int(
        "CHATGPT_DAILY_STRATEGY_MIN_POSTS", 3, 2, 20)
    result = {
        "enabled": enabled,
        "auto_apply": auto_apply,
        "activated": False,
        "reason": "",
        "strategy_path": str(root_dir / STRATEGY_FILE),
    }
    if not enabled or not auto_apply:
        result["reason"] = "strategy_disabled"
        return result
    if reviewed_count < minimum_samples:
        result["reason"] = "insufficient_review_samples"
        return result
    prior_evaluation = review_payload.get("prior_strategy_evaluation") or {}
    current = review_payload.get("current_active_strategy") or {}
    if (
        current.get("active")
        and prior_evaluation.get("status") in {"insufficient_data", "keep"}
    ):
        result["reason"] = (
            "existing_strategy_collecting_samples"
            if prior_evaluation.get("status") == "insufficient_data"
            else "existing_strategy_retained"
        )
        result["policy"] = current.get("strategy") or {}
        result["expires_at"] = result["policy"].get("expires_at", "")
        return result
    if not isinstance(analysis, dict):
        result["reason"] = "llm_analysis_unavailable"
        return result
    proposed = analysis.get("impression_strategy")
    if not isinstance(proposed, dict):
        result["reason"] = "strategy_missing"
        return result
    valid_ids = {
        str(row.get("tweet_id")) for row in review_payload.get("all_posts", [])
        if row.get("tweet_id")
    }
    evidence = _safe_evidence(proposed, valid_ids)
    if not evidence:
        result["reason"] = "strategy_has_no_metric_evidence"
        return result
    policy = proposed.get("next_day_policy") or {}
    post_types = _unique_allowed(
        policy.get("post_type_priority"), POST_TYPES, 4)
    hook_types = _unique_allowed(
        policy.get("hook_type_priority"), HOOK_TYPES, 4)
    body_structure = str(policy.get("body_structure") or "")
    if body_structure not in BODY_STRUCTURES:
        body_structure = "fact_impact_accountability_improvement"
    cta_style = str(policy.get("cta_style") or "")
    if cta_style not in CTA_STYLES:
        cta_style = "specific_accountability_question"
    preferred_hours = []
    for value in policy.get("preferred_hours_jst", [])[:8]:
        try:
            hour = int(value)
        except (TypeError, ValueError):
            continue
        if 5 <= hour <= 23 and hour not in preferred_hours:
            preferred_hours.append(hour)
    try:
        target_min = int(policy.get("target_text_min", 120))
        target_max = int(policy.get("target_text_max", 240))
    except (TypeError, ValueError):
        target_min, target_max = 120, 240
    target_min = max(100, min(200, target_min))
    target_max = max(target_min + 20, min(260, target_max))
    expires_hours = _int(
        "CHATGPT_DAILY_STRATEGY_TTL_HOURS", 48, 12, 168)
    experiment_name = re.sub(
        r"[^a-zA-Z0-9_-]", "_",
        str(policy.get("experiment_name") or "daily_review_strategy"),
    )[:60]
    if (
        prior_evaluation.get("status") == "rollback"
        and prior_evaluation.get("experiment_name") == experiment_name
    ):
        result["reason"] = "rolled_back_experiment_not_reactivated"
        return result
    generated_at = now.isoformat()
    active = {
        "version": 2,
        "active": True,
        "source": "openai_daily_review",
        "generated_at": generated_at,
        "expires_at": (now + timedelta(hours=expires_hours)).isoformat(),
        "reviewed_count": reviewed_count,
        "objective": "maximize_impressions_with_safety_and_trust_unchanged",
        "summary": str(proposed.get("summary") or "")[:500],
        "evidence": evidence,
        "post_type_priority": post_types,
        "hook_type_priority": hook_types,
        "preferred_hours_jst": preferred_hours,
        "target_text_min": target_min,
        "target_text_max": target_max,
        "body_structure": body_structure,
        "cta_style": cta_style,
        "experiment_name": experiment_name,
        "strategy_id": _strategy_id(generated_at, experiment_name),
        "treatment_ratio": _float(
            "CHATGPT_DAILY_STRATEGY_TREATMENT_RATIO", 0.8, 0.5, 0.9),
        "safety_locked": True,
        "posting_limits_locked": True,
        "budgets_locked": True,
        "political_position_locked": True,
    }
    target = root_dir / STRATEGY_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(active, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    markdown = root_dir / STRATEGY_MARKDOWN
    markdown.write_text(render_prompt_guidance(active), encoding="utf-8")
    _append_history(root_dir, {
        "event": "activated",
        "at": now.isoformat(),
        "strategy_id": active["strategy_id"],
        "experiment_name": active["experiment_name"],
        "reason": "validated_strategy_activated",
    })
    result.update({
        "activated": True,
        "reason": "validated_strategy_activated",
        "expires_at": active["expires_at"],
        "policy": active,
    })
    return result


def load_active_strategy(
    root_dir: Path, *, now: datetime | None = None
) -> dict:
    if not _bool("CHATGPT_DAILY_STRATEGY_ENABLED", True):
        return {}
    now = now or datetime.now(JST)
    try:
        data = json.loads(
            (root_dir / STRATEGY_FILE).read_text(encoding="utf-8"))
        if data.get("active", True) is not True:
            return {}
        expires = datetime.fromisoformat(str(data.get("expires_at") or ""))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=JST)
        if expires.astimezone(JST) <= now.astimezone(JST):
            return {}
        if not data.get("safety_locked"):
            return {}
        if not data.get("strategy_id"):
            data["strategy_id"] = _strategy_id(
                str(data.get("generated_at") or ""),
                str(data.get("experiment_name") or "daily_review_strategy"),
            )
        data.setdefault(
            "treatment_ratio",
            _float(
                "CHATGPT_DAILY_STRATEGY_TREATMENT_RATIO",
                0.8,
                0.5,
                0.9,
            ),
        )
        return data
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return {}


def evaluate_strategy_performance(
    review_payload: dict,
    strategy: dict,
    *,
    root_dir: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Compare treatment/control outcomes and recommend keep or rollback."""
    now = now or datetime.now(JST)
    if not strategy:
        return {"status": "inactive", "reason": "no_active_strategy"}
    strategy_id = str(strategy.get("strategy_id") or "")
    experiment = str(strategy.get("experiment_name") or "")
    matched = [
        row for row in review_payload.get("all_posts", [])
        if (
            (strategy_id and row.get("review_strategy_id") == strategy_id)
            or (
                not strategy_id
                and row.get("review_strategy_experiment") == experiment
            )
        )
    ]
    treatment = [
        row for row in matched
        if row.get("review_strategy_variant") == "treatment"
    ]
    control = [
        row for row in matched
        if row.get("review_strategy_variant") == "control"
    ]

    def average(rows: list[dict], key: str) -> float:
        values = []
        for row in rows:
            try:
                values.append(float(row.get(key, 0) or 0))
            except (TypeError, ValueError):
                continue
        return round(sum(values) / len(values), 3) if values else 0.0

    def unsafe_row(row: dict) -> bool:
        raw_safety = row.get("safety_score")
        try:
            safety = 10.0 if raw_safety is None else float(raw_safety)
        except (TypeError, ValueError):
            safety = 10.0
        return (
            bool(row.get("correction_required"))
            or bool(row.get("manual_delete_required"))
            or bool(row.get("manual_delete"))
            or safety < 7
        )

    unsafe = any(unsafe_row(row) for row in treatment)
    minimum = _int("CHATGPT_STRATEGY_EVAL_MIN_PER_ARM", 3, 2, 20)
    treatment_rate = average(treatment, "impressions_per_hour")
    control_rate = average(control, "impressions_per_hour")
    ratio = (
        round(treatment_rate / control_rate, 3)
        if control_rate > 0 else None
    )
    result = {
        "status": "insufficient_data",
        "reason": "minimum_samples_not_met",
        "strategy_id": strategy_id,
        "experiment_name": experiment,
        "treatment_count": len(treatment),
        "control_count": len(control),
        "treatment_impressions_per_hour": treatment_rate,
        "control_impressions_per_hour": control_rate,
        "performance_ratio": ratio,
    }
    if unsafe:
        result.update({
            "status": "rollback",
            "reason": "treatment_safety_regression",
        })
    elif len(treatment) >= minimum and len(control) >= minimum:
        threshold = _float(
            "CHATGPT_STRATEGY_ROLLBACK_RATIO", 0.8, 0.5, 0.95)
        if ratio is not None and ratio < threshold:
            result.update({
                "status": "rollback",
                "reason": "treatment_underperformed_control",
            })
        else:
            result.update({
                "status": "keep",
                "reason": "treatment_not_materially_worse",
            })
    if root_dir is not None:
        _append_history(root_dir, {
            "event": "evaluated",
            "at": now.isoformat(),
            **result,
        })
    return result


def deactivate_strategy(
    root_dir: Path,
    *,
    reason: str,
    now: datetime | None = None,
) -> dict:
    """Disable the current strategy without deleting its audit record."""
    now = now or datetime.now(JST)
    target = root_dir / STRATEGY_FILE
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"deactivated": False, "reason": "no_strategy_file"}
    data["active"] = False
    data["deactivated_at"] = now.isoformat()
    data["deactivation_reason"] = str(reason)[:160]
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    (root_dir / STRATEGY_MARKDOWN).write_text(
        "# ChatGPT日次レビュー方針\n\n現在は無効です。\n",
        encoding="utf-8",
    )
    _append_history(root_dir, {
        "event": "deactivated",
        "at": now.isoformat(),
        "strategy_id": data.get("strategy_id", ""),
        "experiment_name": data.get("experiment_name", ""),
        "reason": str(reason)[:160],
    })
    return {
        "deactivated": True,
        "reason": reason,
        "strategy_id": data.get("strategy_id", ""),
    }


def render_prompt_guidance(strategy: dict) -> str:
    if not strategy:
        return ""
    structures = {
        "fact_impact_accountability_improvement":
            "確認済み事実→生活・制度への影響→責任主体→改善要求",
        "claim_evidence_conclusion": "結論→根拠→具体例→結論",
        "before_after_comparison": "変更前→変更後→差分→検証点",
        "timeline_cause_response": "時系列→原因→対応→残る課題",
    }
    ctas = {
        "specific_accountability_question": "責任主体と期限を明示した質問を1つ",
        "improvement_request": "具体的改善要求で締める",
        "source_check": "一次資料で確認すべき点を示す",
        "no_question": "質問で終えず結論で締める",
    }
    lines = [
        "## ChatGPT日次レビューの検証済み方針",
        f"- 文章構造: {structures.get(strategy.get('body_structure'), '')}",
        f"- 締め方: {ctas.get(strategy.get('cta_style'), '')}",
        f"- 推奨文字数: {strategy.get('target_text_min', 120)}〜"
        f"{strategy.get('target_text_max', 240)}字",
    ]
    if strategy.get("hook_type_priority"):
        lines.append(
            "- 優先フック: "
            + ", ".join(strategy["hook_type_priority"]))
    lines.extend([
        "- 安全基準、事実確認、投稿上限、予算、政治的評価原則は変更しない",
        "",
    ])
    return "\n".join(lines)


def strategy_status(root_dir: Path) -> dict:
    active = load_active_strategy(root_dir)
    return {
        "active": bool(active),
        "strategy": active,
        "path": str(root_dir / STRATEGY_FILE),
        "history": strategy_history(root_dir, 10),
    }
