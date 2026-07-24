import csv
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import audit_tools
import content_pipeline
import engagement_queue
import growth_analytics
import growth_tracking
import metrics_db
import quality_evals
import usage_reports
from xai_cost import ticks_to_usd


JST = ZoneInfo("Asia/Tokyo")


class GrowthRevenueP0P2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.db = self.base / "bot_metrics.db"
        metrics_db.init_db(self.db)
        metrics_db.apply_additive_migrations(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_01_ticks_conversion(self):
        self.assertEqual(ticks_to_usd(10_000_000_000), 1.0)
        self.assertEqual(ticks_to_usd(123_456_789), 0.0123456789)

    def test_02_clean_ledger_passes(self):
        result = audit_tools.verify_xai_ledger(self.db)
        self.assertTrue(result["passed"])
        self.assertIn("Set XAI_COST_LEDGER_VERIFIED=true",
                      result["recommendation"])

    def test_03_bad_actual_cost_fails(self):
        metrics_db.write("""INSERT INTO xai_usage_events
          (request_id,timestamp,model,operation,input_tokens,output_tokens,
           tool_call_count,successful_tool_call_count,cost_in_usd_ticks,
           actual_cost_usd,estimated_cost_usd,cost_source,cache_used,success)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            "bad-cost", datetime.now(JST).isoformat(), "grok", "x_search_radar",
            1, 1, 1, 1, 100_000_000, 9.0, .02, "actual", 0, 1,
        ), self.db)
        result = audit_tools.verify_xai_ledger(self.db)
        self.assertFalse(result["checks"]["ticks_conversion"]["pass"])

    def test_04_billed_cache_fails(self):
        metrics_db.write("""INSERT INTO xai_usage_events
          (request_id,timestamp,model,operation,tool_call_count,
           successful_tool_call_count,cost_in_usd_ticks,actual_cost_usd,
           estimated_cost_usd,cost_source,cache_used,success)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
            "cache-cost", datetime.now(JST).isoformat(), "grok",
            "x_search_radar", 0, 0, 0, 0, .01, "estimated", 1, 1,
        ), self.db)
        result = audit_tools.verify_xai_ledger(self.db)
        self.assertFalse(result["checks"]["cache_billing"]["pass"])

    def test_05_provider_guard_blocks_second_provider(self):
        now = datetime(2026, 7, 25, 6, 0, tzinfo=JST)
        with patch.dict(os.environ, {"STATE_DIR": str(self.base)}, clear=False):
            self.assertTrue(audit_tools.guard_provider_execution("xai", now))
            self.assertFalse(audit_tools.guard_provider_execution("native_x", now))

    def test_06_provider_none_is_valid_rss_only(self):
        with patch.dict(os.environ, {
            "STATE_DIR": str(self.base),
            "X_TOPIC_DISCOVERY_PROVIDER": "none",
            "X_NATIVE_SEARCH_ENABLED": "false",
            "XAI_ENABLED": "true",
        }, clear=False):
            status = audit_tools.discovery_provider_status(self.db)
        self.assertEqual(status["configured_provider"], "none")
        self.assertTrue(status["configuration_valid"])

    def test_07_quality_dashboard_empty_is_nonfatal(self):
        result = quality_evals.quality_dashboard(self.db)
        self.assertIn("No quality eval results", result["quality_evals"]["message"])

    def test_08_quality_dashboard_contains_latest_eval(self):
        metrics_db.write("""INSERT INTO quality_eval_runs
          (run_at,mode,prompt_version,fixture_count,passed_count,failed_count,
           average_scores_json,pass_rate,automatic_disqualifications_json,
           estimated_cost_usd,result_path)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
            datetime.now(JST).isoformat(), "rule-only", "v2", 10, 9, 1,
            json.dumps({"factuality": 4.5}), .9,
            json.dumps({"url": 1}), 0, "result.json",
        ), self.db)
        latest = quality_evals.quality_dashboard(self.db)["quality_evals"]["latest"]
        self.assertEqual(latest["fixture_count"], 10)
        self.assertEqual(latest["automatic_disqualifications"]["url"], 1)

    def test_09_unknown_openai_task_is_reported(self):
        metrics_db.write("""INSERT INTO api_usage_events
          (timestamp,provider,operation,model_or_endpoint,resource_count,
           input_tokens,cached_input_tokens,output_tokens,estimated_cost_usd,
           success,fallback_used,error_type,metadata_json,task_type_source)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            datetime.now(JST).isoformat(), "openai", "mystery", "model", 1,
            10, 0, 2, .01, 1, 0, "", "{}", None,
        ), self.db)
        result = usage_reports.openai_usage_breakdown(self.db)
        self.assertEqual(result["unknown_task_type_calls"], 1)
        self.assertEqual(result["task_types"]["unknown"]["input_tokens"], 10)

    def test_10_queue_mark_posted_saves_post_id(self):
        queue_id = metrics_db.write("""INSERT INTO engagement_queue
          (queue_type,source_post_id,author_handle,author_type,topic_key,
           source_verified,status,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?)""", (
            "quote", "source1", "official", "official", "topic", 1,
            "approved", datetime.now(JST).isoformat(),
            datetime.now(JST).isoformat(),
        ), self.db)
        self.assertTrue(engagement_queue.mark_posted(
            "quote", queue_id, "999", path=self.db))
        with closing(metrics_db.connect(self.db)) as conn:
            row = conn.execute(
                "SELECT status,manual_post_id FROM engagement_queue WHERE id=?",
                (queue_id,)).fetchone()
        self.assertEqual(row["status"], "posted_manually")
        self.assertEqual(row["manual_post_id"], "999")

    def test_11_engagement_results_import(self):
        queue_id = metrics_db.write("""INSERT INTO engagement_queue
          (queue_type,source_post_id,status,manual_post_id,created_at,updated_at)
          VALUES ('reply','s','posted_manually','777',?,?)""", (
            datetime.now(JST).isoformat(), datetime.now(JST).isoformat()), self.db)
        source = self.base / "engagement.csv"
        source.write_text(
            "queue_id,queue_type,measurement_window,impressions,likes\n"
            f"{queue_id},reply,24h,100,5\n", encoding="utf-8")
        result = engagement_queue.import_engagement_results(source, self.db)
        self.assertEqual(result["inserted"], 1)

    def test_12_engagement_performance_requires_ten(self):
        result = engagement_queue.engagement_performance(self.db)
        self.assertEqual(result["decision"], "insufficient_data")
        self.assertEqual(result["x_writes"], 0)

    def _conversion_csv(self):
        source = self.base / "conversions.csv"
        source.write_text(
            "occurred_at,source,campaign,content_id,event_type,value\n"
            "2026-07-25T20:00:00+09:00,x,evening,tweet_1,note_click,1\n"
            "2026-07-25T20:00:00+09:00,x,evening,tweet_1,bad_type,1\n",
            encoding="utf-8")
        return source

    def test_13_conversion_import_deduplicates(self):
        source = self._conversion_csv()
        first = growth_tracking.import_conversions(source, self.db)
        second = growth_tracking.import_conversions(source, self.db)
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["duplicates"], 1)

    def test_14_invalid_conversion_is_quarantined(self):
        result = growth_tracking.import_conversions(
            self._conversion_csv(), self.db)
        self.assertEqual(result["quarantined"], 1)

    def test_15_conversion_dashboard_counts_note(self):
        growth_tracking.import_conversions(self._conversion_csv(), self.db)
        result = growth_analytics.conversion_dashboard(self.db)
        self.assertEqual(result["event_totals"]["note_click"], 1)

    def test_16_overlapping_posts_lower_follow_confidence(self):
        start = datetime(2026, 7, 25, 10, 0, tzinfo=JST)
        for index, minute in enumerate((0, 30)):
            metrics_db.write("""INSERT INTO published_posts
              (tweet_id,posted_at,post_type,hook_type,critique_axis)
              VALUES (?,?,?,?,?)""", (
                str(index + 1), (start + timedelta(minutes=minute)).isoformat(),
                "issue_diagram", "number", "fiscal_discipline"), self.db)
        for stamp, count in ((start - timedelta(minutes=10), 100),
                             (start + timedelta(minutes=60), 102)):
            metrics_db.write("""INSERT INTO follower_snapshots
              (captured_at,followers_count,source) VALUES (?,?,?)""",
              (stamp.isoformat(), count, "test"), self.db)
        result = growth_analytics.follower_conversion_analysis(self.db)
        self.assertGreaterEqual(
            result["confidence_sample_counts"].get("medium", 0)
            + result["confidence_sample_counts"].get("low", 0), 1)

    def test_17_digest_comparison_does_not_pick_small_sample_winner(self):
        metrics_db.write("""INSERT INTO published_posts
          (tweet_id,posted_at,post_type,digest_type)
          VALUES ('1',?,'morning_evening_digest','morning')""",
          (datetime.now(JST).isoformat(),), self.db)
        result = growth_analytics.digest_comparison(self.db)
        self.assertEqual(result["decision"], "insufficient_data")

    def test_18_content_pipeline_generates_local_drafts(self):
        week = datetime.now(JST).date() - timedelta(
            days=datetime.now(JST).date().weekday())
        metrics_db.write("""INSERT INTO published_posts
          (tweet_id,posted_at,topic_key,post_type,text)
          VALUES ('1',?,'budget-policy','issue_diagram','source text')""",
          (week.isoformat(),), self.db)
        with patch.object(content_pipeline, "_root", return_value=self.base), \
             patch.dict(os.environ, {
                 "CONTENT_PIPELINE_ENABLED": "true",
                 "CONTENT_PIPELINE_WEEKLY_BUDGET_USD": "0.40",
             }, clear=False):
            result = content_pipeline.build_content_pipeline(
                week.isoformat(), path=self.db)
        self.assertEqual(result["status"], "draft")
        self.assertTrue(result["shorts"])
        self.assertTrue(result["note_articles"])
        self.assertFalse(result["published"])

    def test_19_content_pipeline_filters_correction_rows(self):
        week = datetime.now(JST).date() - timedelta(
            days=datetime.now(JST).date().weekday())
        metrics_db.write("""INSERT INTO published_posts
          (tweet_id,posted_at,topic_key,post_type,text)
          VALUES ('1',?,'unsafe-topic','issue_diagram','text')""",
          (week.isoformat(),), self.db)
        metrics_db.write("""INSERT INTO post_quality_dimensions
          (tweet_id,correction_required,anger_score,trust_score)
          VALUES ('1',1,9,2)""", path=self.db)
        with patch.object(content_pipeline, "_root", return_value=self.base):
            result = content_pipeline.build_content_pipeline(
                week.isoformat(), path=self.db)
        self.assertEqual(result["status"], "insufficient_data")

    def test_20_legacy_task_script_protects_current_task(self):
        text = (ROOT / "production" / "disable_legacy_tasks_admin.ps1").read_text(
            encoding="utf-8")
        self.assertIn("PoliticsNarrativeBot", text)
        self.assertIn("$CurrentTasks -contains", text)
        self.assertNotIn("Unregister-ScheduledTask", text)

    def test_21_stale_model_scripts_are_quarantined(self):
        directory = ROOT / "archive" / "deprecated" / "model_migrations"
        self.assertTrue((directory /
            "DEPRECATED_DO_NOT_RUN_update_openai_models.ps1").exists())
        self.assertFalse((ROOT / "production" / "update_openai_models.ps1").exists())

    def test_22_content_pipeline_has_no_publish_clients(self):
        text = (ROOT / "src" / "content_pipeline.py").read_text(encoding="utf-8")
        for forbidden in ("selenium", "playwright", "create_tweet", "note.com/api"):
            self.assertNotIn(forbidden, text.lower())


if __name__ == "__main__":
    unittest.main()
