import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from politics_multistage import (  # noqa: E402
    CallLimitExceeded,
    MultiStagePipeline,
    PipelineConfig,
    angle_distinctness_reasons,
    call_structured_json_with_retry,
    contains_banned_phrase,
    fit_platform_text,
    independent_platform_versions,
    quality_score,
    safety_rejection_reasons,
    select_winner,
    semantic_duplicate,
)


def analysis():
    return {
        "verified_facts": [{"fact": "政府が法案を国会に提出した",
                            "source_url": "https://example.test/a",
                            "confidence": 1.0}],
        "main_event": "法案提出", "key_actors": ["政府", "国会"],
        "decision_maker": "国会", "beneficiaries": ["対象世帯"],
        "cost_bearers": ["納税者"], "official_explanation": "支援拡充",
        "practical_effect": "対象世帯への給付", "contradictions": [],
        "accountability_question": "財源説明を誰が担うか",
        "impact_on_daily_life": "家計と税負担に影響",
        "missing_information": ["総額"], "usable_numbers": [],
    }


def angles():
    ids = ["taxpayer", "accountability", "daily_life", "official_gap",
           "contradiction", "feasibility"]
    return [{
        "id": value, "title": value, "thesis": f"{value}の説明が必要",
        "target_actor": "政府", "reader_relevance": "家計",
        "supporting_facts": ["政府が法案を提出"], "risk_notes": [],
    } for value in ids]


def candidates():
    return [{
        "candidate_id": f"c{i}", "angle_id": row["id"],
        "text": f"{row['title']}から見る。政府が法案を提出した。"
                "負担と検証責任を法案審議で明示すべきだ。",
        "hook": f"{row['title']}から見る。",
        "thesis": row["thesis"], "target_actor": "政府",
        "template_id": row["id"],
        "supporting_fact_urls": ["https://example.test/a"],
    } for i, row in enumerate(angles(), 1)]


def safe_evaluations():
    return [{
        "candidate_id": f"c{i}", "factual_accuracy": 9,
        "source_support": 9, "defamation_risk": 0,
        "unsupported_inference_risk": 1, "misleading_risk": 1,
        "policy_violation_risk": 0, "notes": [],
    } for i in range(1, 7)]


def quality_evaluations():
    keys = ("scroll_stop", "specificity", "novelty", "daily_life_relevance",
            "accountability_clarity", "quotability", "argument_strength",
            "emotional_resonance", "cliche_penalty")
    return [{
        "candidate_id": f"c{i}",
        **{key: (i + 3 if key != "cliche_penalty" else 0) for key in keys},
        "summary": "明確",
    } for i in range(1, 7)]


class FakeCalls:
    def __init__(self):
        self.stages = []

    def __call__(self, stage, prompt, schema, role):
        self.stages.append(stage)
        values = {
            "analysis": {"analysis": analysis()},
            "angles": {"angles": angles()},
            "candidates": {"candidates": candidates()},
            "safety": {"evaluations": safe_evaluations()},
            "quality": {"evaluations": quality_evaluations()},
            "finalize": {
                "candidate_id": "c6",
                "x_text": "政府が法案を提出した。争点は看板ではなく、負担と検証責任を誰が引き受けるかだ。審議で総額と見直し条件を示すべきだ。",
                "threads_text": "政府が法案を提出した。争点は看板ではなく、負担と検証責任を誰が引き受けるかだ。対象世帯への効果だけでなく、総額、財源、実施後の見直し条件を審議で示すべきだ。",
            },
            "final_verification": {
                "passed": True, "factual_accuracy": 9, "source_support": 9,
                "names_unchanged": True, "numbers_unchanged": True,
                "claim_unchanged": True, "assertion_strength_safe": True,
                "semantic_cliche": False, "personal_attack": False,
                "reasons": [],
            },
        }
        return values[stage]


class PoliticsMultistageTests(unittest.TestCase):
    def test_incomplete_json_is_retried_once(self):
        class Response:
            def __init__(self, text):
                self.output_text = text

        values = iter([Response('{"analysis":'), Response('{"analysis": {}}')])
        response, payload, attempts = call_structured_json_with_retry(
            lambda: next(values), max_attempts=2)
        self.assertEqual(payload, {"analysis": {}})
        self.assertEqual(attempts, 2)
        self.assertEqual(response.output_text, '{"analysis": {}}')

    def test_invalid_json_stops_after_bounded_retry(self):
        calls = []

        class Response:
            output_text = "{"

        with self.assertRaises(ValueError):
            call_structured_json_with_retry(
                lambda: calls.append(1) or Response(), max_attempts=2)
        self.assertEqual(len(calls), 2)

    def test_structured_pipeline_and_single_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = FakeCalls()
            pipe = MultiStagePipeline(
                calls, root=Path(directory),
                config=PipelineConfig(6, 6, 12, 24, False, True))
            result = pipe.run({
                "title": "法案提出", "summary": "政府が法案を提出した",
                "url": "https://example.test/a", "source_name": "官公庁",
            })
        self.assertEqual(calls.stages, [
            "analysis", "angles", "candidates", "safety", "quality", "finalize",
            "final_verification"])
        self.assertEqual(result["selected_candidate_id"], "c6")
        self.assertNotIn(candidates()[0]["text"], result["final_text"])
        self.assertLessEqual(len(result["final_text"]), 260)

    def test_analysis_cache_prevents_repeat_call(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = PipelineConfig(6, 6, 12, 24, False, False)
            first = FakeCalls()
            article = {"title": "法案", "summary": "提出",
                       "url": "https://example.test/cache"}
            MultiStagePipeline(first, root=Path(directory), config=cfg).run(article)
            second = FakeCalls()
            MultiStagePipeline(second, root=Path(directory), config=cfg).run(article)
            self.assertNotIn("analysis", second.stages)

    def test_model_usage_is_aggregated_per_article(self):
        calls = FakeCalls()

        def with_usage(stage, prompt, schema, role):
            return {
                **calls(stage, prompt, schema, role),
                "__usage": {
                    "input_tokens": 10, "output_tokens": 5,
                    "estimated_cost_usd": .001,
                },
            }

        with tempfile.TemporaryDirectory() as directory:
            result = MultiStagePipeline(
                with_usage, root=Path(directory),
                config=PipelineConfig(6, 6, 12, 24, False, False)
            ).run({
                "title": "法案", "summary": "提出",
                "url": "https://example.test/usage",
            })
        self.assertEqual(result["model_usage"]["input_tokens"], 70)
        self.assertEqual(result["model_usage"]["output_tokens"], 35)
        self.assertAlmostEqual(
            result["model_usage"]["estimated_cost_usd"], .007)

    def test_banned_phrases_are_detected(self):
        self.assertIn(
            "今後の議論が注目されます",
            contains_banned_phrase("今後の議論が注目されます。"))

    def test_semantically_equivalent_cliche_is_detected(self):
        hits = contains_banned_phrase(
            "これからの政策動向を注視する必要があります。")
        self.assertTrue(any(value.startswith("semantic_cliche:")
                            for value in hits))

    def test_platform_versions_are_independent(self):
        result = independent_platform_versions(
            "X独自の主張。確認済み事実だ。",
            "Threads独自の導入。確認済み事実を詳しく説明する。")
        self.assertTrue(result["x"].startswith("X独自"))
        self.assertTrue(result["threads"].startswith("Threads独自"))

    def test_length_fit_never_returns_mid_sentence(self):
        text = "主張を示す。" + ("補足説明です。" * 80) + "結論です。"
        fitted = fit_platform_text(text, 260)
        self.assertLessEqual(len(fitted), 260)
        self.assertTrue(fitted.endswith(("。", "！", "？")))

    def test_safety_is_a_hard_gate(self):
        with patch.dict(os.environ, {
            "POLITICS_MIN_FACTUAL_SCORE": "8",
            "POLITICS_MAX_DEFAMATION_RISK": "3",
            "POLITICS_MAX_INFERENCE_RISK": "4",
        }, clear=False):
            reasons = safety_rejection_reasons({
                "factual_accuracy": 7, "source_support": 9,
                "defamation_risk": 4, "unsupported_inference_risk": 5,
                "misleading_risk": 4, "policy_violation_risk": 0,
            })
        self.assertEqual(reasons, [
            "factual_accuracy", "defamation_risk",
            "unsupported_inference_risk"])

    def test_quality_formula(self):
        scores = {key: 10 for key in (
            "scroll_stop", "specificity", "novelty", "daily_life_relevance",
            "accountability_clarity", "quotability", "argument_strength",
            "emotional_resonance")}
        scores["cliche_penalty"] = 10
        self.assertAlmostEqual(quality_score(scores), 9.4)

    def test_tie_break_uses_factuality_then_hook(self):
        rows = [
            {"candidate_id": "a", "text": "短い", "final_score": 7,
             "rejected": False, "safety_scores": {"factual_accuracy": 8},
             "quality_scores": {"scroll_stop": 10, "quotability": 10,
                                "specificity": 10}},
            {"candidate_id": "b", "text": "やや長い文章", "final_score": 7,
             "rejected": False, "safety_scores": {"factual_accuracy": 9},
             "quality_scores": {"scroll_stop": 1, "quotability": 1,
                                "specificity": 1}},
        ]
        self.assertEqual(select_winner(rows)["candidate_id"], "b")

    def test_duplicate_hook_and_template_are_detected(self):
        history = [
            {"hook": "同じ冒頭", "template_id": "taxpayer",
             "tweet_text": f"履歴{i}"} for i in range(3)
        ]
        reasons = semantic_duplicate({
            "text": "新しい本文", "hook": "同じ冒頭",
            "template_id": "taxpayer"}, history)
        self.assertIn("duplicate_hook", reasons)
        self.assertIn("template_overuse", reasons)

    def test_duplicate_angle_theses_are_rejected(self):
        rows = angles()
        rows[1]["thesis"] = rows[0]["thesis"]
        self.assertTrue(any(
            value.startswith("duplicate_angle_thesis:")
            for value in angle_distinctness_reasons(rows)))

    def test_final_verification_can_block_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = FakeCalls()
            original = calls.__call__

            def reject(stage, prompt, schema, role):
                value = original(stage, prompt, schema, role)
                if stage == "final_verification":
                    value["passed"] = False
                    value["reasons"] = ["organization_name_changed"]
                return value

            result = MultiStagePipeline(
                reject, root=Path(directory),
                config=PipelineConfig(6, 6, 12, 24, False, False)
            ).run({
                "title": "法案", "summary": "政府が法案を提出",
                "url": "https://example.test/final-review",
            })
        self.assertEqual(result["final_text"], "")

    def test_api_limit_stops_without_unbounded_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            pipe = MultiStagePipeline(
                FakeCalls(), root=Path(directory),
                config=PipelineConfig(6, 6, 1, 24, False, False))
            with self.assertRaises(CallLimitExceeded):
                pipe.run({"title": "法案", "summary": "提出",
                          "url": "https://example.test/limit"})

    def test_embeddings_can_reject_semantic_duplicates(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"POLITICS_ENABLE_SEMANTIC_DEDUP": "true"}, clear=False
        ):
            pipe = MultiStagePipeline(
                FakeCalls(), root=Path(directory),
                history=[{"tweet_text": "過去と同じ主張"}],
                config=PipelineConfig(6, 6, 12, 24, False, False),
                embed_texts=lambda texts: [[1.0, 0.0] for _ in texts],
            )
            result = pipe.run({
                "title": "法案", "summary": "提出",
                "url": "https://example.test/embedding",
            })
        self.assertEqual(result["final_text"], "")
        self.assertTrue(all(
            "embedding_semantic_duplicate" in row["rejection_reasons"]
            for row in result["candidates"]))

    def test_mock_mode_makes_zero_external_calls(self):
        def forbidden(*args):
            raise AssertionError("external LLM call")
        with tempfile.TemporaryDirectory() as directory:
            pipe = MultiStagePipeline(
                forbidden, root=Path(directory),
                config=PipelineConfig(6, 6, 12, 24, True, False))
            result = pipe.run({"title": "制度案", "summary": "政府が公表",
                               "url": "https://example.test/mock"})
        self.assertTrue(result["final_text"])


if __name__ == "__main__":
    unittest.main()
