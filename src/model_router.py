"""Central OpenAI model routing for posts and reports."""

from __future__ import annotations

import os
from pathlib import Path

from openai_usage import calculate_cost, load_pricing


HIGH_RISK_TERMS = (
    "選挙結果", "辞任", "逮捕", "犯罪", "疑惑", "死亡", "病気", "開戦", "停戦",
    "災害", "再審", "判決", "司法", "検察", "証拠", "移民", "難民",
)
IMPORTANT_TERMS = (
    "選挙", "首相", "閣僚", "党首", "税制改革", "社会保障", "外交", "関税交渉",
    "防衛", "安全保障", "戦争", "停戦", "移民", "難民", "司法", "判決", "再審",
    "逮捕", "災害", "重大事件", "与党案", "野党案", "一次資料",
)


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes"}


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


def is_auth_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status in {401, 403} or any(term in text for term in (
        "invalid api key", "incorrect api key", "authentication", "unauthorized",
    ))


class ModelRouter:
    def __init__(self, pricing_path: Path):
        self.pricing_path = Path(pricing_path)
        self.pricing = load_pricing(self.pricing_path)

    def estimate_max_cost(self, model: str, max_output_tokens: int, input_tokens: int = 5000) -> float:
        return calculate_cost(self.pricing, model, input_tokens, 0, max_output_tokens)

    def _affordable(self, model: str, max_output_tokens: int, budget_state: dict,
                    task_type: str = "post_generation") -> bool:
        budget = _float("OPENAI_MONTHLY_BUDGET_USD", 8.0)
        reserve = max(0.0, _float("OPENAI_BUDGET_RESERVE_USD", 0.50))
        if budget <= 0:
            return True
        if model not in self.pricing:
            return False
        # Preserve additional headroom for posting. Optional long reports are
        # the first work to be denied as the monthly ceiling approaches.
        priority_hold = {
            "daily_review": 0.25,
            "classifier": 0.50,
            "weekly_report": 1.00,
            "premium_report": 2.00,
        }.get(task_type, 0.0)
        spent = float((budget_state or {}).get("estimated_cost_usd", 0.0) or 0.0)
        ceiling = max(0.0, budget - reserve - priority_hold)
        return spent + self.estimate_max_cost(model, max_output_tokens) <= ceiling

    def select_model(
        self,
        task_type: str,
        importance_score: float = 0.0,
        genre: str = "",
        source_reliability: float = 0.0,
        claim_risk: str = "low",
        budget_state: dict | None = None,
        daily_usage: dict | None = None,
        text: str = "",
        premium_requested: bool = False,
    ) -> dict:
        budget_state = budget_state or {}
        daily_usage = daily_usage or {}
        legacy_default = os.environ.get("OPENAI_MODEL_DEFAULT", "gpt-5.4-mini").strip() or "gpt-5.4-mini"
        legacy_important = os.environ.get("OPENAI_MODEL_IMPORTANT", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
        settings = {
            "classifier": (
                os.environ.get("OPENAI_MODEL_CLASSIFIER", "gpt-5.4-nano"),
                os.environ.get("OPENAI_REASONING_EFFORT_CLASSIFIER", "none"),
                _int("OPENAI_MAX_OUTPUT_TOKENS_POST", 1400),
                ["gpt-5-nano"],
            ),
            "default": (
                legacy_default,
                os.environ.get("OPENAI_REASONING_EFFORT_DEFAULT", os.environ.get("OPENAI_REASONING_EFFORT", "none")),
                _int("OPENAI_MAX_OUTPUT_TOKENS_POST", 1400),
                [os.environ.get("OPENAI_MODEL_CLASSIFIER", "gpt-5.4-nano")],
            ),
            "important": (
                legacy_important,
                os.environ.get("OPENAI_REASONING_EFFORT_IMPORTANT", os.environ.get("OPENAI_REASONING_EFFORT", "low")),
                _int("OPENAI_MAX_OUTPUT_TOKENS_POST", 1400),
                [legacy_default, os.environ.get("OPENAI_MODEL_CLASSIFIER", "gpt-5.4-nano")],
            ),
            "daily_review": (
                os.environ.get("OPENAI_MODEL_DAILY_REVIEW", legacy_important),
                os.environ.get("OPENAI_REASONING_EFFORT_DAILY_REVIEW", "low"),
                _int("OPENAI_MAX_OUTPUT_TOKENS_DAILY_REVIEW", 3000),
                [legacy_default, os.environ.get("OPENAI_MODEL_CLASSIFIER", "gpt-5.4-nano")],
            ),
            "weekly_report": (
                os.environ.get("OPENAI_MODEL_WEEKLY_REPORT", "gpt-5.6-terra"),
                os.environ.get("OPENAI_REASONING_EFFORT_WEEKLY_REPORT", "medium"),
                _int("OPENAI_MAX_OUTPUT_TOKENS_WEEKLY_REPORT", 6000),
                [os.environ.get("OPENAI_MODEL_DAILY_REVIEW", legacy_important), legacy_default],
            ),
            "premium_report": (
                os.environ.get("OPENAI_MODEL_PREMIUM", "gpt-5.6-sol"),
                os.environ.get("OPENAI_REASONING_EFFORT_PREMIUM", "medium"),
                _int("OPENAI_MAX_OUTPUT_TOKENS_WEEKLY_REPORT", 6000),
                [],
            ),
        }

        if task_type == "classifier" and not _bool("OPENAI_CLASSIFIER_ENABLED"):
            return {"model": "", "reasoning_effort": "none", "max_output_tokens": 0,
                    "route_reason": "local_classifier", "fallback_models": [], "skip_reason": ""}
        if task_type == "weekly_report" and not _bool("WEEKLY_REPORT_ENABLED"):
            return {"model": "", "reasoning_effort": "none", "max_output_tokens": 0,
                    "route_reason": "weekly_report_disabled", "fallback_models": [], "skip_reason": "weekly_report_disabled"}
        if task_type == "premium_report" and (not premium_requested or not _bool("OPENAI_PREMIUM_ENABLED")):
            return {"model": "", "reasoning_effort": "none", "max_output_tokens": 0,
                    "route_reason": "premium_disabled", "fallback_models": [], "skip_reason": "premium_disabled"}

        route_key = task_type
        route_reason = task_type
        high_risk = claim_risk.lower() == "high" or any(term in text for term in HIGH_RISK_TERMS)
        if task_type == "post_generation":
            threshold = _float("IMPORTANT_NEWS_SCORE_THRESHOLD", 8.0)
            important = (
                float(importance_score or 0.0) >= threshold
                or any(term in text for term in IMPORTANT_TERMS)
                or high_risk
                or genre in {"安全保障", "移民政策"}
            )
            route_key = "important" if important else "default"
            route_reason = "important_news" if important else "normal_news"
            if any(term in text for term in ("司法", "判決", "再審", "検察")):
                route_reason = "important_judicial_news"
            limit = max(0, _int("DAILY_IMPORTANT_MODEL_LIMIT", 4))
            if route_key == "important" and int(daily_usage.get("important_calls", 0) or 0) >= limit:
                route_key = "default"
                route_reason = "important_daily_limit_fallback"
                if high_risk:
                    return {"model": "", "reasoning_effort": "none", "max_output_tokens": 0,
                            "route_reason": route_reason, "fallback_models": [],
                            "skip_reason": "important_limit_high_risk"}
        elif task_type == "daily_review":
            limit = max(0, _int("DAILY_REVIEW_MODEL_LIMIT", 1))
            if int(daily_usage.get("daily_review_calls", 0) or 0) >= limit:
                return {"model": "", "reasoning_effort": "none", "max_output_tokens": 0,
                        "route_reason": "daily_review_limit", "fallback_models": [],
                        "skip_reason": "daily_review_model_limit"}

        model, effort, max_tokens, fallbacks = settings[route_key]
        candidates = [model] + [item for item in fallbacks if item and item != model]
        selected = ""
        fallback_used = False
        for index, candidate in enumerate(candidates):
            if self._affordable(candidate, max_tokens, budget_state, task_type):
                selected = candidate
                fallback_used = index > 0
                break
        if not selected:
            return {"model": "", "reasoning_effort": effort, "max_output_tokens": max_tokens,
                    "route_reason": route_reason, "fallback_models": candidates[1:],
                    "skip_reason": "openai_monthly_budget_guard", "fallback_used": False}
        return {
            "model": selected,
            "reasoning_effort": effort,
            "max_output_tokens": max_tokens,
            "route_reason": route_reason,
            "fallback_models": [item for item in candidates if item != selected],
            "skip_reason": "",
            "fallback_used": fallback_used,
            "importance_score": float(importance_score or 0.0),
            "source_reliability": float(source_reliability or 0.0),
            "claim_risk": "high" if high_risk else claim_risk,
        }


def select_model(*args, **kwargs) -> dict:
    root = Path(__file__).resolve().parent.parent
    return ModelRouter(root / "config" / "openai_model_pricing.json").select_model(*args, **kwargs)
