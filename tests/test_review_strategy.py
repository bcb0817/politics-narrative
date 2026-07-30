from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import publishing_policy
import report_ai
import review_strategy
import post


class ReviewStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Keep the active-strategy TTL meaningful regardless of test run date.
        self.now = datetime.now(review_strategy.JST).replace(
            hour=4, minute=40, second=0, microsecond=0)
        self.payload = {
            "reviewed_count": 4,
            "all_posts": [
                {"tweet_id": "1"}, {"tweet_id": "2"},
                {"tweet_id": "3"}, {"tweet_id": "4"},
            ],
        }
        self.analysis = {
            "impression_strategy": {
                "summary": "数字フックと比較構造を翌日に検証する。",
                "evidence": [{
                    "finding": "数字フックの表示速度が高い",
                    "tweet_ids": ["1", "2"],
                    "metric": "impressions_per_hour",
                    "confidence": 0.75,
                }],
                "next_day_policy": {
                    "post_type_priority": [
                        "comparison_factcheck", "invalid_type",
                        "issue_diagram",
                    ],
                    "hook_type_priority": [
                        "number", "invalid_hook", "contrast",
                    ],
                    "preferred_hours_jst": [6, 12, 18, 99],
                    "target_text_min": 50,
                    "target_text_max": 999,
                    "body_structure":
                        "before_after_comparison",
                    "cta_style": "source_check",
                    "experiment_name": "daily comparison test",
                },
            },
        }
        self.env = patch.dict(os.environ, {
            "CHATGPT_DAILY_STRATEGY_ENABLED": "true",
            "CHATGPT_DAILY_STRATEGY_AUTO_APPLY": "true",
            "CHATGPT_DAILY_STRATEGY_MIN_POSTS": "3",
            "CHATGPT_DAILY_STRATEGY_TTL_HOURS": "48",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def activate(self):
        return review_strategy.activate_strategy(
            self.analysis, self.payload, root_dir=self.root, now=self.now)

    def test_01_activation_requires_minimum_samples(self):
        payload = {**self.payload, "reviewed_count": 2}
        result = review_strategy.activate_strategy(
            self.analysis, payload, root_dir=self.root, now=self.now)
        self.assertFalse(result["activated"])
        self.assertEqual(result["reason"], "insufficient_review_samples")

    def test_02_activation_requires_metric_evidence(self):
        analysis = json.loads(json.dumps(self.analysis))
        analysis["impression_strategy"]["evidence"][0]["tweet_ids"] = ["x"]
        result = review_strategy.activate_strategy(
            analysis, self.payload, root_dir=self.root, now=self.now)
        self.assertFalse(result["activated"])
        self.assertEqual(result["reason"], "strategy_has_no_metric_evidence")

    def test_03_valid_strategy_is_activated(self):
        result = self.activate()
        self.assertTrue(result["activated"])
        self.assertTrue(
            (self.root / review_strategy.STRATEGY_FILE).exists())

    def test_04_unknown_enums_are_removed(self):
        policy = self.activate()["policy"]
        self.assertEqual(
            policy["post_type_priority"],
            ["comparison_factcheck", "issue_diagram"],
        )
        self.assertEqual(policy["hook_type_priority"], ["number", "contrast"])

    def test_05_text_length_is_clamped_to_x_safe_range(self):
        policy = self.activate()["policy"]
        self.assertEqual(policy["target_text_min"], 100)
        self.assertEqual(policy["target_text_max"], 260)

    def test_06_hours_are_clamped_to_active_hours(self):
        policy = self.activate()["policy"]
        self.assertEqual(policy["preferred_hours_jst"], [6, 12, 18])

    def test_07_safety_and_budget_are_locked(self):
        policy = self.activate()["policy"]
        self.assertTrue(policy["safety_locked"])
        self.assertTrue(policy["posting_limits_locked"])
        self.assertTrue(policy["budgets_locked"])
        self.assertTrue(policy["political_position_locked"])

    def test_08_active_strategy_expires(self):
        self.activate()
        active = review_strategy.load_active_strategy(
            self.root, now=self.now + timedelta(hours=49))
        self.assertEqual(active, {})

    def test_09_disabled_strategy_is_not_loaded(self):
        self.activate()
        with patch.dict(os.environ, {
            "CHATGPT_DAILY_STRATEGY_ENABLED": "false",
        }):
            self.assertEqual(
                review_strategy.load_active_strategy(self.root), {})

    def test_10_markdown_contains_only_bounded_guidance(self):
        policy = self.activate()["policy"]
        markdown = review_strategy.render_prompt_guidance(policy)
        self.assertIn("推奨文字数", markdown)
        self.assertIn("安全基準", markdown)
        self.assertNotIn("invalid_type", markdown)

    def test_11_compact_payload_contains_operational_counts(self):
        compact = report_ai.compact_daily_payload({
            "reviewed_count": 3,
            "operational_log_summary": {
                "skip_reasons": {"no_news": 2},
                "raw_log_text_shared_with_model": False,
            },
        })
        self.assertEqual(
            compact["operational_log_summary"]["skip_reasons"]["no_news"], 2)

    def test_12_analysis_schema_requires_impression_strategy(self):
        self.assertIn(
            "impression_strategy", report_ai.ANALYSIS_SCHEMA["required"])
        self.assertFalse(
            report_ai.ANALYSIS_SCHEMA["additionalProperties"])

    def test_13_summary_logs_do_not_include_raw_text(self):
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "post_attempts.jsonl").write_text(
            json.dumps({
                "ts_jst": self.now.isoformat(),
                "decision": "skip",
                "reason": "no_news",
                "secret": "do-not-copy",
            }) + "\n",
            encoding="utf-8",
        )
        result = review_strategy.summarize_operational_logs(
            logs, self.now - timedelta(hours=1), self.now + timedelta(hours=1))
        self.assertEqual(result["skip_reasons"]["no_news"], 1)
        self.assertFalse(result["raw_log_text_shared_with_model"])
        self.assertNotIn("do-not-copy", json.dumps(result))

    def test_14_style_priority_influences_only_supported_candidates(self):
        self.activate()
        with patch.object(publishing_policy, "ROOT_DIR", self.root):
            style, _ = publishing_policy.choose_post_style(
                {"title": "制度の見直し", "summary": "制度を比較する",
                 "topic_key": "topic"},
                [], self.now,
            )
        self.assertIn(style, publishing_policy.POST_TYPES)
        self.assertNotEqual(style, "invalid_type")

    def test_15_number_hook_requires_number_in_source(self):
        self.activate()
        with patch.object(publishing_policy, "ROOT_DIR", self.root):
            hook = publishing_policy.classify_hook_type(
                {"title": "制度の見直し", "summary": "公式発表"}, [])
        self.assertNotEqual(hook, "number")

    def test_16_chatgpt_guidance_is_not_truncated_after_legacy_patterns(self):
        self.activate()
        patterns = self.root / "knowledge" / "viral_patterns"
        (patterns / "winning_patterns.md").write_text(
            "- " + ("legacy " * 300), encoding="utf-8")
        with patch.object(post, "ROOT_DIR", self.root):
            output = post._load_performance_patterns(max_chars=900)
        self.assertIn("ChatGPT日次レビュー", output)
        self.assertLessEqual(len(output), 900)

    def test_17_assignment_is_deterministic_and_bounded(self):
        policy = self.activate()["policy"]
        item = {"topic_key": "budget", "title": "予算案を公表"}
        first = review_strategy.assignment_for(
            item, strategy=policy, now=self.now)
        second = review_strategy.assignment_for(
            item, strategy=policy, now=self.now)
        self.assertEqual(first, second)
        self.assertIn(first, {"treatment", "control"})

    def test_18_control_does_not_receive_chatgpt_guidance(self):
        self.activate()
        patterns = self.root / "knowledge" / "viral_patterns"
        (patterns / "winning_patterns.md").write_text(
            "- legacy", encoding="utf-8")
        with patch.object(post, "ROOT_DIR", self.root):
            output = post._load_performance_patterns(
                use_ai_strategy=False)
        self.assertNotIn("ChatGPT日次レビュー", output)
        self.assertIn("legacy", output)

    def test_19_underperformance_rolls_back_after_minimum_samples(self):
        policy = self.activate()["policy"]
        rows = []
        for index in range(3):
            rows.append({
                "review_strategy_id": policy["strategy_id"],
                "review_strategy_variant": "treatment",
                "impressions_per_hour": 10,
            })
            rows.append({
                "review_strategy_id": policy["strategy_id"],
                "review_strategy_variant": "control",
                "impressions_per_hour": 20,
            })
        result = review_strategy.evaluate_strategy_performance(
            {"all_posts": rows}, policy, root_dir=self.root, now=self.now)
        self.assertEqual(result["status"], "rollback")
        self.assertEqual(
            result["reason"], "treatment_underperformed_control")

    def test_20_deactivation_preserves_history_and_disables_loading(self):
        policy = self.activate()["policy"]
        result = review_strategy.deactivate_strategy(
            self.root, reason="test", now=self.now)
        self.assertTrue(result["deactivated"])
        self.assertEqual(
            review_strategy.load_active_strategy(self.root, now=self.now), {})
        history = review_strategy.strategy_history(self.root)
        self.assertEqual(history[-1]["event"], "deactivated")
        self.assertEqual(history[-1]["strategy_id"], policy["strategy_id"])

    def test_21_alignment_bonus_only_applies_to_treatment(self):
        policy = self.activate()["policy"]
        treatment = review_strategy.alignment_bonus(
            policy,
            variant="treatment",
            post_type="comparison_factcheck",
            hook_type="number",
            posted_hour_jst=6,
        )
        control = review_strategy.alignment_bonus(
            policy,
            variant="control",
            post_type="comparison_factcheck",
            hook_type="number",
            posted_hour_jst=6,
        )
        self.assertGreater(treatment, 0)
        self.assertLessEqual(treatment, 0.25)
        self.assertEqual(control, 0)


if __name__ == "__main__":
    unittest.main()
