import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
os.environ["POST_ENABLED"] = "false"

import api_budget
import local_bot
import metrics_db
import news
import post
from publishing_policy import phase_daily_limit_reached, pre_generation_skip_reason

JST = ZoneInfo("Asia/Tokyo")


def history(now, normal=0, breaking=0):
    rows = []
    for index in range(normal):
        rows.append({"tweet_id": f"n{index}", "post_type": "strong_opinion",
                     "posted_at_jst": (now - timedelta(minutes=index + 1)).isoformat()})
    for index in range(breaking):
        rows.append({"tweet_id": f"b{index}", "post_type": "breaking_news",
                     "posted_at_jst": (now - timedelta(minutes=normal + index + 1)).isoformat()})
    return rows


class HourlyBudgetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 21, 18, 0, tzinfo=JST)

    def test_01_monitoring_is_every_forty_five_minutes(self):
        with patch.dict(os.environ, {"MONITOR_INTERVAL_MINUTES": "45"}):
            self.assertEqual(local_bot._slot_interval_minutes(), 45)

    def test_02_active_window_has_twenty_five_slots(self):
        expected = [f"{minute // 60:02d}:{minute % 60:02d}"
                    for minute in range(0, 1440, 45)
                    if 5 <= minute // 60 <= 23]
        with patch.object(post, "SLOT_INTERVAL_MINUTES", 45), \
                patch.object(post, "ACTIVE_HOURS", set(range(5, 24))), \
                patch.dict(os.environ, {"MONITOR_SCHEDULE_MINUTE": "0"}):
            self.assertEqual(post.build_post_slots(), expected)
            self.assertEqual(len(expected), 25)

    def test_03_x_search_has_only_three_schedule_slots(self):
        with patch.dict(os.environ, {"X_SEARCH_SCHEDULE": "06:00,12:00,18:00"}):
            matches = sum(news.should_run_x_search(self.now.replace(hour=h, minute=m))
                          for h in range(24) for m in range(60))
            self.assertEqual(matches, 3)

    def test_04_x_search_per_run_cap_is_eighteen(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {
            "X_SEARCH_MAX_POST_READS_PER_RUN": "18", "X_BUDGET_RESERVE_USD": "0",
        }):
            path = Path(td) / "db.sqlite"
            _, reason = api_budget.reserve("x", "x_search", "recent_search", 0, 19, path=path)
            self.assertEqual(reason, "x_search_per_run_resource_cap")

    def test_05_x_search_daily_cap_is_fifty_four(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {
            "X_SEARCH_MAX_POST_READS_PER_DAY": "54", "X_BUDGET_RESERVE_USD": "0",
        }):
            path = Path(td) / "db.sqlite"; metrics_db.init_db(path)
            for _ in range(3):
                metrics_db.write("""INSERT INTO api_usage_events
                  (timestamp,provider,operation,resource_count,estimated_cost_usd,success)
                  VALUES (?,?,?,?,0,1)""", (datetime.now(JST).isoformat(), "x", "x_search", 18), path)
            _, reason = api_budget.reserve("x", "x_search", "recent_search", 0, 1, path=path)
            self.assertEqual(reason, "x_search_daily_resource_cap")

    def test_06_normal_posts_stop_at_twenty(self):
        self.assertTrue(phase_daily_limit_reached(history(self.now, normal=20), self.now, False))

    def test_07_total_posts_stop_at_twenty(self):
        self.assertTrue(phase_daily_limit_reached(history(self.now, 18, 2), self.now, True))

    def test_08_post_interval_is_forty_five_minutes(self):
        rows = history(self.now, normal=1)
        rows[0]["posted_at_jst"] = (self.now - timedelta(minutes=44)).isoformat()
        self.assertEqual(pre_generation_skip_reason(rows, self.now, 20, 45), "minimum_post_interval")

    def test_09_openai_provider_cap_is_enforced(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {
            "OPENAI_MONTHLY_BUDGET_USD": "8", "OPENAI_BUDGET_RESERVE_USD": "0",
        }):
            path = Path(td) / "db.sqlite"; metrics_db.init_db(path)
            metrics_db.write("""INSERT INTO api_usage_events
              (timestamp,provider,operation,estimated_cost_usd,success) VALUES (?,?,?,?,1)""",
              (datetime.now(JST).isoformat(), "openai", "legacy", 7.99), path)
            self.assertEqual(api_budget.reserve("openai", "post_generation", "m", .02, path=path)[1],
                             "openai_monthly_api_budget_guard")

    def test_10_x_provider_cap_is_enforced(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {
            "X_MONTHLY_BUDGET_USD": "13", "X_BUDGET_RESERVE_USD": "0",
        }):
            path = Path(td) / "db.sqlite"; metrics_db.init_db(path)
            metrics_db.write("""INSERT INTO api_usage_events
              (timestamp,provider,operation,estimated_cost_usd,success) VALUES (?,?,?,?,1)""",
              (datetime.now(JST).isoformat(), "x", "legacy", 12.99), path)
            self.assertEqual(api_budget.reserve("x", "post_create", "x", .02, 1, path=path)[1],
                             "x_monthly_api_budget_guard")

    def test_11_total_api_cap_is_enforced(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {
            "OPENAI_MONTHLY_BUDGET_USD": "100", "X_MONTHLY_BUDGET_USD": "100",
            "TOTAL_MONTHLY_API_BUDGET_USD": "23", "TOTAL_BUDGET_RESERVE_USD": "0",
        }):
            path = Path(td) / "db.sqlite"; metrics_db.init_db(path)
            metrics_db.write("""INSERT INTO api_usage_events
              (timestamp,provider,operation,estimated_cost_usd,success) VALUES (?,?,?,?,1)""",
              (datetime.now(JST).isoformat(), "openai", "legacy", 22.99), path)
            self.assertEqual(api_budget.reserve("x", "post_create", "x", .02, 1, path=path)[1],
                             "total_monthly_api_budget_guard")

    def test_12_x_search_pauses_above_jpy_4800_projection(self):
        with patch.dict(os.environ, {"X_SEARCH_ENABLED": "true"}), \
             patch.object(news, "should_run_x_search", return_value=True), \
             patch.object(news, "_already_ran_current_schedule", return_value=False), \
             patch.object(news, "cost_forecast", return_value={"pause_x_search": True}), \
             patch.object(news, "load_x_search_cache", return_value=[{"cached": True}]):
            self.assertEqual(news.fetch_x_search_topics([]), [{"cached": True}])

    def test_13_review_budget_cannot_consume_post_budget(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, patch.dict(os.environ, {
            "OPENAI_MONTHLY_BUDGET_USD": "8", "OPENAI_BUDGET_RESERVE_USD": "0",
            "OPENAI_POST_BUDGET_USD": "5", "OPENAI_DAILY_REVIEW_BUDGET_USD": "1.5",
        }):
            path = Path(td) / "db.sqlite"; metrics_db.init_db(path)
            metrics_db.write("""INSERT INTO api_usage_events
              (timestamp,provider,operation,estimated_cost_usd,success) VALUES (?,?,?,?,1)""",
              (datetime.now(JST).isoformat(), "openai", "daily_review", 1.5), path)
            self.assertEqual(api_budget.reserve("openai", "daily_review", "m", .01, path=path)[1],
                             "openai_daily_review_budget_guard")
            self.assertEqual(api_budget.reserve("openai", "post_generation", "m", .01, path=path)[1], "")
            metrics_db.write("""INSERT INTO api_usage_events
              (timestamp,provider,operation,estimated_cost_usd,success) VALUES (?,?,?,?,1)""",
              (datetime.now(JST).isoformat(), "openai", "post_generation", 4.5), path)
            self.assertEqual(api_budget.reserve("openai", "post_generation", "m", .01, path=path)[1],
                             "")
            self.assertEqual(api_budget.reserve("openai", "post_generation", "m", .01,
                                                metadata={"is_breaking": True}, path=path)[1], "")

    def test_14_twelve_posts_is_a_target_not_a_quota(self):
        self.assertFalse(phase_daily_limit_reached(history(self.now, normal=3), self.now, False))
        self.assertEqual(post.prefilter_news([{"title": "弱い芸能ニュース", "url": "x",
                                               "source_name": "unknown"}]), [])


if __name__ == "__main__":
    unittest.main()
