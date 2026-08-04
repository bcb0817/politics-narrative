from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import metrics_db
import runtime_config
import social_content_factory as factory


class SocialContentFactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "factory.db"
        metrics_db.init_db(self.db)
        self.assertTrue(factory.apply_migrations(self.db))
        now = datetime.now(factory.JST).isoformat()
        with closing(metrics_db.connect(self.db)) as conn:
            for index in range(3):
                conn.execute(
                    """INSERT INTO news_candidates
                       (source_type,source_name,source_url,title,summary,published_at,
                        fetched_at,topic_key,genre,source_reliability_score,
                        freshness_score,final_news_score,verified,verification_reason,
                        is_major_update,metadata_json,discovered_via_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "government_official", f"公式{index}",
                        f"https://example.go.jp/source-{index}",
                        "社会保険制度の見直し", f"確認済み要点{index}", now, now,
                        "social-insurance-reform", "社会保障", 9, 9, 8,
                        1, "official", 1, "{}", '["official"]',
                    ),
                )
            conn.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def test_01_migrations_are_idempotent(self):
        self.assertTrue(factory.apply_migrations(self.db))
        self.assertTrue(factory.apply_migrations(self.db))

    def test_02_all_required_tables_exist(self):
        required = {
            "content_topics", "content_claims", "content_angles", "content_packets",
            "content_inventory", "content_hypotheses", "content_variants",
            "content_experiments", "content_performance_windows",
            "content_demand_signals", "content_visual_candidates",
            "content_thread_candidates", "content_short_candidates",
            "content_longform_candidates", "content_article_candidates",
            "content_reuse_links", "platform_actions", "reply_candidates",
            "quote_candidates", "source_health", "config_audit_results",
            "growth_daily_reports", "growth_weekly_reports",
        }
        with closing(metrics_db.connect(self.db)) as conn:
            actual = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertTrue(required <= actual)

    def test_03_packet_has_required_shape(self):
        packet = factory.generate_packet(path=self.db)
        required = {
            "content_id", "topic_key", "source_news_ids", "primary_sources",
            "verified_facts", "main_event", "stakeholders", "financial_impact",
            "legal_or_policy_impact", "supporting_arguments",
            "opposing_arguments", "common_misunderstandings", "reader_questions",
            "content_angles", "visual_angles", "short_video_angles",
            "longform_potential", "note_potential", "x_article_potential",
        }
        self.assertTrue(required <= set(packet))

    def test_04_one_news_generates_multiple_angles(self):
        packet = factory.generate_packet(path=self.db)
        self.assertGreaterEqual(len(packet["content_angles"]), 16)

    def test_05_topic_claim_angle_are_separate(self):
        packet = factory.generate_packet(path=self.db)
        angle = packet["content_angles"][0]
        self.assertNotEqual(packet["topic_key"], angle["claim_key"])
        self.assertNotEqual(angle["claim_key"], angle["content_angle"])

    def test_06_required_argument_angles_exist(self):
        packet = factory.generate_packet(path=self.db)
        actual = {item["content_angle"] for item in packet["content_angles"]}
        required = {
            "breaking", "change", "beneficiary", "burden", "fiscal_source",
            "institutional_problem", "historical_comparison",
            "international_comparison", "support", "opposition", "editorial",
            "misunderstanding", "short_video", "longform", "x_article", "note",
        }
        self.assertEqual(actual, required)

    def test_07_packet_persistence_is_idempotent(self):
        first = factory.generate_packet(path=self.db)
        second = factory.generate_packet(path=self.db)
        self.assertEqual(first["content_id"], second["content_id"])
        with closing(metrics_db.connect(self.db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM content_packets WHERE content_id=?",
                (first["content_id"],),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_08_inventory_build_writes_only_local_state(self):
        result = factory.build_inventory(dry_run=True, path=self.db)
        self.assertEqual(result["external_writes"], 0)
        self.assertGreater(result["inventory_added"], 0)

    def test_09_evergreen_inventory_has_at_least_30(self):
        result = factory.seed_evergreen_inventory(self.db)
        self.assertGreaterEqual(result["evergreen_available"], 30)

    def test_10_inventory_has_expiration(self):
        factory.build_inventory(dry_run=True, path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            values = conn.execute(
                "SELECT expires_at FROM content_inventory WHERE expires_at IS NOT NULL"
            ).fetchall()
        self.assertTrue(values)
        self.assertTrue(all(datetime.fromisoformat(row[0]) for row in values))

    def test_11_inventory_tracks_claim_and_angle(self):
        factory.build_inventory(dry_run=True, path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            row = conn.execute(
                "SELECT claim_key,content_angle FROM content_inventory LIMIT 1"
            ).fetchone()
        self.assertTrue(row[0])
        self.assertTrue(row[1])

    def test_12_variants_supply_x_target(self):
        factory.build_inventory(dry_run=True, path=self.db)
        result = factory.generate_variants(dry_run=True, path=self.db)
        self.assertGreaterEqual(result["x_candidates"], 10)
        self.assertLessEqual(result["x_candidates"], 14)

    def test_13_variants_supply_threads_target(self):
        factory.build_inventory(dry_run=True, path=self.db)
        result = factory.generate_variants(dry_run=True, path=self.db)
        self.assertGreaterEqual(result["threads_candidates"], 4)
        self.assertLessEqual(result["threads_candidates"], 7)

    def test_14_variants_are_not_same_claim_paraphrases(self):
        factory.build_inventory(dry_run=True, path=self.db)
        factory.generate_variants(dry_run=True, path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            rows = conn.execute(
                """SELECT topic_key,claim_key,content_angle,hook
                   FROM content_variants"""
            ).fetchall()
        signatures = {(row[0], row[1], row[2], row[3]) for row in rows}
        self.assertEqual(len(rows), len(signatures))

    def test_15_conversation_has_own_threshold(self):
        self.assertEqual(factory.threshold_for("conversation"), 6.5)
        self.assertLess(factory.threshold_for("conversation"), factory.threshold_for("breaking"))

    def test_16_theme_value_scoring_weights(self):
        score = factory.theme_value_score({key: 10 for key in (
            "demand", "public_value", "novelty", "video_potential",
            "discussion", "primary_sources", "persona_fit",
        )})
        self.assertEqual(score, 10)

    def test_17_post_quality_scoring_weights(self):
        score = factory.post_quality_score({key: 10 for key in (
            "accuracy", "hook", "clarity", "originality", "conversation", "safety",
        )})
        self.assertEqual(score, 10)

    def test_18_all_type_thresholds_exist(self):
        for post_type in factory.THRESHOLDS:
            self.assertGreaterEqual(factory.threshold_for(post_type), 6.5)

    def test_19_visual_candidates_have_all_aspects(self):
        factory.build_inventory(dry_run=True, path=self.db)
        result = factory.visual_candidates(dry_run=True, path=self.db)
        self.assertGreaterEqual(len(result["candidates"]), 1)
        outputs = result["candidates"][0]["outputs"]
        self.assertEqual(set(outputs), {"x", "threads", "video"})

    def test_20_visual_numbers_require_primary_source(self):
        factory.build_inventory(dry_run=True, path=self.db)
        result = factory.visual_candidates(dry_run=True, path=self.db)
        self.assertTrue(result["candidates"][0]["numbers_require_primary_source"])

    def test_21_thread_candidate_is_three_to_five_posts(self):
        factory.build_inventory(dry_run=True, path=self.db)
        factory.thread_candidates(dry_run=True, path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            row = conn.execute(
                "SELECT posts_json FROM content_thread_candidates LIMIT 1"
            ).fetchone()
        self.assertIn(len(json.loads(row[0])), range(3, 6))

    def test_22_thread_auto_publish_is_false(self):
        factory.build_inventory(dry_run=True, path=self.db)
        result = factory.thread_candidates(dry_run=True, path=self.db)
        self.assertFalse(result["auto_publish"])

    def test_23_reply_candidates_are_low_risk_verified(self):
        factory.build_inventory(dry_run=True, path=self.db)
        factory.reply_candidate_list(dry_run=True, path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            rows = conn.execute(
                "SELECT risk_level,fact_status,status FROM reply_candidates"
            ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(tuple(row) == ("low", "verified", "approval_required") for row in rows))

    def test_24_auto_reply_is_initially_off(self):
        result = factory.reply_candidate_list(dry_run=True, path=self.db)
        self.assertFalse(result["auto_reply_enabled"])

    def test_25_quote_candidates_only_use_primary_sources(self):
        factory.build_inventory(dry_run=True, path=self.db)
        factory.quote_candidate_list(dry_run=True, path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            rows = conn.execute("SELECT source_type FROM quote_candidates").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row[0] == "official_primary_source" for row in rows))

    def test_26_auto_quote_is_initially_off(self):
        result = factory.quote_candidate_list(dry_run=True, path=self.db)
        self.assertFalse(result["auto_quote_enabled"])

    def test_27_metric_windows_include_five_required_windows(self):
        self.assertEqual(factory.WINDOWS, ("15m", "1h", "6h", "24h", "72h"))

    def test_28_short_promotion_requires_three_criteria(self):
        factory.build_inventory(dry_run=True, path=self.db)
        result = factory.promote_short_candidates(dry_run=True, path=self.db)
        self.assertEqual(result["minimum_criteria"], 3)
        for candidate in result["candidates"]:
            self.assertGreaterEqual(sum(candidate["criteria"].values()), 3)

    def test_29_short_candidate_shape(self):
        factory.build_inventory(dry_run=True, path=self.db)
        result = factory.promote_short_candidates(dry_run=True, path=self.db)
        candidate = result["candidates"][0]
        required = {
            "content_id", "topic_key", "winning_hook", "winning_platform",
            "audience_question", "core_claim", "counterpoint",
            "visual_metaphor", "source_posts", "short_script_outline",
            "longform_potential", "confidence",
        }
        self.assertTrue(required <= set(candidate))

    def test_30_article_promotion_requires_two_sources(self):
        factory.seed_evergreen_inventory(self.db)
        result = factory.promote_article_candidates(dry_run=True, path=self.db)
        self.assertTrue(result["candidates"])
        self.assertTrue(all(row["source_count"] >= 2 for row in result["candidates"]))

    def test_31_source_registry_is_local_in_dry_run(self):
        result = factory.source_health(self.db)
        self.assertEqual(result["network_calls"], 0)
        self.assertGreaterEqual(len(result["sources"]), 4)

    def test_32_budget_simulation_does_not_change_budget(self):
        result = factory.budget_simulation(14, 6, 2, 5, 30)
        self.assertFalse(result["production_budget_changed"])
        self.assertGreater(result["total_forecast_usd"], 0)

    def test_33_budget_simulation_has_unit_costs(self):
        result = factory.budget_simulation()
        self.assertIn("candidate_generation_unit_cost_usd", result)
        self.assertIn("short_candidate_unit_cost_usd", result)
        self.assertIn("budget_overrun_day", result)

    def test_34_full_cycle_has_no_external_writes(self):
        result = factory.full_cycle(dry_run=True, path=self.db)
        self.assertEqual(result["safety"]["external_posts"], 0)
        self.assertEqual(result["safety"]["windows_tasks_registered"], 0)
        self.assertFalse(result["safety"]["env_modified"])

    def test_35_full_cycle_keeps_x_and_threads_publish_zero(self):
        result = factory.full_cycle(dry_run=True, path=self.db)
        self.assertEqual(result["safety"]["x_posts"], 0)
        self.assertEqual(result["safety"]["threads_posts"], 0)

    def test_36_growth_report_uses_hypothesis_kpis(self):
        factory.full_cycle(dry_run=True, path=self.db)
        report = factory.daily_report(dry_run=True, path=self.db)
        self.assertIn("effective_content_hypotheses", report["kpi_priority"])
        self.assertIn("short_conversion_rate", report["kpi_priority"])

    def test_37_config_audit_never_writes_env(self):
        env_file = Path(self.tmp.name) / ".env"
        env_file.write_text("POST_ENABLED=true\n", encoding="utf-8")
        before = hashlib.sha256(env_file.read_bytes()).hexdigest()
        result = runtime_config.audit(env_file, persist=False)
        after = hashlib.sha256(env_file.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertFalse(result["env_modified"])

    def test_38_config_audit_detects_persona_conflict(self):
        fake_root = Path(self.tmp.name) / "repo"
        (fake_root / "config").mkdir(parents=True)
        (fake_root / "config" / "bot_persona.md").write_text(
            "絵文字を必ず2〜5個使う", encoding="utf-8")
        (fake_root / "README.md").write_text("", encoding="utf-8")
        env_file = fake_root / ".env"
        env_file.write_text("", encoding="utf-8")
        with patch.object(runtime_config, "ROOT", fake_root):
            result = runtime_config.audit(env_file, persist=False)
        issues = {row["issue"] for row in result["document_findings"]}
        self.assertIn("persona_emoji_policy_conflicts_with_runtime", issues)

    def test_39_config_audit_detects_x_only_readme(self):
        fake_root = Path(self.tmp.name) / "repo"
        (fake_root / "config").mkdir(parents=True)
        (fake_root / "config" / "bot_persona.md").write_text("", encoding="utf-8")
        (fake_root / "README.md").write_text(
            "対象プラットフォームは **X のみ**", encoding="utf-8")
        env_file = fake_root / ".env"
        env_file.write_text("", encoding="utf-8")
        with patch.object(runtime_config, "ROOT", fake_root):
            result = runtime_config.audit(env_file, persist=False)
        issues = {row["issue"] for row in result["document_findings"]}
        self.assertIn("readme_x_only_conflicts_with_threads", issues)

    def test_40_live_defaults_target_twelve_without_expanding_run_limit(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = runtime_config.typed_config(Path(self.tmp.name) / "missing.env")
        self.assertEqual(cfg["MAX_POSTS_PER_RUN"], 1)
        self.assertEqual(cfg["ORIGINAL_DAILY_POST_MIN"], 20)
        self.assertEqual(cfg["ORIGINAL_DAILY_POST_MAX"], 20)
        self.assertEqual(cfg["MAX_DAILY_AUTOMATED_POSTS"], 20)

    def test_41_new_x_capability_limits_are_typed(self):
        cfg = runtime_config.typed_config(Path(self.tmp.name) / "missing.env")
        self.assertEqual(cfg["X_DAILY_ORIGINAL_TARGET_MAX"], 14)
        self.assertEqual(cfg["X_DAILY_ORIGINAL_HARD_MAX"], 16)

    def test_42_new_threads_capability_limits_are_typed(self):
        cfg = runtime_config.typed_config(Path(self.tmp.name) / "missing.env")
        self.assertEqual(cfg["THREADS_DAILY_POST_TARGET_MAX"], 6)
        self.assertEqual(cfg["THREADS_DAILY_POST_HARD_MAX"], 7)

    def test_43_same_claim_is_unique_per_topic(self):
        packet = factory.generate_packet(path=self.db)
        factory._save_packet(packet, self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            with closing(metrics_db.connect(self.db)) as conn:
                conn.execute(
                    """INSERT INTO content_claims
                       (content_id,topic_key,claim_key,created_at)
                       VALUES(?,?,?,?)""",
                    (packet["content_id"], packet["topic_key"],
                     packet["content_angles"][0]["claim_key"], datetime.now().isoformat()),
                )

    def test_44_social_tasks_require_explicit_apply(self):
        script = (ROOT / "production" / "register_social_growth_tasks.ps1").read_text(
            encoding="utf-8")
        self.assertIn("[switch]$Apply", script)
        self.assertIn("No task was registered", script)

    def test_45_social_factory_imports_no_publishing_client(self):
        source = (SRC / "social_content_factory.py").read_text(encoding="utf-8")
        for forbidden in ("tweepy", "ThreadsClient", "post_to_x", "media_publish"):
            self.assertNotIn(forbidden, source)

    def test_46_publication_batch_is_limited_to_two(self):
        candidates = [
            {"topic_key": f"topic-{index}", "claim_key": f"claim-{index}"}
            for index in range(4)
        ]
        planned = factory.plan_publication_batch(candidates, max_candidates=4)
        self.assertEqual(len(planned), 2)
        self.assertTrue(all(not item["publish_authorized"] for item in planned))

    def test_47_publication_batch_uses_time_difference(self):
        now = datetime(2026, 7, 26, 10, 0, tzinfo=factory.JST)
        planned = factory.plan_publication_batch(
            [
                {"topic_key": "a", "claim_key": "a"},
                {"topic_key": "b", "claim_key": "b"},
            ],
            now=now,
            delay_minutes=30,
        )
        first = datetime.fromisoformat(planned[0]["planned_at"])
        second = datetime.fromisoformat(planned[1]["planned_at"])
        self.assertEqual((second - first).total_seconds(), 1800)

    def test_48_publication_batch_filters_same_claim(self):
        planned = factory.plan_publication_batch(
            [
                {"topic_key": "a", "claim_key": "same"},
                {"topic_key": "a", "claim_key": "same"},
                {"topic_key": "b", "claim_key": "other"},
            ]
        )
        self.assertEqual(len(planned), 2)
        self.assertEqual(planned[1]["topic_key"], "b")

    def test_49_relative_performance_uses_peer_median(self):
        self.assertEqual(factory.relative_performance(20, [5, 10, 15]), 2.0)
        self.assertEqual(factory.relative_performance(20, []), 1.0)

    def test_50_demand_signals_never_create_verified_facts(self):
        result = factory.collect_demand_signals(dry_run=True, path=self.db)
        self.assertEqual(result["verified_facts_created"], 0)
        with closing(metrics_db.connect(self.db)) as conn:
            verified = conn.execute(
                "SELECT COUNT(*) FROM content_demand_signals WHERE verified_fact=1"
            ).fetchone()[0]
        self.assertEqual(verified, 0)

    def test_51_inventory_selection_has_two_candidate_limit(self):
        factory.build_inventory(dry_run=True, path=self.db)
        selected = factory.select_inventory("x", limit=20, path=self.db)
        self.assertLessEqual(len(selected), 2)
        self.assertTrue(all(not item["publish_authorized"] for item in selected))

    def test_52_inventory_selection_rejects_unverified(self):
        factory.build_inventory(dry_run=True, path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            conn.execute("UPDATE content_inventory SET fact_status='unverified'")
            conn.commit()
        self.assertEqual(factory.select_inventory("x", path=self.db), [])

    def test_53_inventory_selection_avoids_same_claim(self):
        factory.build_inventory(dry_run=True, path=self.db)
        selected = factory.select_inventory("x", path=self.db)
        claims = {(row["topic_key"], row["claim_key"]) for row in selected}
        self.assertEqual(len(selected), len(claims))

    def test_54_x_variants_include_video_candidate(self):
        factory.build_inventory(dry_run=True, path=self.db)
        factory.generate_variants(dry_run=True, path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            count = conn.execute(
                """SELECT COUNT(*) FROM content_variants
                   WHERE platform='x' AND format='video'"""
            ).fetchone()[0]
        self.assertGreaterEqual(count, 1)

    def test_55_short_criteria_recognize_cross_platform_signal(self):
        packet = factory.generate_packet(path=self.db)
        with closing(metrics_db.connect(self.db)) as conn:
            for platform in ("x", "threads"):
                conn.execute(
                    """INSERT INTO content_demand_signals
                       (content_id,topic_key,claim_key,platform,signal_type,
                        signal_value,sample_size,verified_fact,payload_json,observed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        packet["content_id"], packet["topic_key"], "", platform,
                        "question", 1, 3, 0, "{}", datetime.now().isoformat(),
                    ),
                )
            conn.commit()
        criteria = factory._short_criteria(packet, self.db)
        self.assertTrue(criteria["cross_platform_response"])


if __name__ == "__main__":
    unittest.main()
