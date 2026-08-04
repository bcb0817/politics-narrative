import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import api_budget
import metrics_db
import quality_evals
import usage_reports
import xai_radar
import xai_cost
from publishing_policy import normalize_topic_key

JST = ZoneInfo("Asia/Tokyo")


class StartupLedgerV3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db = self.root / "metrics.db"
        metrics_db.apply_additive_migrations(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, request_id="req-1", ticks=100_000_000, success=True):
        with patch.object(xai_radar, "_state_dir", return_value=self.root):
            return xai_radar._record_usage(
                "grok-test", ticks, 100, 20, 1, success,
                schedule_slot="06:00", path=self.db, request_id=request_id,
                cached_input_tokens=10, successful_tool_calls=1 if success else 0,
                estimated_cost_usd=.02,
            )

    def test_01_request_id_is_saved(self):
        self.record()
        with metrics_db.connect(self.db) as conn:
            self.assertEqual(conn.execute(
                "SELECT request_id FROM xai_usage_events").fetchone()[0], "req-1")

    def test_02_request_id_is_unique(self):
        self.record()
        self.record()
        with metrics_db.connect(self.db) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM xai_usage_events").fetchone()[0], 1)

    def test_03_ticks_convert_at_ten_billion_per_usd(self):
        self.record(ticks=100_000_000)
        with metrics_db.connect(self.db) as conn:
            value = conn.execute(
                "SELECT actual_cost_usd FROM xai_usage_events").fetchone()[0]
        self.assertAlmostEqual(value, .01)

    def test_04_actual_cost_has_priority(self):
        self.record(ticks=100_000_000)
        self.assertAlmostEqual(api_budget.usage_totals(self.db)["xai"], .01)

    def test_05_estimated_cost_used_without_ticks(self):
        self.record(ticks=0)
        self.assertAlmostEqual(api_budget.usage_totals(self.db)["xai"], .02)

    def test_06_event_cost_is_not_cumulative(self):
        self.record("req-a", 100_000_000)
        self.record("req-b", 200_000_000)
        self.assertAlmostEqual(api_budget.usage_totals(self.db)["xai"], .03)

    def test_07_generic_xai_mirror_is_not_double_counted(self):
        self.record(ticks=100_000_000)
        metrics_db.write("""INSERT INTO api_usage_events
            (timestamp,provider,operation,estimated_cost_usd,success)
            VALUES (?,?,?,?,1)""",
            (datetime.now(JST).isoformat(), "xai", "x_search_radar", .01), self.db)
        self.assertAlmostEqual(api_budget.usage_totals(self.db)["xai"], .01)

    def test_08_cached_tokens_are_saved(self):
        self.record()
        with metrics_db.connect(self.db) as conn:
            self.assertEqual(conn.execute(
                "SELECT cached_input_tokens FROM xai_usage_events").fetchone()[0], 10)

    def test_09_successful_tool_count_is_saved(self):
        self.record()
        with metrics_db.connect(self.db) as conn:
            self.assertEqual(conn.execute(
                "SELECT successful_tool_call_count FROM xai_usage_events").fetchone()[0], 1)

    def test_10_failed_tool_count_is_zero(self):
        self.record(success=False)
        with metrics_db.connect(self.db) as conn:
            self.assertEqual(conn.execute(
                "SELECT successful_tool_call_count FROM xai_usage_events").fetchone()[0], 0)

    def test_11_json_history_is_audit_only(self):
        self.record()
        history = json.loads((self.root / "xai_usage_history.jsonl").read_text(
            encoding="utf-8").splitlines()[0])
        self.assertEqual(history["request_id"], "req-1")
        self.assertAlmostEqual(api_budget.usage_totals(self.db)["xai"], .01)

    def test_12_unverified_ledger_uses_operator_cap(self):
        with patch.dict(os.environ, {
            "XAI_MONTHLY_BUDGET_USD": "7",
            "XAI_UNVERIFIED_EFFECTIVE_LIMIT_USD": "5",
            "XAI_COST_LEDGER_VERIFIED": "false",
        }):
            self.assertEqual(api_budget.effective_xai_limit(), 5)

    def test_13_verified_ledger_unlocks_three(self):
        with patch.dict(os.environ, {
            "XAI_MONTHLY_BUDGET_USD": "3",
            "XAI_COST_LEDGER_VERIFIED": "true",
        }):
            self.assertEqual(api_budget.effective_xai_limit(), 3)

    def test_14_three_slot_schedule(self):
        with patch.dict(os.environ, {
            "XAI_SEARCH_SCHEDULE": "06:00,12:00,18:00",
            "XAI_ADAPTIVE_SCHEDULE_ENABLED": "false",
        }), patch.object(xai_radar, "usage_totals", return_value={"xai": 0}), \
             patch.object(xai_radar, "forecast",
                          return_value={"projected": {"xai": 0}}):
            self.assertEqual(len(xai_radar.effective_schedule(path=self.db)), 3)

    def test_15_low_volatility_has_three_slots(self):
        with patch.dict(os.environ, {
            "XAI_SEARCH_SCHEDULE": "06:00,12:00,18:00",
            "XAI_ADAPTIVE_SCHEDULE_ENABLED": "true",
        }), patch.object(xai_radar, "usage_totals", return_value={"xai": 0}), \
             patch.object(xai_radar, "forecast",
                          return_value={"projected": {"xai": 0}}), \
             patch.object(xai_radar, "local_volatility_score", return_value=1):
            self.assertEqual(xai_radar.effective_schedule(path=self.db),
                             {"06:00", "12:00", "18:00"})

    def test_16_schema_has_representative_post_ids(self):
        props = xai_radar._schema(5, 3)["properties"]["topics"]["items"]["properties"]
        self.assertIn("representative_post_ids", props)

    def test_17_schema_does_not_request_full_post_text(self):
        self.assertNotIn("post_text", json.dumps(xai_radar._schema(5, 3)))

    def test_18_attribution_uses_allowed_xai_label(self):
        with patch.object(xai_radar, "connect",
                          side_effect=RuntimeError("no database")):
            item = {"title": "税制改正案", "summary": "", "discovered_via": ["rss"]}
            xai_radar.apply_verified_attention(
                [item], [{"topic_key": "税制", "attention_score": 8,
                          "velocity_score": 7}])
        self.assertEqual(item["discovered_via"], ["rss", "xai"])

    def test_19_attribution_marks_external_verification(self):
        with patch.object(xai_radar, "connect",
                          side_effect=RuntimeError("no database")):
            item = {"title": "税制改正案", "summary": "", "discovered_via": ["rss"]}
            xai_radar.apply_verified_attention(
                [item], [{"topic_key": "税制", "attention_score": 8,
                          "velocity_score": 7}])
        self.assertTrue(item["verified"])

    def test_20_nfkc_normalizes_full_width_ascii(self):
        self.assertIn("ABC", normalize_topic_key("ＡＢＣ 政策"))

    def test_21_nfkc_detects_personal_attack(self):
        self.assertEqual(quality_evals.auto_fail_reason(
            "政策を審議中", "大臣は無能だ"), "personal_attack")

    def test_22_nfkc_detects_full_width_attack(self):
        self.assertEqual(quality_evals.auto_fail_reason(
            "政策を審議中", "大臣は無能だ"), "personal_attack")

    def test_23_policy_criticism_is_not_personal_attack(self):
        self.assertEqual(quality_evals.auto_fail_reason(
            "政策を審議中", "政府の政策判断には根拠の説明が必要です"), "")

    def test_24_unconfirmed_criminal_claim_is_rejected(self):
        self.assertEqual(quality_evals.auto_fail_reason(
            "捜査中", "あの人は犯罪者だ"), "criminal_assertion")

    def test_25_register_script_does_not_start_task(self):
        source = (ROOT / "production" / "register_task.ps1").read_text(
            encoding="utf-8-sig")
        self.assertNotIn("Start-ScheduledTask -TaskName $TaskName", source)

    def test_26_register_script_verifies_with_get_scheduled_task(self):
        source = (ROOT / "production" / "register_task.ps1").read_text(
            encoding="utf-8-sig")
        self.assertIn("Get-ScheduledTask -TaskName $TaskName", source)

    def test_27_register_script_uses_ignore_new(self):
        source = (ROOT / "production" / "register_task.ps1").read_text(
            encoding="utf-8-sig")
        self.assertIn("-MultipleInstances IgnoreNew", source)

    def test_28_register_script_uses_hidden_powershell(self):
        source = (ROOT / "production" / "register_task.ps1").read_text(
            encoding="utf-8-sig")
        self.assertIn("-WindowStyle Hidden", source)

    def test_29_register_script_uses_one_minute_restart(self):
        source = (ROOT / "production" / "register_task.ps1").read_text(
            encoding="utf-8-sig")
        self.assertIn("-RestartInterval (New-TimeSpan -Minutes 1)", source)

    def test_30_roi_requires_minimum_sample(self):
        with patch.dict(os.environ, {"XAI_ATTRIBUTION_MIN_SAMPLE_SIZE": "10"}):
            self.assertEqual(usage_reports.xai_roi(path=self.db)["decision"],
                             "insufficient_data")

    def test_31_openai_breakdown_handles_zero_posts(self):
        report = usage_reports.openai_usage_breakdown(path=self.db)
        self.assertIsNone(report["api_calls_per_post"])

    def test_32_xai_roi_never_uses_impressions_only(self):
        self.assertIn("Impressions alone",
                      usage_reports.xai_roi(path=self.db)["decision_rule"])

    def test_33_no_replacement_character_in_critical_sources(self):
        for path in (
            ROOT / "src" / "xai_radar.py",
            ROOT / "src" / "api_budget.py",
            ROOT / "production" / "register_task.ps1",
        ):
            self.assertNotIn("\ufffd", path.read_text(encoding="utf-8-sig"))

    def test_34_ticks_conversion_has_one_canonical_function(self):
        self.assertAlmostEqual(xai_cost.ticks_to_usd(100_000_000), .01)
        self.assertNotIn("10_000_000_000",
                         (ROOT / "src" / "xai_radar.py").read_text("utf-8"))
        self.assertNotIn("10_000_000_000",
                         (ROOT / "src" / "metrics_db.py").read_text("utf-8"))

    def test_35_compact_xai_input_is_limited_to_five(self):
        rows = [{"title": f"政策{i}", "summary": "短い概要"} for i in range(8)]
        self.assertEqual(len(xai_radar._compact_candidate_input(rows, 5)), 5)

    def test_36_compact_xai_input_truncates_summary(self):
        row = xai_radar._compact_candidate_input(
            [{"title": "税制改正", "summary": "あ" * 500}], 5)[0]
        self.assertLessEqual(len(row["summary"]), 180)

    def test_37_compact_xai_input_has_no_article_body(self):
        row = xai_radar._compact_candidate_input(
            [{"title": "税制改正", "summary": "概要", "body": "本文" * 500}], 5)[0]
        self.assertNotIn("body", row)

    def test_38_roi_includes_impressions_per_hour(self):
        self.assertIn("average_impressions_per_hour",
                      usage_reports.xai_roi(path=self.db)["xai_posts"])

    def test_39_roi_includes_engagement_rate(self):
        self.assertIn("average_engagement_rate",
                      usage_reports.xai_roi(path=self.db)["xai_posts"])

    def test_40_roi_includes_separate_bookmark_and_quote_rates(self):
        result = usage_reports.xai_roi(path=self.db)["xai_posts"]
        self.assertIn("bookmark_rate", result)
        self.assertIn("quote_rate", result)

    def test_41_roi_includes_quality_and_follow_conversion(self):
        result = usage_reports.xai_roi(path=self.db)["xai_posts"]
        self.assertIn("average_quality_score", result)
        self.assertIn("follow_conversion_estimate", result)

    def test_42_roi_includes_conversion_costs(self):
        result = usage_reports.xai_roi(path=self.db)
        self.assertIn("xai_cost_per_profile_click_usd", result)
        self.assertIn("xai_cost_per_estimated_follow_usd", result)

    def test_43_required_audit_filename_exists(self):
        self.assertTrue((ROOT / "AUDIT_STARTUP_XAI_COST_ENCODING.md").exists())

    def test_44_legacy_task_action_is_functional_noop(self):
        source = (ROOT / "production" / "run_daily_review.ps1").read_text(
            encoding="utf-8-sig")
        self.assertIn("legacy daily-review task skipped", source)
        self.assertNotIn("local_bot.py", source)


if __name__ == "__main__":
    unittest.main()
