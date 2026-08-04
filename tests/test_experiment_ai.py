import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import experiment_ai
import post_experiments


class _Responses:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class _Factory:
    def __init__(self, response):
        self.responses = _Responses(response)
        self.init_kwargs = None

    def __call__(self, **kwargs):
        self.init_kwargs = kwargs
        return self


class ExperimentAiTests(unittest.TestCase):
    def test_disabled_never_constructs_client(self):
        factory = _Factory(None)
        with patch.dict(os.environ, {
                "POST_EXPERIMENT_OPENAI_ENABLED": "false"}, clear=False):
            result = experiment_ai.generate_fact_packet_variants(
                {"title": "事実"}, 3, client_factory=factory)
        self.assertEqual(result["status"], "disabled")
        self.assertIsNone(factory.init_kwargs)

    def test_structured_request_contains_fact_packet_not_outcomes(self):
        variants = [
            {"angle_type": f"角度{i}", "text": "確認済み事実10"}
            for i in range(3)
        ]
        response = SimpleNamespace(
            output_text=json.dumps({"variants": variants}, ensure_ascii=False),
            usage=SimpleNamespace(
                input_tokens=120, output_tokens=80,
                input_tokens_details=SimpleNamespace(cached_tokens=10)),
        )
        factory = _Factory(response)
        packet = {"title": "確認済み", "numbers": ["10"], "sources": ["一次資料"]}
        with patch.dict(os.environ, {
            "POST_EXPERIMENT_OPENAI_ENABLED": "true",
            "OPENAI_API_KEY": "test-only",
            "POST_EXPERIMENT_OPENAI_MODEL": "test-model",
        }, clear=False), patch.object(
            experiment_ai, "estimate_openai", side_effect=[0.01, 0.002]
        ), patch.object(
            experiment_ai, "reserve", return_value=(7, "")
        ) as reserve_mock, patch.object(
            experiment_ai, "finalize"
        ) as finalize_mock:
            result = experiment_ai.generate_fact_packet_variants(
                packet, 3, client_factory=factory)
        self.assertEqual(result["status"], "generated")
        request = factory.responses.kwargs
        sent = json.loads(request["input"])
        self.assertEqual(sent["fact_packet"], packet)
        self.assertNotIn("outcomes", request["input"])
        self.assertNotIn("historical", request["input"])
        self.assertFalse(request["store"])
        self.assertEqual(
            request["text"]["format"]["schema"]["properties"]["variants"][
                "minItems"], 3)
        self.assertFalse(reserve_mock.call_args.kwargs["metadata"][
            "publishing_allowed"])
        finalize_mock.assert_called_once()

    def test_invented_number_is_rejected_and_budget_finalized(self):
        response = SimpleNamespace(
            output_text=json.dumps({"variants": [
                {"angle_type": "数字", "text": "確認されていない99件"}
            ]}, ensure_ascii=False),
            usage=None,
        )
        factory = _Factory(response)
        with patch.dict(os.environ, {
            "POST_EXPERIMENT_OPENAI_ENABLED": "true",
            "OPENAI_API_KEY": "test-only",
            "POST_EXPERIMENT_OPENAI_MODEL": "test-model",
        }, clear=False), patch.object(
            experiment_ai, "estimate_openai", return_value=0.01
        ), patch.object(
            experiment_ai, "reserve", return_value=(8, "")
        ), patch.object(experiment_ai, "finalize") as finalize_mock:
            result = experiment_ai.generate_fact_packet_variants(
                {"title": "確認済み10件"}, 1, client_factory=factory)
        self.assertEqual(result["status"], "generation_failed")
        self.assertEqual(result["error_type"], "ValueError")
        self.assertFalse(finalize_mock.call_args.kwargs["success"])

    def test_generation_failure_falls_back_to_local_candidates(self):
        content = {
            "content_id": "x", "text": "確認済みニュース",
            "fact_packet": {"title": "確認済みニュース", "summary": "要約"},
        }
        with patch(
            "experiment_ai.generate_fact_packet_variants",
            return_value={"variants": None, "status": "budget_restricted"},
        ):
            result = post_experiments.generate_candidates(
                content, use_openai=True)
        self.assertEqual(
            result["variant_generation"]["status"], "budget_restricted")
        self.assertGreaterEqual(len(result["variants"]), 3)
        self.assertFalse(result["auto_publish"])
        self.assertEqual(result["selection_status"], "analysis_only")


if __name__ == "__main__":
    unittest.main()
