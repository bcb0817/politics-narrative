"""Validate and activate bounded ChatGPT daily-review strategies.

The model may optimize presentation choices only. It cannot change safety
thresholds, budgets, posting limits, account identity, political positions, or
external publishing settings.
"""

from __future__ import annotations

import json
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
    active = {
        "version": 1,
        "source": "openai_daily_review",
        "generated_at": now.isoformat(),
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
        "experiment_name": re.sub(
            r"[^a-zA-Z0-9_-]", "_",
            str(policy.get("experiment_name") or "daily_review_strategy"),
        )[:60],
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
        expires = datetime.fromisoformat(str(data.get("expires_at") or ""))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=JST)
        if expires.astimezone(JST) <= now.astimezone(JST):
            return {}
        if not data.get("safety_locked"):
            return {}
        return data
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return {}


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
    }
