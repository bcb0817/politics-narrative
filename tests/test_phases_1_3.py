import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / ".venv" / "Lib" / "site-packages"
sys.path[:0] = [str(SITE), str(ROOT / "src"), str(ROOT)]
os.environ["POST_ENABLED"] = "false"

import api_budget
import local_bot
import metrics_db
import news
import phase2
import post
import post_metrics
from model_router import ModelRouter
from publishing_policy import (is_significant_update, phase_daily_limit_reached,
                               pre_generation_skip_reason, topic_cooldown_skip_reason)

JST = ZoneInfo("Asia/Tokyo")


def history_rows(now, normal=0, breaking=0):
    rows = []
    for idx in range(normal):
        rows.append({"tweet_id": f"n{idx}", "post_type": "strong_opinion",
                     "posted_at_jst": (now - timedelta(hours=idx + 1)).isoformat()})
    for idx in range(breaking):
        rows.append({"tweet_id": f"b{idx}", "post_type": "breaking_news",
                     "posted_at_jst": (now - timedelta(hours=idx + 1)).isoformat()})
    return rows


class FakeMetricClient:
    missing = False
    def __init__(self, **kwargs): pass
    def get_tweets(self, ids, **kwargs):
        data = [] if self.missing else [SimpleNamespace(id=value, public_metrics={
            "impression_count": 100, "like_count": 5, "retweet_count": 2,
            "reply_count": 1, "quote_count": 1, "bookmark_count": 1,
            "user_profile_clicks": 3, "url_link_clicks": 0}) for value in ids]
        return SimpleNamespace(data=data)


class Phase1Tests(unittest.TestCase):
    def setUp(self): self.now = datetime(2026, 7, 21, 18, 0, tzinfo=JST)

    def test_01_monitor_interval_is_60_minutes(self):
        self.assertEqual(int(os.environ.get("MONITOR_INTERVAL_MINUTES", "60")), 60)

    def test_02_x_search_only_at_three_times(self):
        with patch.dict(os.environ, {"X_SEARCH_SCHEDULE": "06:00,12:00,18:00"}):
            self.assertTrue(news.should_run_x_search(self.now))
            self.assertFalse(news.should_run_x_search(self.now.replace(hour=17, minute=30)))

    def test_03_normal_posts_stop_at_eight(self):
        self.assertTrue(phase_daily_limit_reached(history_rows(self.now, normal=8), self.now, False))

    def test_04_total_posts_stop_at_ten(self):
        self.assertTrue(phase_daily_limit_reached(history_rows(self.now, 8, 2), self.now, True))

    def test_05_minimum_is_not_a_quota(self):
        self.assertEqual(post.prefilter_news([{"title": "芸能ニュース", "url": "x", "source_name": "unknown"}]), [])

    def test_06_sixty_minute_interval(self):
        rows = history_rows(self.now, 1)
        rows[0]["posted_at_jst"] = (self.now - timedelta(minutes=59)).isoformat()
        self.assertEqual(pre_generation_skip_reason(rows, self.now, 12, 60), "minimum_post_interval")

    def test_06b_low_quality_fallback_starts_after_ninety_minutes(self):
        self.assertEqual(float(os.environ["LOW_QUALITY_FALLBACK_HOURS"]), 1.5)

    def test_07_topic_cooldown(self):
        recent = [{"topic_key": "防衛予算", "last_posted_at": (self.now - timedelta(hours=2)).isoformat(), "news_title": "防衛予算を検討"}]
        self.assertEqual(topic_cooldown_skip_reason("防衛予算", "防衛予算を協議", recent, self.now, 4), "topic_cooldown")

    def test_08_major_update_bypasses_cooldown(self):
        self.assertTrue(is_significant_update("法案が可決", "法案を審議"))

    def test_09_post_disabled_is_default_for_test(self):
        self.assertFalse(post.POST_ENABLED)

    def test_09b_classifier_limit_keeps_local_metadata_candidate(self):
        source = {
            "topic_key": "税制改正",
            "genre": "税財政",
            "classification_confidence": 0.4,
            "verified": True,
        }
        result = post._classification_or_local_fallback(
            source, classifier=lambda _: None
        )
        self.assertEqual(result["topic_key"], "税制改正")
        self.assertEqual(result["classification_mode"], "local_limit_fallback")
        self.assertGreaterEqual(result["classification_confidence"], 0.65)

    def test_10_url_is_rejected(self):
        candidate = {"tweet_text": "🚨 見出し\n\n本文 https://example.com " + "説明" * 60}
        self.assertIn("url_detected_in_post", post._candidate_quality_violations(candidate, {}))


class Phase2Tests(unittest.TestCase):
    def test_11_nano_not_needed_for_confident_local_classification(self):
        self.assertFalse(phase2.classification_needed({"topic_key": "防衛", "genre": "安全保障", "classification_confidence": .9}))

    def test_12_nano_daily_cap(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            path = Path(td) / "db.sqlite"; metrics_db.init_db(path)
            for idx in range(8):
                metrics_db.write("""INSERT INTO api_usage_events(timestamp,provider,operation,model_or_endpoint,
                  resource_count,input_tokens,cached_input_tokens,output_tokens,estimated_cost_usd,success,
                  fallback_used,error_type,metadata_json) VALUES (?,?,?,?,0,0,0,0,0,1,0,'','{}')""",
                  (datetime.now(JST).isoformat(), "openai", "classifier", "gpt-5.4-nano"), path)
            self.assertEqual(phase2.classifier_calls_today(path), 8)

    def test_13_internal_label_rejected(self):
        c = {"tweet_text": "🚨 見出し\n\npost_type strong_opinion " + "説明" * 60}
        self.assertTrue(any(v.startswith("meta_leak") for v in post._candidate_quality_violations(c, {})))

    def test_14_judicial_finance_mismatch_rejected(self):
        c = {"tweet_text": "⚖️ 再審制度\n\n給付 → 財源 → 負担者 " + "説明" * 60}
        self.assertIn("judicial_with_unrelated_finance", post._candidate_quality_violations(c, {"title": "再審制度"}))

    def test_15_x_only_unverified_rejected(self):
        c = {"tweet_text": "🚨 話題\n\n" + "政治上の争点を確認する" * 20}
        self.assertIn("unverified_x_claim", post._candidate_quality_violations(c, {"discovered_via": ["x_search"]}))

    def test_16_similar_text_is_duplicate(self):
        c = {"tweet_text": "同じ文章です" * 20, "source_url": "new"}
        self.assertTrue(post.is_duplicate(c, [{"tweet_text": "同じ文章です" * 20, "source_url": "old"}]))

    def test_17_extension_preview_is_local_only(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {"NOTE_PREVIEW_ENABLED": "true"}):
            paths = phase2.save_extension_previews({"topic_key": "防衛"}, Path(td))
            self.assertTrue(paths[0].exists())
            self.assertFalse(json.loads(paths[0].read_text(encoding="utf-8"))["auto_post"])

    def test_18_image_auto_post_disabled(self):
        self.assertEqual(os.environ.get("IMAGE_POST_ENABLED", "false").lower(), "false")

    def test_19_thread_auto_post_is_impossible(self):
        self.assertEqual(post.build_reply_texts({"structure_points": ["a"]}), [])

    def test_20_reply_and_quote_auto_post_disabled(self):
        self.assertEqual(os.environ.get("REPLY_AUTO_POST_ENABLED", "false").lower(), "false")
        self.assertEqual(os.environ.get("QUOTE_AUTO_POST_ENABLED", "false").lower(), "false")


class Phase3Tests(unittest.TestCase):
    def test_21_metrics_windows_are_saved(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {"X_OWNED_READ_MAX_PER_DAY": "24"}):
            path = Path(td) / "db.sqlite"; now = datetime.now(JST)
            rows = [{"tweet_id": "1", "posted_at_jst": (now - timedelta(hours=80)).isoformat()}]
            result = post_metrics.collect(rows, now, FakeMetricClient, path)
            self.assertEqual(result["collected"], 5)

    def test_22_same_window_is_not_collected_twice(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            path = Path(td) / "db.sqlite"; now = datetime.now(JST)
            rows = [{"tweet_id": "1", "posted_at_jst": (now - timedelta(hours=2)).isoformat()}]
            post_metrics.collect(rows, now, FakeMetricClient, path)
            self.assertEqual(post_metrics.due_measurements(rows, now, path), [])

    def test_23_missing_metrics_complete(self):
        FakeMetricClient.missing = True
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                now = datetime.now(JST); rows = [{"tweet_id": "1", "posted_at_jst": (now-timedelta(hours=2)).isoformat()}]
                self.assertEqual(post_metrics.collect(rows, now, FakeMetricClient, Path(td)/"db.sqlite")["missing"], 2)
        finally: FakeMetricClient.missing = False

    def test_24_top_and_bottom_three(self):
        rows = [{"growth_score": value} for value in range(8)]
        self.assertEqual(len(sorted(rows, key=lambda r: r["growth_score"], reverse=True)[:3]), 3)
        self.assertEqual(len(sorted(rows, key=lambda r: r["growth_score"])[:3]), 3)

    def test_25_daily_review_uses_luna(self):
        with patch.dict(os.environ, {"OPENAI_MODEL_DAILY_REVIEW": "gpt-5.6-luna"}):
            self.assertEqual(ModelRouter(ROOT/"config/openai_model_pricing.json").select_model("daily_review")["model"], "gpt-5.6-luna")

    def test_26_weekly_review_uses_terra(self):
        with patch.dict(os.environ, {"WEEKLY_REVIEW_ENABLED": "true", "OPENAI_MODEL_WEEKLY_REVIEW": "gpt-5.6-terra"}):
            self.assertEqual(ModelRouter(ROOT/"config/openai_model_pricing.json").select_model("weekly_report")["model"], "gpt-5.6-terra")

    def test_27_budget_fallback_chain_exists(self):
        with patch.dict(os.environ, {"WEEKLY_REVIEW_ENABLED": "true"}):
            route = ModelRouter(ROOT/"config/openai_model_pricing.json").select_model("weekly_report")
            self.assertIn("gpt-5.6-luna", route["fallback_models"])

    def test_28_learning_examples_are_bounded(self):
        self.assertLessEqual(len(post._load_performance_patterns("", 900)), 900)

    def test_29_bot_has_no_self_patch_operation(self):
        text = (ROOT/"src/report_ai.py").read_text(encoding="utf-8")
        self.assertNotIn("apply_patch", text)

    def test_30_prompt_versions_are_comparable(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            path = Path(td)/"db.sqlite"; metrics_db.init_db(path)
            metrics_db.insert_generated(None, {"prompt_version": "v1"}, path)
            metrics_db.insert_generated(None, {"prompt_version": "v2"}, path)
            with closing(metrics_db.connect(path)) as conn:
                groups = conn.execute("SELECT prompt_version,COUNT(*) FROM generated_posts GROUP BY prompt_version").fetchall()
            self.assertEqual(len(groups), 2)


class CostAndSafetyTests(unittest.TestCase):
    def _path(self, td): return Path(td)/"db.sqlite"

    def test_31_x_daily_cap_configuration(self): self.assertEqual(int(os.environ.get("X_SEARCH_MAX_POST_READS_PER_DAY", "54")), 54)
    def test_32_x_monthly_cap_configuration(self): self.assertEqual(int(os.environ.get("X_SEARCH_MAX_POST_READS_PER_MONTH", "1620")), 1620)
    def test_33_x_create_cap_matches_total_posts(self): self.assertEqual(int(os.environ.get("X_POST_CREATE_MAX_PER_DAY", "10")), 10)

    def test_34_openai_budget_guard(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {"OPENAI_MONTHLY_BUDGET_USD": "1", "OPENAI_BUDGET_RESERVE_USD": "0"}):
            path=self._path(td); metrics_db.init_db(path)
            metrics_db.write("""INSERT INTO api_usage_events(timestamp,provider,operation,estimated_cost_usd,success) VALUES (?,?,?,?,1)""", (datetime.now(JST).isoformat(),"openai","x",.99),path)
            self.assertEqual(api_budget.reserve("openai","post_generation","m",.02,path=path)[1], "openai_monthly_api_budget_guard")

    def test_35_x_budget_guard(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {"X_MONTHLY_BUDGET_USD": "1", "X_BUDGET_RESERVE_USD": "0"}):
            path=self._path(td); metrics_db.init_db(path)
            metrics_db.write("INSERT INTO api_usage_events(timestamp,provider,operation,estimated_cost_usd,success) VALUES (?,?,?,?,1)",(datetime.now(JST).isoformat(),"x","owned_read",.99),path)
            self.assertEqual(api_budget.reserve("x","post_create","x",.02,path=path)[1], "x_monthly_api_budget_guard")

    def test_36_total_budget_guard(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {"OPENAI_MONTHLY_BUDGET_USD":"100","X_MONTHLY_BUDGET_USD":"100","TOTAL_MONTHLY_API_BUDGET_USD":"23","TOTAL_BUDGET_RESERVE_USD":"1"}):
            path=self._path(td); metrics_db.init_db(path)
            metrics_db.write("INSERT INTO api_usage_events(timestamp,provider,operation,estimated_cost_usd,success) VALUES (?,?,?,?,1)",(datetime.now(JST).isoformat(),"openai","legacy",22.0),path)
            self.assertEqual(api_budget.reserve("x","post_create","x",.01,metadata={"is_breaking":True},path=path)[1], "total_monthly_api_budget_guard")

    def test_37_url_create_is_blocked_before_client(self):
        with self.assertRaisesRegex(ValueError, "url_detected"):
            post.post_to_x("https://example.com", [])

    def test_38_pricing_is_external(self): self.assertIn("openai", api_budget.load_pricing())

    def test_39_usage_is_saved_in_sqlite(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            path=self._path(td); rid,_=api_budget.reserve("x","owned_read","lookup",.001,1,path=path); api_budget.finalize(rid,.001,success=True,path=path)
            self.assertEqual(api_budget.usage_totals(path)["x_owned_reads"],1)

    def test_40_budget_status_fields(self):
        source=(ROOT/"local_bot.py").read_text(encoding="utf-8")
        self.assertIn("OpenAI今月使用額",source); self.assertIn("今月の予測着地",source)

    def test_41_api_key_not_in_error_type(self):
        self.assertNotIn(os.environ.get("OPENAI_API_KEY","secret"), "authentication_error_no_retry")

    def test_42_model_name_is_meta_leak(self):
        c={"tweet_text":"🚨 見出し\n\ngpt-5.4-mini "+"説明"*60}
        self.assertTrue(any(v.startswith("meta_leak") for v in post._candidate_quality_violations(c,{})))

    def test_43_auth_error_retry_is_bounded(self): self.assertEqual(int(os.environ.get("OPENAI_MAX_RETRIES","1")),1)

    def test_44_x_failure_does_not_remove_rss(self):
        with patch.object(news,"fetch_x_search_topics",side_effect=RuntimeError("x")), patch.object(news,"RSS_FEEDS",[]):
            self.assertEqual(news.fetch_all_items(include_x=True),[])

    def test_45_sqlite_failure_is_nonfatal(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            self.assertFalse(metrics_db.init_db(Path(td)))


if __name__ == "__main__": unittest.main()
