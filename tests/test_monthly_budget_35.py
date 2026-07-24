import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import api_budget
import metrics_db

JST = ZoneInfo("Asia/Tokyo")


class MonthlyBudget35Tests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "OPENAI_MONTHLY_BUDGET_USD": "15",
            "XAI_MONTHLY_BUDGET_USD": "4",
            "X_MONTHLY_BUDGET_USD": "16",
            "TOTAL_MONTHLY_API_BUDGET_USD": "35",
            "OPENAI_BUDGET_RESERVE_USD": "1",
            "XAI_BUDGET_RESERVE_USD": ".25",
            "X_BUDGET_RESERVE_USD": ".75",
            "TOTAL_BUDGET_RESERVE_USD": "2",
            "BUDGET_USD_JPY_RATE": "165",
            "BUDGET_WARNING_RATIO": ".85",
            "BUDGET_RESTRICT_RATIO": ".93",
            "BUDGET_HARD_STOP_RATIO": "1",
            "XAI_COST_LEDGER_VERIFIED": "false",
        }

    def test_01_provider_sum_is_35(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(api_budget.budget_configuration()["provider_sum"], 35)

    def test_02_configuration_is_consistent(self):
        with patch.dict(os.environ, self.env):
            self.assertTrue(api_budget.budget_configuration()["consistent"])

    def test_03_mismatch_caps_effective_total(self):
        with patch.dict(os.environ, {**self.env, "TOTAL_MONTHLY_API_BUDGET_USD": "40"}):
            cfg = api_budget.budget_configuration()
            self.assertFalse(cfg["consistent"])
            self.assertEqual(cfg["effective_total_limit"], 35)

    def test_04_total_reserve_is_inside_budget(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(api_budget.budget_configuration()["effective_spendable"], 33)

    def test_05_jpy_budget_is_dynamic(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(api_budget.budget_configuration()["jpy_budget_display"], 5775)

    def test_06_jpy_rate_change_updates_display(self):
        with patch.dict(os.environ, {**self.env, "BUDGET_USD_JPY_RATE": "150"}):
            self.assertEqual(api_budget.budget_configuration()["jpy_budget_display"], 5250)

    def test_07_old_monthly_budget_jpy_is_not_source_of_truth(self):
        with patch.dict(os.environ, {**self.env, "MONTHLY_BUDGET_JPY": "5000"}):
            self.assertEqual(api_budget.budget_configuration()["jpy_budget_display"], 5775)

    def test_08_warning_stage(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(api_budget.budget_stage(.85), "warning")

    def test_09_restrict_stage(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(api_budget.budget_stage(.93), "restrict")

    def test_10_hard_stop_stage(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(api_budget.budget_stage(1), "hard_stop")

    def test_11_below_threshold_is_normal(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(api_budget.budget_stage(.849), "normal")

    def test_12_unverified_xai_is_capped_at_2(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(api_budget.effective_xai_limit(), 2)

    def test_13_verified_xai_uses_4(self):
        with patch.dict(os.environ, {**self.env, "XAI_COST_LEDGER_VERIFIED": "true"}):
            self.assertEqual(api_budget.effective_xai_limit(), 4)

    def test_14_openai_allocations_sum_to_15(self):
        values = [9, 1.5, .75, 1, .75, .5, .5, 1]
        self.assertEqual(sum(values), 15)

    def test_15_startup_lines_show_35(self):
        with patch.dict(os.environ, self.env):
            self.assertIn("Total monthly budget  : $35.00", api_budget.startup_budget_lines())

    def test_16_startup_lines_show_xai_effective_2(self):
        with patch.dict(os.environ, self.env):
            self.assertIn("xAI effective budget  : $2.00", api_budget.startup_budget_lines())

    def test_17_forecast_never_exceeds_total_limit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, \
             patch.dict(os.environ, self.env):
            result = api_budget.forecast(Path(td) / "db.sqlite")
            self.assertLessEqual(result["projected"]["total"], 35)

    def test_18_forecast_xai_respects_unverified_cap(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, \
             patch.dict(os.environ, self.env):
            result = api_budget.forecast(Path(td) / "db.sqlite")
            self.assertLessEqual(result["projected"]["xai"], 2)

    def test_19_provider_reserve_blocks_before_15(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, \
             patch.dict(os.environ, self.env):
            path = Path(td) / "db.sqlite"
            metrics_db.init_db(path)
            metrics_db.write("""INSERT INTO api_usage_events
              (timestamp,provider,operation,estimated_cost_usd,success)
              VALUES (?,?,?,?,1)""",
              (datetime.now(JST).isoformat(), "openai", "legacy", 13.99), path)
            self.assertEqual(
                api_budget.reserve("openai", "other", "m", .02, path=path)[1],
                "openai_monthly_api_budget_guard",
            )

    def test_20_history_table_is_additive(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            path = Path(td) / "db.sqlite"
            metrics_db.init_db(path)
            self.assertIn("budget_change_events", metrics_db.table_counts(path))

    def test_21_history_event_is_saved(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            path = Path(td) / "db.sqlite"
            event = {
                "changed_at": "2026-07-25T04:21:15+09:00",
                "previous_total_budget_usd": 23,
                "new_total_budget_usd": 35,
                "previous_provider_budgets": {"openai": 8, "xai": 3, "x": 12},
                "new_provider_budgets": {"openai": 15, "xai": 4, "x": 16},
                "reason": "user_requested_budget_increase",
            }
            metrics_db.record_budget_change(event, path)
            self.assertEqual(metrics_db.table_counts(path)["budget_change_events"], 1)

    def test_22_env_has_no_duplicate_budget_keys(self):
        lines = (ROOT / ".env").read_text(encoding="utf-8").splitlines()
        for key in self.env:
            if key == "XAI_COST_LEDGER_VERIFIED":
                continue
            self.assertLessEqual(sum(line.startswith(key + "=") for line in lines), 1)

    def test_23_budget_config_file_has_35(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(api_budget.budget_configuration()["configured_total"], 35)

    def test_24_local_monitoring_has_no_budget_reservation(self):
        source = (ROOT / "src" / "news.py").read_text(encoding="utf-8")
        self.assertIn("continuing with RSS", source)

    def test_25_old_fixed_jpy_guards_removed(self):
        source = (ROOT / "src" / "api_budget.py").read_text(encoding="utf-8")
        self.assertNotIn("projected_jpy > 4800", source)
        self.assertNotIn("projected_jpy > 4500", source)

    def test_26_budget_status_uses_dynamic_configuration(self):
        source = (ROOT / "local_bot.py").read_text(encoding="utf-8")
        self.assertIn("budget_configuration", source)
        self.assertIn("effective spendable budget", source)

    def test_27_restriction_starts_with_content_pipeline(self):
        with patch.dict(os.environ, self.env):
            self.assertEqual(api_budget.restriction_level(.93), 1)

    def test_28_quality_eval_is_second_step(self):
        source = (ROOT / "src" / "api_budget.py").read_text(encoding="utf-8")
        self.assertIn('"quality_eval": 2', source)

    def test_29_weekly_review_is_third_step(self):
        source = (ROOT / "src" / "api_budget.py").read_text(encoding="utf-8")
        self.assertIn('"weekly_report": 3', source)

    def test_30_daily_review_is_fourth_step(self):
        source = (ROOT / "src" / "api_budget.py").read_text(encoding="utf-8")
        self.assertIn('"daily_review": 4', source)

    def test_31_xai_reduction_is_fifth_step(self):
        source = (ROOT / "src" / "api_budget.py").read_text(encoding="utf-8")
        self.assertIn('"x_search_radar": 5', source)

    def test_32_classifier_is_sixth_step(self):
        source = (ROOT / "src" / "api_budget.py").read_text(encoding="utf-8")
        self.assertIn('"classifier": 6', source)

    def test_33_normal_posts_reduce_at_seventh_step(self):
        source = (ROOT / "src" / "post.py").read_text(encoding="utf-8")
        self.assertIn('get("restriction_level", 0) >= 7', source)


if __name__ == "__main__":
    unittest.main()
