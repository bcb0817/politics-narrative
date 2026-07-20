import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if SITE_PACKAGES.exists():
    sys.path.insert(0, str(SITE_PACKAGES))
sys.path.insert(0, str(ROOT / "src"))

from model_router import ModelRouter, is_auth_error  # noqa: E402
from openai_usage import calculate_cost, load_usage_state, record_usage, today_usage  # noqa: E402
from report_ai import analyze_report, compact_daily_payload  # noqa: E402
import post  # noqa: E402


BASE_ENV = {
    "OPENAI_MODEL_CLASSIFIER": "gpt-5.4-nano",
    "OPENAI_MODEL_DEFAULT": "gpt-5.4-mini",
    "OPENAI_MODEL_IMPORTANT": "gpt-5.6-luna",
    "OPENAI_MODEL_DAILY_REVIEW": "gpt-5.6-luna",
    "OPENAI_MODEL_WEEKLY_REPORT": "gpt-5.6-terra",
    "OPENAI_MODEL_PREMIUM": "gpt-5.6-sol",
    "OPENAI_CLASSIFIER_ENABLED": "false",
    "WEEKLY_REPORT_ENABLED": "false",
    "OPENAI_PREMIUM_ENABLED": "false",
    "OPENAI_MONTHLY_BUDGET_USD": "8",
    "OPENAI_BUDGET_RESERVE_USD": "0.5",
    "DAILY_IMPORTANT_MODEL_LIMIT": "4",
    "DAILY_REVIEW_MODEL_LIMIT": "1",
    "OPENAI_MAX_RETRIES": "1",
}


class FakeResponse:
    status = "completed"
    output_text = json.dumps({
        "summary": "ok", "strengths": [], "weaknesses": [],
        "recommendations": [], "timing_findings": [],
    })
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=20,
        input_tokens_details=SimpleNamespace(cached_tokens=40),
    )


class FakeClient:
    calls = []
    failures = []

    def __init__(self, **kwargs):
        self.responses = self

    def create(self, **kwargs):
        self.__class__.calls.append(kwargs)
        if self.__class__.failures:
            raise self.__class__.failures.pop(0)
        return FakeResponse()


class ModelRouterTests(unittest.TestCase):
    def setUp(self):
        self.router = ModelRouter(ROOT / "config" / "openai_model_pricing.json")

    def route(self, task="post_generation", **kwargs):
        with patch.dict(os.environ, BASE_ENV, clear=False):
            return self.router.select_model(task, budget_state={"estimated_cost_usd": 0},
                                            daily_usage={}, **kwargs)

    def test_01_normal_post_uses_mini(self):
        self.assertEqual(self.route(text="通常の政策説明")["model"], "gpt-5.4-mini")

    def test_02_judicial_news_uses_luna(self):
        route = self.route(text="再審と司法制度の判決")
        self.assertEqual(route["model"], "gpt-5.6-luna")
        self.assertEqual(route["route_reason"], "important_judicial_news")

    def test_03_high_score_uses_luna(self):
        self.assertEqual(self.route(importance_score=8.1)["model"], "gpt-5.6-luna")

    def test_04_important_daily_limit_downgrades(self):
        with patch.dict(os.environ, BASE_ENV, clear=False):
            route = self.router.select_model("post_generation", text="首相の政策",
                                             daily_usage={"important_calls": 4})
        self.assertEqual(route["model"], "gpt-5.4-mini")

    def test_05_high_risk_at_limit_skips(self):
        with patch.dict(os.environ, BASE_ENV, clear=False):
            route = self.router.select_model("post_generation", text="逮捕と疑惑",
                                             claim_risk="high", daily_usage={"important_calls": 4})
        self.assertEqual(route["skip_reason"], "important_limit_high_risk")

    def test_06_budget_reached_skips(self):
        with patch.dict(os.environ, BASE_ENV, clear=False):
            route = self.router.select_model("post_generation",
                                             budget_state={"estimated_cost_usd": 7.99})
        self.assertEqual(route["skip_reason"], "openai_monthly_budget_guard")

    def test_07_budget_reserve_is_preserved(self):
        with patch.dict(os.environ, BASE_ENV, clear=False):
            route = self.router.select_model("daily_review",
                                             budget_state={"estimated_cost_usd": 7.4999})
        self.assertFalse(route["model"])

    def test_08_premium_disabled(self):
        self.assertEqual(self.route("premium_report", premium_requested=True)["skip_reason"],
                         "premium_disabled")

    def test_09_daily_review_uses_luna(self):
        self.assertEqual(self.route("daily_review")["model"], "gpt-5.6-luna")

    def test_10_daily_review_limit(self):
        with patch.dict(os.environ, BASE_ENV, clear=False):
            route = self.router.select_model("daily_review", daily_usage={"daily_review_calls": 1})
        self.assertEqual(route["skip_reason"], "daily_review_model_limit")

    def test_11_weekly_disabled(self):
        self.assertEqual(self.route("weekly_report")["skip_reason"], "weekly_report_disabled")

    def test_12_classifier_disabled_uses_local(self):
        self.assertEqual(self.route("classifier")["route_reason"], "local_classifier")

    def test_13_auth_error_detection(self):
        self.assertTrue(is_auth_error(RuntimeError("Incorrect API key")))
        self.assertFalse(is_auth_error(RuntimeError("model unavailable")))

    def test_14_legacy_env_only_still_routes(self):
        env = {"OPENAI_MODEL_DEFAULT": "legacy-default", "OPENAI_MODEL_IMPORTANT": "legacy-important",
               "OPENAI_MONTHLY_BUDGET_USD": "0"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.router.select_model("post_generation")["model"], "legacy-default")


class UsageAndReportTests(unittest.TestCase):
    def test_15_cost_includes_cached_input(self):
        pricing = {"m": {"input_per_million": 1, "cached_input_per_million": .1,
                         "output_per_million": 2}}
        self.assertAlmostEqual(calculate_cost(pricing, "m", 1000, 500, 1000), .00255)

    def test_16_usage_state_and_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pricing = json.loads((ROOT / "config" / "openai_model_pricing.json").read_text())
            record_usage(response=FakeResponse(), model="gpt-5.6-luna", task_type="daily_review",
                         pricing=pricing, state_path=root / "usage.json", history_dir=root / "history")
            state = load_usage_state(root / "usage.json")
            self.assertEqual(state["calls"], 1)
            self.assertEqual(today_usage(state)["daily_review_calls"], 1)
            self.assertEqual(len(list((root / "history").glob("*.jsonl"))), 1)

    def test_17_compaction_deduplicates_posts(self):
        row = {"tweet_id": "1", "text": "x" * 500}
        compact = compact_daily_payload({"top_impressions_3": [row], "top_growth_3": [row],
                                         "bottom_3": [], "quality_errors": list(range(10))})
        self.assertEqual(len(compact["samples"]), 1)
        self.assertEqual(len(compact["samples"][0]["text"]), 280)
        self.assertEqual(len(compact["quality_errors"]), 5)

    def test_18_daily_analysis_success_is_one_call(self):
        FakeClient.calls, FakeClient.failures = [], []
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, BASE_ENV, clear=False):
            result = analyze_report(task_type="daily_review", payload={}, root_dir=ROOT,
                                    state_dir=Path(td), client_factory=FakeClient)
        self.assertEqual(result["analysis"]["summary"], "ok")
        self.assertEqual(len(FakeClient.calls), 1)

    def test_19_auth_failure_has_no_retry(self):
        FakeClient.calls = []
        FakeClient.failures = [RuntimeError("Incorrect API key")]
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, BASE_ENV, clear=False):
            result = analyze_report(task_type="daily_review", payload={}, root_dir=ROOT,
                                    state_dir=Path(td), client_factory=FakeClient)
        self.assertEqual(result["error"], "authentication_error_no_retry")
        self.assertEqual(len(FakeClient.calls), 1)

    def test_20_unavailable_model_falls_back_once(self):
        FakeClient.calls = []
        FakeClient.failures = [RuntimeError("model unavailable")]
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, BASE_ENV, clear=False):
            result = analyze_report(task_type="daily_review", payload={}, root_dir=ROOT,
                                    state_dir=Path(td), client_factory=FakeClient)
        self.assertFalse(result["error"])
        self.assertEqual(len(FakeClient.calls), 2)
        self.assertTrue(result["route"]["fallback_used"])

    def test_21_candidate_schema_has_review_fields(self):
        required = set(post.CANDIDATE_RESPONSE_SCHEMA["properties"]["candidates"]["items"]["required"])
        self.assertTrue({"importance_score", "source_reliability_score", "claim_risk",
                         "final_text", "quality_score"}.issubset(required))

    def test_22_model_name_is_rejected_from_public_body(self):
        candidate = {"tweet_text": "🚨 見出し\n\n政策本文 gpt-5.6-luna " + "説明" * 60}
        violations = post._candidate_quality_violations(candidate, {})
        self.assertTrue(any(item.startswith("meta_leak:") for item in violations))


if __name__ == "__main__":
    unittest.main()
