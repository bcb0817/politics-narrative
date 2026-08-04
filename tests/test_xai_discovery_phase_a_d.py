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

import metrics_db
import xai_discovery
import xai_radar

JST = ZoneInfo("Asia/Tokyo")


class XaiDiscoveryPhaseADTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db = self.root / "metrics.db"
        metrics_db.apply_additive_migrations(self.db)
        self.now = datetime(2026, 7, 31, 12, 0, tzinfo=JST)
        self.env = {
            "XAI_MONTHLY_BUDGET_USD": "30",
            "XAI_VERIFIED_EFFECTIVE_LIMIT_USD": "30",
            "XAI_UNVERIFIED_EFFECTIVE_LIMIT_USD": "7.5",
            "XAI_ALLOW_UNVERIFIED_FULL_BUDGET": "false",
            "XAI_COST_LEDGER_VERIFIED": "false",
            "XAI_SEARCH_SCHEDULE": "06:00,09:00,12:00,15:00,18:00,21:00",
            "XAI_LOW_VOLATILITY_SCHEDULE": "06:00,12:00,18:00",
            "XAI_RESTRICTED_SCHEDULE": "06:00,18:00",
            "XAI_SEARCH_DEFAULT_LOOKBACK_MINUTES": "240",
            "XAI_SEARCH_OVERLAP_MINUTES": "30",
            "XAI_SEARCH_MAX_LOOKBACK_HOURS": "24",
            "XAI_STANDARD_MAX_TOPICS": "5",
            "XAI_STANDARD_MAX_TOOL_CALLS": "2",
            "XAI_STANDARD_MAX_TURNS": "2",
            "XAI_EXTENDED_MAX_TOPICS": "8",
            "XAI_EXTENDED_MAX_TOOL_CALLS": "3",
            "XAI_EXTENDED_MAX_TURNS": "3",
            "XAI_EXTENDED_RESEARCH_ENABLED": "true",
            "XAI_IMAGE_UNDERSTANDING_IMPORTANT_ONLY": "true",
            "XAI_VIDEO_UNDERSTANDING_ENABLED": "false",
            "XAI_ROI_MIN_SAMPLE_SIZE": "30",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _usage(self, cost, *, verified=True, request_id=None):
        ticks = int(cost * 10_000_000_000) if verified else 0
        metrics_db.write(
            """INSERT INTO xai_usage_events
               (request_id,timestamp,model,operation,cost_in_usd_ticks,
                actual_cost_usd,estimated_cost_usd,cost_source,success,
                started_at,completed_at,cost_verified)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                request_id or f"req-{cost}-{datetime.now().timestamp()}",
                self.now.isoformat(), "grok", "x_search_radar", ticks,
                cost if verified else 0, cost,
                "actual" if verified else "unverified_estimate", 1,
                self.now.isoformat(), self.now.isoformat(), int(verified),
            ),
            self.db,
        )

    def _candidate(self, important=False, image=False):
        return {
            "content_id": "official-1",
            "title": "政府が重要法案の採決日程を公式発表",
            "summary": "国会の公式資料に採決日程を掲載",
            "source_url": "fixture://official/1",
            "verified": True,
            "source_reliability_score": 9,
            "importance_score": 9 if important else 5,
            "is_major_update": important,
            "has_image": image,
        }

    def _topic(self):
        return {
            "topic_key": "重要法案 採決",
            "attention_score": 8,
            "velocity_score": 7,
            "main_claims": ["関心がある"],
            "counter_claims": ["慎重論"],
            "representative_post_ids": ["1", "2", "3"],
            "evidence_count": 3,
            "unique_source_estimate": 3,
            "search_confidence": .9,
            "data_sufficiency": "sufficient",
        }

    def test_01_unverified_limit_is_7_5(self):
        with patch.dict(os.environ, self.env, clear=False):
            self.assertEqual(xai_discovery.effective_limit(), (7.5, "unverified_cap"))

    def test_02_verified_limit_is_30(self):
        env = {**self.env, "XAI_COST_LEDGER_VERIFIED": "true"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(xai_discovery.effective_limit(), (30.0, "verified"))

    def test_03_operator_override_is_explicit(self):
        env = {**self.env, "XAI_ALLOW_UNVERIFIED_FULL_BUDGET": "true"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(xai_discovery.effective_limit(), (30.0, "operator_override"))

    def test_04_remaining_budget_subtracts_actual(self):
        self._usage(1.5)
        env = {**self.env, "XAI_COST_LEDGER_VERIFIED": "true"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(
                xai_discovery.budget_plan(self.db, self.now).remaining_budget_usd,
                28.5,
            )

    def test_05_dynamic_budget_has_safety_factor(self):
        env = {**self.env, "XAI_COST_LEDGER_VERIFIED": "true"}
        with patch.dict(os.environ, env, clear=False):
            plan = xai_discovery.budget_plan(self.db, self.now)
            self.assertLessEqual(plan.dynamic_target_per_run_usd, .2)
            self.assertGreaterEqual(plan.dynamic_target_per_run_usd, 0)

    def test_06_normal_mode(self):
        env = {**self.env, "XAI_COST_LEDGER_VERIFIED": "true"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(xai_discovery.budget_plan(
                self.db, self.now).mode, "normal")

    def test_07_low_frequency_mode(self):
        self._usage(26.0)
        env = {**self.env, "XAI_COST_LEDGER_VERIFIED": "true"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(xai_discovery.budget_plan(
                self.db, self.now).mode, "low_frequency")

    def test_08_restricted_mode(self):
        self._usage(28.0)
        env = {**self.env, "XAI_COST_LEDGER_VERIFIED": "true"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(xai_discovery.budget_plan(
                self.db, self.now).mode, "restricted")

    def test_09_stopped_at_limit(self):
        self._usage(30.0)
        env = {**self.env, "XAI_COST_LEDGER_VERIFIED": "true"}
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(xai_discovery.budget_plan(
                self.db, self.now).mode, "stopped")

    def test_10_three_consecutive_unverified_stops(self):
        for i in range(3):
            self._usage(.1, verified=False, request_id=f"u-{i}")
        with patch.dict(os.environ, self.env, clear=False):
            self.assertEqual(xai_discovery.budget_plan(
                self.db, self.now).mode, "stopped")

    def test_11_legacy_unverified_rows_do_not_stop(self):
        metrics_db.write(
            """INSERT INTO xai_usage_events
               (request_id,timestamp,model,operation,estimated_cost_usd,
                cost_source,success,cost_verified)
               VALUES (?,?,?,?,?,?,1,0)""",
            ("legacy", self.now.isoformat(), "grok", "x_search_radar",
             .1, "estimated"),
            self.db,
        )
        with patch.dict(os.environ, self.env, clear=False):
            self.assertTrue(xai_discovery.ledger_health(
                self.db, self.now)["healthy"])

    def test_12_default_window_is_240_minutes(self):
        with patch.dict(os.environ, self.env, clear=False):
            window = xai_discovery.search_window(self.db, self.now)
            self.assertEqual(window.actual_search_window_minutes, 240)

    def test_13_window_has_30_minute_overlap(self):
        with patch.dict(os.environ, self.env, clear=False):
            window = xai_discovery.search_window(
                self.db, self.now, self.now - timedelta(hours=3))
            self.assertEqual(window.actual_search_window_minutes, 210)

    def test_14_six_hour_gap_is_covered(self):
        with patch.dict(os.environ, self.env, clear=False):
            window = xai_discovery.search_window(
                self.db, self.now, self.now - timedelta(hours=6))
            self.assertEqual(window.actual_search_window_minutes, 390)

    def test_15_twelve_hour_gap_is_covered(self):
        with patch.dict(os.environ, self.env, clear=False):
            window = xai_discovery.search_window(
                self.db, self.now, self.now - timedelta(hours=12))
            self.assertEqual(window.actual_search_window_minutes, 750)

    def test_16_thirty_hour_gap_is_capped_at_24h(self):
        with patch.dict(os.environ, self.env, clear=False):
            window = xai_discovery.search_window(
                self.db, self.now, self.now - timedelta(hours=30))
            self.assertEqual(window.actual_search_window_minutes, 1440)
            self.assertGreater(window.coverage_gap_minutes, 0)

    def test_17_standard_profile_limits(self):
        with patch.dict(os.environ, self.env, clear=False):
            profile = xai_discovery.research_profile(
                [self._candidate()], "normal", .2)
            self.assertEqual((profile["max_topics"], profile["max_tool_calls"],
                              profile["max_turns"]), (5, 2, 2))

    def test_18_extended_profile_limits(self):
        with patch.dict(os.environ, self.env, clear=False):
            profile = xai_discovery.research_profile(
                [self._candidate(important=True)], "normal", .2)
            self.assertEqual((profile["max_topics"], profile["max_tool_calls"],
                              profile["max_turns"]), (8, 3, 3))

    def test_19_standard_images_are_off(self):
        with patch.dict(os.environ, self.env, clear=False):
            self.assertFalse(xai_discovery.research_profile(
                [self._candidate(image=True)], "normal", .2)[
                    "image_understanding"])

    def test_20_video_is_always_off_by_default(self):
        with patch.dict(os.environ, self.env, clear=False):
            self.assertFalse(xai_discovery.research_profile(
                [self._candidate(important=True, image=True)], "normal", .2)[
                    "video_understanding"])

    def test_21_low_match_confidence_has_no_bonus(self):
        with patch.dict(os.environ, self.env, clear=False):
            topic = {**self._topic(), "topic_key": "無関係なテーマ"}
            record = xai_discovery.topic_audit_record(
                self._candidate(), topic, "run")
            self.assertEqual(record["score_bonus"], 0)

    def test_22_bonus_is_capped_at_two(self):
        with patch.dict(os.environ, self.env, clear=False):
            record = xai_discovery.topic_audit_record(
                self._candidate(), self._topic(), "run")
            self.assertLessEqual(record["score_bonus"], 2)

    def test_23_cached_velocity_is_null(self):
        with patch.dict(os.environ, self.env, clear=False):
            cached = xai_discovery.cached_signal(
                self._topic(), self.now - timedelta(hours=2), self.now)
            self.assertIsNone(cached["x_velocity_estimate"])
            self.assertEqual(cached["x_signal_type"], "cached")

    def test_24_cache_attention_decays(self):
        with patch.dict(os.environ, self.env, clear=False):
            cached = xai_discovery.cached_signal(
                self._topic(), self.now - timedelta(hours=2), self.now)
            self.assertLess(cached["x_attention_estimate"], 8)

    def test_25_schema_tables_are_idempotent(self):
        metrics_db.apply_additive_migrations(self.db)
        counts = metrics_db.table_counts(self.db)
        for table in ("xai_discovery_runs", "xai_discovery_topics",
                      "xai_budget_mode_history", "xai_roi_results"):
            self.assertIn(table, counts)

    def test_26_fixture_dry_run_never_calls_api_or_posts(self):
        with patch.dict(os.environ, self.env, clear=False):
            result = xai_discovery.dry_run()
            self.assertEqual(result["api_calls"], 0)
            self.assertEqual(result["external_posts"], 0)
            self.assertTrue(all(result["checks"].values()))

    def test_27_phase_d_requires_30_successes(self):
        with patch.dict(os.environ, self.env, clear=False):
            result = xai_discovery.phase_d_optimize(
                path=self.db, now=self.now, apply=True)
            self.assertFalse(result["eligible"])
            self.assertFalse(result["applied"])

    def test_28_phase_d_applies_bounded_tuning_after_30(self):
        for i in range(30):
            run = {
                "run_id": f"r-{i}", "mode": "standard",
                "started_at": (self.now - timedelta(minutes=i)).isoformat(),
                "completed_at": self.now.isoformat(),
                "requested_from_at": self.now.isoformat(),
                "requested_to_at": self.now.isoformat(),
                "search_window_minutes": 240, "coverage_gap_minutes": 0,
                "topic_count": 1, "tool_call_count": 2, "turn_count": 2,
                "image_understanding_used": 0,
                "video_understanding_used": 0,
                "estimated_cost_usd": .1, "reserved_cost_usd": 0,
                "actual_cost_ticks": 1_000_000_000,
                "actual_cost_usd": .1, "cost_verified": 1,
                "budget_mode": "normal", "dynamic_target_usd": .1,
                "status": "success", "failure_reason": "",
                "created_at": self.now.isoformat(),
            }
            xai_discovery.save_discovery_run(run, [], self.db)
        with patch.dict(os.environ, self.env, clear=False):
            result = xai_discovery.phase_d_optimize(
                path=self.db, now=self.now, apply=True)
            self.assertTrue(result["eligible"])
            self.assertTrue(result["applied"])
            self.assertLessEqual(result["recommended_daily_runs"], 6)
            self.assertLessEqual(
                result["recommended_standard_tool_calls"], 2)

    def test_29_phase_d_tuning_is_auditable(self):
        self.test_28_phase_d_applies_bounded_tuning_after_30()
        self.assertTrue(xai_discovery.phase_d_tuning(self.db))

    def test_30_usage_record_marks_missing_ticks_unverified(self):
        with patch.object(xai_radar, "_state_dir", return_value=self.root):
            xai_radar._record_usage(
                "grok", 0, 10, 5, 1, True, path=self.db,
                request_id="missing-ticks", estimated_cost_usd=.1,
                started_at=self.now.isoformat(),
                completed_at=self.now.isoformat())
        with metrics_db.connect(self.db) as conn:
            row = conn.execute(
                "SELECT * FROM xai_usage_events WHERE request_id='missing-ticks'"
            ).fetchone()
        self.assertEqual(row["cost_source"], "unverified_estimate")
        self.assertEqual(row["cost_verified"], 0)

    def test_31_cli_exposes_all_required_commands(self):
        source = (ROOT / "local_bot.py").read_text(encoding="utf-8")
        for command in (
            "xai-discovery-status", "xai-budget-mode", "xai-run-budget",
            "xai-coverage-report", "xai-cost-breakdown",
            "xai-discovery-audit", "xai-discovery-dry-run",
            "xai-roi-report", "xai-live-validation", "xai-optimize",
        ):
            self.assertIn(command, source)

    def test_32_live_validation_requires_confirmation(self):
        source = (ROOT / "local_bot.py").read_text(encoding="utf-8")
        self.assertIn("Refusing live xAI validation without --confirm", source)


if __name__ == "__main__":
    unittest.main()
