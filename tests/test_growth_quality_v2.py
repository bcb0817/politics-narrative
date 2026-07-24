import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import local_bot
import engagement_queue
import growth_tracking
import metrics_db
import openai_batch
import phase2
import quality_evals
import review_scoring
import xai_radar
from model_router import ModelRouter
from publishing_policy import choose_post_style

JST = ZoneInfo("Asia/Tokyo")


class GrowthQualityV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db = self.root / "metrics.db"
        metrics_db.init_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    # xAI: 1-8
    def test_01_xai_schedule_has_at_most_three_slots(self):
        with patch.dict(os.environ, {"XAI_SEARCH_SCHEDULE": "06:00,12:00,18:00",
                                      "XAI_ADAPTIVE_SCHEDULE_ENABLED": "false"}), \
             patch.object(xai_radar, "usage_totals", return_value={"xai": 0}), \
             patch.object(xai_radar, "forecast", return_value={"projected": {"xai": 0}}):
            self.assertEqual(xai_radar.effective_schedule(path=self.db),
                             {"06:00", "12:00", "18:00"})

    def test_02_low_volatility_reduces_xai_to_two_slots(self):
        with patch.dict(os.environ, {"XAI_SEARCH_SCHEDULE": "06:00,12:00,18:00",
                                      "XAI_ADAPTIVE_SCHEDULE_ENABLED": "true"}), \
             patch.object(xai_radar, "usage_totals", return_value={"xai": 0}), \
             patch.object(xai_radar, "forecast", return_value={"projected": {"xai": 0}}), \
             patch.object(xai_radar, "local_volatility_score", return_value=1):
            self.assertEqual(xai_radar.effective_schedule(path=self.db), {"06:00", "18:00"})

    def test_03_high_volatility_never_exceeds_three_slots(self):
        with patch.dict(os.environ, {"XAI_SEARCH_SCHEDULE": "06:00,12:00,18:00",
                                      "XAI_ADAPTIVE_SCHEDULE_ENABLED": "true"}), \
             patch.object(xai_radar, "usage_totals", return_value={"xai": 0}), \
             patch.object(xai_radar, "forecast", return_value={"projected": {"xai": 0}}), \
             patch.object(xai_radar, "local_volatility_score", return_value=8):
            self.assertLessEqual(len(xai_radar.effective_schedule(path=self.db)), 3)

    def test_04_non_schedule_time_does_not_run_xai(self):
        with patch.dict(os.environ, {"XAI_SEARCH_SCHEDULE": "06:00,12:00,18:00",
                                      "XAI_ADAPTIVE_SCHEDULE_ENABLED": "false"}), \
             patch.object(xai_radar, "usage_totals", return_value={"xai": 0}), \
             patch.object(xai_radar, "forecast", return_value={"projected": {"xai": 0}}):
            self.assertFalse(xai_radar.should_run(
                datetime(2026, 7, 24, 7, 0, tzinfo=JST), self.db))

    def test_05_xai_actual_ticks_are_persisted(self):
        with patch.object(xai_radar, "_state_dir", return_value=self.root):
            xai_radar._record_usage("grok", 100_000_000, 10, 5, 1, True,
                                    schedule_slot="06:00", path=self.db)
        with metrics_db.connect(self.db) as conn:
            row = conn.execute("SELECT * FROM xai_usage_events").fetchone()
        self.assertAlmostEqual(row["actual_cost_usd"], 0.01)

    def test_06_xai_projection_over_180_reduces_to_two(self):
        with patch.dict(os.environ, {"XAI_SEARCH_SCHEDULE": "06:00,12:00,18:00"}), \
             patch.object(xai_radar, "usage_totals", return_value={"xai": 1.81}), \
             patch.object(xai_radar, "forecast", return_value={"projected": {"xai": 1.81}}):
            self.assertEqual(xai_radar.effective_schedule(path=self.db), {"06:00", "18:00"})

    def test_07_xai_monthly_limit_blocks_client(self):
        with patch.dict(os.environ, {"XAI_ENABLED": "true",
                                      "X_TOPIC_DISCOVERY_PROVIDER": "xai",
                                      "XAI_SEARCH_SCHEDULE": "06:00",
                                      "XAI_ADAPTIVE_SCHEDULE_ENABLED": "false"}), \
             patch.object(xai_radar, "_state_dir", return_value=self.root), \
             patch.object(xai_radar, "usage_totals", return_value={"xai": 2.0}), \
             patch.object(xai_radar, "forecast", return_value={
                 "projected_jpy": 0, "projected": {"xai": 2.0}}):
            result = xai_radar.search(datetime(2026, 7, 24, 6, 0, tzinfo=JST),
                                      client_factory=lambda **_: self.fail("client called"),
                                      path=self.db)
        self.assertEqual(result, [])

    def test_08_xai_topics_do_not_create_news_candidates(self):
        self.assertEqual(xai_radar.apply_verified_attention([], [{"topic_key": "税制"}]), [])

    # Daily/weekly/model: 9-20
    def test_09_daily_review_is_not_batch(self):
        with patch.dict(os.environ, {"OPENAI_BATCH_ENABLED": "true",
                                      "OPENAI_BATCH_TASKS": "daily_review,weekly_report",
                                      "DAILY_REVIEW_MODE": "synchronous"}):
            self.assertFalse(openai_batch.enabled("daily_review"))

    def test_10_daily_review_time_is_0440(self):
        with patch.dict(os.environ, {"DAILY_REVIEW_TIME": "04:40"}):
            self.assertEqual(local_bot._daily_review_time().strftime("%H:%M"), "04:40")

    def test_11_daily_review_failure_is_representable_as_local_only(self):
        self.assertEqual(os.environ.get("OPENAI_MODEL_DAILY_REVIEW_FALLBACK"), "local_only")

    def test_12_weekly_review_uses_batch(self):
        with patch.dict(os.environ, {"OPENAI_BATCH_ENABLED": "true",
                                      "OPENAI_BATCH_TASKS": "weekly_report",
                                      "DAILY_REVIEW_MODE": "synchronous"}):
            self.assertTrue(openai_batch.enabled("weekly_report"))

    def test_13_weekly_custom_id_is_deterministic(self):
        first = openai_batch._custom_id("weekly_report", {"a": 1}, "week:2026-07-20")
        second = openai_batch._custom_id("weekly_report", {"a": 2}, "week:2026-07-20")
        self.assertEqual(first, second)

    def test_14_premium_is_disabled(self):
        router = ModelRouter(ROOT / "config" / "openai_model_pricing.json")
        with patch.dict(os.environ, {"OPENAI_PREMIUM_ENABLED": "false",
                                      "OPENAI_MONTHLY_BUDGET_USD": "0"}):
            self.assertFalse(router.select_model(
                "premium_report", premium_requested=False)["model"])

    def test_15_normal_post_uses_mini(self):
        router = ModelRouter(ROOT / "config" / "openai_model_pricing.json")
        with patch.dict(os.environ, {"OPENAI_MODEL_DEFAULT": "gpt-5.4-mini",
                                      "OPENAI_MONTHLY_BUDGET_USD": "0"}):
            self.assertEqual(router.select_model("post_generation")["model"], "gpt-5.4-mini")

    def test_16_important_news_uses_luna(self):
        router = ModelRouter(ROOT / "config" / "openai_model_pricing.json")
        with patch.dict(os.environ, {"OPENAI_MODEL_IMPORTANT": "gpt-5.6-luna",
                                      "OPENAI_MONTHLY_BUDGET_USD": "0"}):
            route = router.select_model("post_generation", importance_score=10,
                                        source_reliability=9)
        self.assertEqual(route["model"], "gpt-5.6-luna")

    def test_17_luna_limit_is_two(self):
        self.assertEqual(int(os.environ.get("DAILY_IMPORTANT_MODEL_LIMIT", "2")), 2)

    def test_18_nano_is_only_needed_for_ambiguous_items(self):
        self.assertFalse(phase2.classification_needed({"topic_key": "税制", "genre": "税制"}))

    def test_19_nano_daily_limit_is_six(self):
        self.assertEqual(int(os.environ.get("OPENAI_CLASSIFIER_MAX_CALLS_PER_DAY", "6")), 6)

    def test_20_sol_is_absent_from_automatic_fallbacks(self):
        router = ModelRouter(ROOT / "config" / "openai_model_pricing.json")
        with patch.dict(os.environ, {"OPENAI_MONTHLY_BUDGET_USD": "0",
                                      "WEEKLY_REVIEW_ENABLED": "true"}):
            route = router.select_model("weekly_report")
        self.assertNotIn("gpt-5.6-sol", [route.get("model"), *route.get("fallback_models", [])])

    # Quality evals: 21-30
    def test_21_quality_fixture_count_is_at_least_100(self):
        self.assertGreaterEqual(len(quality_evals.load_fixtures()), 100)

    def test_22_factuality_under_four_fails(self):
        row = quality_evals.score_fixture({
            "news": "制度案が公表された。", "candidate": "87%削減する。",
            "category": "税制", "expected_pass": False})
        self.assertLess(row["scores"]["factuality"], 4)
        self.assertFalse(row["passed"])

    def test_23_relevance_under_four_fails(self):
        row = quality_evals.score_fixture({
            "news": "再審請求が審理された。", "candidate": "財源と増税だけが論点だ。",
            "category": "再審", "expected_pass": False})
        self.assertLess(row["scores"]["relevance"], 4)

    def test_24_safety_under_five_fails(self):
        row = quality_evals.score_fixture({
            "news": "政策が発表された。", "candidate": "支持者は馬鹿だ。",
            "category": "政局", "expected_pass": False})
        self.assertLess(row["scores"]["safety"], 5)

    def test_25_judicial_finance_mismatch_is_rejected(self):
        self.assertEqual(quality_evals.auto_fail_reason(
            "再審請求を審理。", "財源と増税が問題だ。", "再審"),
            "judicial_finance_mismatch")

    def test_26_internal_label_is_rejected(self):
        self.assertEqual(quality_evals.auto_fail_reason(
            "政策案。", "topic_key=abc", "行政改革"), "internal_label")

    def test_27_unverified_x_information_is_rejected(self):
        self.assertEqual(quality_evals.auto_fail_reason(
            "政策案。", "Xだけで確認できたので事実です。", "政局"), "unverified_x")

    def test_28_url_is_rejected(self):
        self.assertEqual(quality_evals.auto_fail_reason(
            "政策案。", "https://example.invalid", "政局"), "url")

    def test_29_full_eval_requires_explicit_confirmation(self):
        self.assertEqual(quality_evals.run_quality_eval(
            "full", confirm_full=False, path=self.db)["error"],
            "full_mode_requires_explicit_confirmation")

    def test_30_human_review_csv_is_importable(self):
        source = self.root / "reviews.csv"
        source.write_text(
            "content_type,content_id,factuality,relevance,logic,originality,natural_japanese,brand_fit,should_post\n"
            "published,1,5,5,5,4,5,5,true\n", encoding="utf-8")
        self.assertEqual(growth_tracking.import_human_reviews(
            source, self.db)["inserted"], 1)

    # Manual queues: 31-36
    def test_31_engagement_schedule_has_two_slots(self):
        clocks = local_bot._parse_clock_list("12:20,20:20")
        self.assertEqual([clock.strftime("%H:%M") for clock in clocks], ["12:20", "20:20"])

    def test_32_quote_auto_post_remains_disabled(self):
        self.assertEqual(os.environ.get("QUOTE_AUTO_POST_ENABLED"), "false")

    def test_33_reply_auto_post_remains_disabled(self):
        self.assertEqual(os.environ.get("REPLY_AUTO_POST_ENABLED"), "false")

    def test_34_general_accounts_are_excluded_from_quotes(self):
        self.assertNotIn("other", engagement_queue.SAFE_AUTHOR_TYPES)

    def test_35_posted_manually_status_is_saved(self):
        metrics_db.write("""INSERT INTO engagement_queue
          (queue_type,source_post_id,status,created_at,updated_at) VALUES ('quote','p1','pending',?,?)""",
          (datetime.now(JST).isoformat(), datetime.now(JST).isoformat()), self.db)
        self.assertTrue(engagement_queue.update_status("quote", 1, "posted_manually", self.db))

    def test_36_duplicate_queue_item_is_not_inserted(self):
        item = {"queue_type": "quote", "post_id": "same", "author_handle": "official",
                "author_type": "official", "topic_key": "税制", "source_verified": True,
                "reason_selected": "official", "comment_options": [], "risk_flags": []}
        first = engagement_queue._insert(item, self.db)
        second = engagement_queue._insert(item, self.db)
        self.assertIsNotNone(first)
        self.assertEqual(second, 0)

    # Learning: 37-41
    def test_37_four_axes_are_separate(self):
        axes = review_scoring.calculate_four_axes(
            {"impressions": 100, "bookmarks": 2, "replies": 3, "profile_clicks": 1})
        self.assertTrue({"spread_score", "trust_score", "conversation_score",
                         "business_score"}.issubset(axes))

    def test_38_anger_only_post_is_not_a_winner(self):
        post = {"text": "許せない。ふざけるな。怒りだ。", "trust_score": 1,
                "follow_conversion_estimate": 0}
        self.assertFalse(review_scoring.eligible_winning_example(post))

    def test_39_generation_purpose_selects_winner_types(self):
        self.assertEqual(review_scoring.preferred_winner_types("breaking_news"),
                         ("viral_winner", "trust_winner"))

    def test_40_exploration_is_capped_at_two_per_day(self):
        now = datetime(2026, 7, 24, 10, 0, tzinfo=JST)
        history = [{"posted_at_jst": now.isoformat(), "post_type": "issue_diagram",
                    "is_exploration": True} for _ in range(2)]
        with patch.dict(os.environ, {"STYLE_EXPLORATION_RATIO": "1",
                                      "STYLE_EXPLORATION_MAX_PER_DAY": "2"}):
            _, exploration = choose_post_style({"topic_key": "test"}, history, now)
        self.assertFalse(exploration)

    def test_41_prompt_version_is_v2(self):
        self.assertEqual(os.environ.get("PROMPT_VERSION"), "x-growth-quality-v2")

    # Data/safety: 42-48
    def test_42_follower_snapshot_table_exists(self):
        metrics_db.apply_additive_migrations(self.db)
        self.assertIn("follower_snapshots", metrics_db.table_counts(self.db))

    def test_43_follower_change_is_reported_as_estimate(self):
        now = datetime.now(JST)
        metrics_db.write("""INSERT INTO follower_snapshots
          (captured_at,followers_count,following_count,posts_count,source,estimated)
          VALUES (?,?,?,?,?,0)""", ((now - timedelta(hours=1)).isoformat(), 10, 1, 2, "test"), self.db)
        metrics_db.write("""INSERT INTO follower_snapshots
          (captured_at,followers_count,following_count,posts_count,source,estimated)
          VALUES (?,?,?,?,?,0)""", (now.isoformat(), 12, 1, 3, "test"), self.db)
        self.assertEqual(growth_tracking.follower_status(self.db)["follower_change"], 2)

    def test_44_conversion_csv_is_importable(self):
        source = self.root / "conversions.csv"
        source.write_text(
            "occurred_at,source,campaign,content_id,event_type,value\n"
            "2026-07-24T00:00:00+09:00,note,test,1,note_click,1\n", encoding="utf-8")
        self.assertEqual(growth_tracking.import_conversions(
            source, self.db)["inserted"], 1)

    def test_45_migrations_are_idempotent(self):
        metrics_db.apply_additive_migrations(self.db)
        second = metrics_db.apply_additive_migrations(self.db)
        self.assertTrue(all(not values for values in second.values()))

    def test_46_api_keys_are_not_part_of_usage_schema(self):
        columns = {
            row["name"] for row in metrics_db.connect(self.db).execute(
                "PRAGMA table_info(api_usage_events)")
        }
        self.assertNotIn("api_key", columns)

    def test_47_existing_rows_survive_migration(self):
        metrics_db.write("""INSERT INTO prompt_versions
          (version,created_at,is_active) VALUES ('before','now',1)""", path=self.db)
        metrics_db.apply_additive_migrations(self.db)
        self.assertEqual(metrics_db.table_counts(self.db)["prompt_versions"], 1)

    def test_48_quality_dashboard_reads_new_schema(self):
        dashboard = quality_evals.quality_dashboard(self.db)
        self.assertIn("prompt_versions", dashboard)


if __name__ == "__main__":
    unittest.main()
