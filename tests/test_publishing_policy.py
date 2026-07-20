import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if SITE_PACKAGES.exists() and str(SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(SITE_PACKAGES))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ["POST_ENABLED"] = "false"

from publishing_policy import (  # noqa: E402
    budget_reached,
    calculate_growth_score,
    classify_hook_type,
    normalize_topic_key,
    pre_generation_skip_reason,
    stagnation_fallback_active,
    topic_cooldown_skip_reason,
)
import post  # noqa: E402


JST = ZoneInfo("Asia/Tokyo")


class PublishingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 20, 19, 0, tzinfo=JST)

    def _history(self, count, start=None):
        start = start or self.now.replace(hour=5)
        return [
            {
                "tweet_id": str(index),
                "posted_at_jst": (start + timedelta(minutes=50 * index)).isoformat(),
            }
            for index in range(count)
        ]

    def test_daily_limit_after_16_successes(self):
        self.assertEqual(
            pre_generation_skip_reason(self._history(16), self.now, 16, 45),
            "daily_post_limit",
        )

    def test_minimum_interval_under_45_minutes(self):
        history = [{"tweet_id": "1", "posted_at_jst": (self.now - timedelta(minutes=44)).isoformat()}]
        self.assertEqual(
            pre_generation_skip_reason(history, self.now, 16, 45),
            "minimum_post_interval",
        )

    def test_low_quality_fallback_starts_at_three_hours(self):
        before = [{
            "tweet_id": "1",
            "posted_at_jst": (self.now - timedelta(hours=2, minutes=59)).isoformat(),
        }]
        at_threshold = [{
            "tweet_id": "1",
            "posted_at_jst": (self.now - timedelta(hours=3)).isoformat(),
        }]
        self.assertFalse(stagnation_fallback_active(before, self.now, 3))
        self.assertTrue(stagnation_fallback_active(at_threshold, self.now, 3))

    def test_low_quality_fallback_requires_success_history(self):
        self.assertFalse(stagnation_fallback_active([], self.now, 3))
        failed = [{"posted_at_jst": (self.now - timedelta(hours=5)).isoformat()}]
        self.assertFalse(stagnation_fallback_active(failed, self.now, 3))

    def test_low_quality_fallback_relaxes_only_score_threshold(self):
        self.assertFalse(post._score_gate_allows(3.0, False, False, False))
        self.assertTrue(post._score_gate_allows(3.0, False, False, True))

    def test_topic_cooldown_within_four_hours(self):
        rows = [{
            "topic_key": "再審制度改正",
            "last_posted_at": (self.now - timedelta(hours=3)).isoformat(),
            "news_title": "再審制度改正を検討",
        }]
        self.assertEqual(
            topic_cooldown_skip_reason("再審制度改正", "再審制度改正を協議", rows, self.now, 4),
            "topic_cooldown",
        )

    def test_significant_update_bypasses_topic_cooldown(self):
        rows = [{
            "topic_key": "再審制度改正",
            "last_posted_at": (self.now - timedelta(hours=1)).isoformat(),
            "news_title": "再審制度改正を検討",
        }]
        self.assertIsNone(
            topic_cooldown_skip_reason("再審制度改正", "再審制度改正案が成立", rows, self.now, 4)
        )

    def test_judicial_news_rejects_unrelated_finance(self):
        candidate = {
            "tweet_text": "⚖️ 再審制度の論点\n\n🚨 給付→財源→負担者という問題を考える必要があります。"
                            "司法判断では証拠開示と適正手続が重要です。",
        }
        violations = post._candidate_quality_violations(candidate, {
            "title": "再審制度改正を議論", "summary": "証拠開示を検討"
        })
        self.assertIn("judicial_with_unrelated_finance", violations)

    def test_internal_f_type_label_is_rejected(self):
        candidate = {
            "tweet_text": "⚖️ F型の問い\n\n🚨 制度の透明性と適正手続を確認する必要があります。"
                            "政策判断の根拠を公開すべきです。",
        }
        violations = post._candidate_quality_violations(candidate, {
            "title": "行政制度を見直し", "summary": "透明性を議論"
        })
        self.assertTrue(any(value.startswith("meta_leak:") for value in violations))

    def test_flag_emojis_count_for_required_variety(self):
        text = (
            "🇯🇵🇬🇧🇮🇹 共同開発の論点\n\n"
            "政府発表を基に、装備開発の責任分担を確認します。\n\n"
            "仕様変更と説明責任を明確にする必要があります。十分な長さの本文です。"
        )
        violations = post._candidate_quality_violations(
            {"tweet_text": text},
            {"title": "共同開発を発表", "summary": "政府が説明"},
        )
        self.assertNotIn("missing_required_emojis", violations)
        self.assertNotIn("insufficient_emoji_variety", violations)

    def test_same_hook_does_not_continue_three_times(self):
        news = {"title": "予算100億円を発表", "summary": ""}
        history = [{"hook_type": "number"}, {"hook_type": "number"}]
        self.assertNotEqual(classify_hook_type(news, history), "number")

    def test_daily_count_resets_on_jst_date_change(self):
        yesterday = self.now - timedelta(days=1)
        history = [{
            "tweet_id": str(index),
            "posted_at_jst": yesterday.replace(hour=5, minute=index).isoformat(),
        } for index in range(16)]
        self.assertIsNone(pre_generation_skip_reason(history, self.now, 16, 45))

    def test_openai_monthly_budget_gate(self):
        self.assertTrue(budget_reached(8.0, 8.0))
        self.assertFalse(budget_reached(7.99, 8.0))

    def test_review_handles_missing_x_metrics(self):
        score = calculate_growth_score(
            {"impressions_per_hour": 10, "engagement_rate": 0.01},
            {
                "impressions_per_hour": 0.25,
                "engagement_rate": 0.20,
                "profile_clicks": 0.25,
                "quotes_bookmarks": 0.15,
                "follow_conversion": 0.15,
            },
        )
        self.assertGreaterEqual(score, 0)

    def test_topic_normalization_examples(self):
        self.assertEqual(normalize_topic_key("再審制度の改正案を審議"), "再審制度改正")


if __name__ == "__main__":
    unittest.main()
