from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import disaster_updates as disaster


class DisasterUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "metrics.db"
        self.output = self.root / "outputs"
        self.env = patch.dict(os.environ, {
            "KUMAMOTO_DISASTER_OUTPUT_DIR": str(self.output),
            "KUMAMOTO_DISASTER_AUTO_POST_ENABLED": "false",
            "KUMAMOTO_DISASTER_CORRECTION_AUTO_POST": "false",
            "KUMAMOTO_DISASTER_SHORT_AUTO_PUBLISH": "false",
            "KUMAMOTO_DISASTER_X_IMAGE_SIZE": "1600x900",
            "KUMAMOTO_DISASTER_THREADS_IMAGE_SIZE": "1080x1350",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def morning(self):
        return disaster.snapshot_from_fixture("morning")

    def evening(self):
        return disaster.snapshot_from_fixture("evening")

    def pair(self):
        morning = self.morning()
        evening = self.evening()
        return morning, evening, disaster.calculate_delta(morning, evening)

    def test_01_morning_snapshot_generation(self):
        self.assertEqual(self.morning()["snapshot_type"], "morning")

    def test_02_evening_snapshot_generation(self):
        self.assertEqual(self.evening()["snapshot_type"], "evening")

    def test_03_cutoff_fixed(self):
        self.assertEqual(self.morning()["cutoff_at"], "2026-07-29T07:00:00+09:00")

    def test_04_as_of_saved(self):
        self.assertTrue(self.evening()["rescue"]["as_of"])

    def test_05_null_not_coerced_to_zero(self):
        self.assertIsNone(self.morning()["casualties"]["dead"])

    def test_06_sources_saved(self):
        self.assertGreaterEqual(len(self.morning()["sources"]), 3)

    def test_07_latest_integrated_official_wins(self):
        rows = [
            {"value": 10, "priority": 2, "as_of": "2026-07-29T07:10:00+09:00",
             "source_class": "official", "verification_status": "confirmed"},
            {"value": 9, "priority": 1, "as_of": "2026-07-29T07:00:00+09:00",
             "source_class": "official", "verification_status": "confirmed"},
        ]
        self.assertEqual(disaster.select_authoritative_value(rows)["value"], 9)

    def test_08_agency_rescues_not_summed(self):
        rows = [
            {"value": 8, "priority": 2, "as_of": "a", "source_class": "official",
             "verification_status": "confirmed"},
            {"value": 7, "priority": 2, "as_of": "b", "source_class": "official",
             "verification_status": "confirmed"},
        ]
        self.assertIn(disaster.select_authoritative_value(rows)["value"], {7, 8})
        self.assertNotEqual(disaster.select_authoritative_value(rows)["value"], 15)

    def test_09_damage_not_double_counted(self):
        rows = disaster._metric_rows(self.morning())
        self.assertEqual(len({row["key"] for row in rows}), len(rows))

    def test_10_old_value_not_current(self):
        rows = [
            {"value": 10, "priority": 1, "as_of": "2026-07-29T06:00:00+09:00",
             "source_class": "official", "verification_status": "confirmed"},
            {"value": 12, "priority": 1, "as_of": "2026-07-29T07:00:00+09:00",
             "source_class": "official", "verification_status": "confirmed"},
        ]
        self.assertEqual(disaster.select_authoritative_value(rows)["value"], 12)

    def test_11_scope_change_detected(self):
        _, _, delta = self.pair()
        row = next(x for x in delta["changes"] if x["metric_key"] == "transport.rail")
        self.assertEqual(row["delta_status"], "scope_changed")

    def test_12_correction_detected(self):
        old, new = self.morning(), self.evening()
        new["metadata"]["corrections"]["casualties.injured"] = "重複を削除"
        row = next(x for x in disaster.calculate_delta(old, new)["changes"]
                   if x["metric_key"] == "casualties.injured")
        self.assertEqual(row["delta_status"], "corrected")

    def test_13_increased(self):
        _, _, delta = self.pair()
        row = next(x for x in delta["changes"] if x["metric_key"] == "casualties.injured")
        self.assertEqual(row["delta_status"], "increased")

    def test_14_decreased(self):
        _, _, delta = self.pair()
        row = next(x for x in delta["changes"] if x["metric_key"] == "evacuation.evacuees")
        self.assertEqual(row["delta_status"], "decreased")

    def test_15_recovered(self):
        _, _, delta = self.pair()
        row = next(x for x in delta["changes"]
                   if x["metric_key"] == "infrastructure.power_outage_households")
        self.assertEqual(row["delta_status"], "recovered")

    def test_16_resolved(self):
        old, new = self.morning(), self.evening()
        new["metadata"]["resolved_metrics"].append("rescue.isolated_areas")
        row = next(x for x in disaster.calculate_delta(old, new)["changes"]
                   if x["metric_key"] == "rescue.isolated_areas")
        self.assertEqual(row["delta_status"], "resolved")

    def test_17_corrected(self):
        old, new = self.morning(), self.evening()
        new["metadata"]["corrections"]["evacuation.evacuees"] = "公式訂正"
        row = next(x for x in disaster.calculate_delta(old, new)["changes"]
                   if x["metric_key"] == "evacuation.evacuees")
        self.assertEqual(row["delta_status"], "corrected")

    def test_18_scope_changed(self):
        old, new = self.morning(), self.evening()
        new["metadata"]["scope_changes"]["evacuation.open_shelters"] = "対象市町村変更"
        row = next(x for x in disaster.calculate_delta(old, new)["changes"]
                   if x["metric_key"] == "evacuation.open_shelters")
        self.assertEqual(row["delta_status"], "scope_changed")

    def test_19_definition_changed(self):
        old, new = self.morning(), self.evening()
        new["metadata"]["definition_changes"]["rescue.rescue_requests"] = "定義変更"
        row = next(x for x in disaster.calculate_delta(old, new)["changes"]
                   if x["metric_key"] == "rescue.rescue_requests")
        self.assertEqual(row["delta_status"], "definition_changed")

    def test_20_unavailable(self):
        old, new = self.morning(), self.evening()
        new["casualties"]["injured"] = None
        row = next(x for x in disaster.calculate_delta(old, new)["changes"]
                   if x["metric_key"] == "casualties.injured")
        self.assertEqual(row["delta_status"], "unavailable")

    def test_21_no_rescue_rate_estimate(self):
        self.assertNotIn("rescue_rate", json.dumps(self.evening()))

    def test_22_no_percentage_when_denominator_unknown(self):
        self.assertNotIn("%", json.dumps(self.evening(), ensure_ascii=False))

    def test_23_searching_and_rescued_distinct(self):
        rescue = self.evening()["rescue"]
        self.assertIsInstance(rescue["searching_areas"], list)
        self.assertIsInstance(rescue["rescued_people"], int)

    def test_24_isolated_and_resolved_distinct(self):
        rescue = self.evening()["rescue"]
        self.assertNotEqual(rescue["isolated_areas"], rescue["isolated_areas_resolved"] + 1)

    def test_25_x_under_280(self):
        old, new, delta = self.pair()
        self.assertLessEqual(len(disaster.make_candidates(new, delta)["x"]), 280)

    def test_26_threads_not_x_copy(self):
        _, new, delta = self.pair()
        result = disaster.make_candidates(new, delta)
        self.assertNotEqual(result["x"], result["threads"])

    def test_27_no_emoji(self):
        _, new, delta = self.pair()
        result = disaster.make_candidates(new, delta)
        self.assertNotRegex(result["x"] + result["threads"], "[\U0001F300-\U0001FAFF]")

    def test_28_no_light_question(self):
        _, new, delta = self.pair()
        self.assertNotIn("どう思いますか", disaster.make_candidates(new, delta)["threads"])

    def test_29_cutoff_in_post(self):
        _, new, delta = self.pair()
        self.assertIn("19時時点", disaster.make_candidates(new, delta)["x"])

    def test_30_source_difference_explained(self):
        _, new, delta = self.pair()
        self.assertIn("合算", disaster.make_candidates(new, delta)["threads"])

    def test_31_no_change_skips(self):
        snap = self.morning()
        delta = disaster.calculate_delta(snap, deepcopy(snap))
        self.assertFalse(disaster.make_candidates(snap, delta)["publish_eligible"])

    def test_32_skip_is_normal_result(self):
        snap = self.morning()
        delta = disaster.calculate_delta(snap, deepcopy(snap))
        self.assertEqual(disaster.make_candidates(snap, delta)["decision_reason"],
                         "no_meaningful_official_change")

    def test_33_x_image_size(self):
        old, new, delta = self.pair()
        path = disaster.render_visual(new, delta, platform="x", directory=self.root)
        from PIL import Image
        with Image.open(path) as rendered:
            self.assertEqual(rendered.size, (1600, 900))

    def test_34_threads_image_size(self):
        old, new, delta = self.pair()
        path = disaster.render_visual(new, delta, platform="threads", directory=self.root)
        from PIL import Image
        with Image.open(path) as rendered:
            self.assertEqual(rendered.size, (1080, 1350))

    def test_35_safe_area_used(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertIn("margin = int(70 * scale)", text)

    def test_36_cutoff_prominent(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertIn("cutoff_font", text)

    def test_37_delta_displayed(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertIn("前回からの変化", text)

    def test_38_unknown_displayed(self):
        self.assertEqual(disaster._display(None, "人"), "公式集計なし")

    def test_39_casualties_not_sensational(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertNotIn("死者多数", text)

    def test_40_sources_displayed(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertIn("主要情報源", text)

    def test_41_twice_daily(self):
        result = disaster.frequency_recommendation(path=self.db)
        self.assertEqual(result["current_mode"], "active_twice_daily")

    def test_42_daily_mode_exists(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertIn('"active_daily"', text)

    def test_43_recovery_periodic_exists(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertIn('"recovery_periodic"', text)

    def test_44_closed_exists(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertIn('"closed"', text)

    def test_45_frequency_not_auto_changed(self):
        result = disaster.frequency_recommendation(path=self.db)
        self.assertFalse(result["automatic_schedule_change"])

    def test_46_auto_post_initially_off(self):
        self.assertFalse(disaster.settings()["auto_post"])

    def test_47_correction_auto_post_off(self):
        self.assertFalse(disaster.settings()["correction_auto_post"])

    def test_48_env_updater_does_not_overwrite(self):
        text = (ROOT / "production" / "add_kumamoto_disaster_env_defaults.ps1"
                ).read_text(encoding="utf-8")
        self.assertIn("MissingKeysAdded", text)
        self.assertNotIn("Set-Content", text)

    def test_49_normal_x_module_not_imported(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import post", text)

    def test_50_threads_publish_not_called(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertNotIn("threads_api.publish", text)

    def test_51_major_incident_pipeline_not_imported(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertNotIn("social_anger", text)

    def test_52_task_registration_not_called_by_python(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Register-ScheduledTask", text)

    def test_53_git_not_called(self):
        text = Path(disaster.__file__).read_text(encoding="utf-8")
        self.assertNotIn("git commit", text)
        self.assertNotIn("git push", text)

    def test_54_migration_is_idempotent(self):
        self.assertTrue(disaster.apply_migrations(self.db))
        self.assertTrue(disaster.apply_migrations(self.db))
        with closing(sqlite3.connect(self.db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM disaster_incidents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_55_full_cycle_morning(self):
        result = disaster.full_cycle(
            disaster.INCIDENT_ID, "morning", dry_run=True, path=self.db)
        self.assertEqual(result["external_publication_calls"], 0)
        self.assertTrue(Path(result["visuals"]["x"]).exists())

    def test_56_full_cycle_evening(self):
        disaster.full_cycle(
            disaster.INCIDENT_ID, "morning", dry_run=True, path=self.db)
        result = disaster.full_cycle(
            disaster.INCIDENT_ID, "evening", dry_run=True, path=self.db)
        self.assertEqual(result["quality_report"]["failed"], [])
        self.assertFalse(result["x_published"])

    def test_57_all_output_files(self):
        disaster.full_cycle(
            disaster.INCIDENT_ID, "morning", dry_run=True, path=self.db)
        result = disaster.full_cycle(
            disaster.INCIDENT_ID, "evening", dry_run=True, path=self.db)
        required = {
            "snapshot.json", "previous_snapshot.json", "delta.json",
            "source_matrix.csv", "sources.md", "quality_report.json",
            "x_post.md", "threads_post.md", "x_visual.png",
            "threads_visual.png", "visual_data.json",
            "correction_candidate.md", "short_candidate.md",
            "generation_log.md",
        }
        self.assertEqual(
            required, {p.name for p in Path(result["directory"]).iterdir()})

    def test_58_sqlite_tables(self):
        disaster.apply_migrations(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {
            "disaster_status_snapshots", "disaster_snapshot_metrics",
            "disaster_snapshot_deltas", "disaster_rescue_progress",
            "disaster_update_publications",
            "disaster_update_frequency_status",
        }
        self.assertTrue(expected <= tables)

    def test_59_post_types_isolated(self):
        self.assertIn("disaster_morning_brief", disaster.POST_TYPES)
        self.assertIn("disaster_evening_brief", disaster.POST_TYPES)

    def test_60_no_external_publication_api(self):
        result = disaster.full_cycle(
            disaster.INCIDENT_ID, "morning", dry_run=True, path=self.db)
        self.assertFalse(any(result[key] for key in (
            "x_published", "threads_published", "youtube_published",
            "instagram_published", "note_published")))

    def test_61_stale_official_source_blocks_candidate(self):
        snapshot = self.morning()
        for source in snapshot["sources"]:
            source["as_of"] = "2026-07-28T07:00:00+09:00"
            source["published_at"] = "2026-07-28T07:00:00+09:00"
        delta = disaster.calculate_delta(None, snapshot)
        result = disaster.make_candidates(snapshot, delta)
        self.assertFalse(result["publish_eligible"])
        self.assertEqual(
            result["decision_reason"],
            "official_source_too_old_or_time_unknown",
        )

    def test_62_fresh_official_source_allows_quality_decision(self):
        snapshot = self.morning()
        self.assertGreaterEqual(len(disaster.fresh_official_sources(snapshot)), 1)

    def test_63_source_health_never_extracts_damage_numbers(self):
        response = Mock()
        response.content = b"official page"
        response.headers = {"Last-Modified": "Wed, 29 Jul 2026 00:00:00 GMT"}
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response
        with patch.object(disaster, "official_source_registry", return_value=[{
            "source_id": "official_test",
            "name": "Official Test",
            "url": "https://example.invalid/official",
            "source_class": "official",
            "priority": 1,
        }]):
            rows = disaster.collect_official_source_health(
                cutoff_at=disaster._cutoff(
                    "morning", date_text="2026-07-29"),
                session=session,
            )
        self.assertEqual(rows[0]["status"], "reachable")
        self.assertNotIn("dead", rows[0])
        self.assertNotIn("injured", rows[0])
        self.assertTrue(rows[0]["content_hash"])

    def test_64_dry_run_suppresses_all_discord_notifications(self):
        result = disaster.full_cycle(
            disaster.INCIDENT_ID, "evening", dry_run=True, path=self.db)
        self.assertFalse(result["discord_sent"])
        self.assertFalse(result["correction_discord_sent"])
        self.assertFalse(result["frequency_discord_sent"])

    def _candidate(self, snapshot_type="morning"):
        return disaster.full_cycle(
            disaster.INCIDENT_ID, snapshot_type, dry_run=True, path=self.db)

    def test_65_phase_a_blocks_manual_publish(self):
        result = self._candidate()
        with patch.dict(os.environ, {
            "KUMAMOTO_DISASTER_PHASE": "A",
        }):
            published = disaster.publish_candidate(
                result["snapshot_id"], "x", confirm=True, path=self.db)
        self.assertEqual(published["reason"], "phase_a_publication_disabled")

    def test_66_publish_requires_explicit_confirmation(self):
        result = self._candidate()
        with patch.dict(os.environ, {
            "KUMAMOTO_DISASTER_PHASE": "B",
            "KUMAMOTO_DISASTER_PUBLISH_ENABLED": "true",
        }):
            published = disaster.publish_candidate(
                result["snapshot_id"], "x", path=self.db)
        self.assertEqual(published["reason"], "explicit_confirmation_required")

    def test_67_approval_records_without_external_post(self):
        result = self._candidate()
        approval = disaster.approve_candidate(
            result["snapshot_id"], "x", path=self.db)
        self.assertEqual(approval["status"], "approved")
        self.assertEqual(approval["external_posts"], 0)

    def test_68_phase_b_x_publish_after_approval(self):
        result = self._candidate()
        disaster.approve_candidate(result["snapshot_id"], "x", path=self.db)
        response = Mock()
        response.data = {"id": "x-test-id"}
        client = Mock()
        client.create_tweet.return_value = response
        with patch.dict(os.environ, {
            "KUMAMOTO_DISASTER_PHASE": "B",
            "KUMAMOTO_DISASTER_PUBLISH_ENABLED": "true",
            "KUMAMOTO_DISASTER_X_POST_ENABLED": "true",
            "POST_ENABLED": "true",
            "X_POST_ENABLED": "true",
        }):
            published = disaster.publish_candidate(
                result["snapshot_id"], "x", confirm=True,
                x_client=client, path=self.db)
        self.assertTrue(published["published"])
        client.create_tweet.assert_called_once()

    def test_69_phase_b_threads_publish_after_approval(self):
        result = self._candidate()
        disaster.approve_candidate(
            result["snapshot_id"], "threads", path=self.db)
        client = Mock()
        client.create_container.return_value = {"id": "container-id"}
        client.publish_container.return_value = {"id": "threads-test-id"}
        with patch.dict(os.environ, {
            "KUMAMOTO_DISASTER_PHASE": "B",
            "KUMAMOTO_DISASTER_PUBLISH_ENABLED": "true",
            "KUMAMOTO_DISASTER_THREADS_POST_ENABLED": "true",
            "THREADS_POST_ENABLED": "true",
        }):
            published = disaster.publish_candidate(
                result["snapshot_id"], "threads", confirm=True,
                threads_client=client, path=self.db)
        self.assertTrue(published["published"])
        client.publish_container.assert_called_once_with("container-id")

    def test_70_rejected_candidate_cannot_publish(self):
        result = self._candidate()
        disaster.approve_candidate(
            result["snapshot_id"], "x", decision="rejected", path=self.db)
        with patch.dict(os.environ, {
            "AUTONOMOUS_POSTING_ENABLED": "false",
            "KUMAMOTO_DISASTER_HUMAN_APPROVAL_REQUIRED": "true",
            "KUMAMOTO_DISASTER_PHASE": "B",
            "KUMAMOTO_DISASTER_PUBLISH_ENABLED": "true",
            "KUMAMOTO_DISASTER_X_POST_ENABLED": "true",
            "POST_ENABLED": "true",
            "X_POST_ENABLED": "true",
        }):
            published = disaster.publish_candidate(
                result["snapshot_id"], "x", confirm=True,
                x_client=Mock(), path=self.db)
        self.assertEqual(published["reason"], "human_approval_required")

    def test_71_publish_is_idempotent(self):
        result = self._candidate()
        disaster.approve_candidate(result["snapshot_id"], "x", path=self.db)
        response = Mock()
        response.data = {"id": "one-id"}
        client = Mock()
        client.create_tweet.return_value = response
        env = {
            "KUMAMOTO_DISASTER_PHASE": "B",
            "KUMAMOTO_DISASTER_PUBLISH_ENABLED": "true",
            "KUMAMOTO_DISASTER_X_POST_ENABLED": "true",
            "POST_ENABLED": "true", "X_POST_ENABLED": "true",
        }
        with patch.dict(os.environ, env):
            first = disaster.publish_candidate(
                result["snapshot_id"], "x", confirm=True,
                x_client=client, path=self.db)
            second = disaster.publish_candidate(
                result["snapshot_id"], "x", confirm=True,
                x_client=client, path=self.db)
        self.assertTrue(first["published"])
        self.assertEqual(second["reason"], "already_published")
        self.assertEqual(client.create_tweet.call_count, 1)

    def test_72_phase_c_auto_publish_defaults_off(self):
        result = self._candidate()
        outcome = disaster.auto_publish_verified(
            result["snapshot_id"], path=self.db)
        self.assertEqual(outcome["external_posts"], 0)

    def test_73_phase_c_verified_auto_publish(self):
        result = self._candidate()
        x_response = Mock()
        x_response.data = {"id": "x-auto"}
        x_client = Mock()
        x_client.create_tweet.return_value = x_response
        threads_client = Mock()
        threads_client.create_container.return_value = {"id": "create-auto"}
        threads_client.publish_container.return_value = {"id": "threads-auto"}
        with patch.dict(os.environ, {
            "KUMAMOTO_DISASTER_PHASE": "C",
            "KUMAMOTO_DISASTER_PUBLISH_ENABLED": "true",
            "KUMAMOTO_DISASTER_AUTO_POST_ENABLED": "true",
            "KUMAMOTO_DISASTER_X_POST_ENABLED": "true",
            "KUMAMOTO_DISASTER_THREADS_POST_ENABLED": "true",
            "POST_ENABLED": "true", "X_POST_ENABLED": "true",
            "THREADS_POST_ENABLED": "true",
        }):
            outcome = disaster.auto_publish_verified(
                result["snapshot_id"], x_client=x_client,
                threads_client=threads_client, path=self.db)
        self.assertEqual(outcome["external_posts"], 2)

    def test_74_frequency_change_requires_confirmation(self):
        result = disaster.apply_frequency_mode(
            disaster.INCIDENT_ID, "active_daily", path=self.db)
        self.assertFalse(result["changed"])
        self.assertEqual(
            disaster._incident_mode(disaster.INCIDENT_ID, self.db),
            "active_twice_daily")

    def test_75_frequency_change_is_human_approved(self):
        result = disaster.apply_frequency_mode(
            disaster.INCIDENT_ID, "active_daily",
            confirm=True, path=self.db)
        self.assertTrue(result["human_approved"])
        self.assertEqual(
            disaster._incident_mode(disaster.INCIDENT_ID, self.db),
            "active_daily")
        self.assertFalse(result["windows_tasks_changed"])

    def test_76_recovery_brief_is_local_only(self):
        self._candidate("evening")
        result = disaster.recovery_brief(path=self.db)
        self.assertEqual(result["external_posts"], 0)
        self.assertTrue(Path(result["path"]).exists())

    def test_77_closure_package_is_local_only(self):
        self._candidate("evening")
        result = disaster.closure_package(path=self.db)
        self.assertEqual(result["external_posts"], 0)
        self.assertTrue(Path(result["summary"]["path"]).exists())
        self.assertTrue(Path(result["preparedness"]["path"]).exists())

    def test_78_lifecycle_copy_has_no_emoji(self):
        self._candidate("evening")
        result = disaster.closure_package(path=self.db)
        text = (
            Path(result["summary"]["path"]).read_text(encoding="utf-8")
            + Path(result["preparedness"]["path"]).read_text(encoding="utf-8")
        )
        self.assertNotRegex(text, "[\U0001F300-\U0001FAFF]")

    def test_79_phase_tables_are_idempotent(self):
        disaster.apply_migrations(self.db)
        disaster.apply_migrations(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("disaster_update_approvals", tables)
        self.assertIn("disaster_lifecycle_reports", tables)

    def test_80_frequency_script_requires_human_confirmation(self):
        text = (
            ROOT / "production" / "set_kumamoto_disaster_frequency.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("ConfirmChange", text)
        self.assertIn("No task was started immediately", text)

    def test_81_recovery_mode_full_cycle_creates_brief(self):
        disaster.apply_frequency_mode(
            disaster.INCIDENT_ID, "recovery_periodic",
            confirm=True, path=self.db)
        result = disaster.full_cycle(
            disaster.INCIDENT_ID, "evening", dry_run=True, path=self.db)
        self.assertEqual(result["frequency_mode"], "recovery_periodic")
        self.assertEqual(result["lifecycle"]["report_type"],
                         "recovery_periodic")
        self.assertEqual(result["external_publication_calls"], 0)

    def test_82_recovery_schedule_is_every_three_days(self):
        text = (
            ROOT / "production" / "set_kumamoto_disaster_frequency.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("-DaysInterval 3", text)
        self.assertIn('New-ScheduledTaskTrigger -Daily -At "19:00"', text)

    def test_83_correction_auto_publish_defaults_off(self):
        result = self._candidate("evening")
        outcome = disaster.auto_publish_corrections(
            result["snapshot_id"], path=self.db)
        self.assertEqual(outcome["status"], "disabled")
        self.assertEqual(outcome["external_posts"], 0)

    def test_84_verified_corrections_can_auto_publish(self):
        result = self._candidate("evening")
        x_response = Mock()
        x_response.data = {"id": "x-correction"}
        x_client = Mock()
        x_client.create_tweet.return_value = x_response
        threads_client = Mock()
        threads_client.create_container.return_value = {
            "id": "correction-container"}
        threads_client.publish_container.return_value = {
            "id": "threads-correction"}
        with patch.dict(os.environ, {
            "KUMAMOTO_DISASTER_PHASE": "C",
            "KUMAMOTO_DISASTER_PUBLISH_ENABLED": "true",
            "KUMAMOTO_DISASTER_AUTO_POST_ENABLED": "true",
            "KUMAMOTO_DISASTER_CORRECTION_AUTO_POST": "true",
            "KUMAMOTO_DISASTER_X_POST_ENABLED": "true",
            "KUMAMOTO_DISASTER_THREADS_POST_ENABLED": "true",
            "POST_ENABLED": "true", "X_POST_ENABLED": "true",
            "THREADS_POST_ENABLED": "true",
        }):
            outcome = disaster.auto_publish_corrections(
                result["snapshot_id"], x_client=x_client,
                threads_client=threads_client, path=self.db)
        self.assertEqual(outcome["external_posts"], 2)

    def test_85_correction_auto_publish_is_idempotent(self):
        result = self._candidate("evening")
        x_response = Mock()
        x_response.data = {"id": "x-correction-once"}
        x_client = Mock()
        x_client.create_tweet.return_value = x_response
        threads_client = Mock()
        threads_client.create_container.return_value = {"id": "create-once"}
        threads_client.publish_container.return_value = {"id": "thread-once"}
        env = {
            "KUMAMOTO_DISASTER_PHASE": "C",
            "KUMAMOTO_DISASTER_PUBLISH_ENABLED": "true",
            "KUMAMOTO_DISASTER_AUTO_POST_ENABLED": "true",
            "KUMAMOTO_DISASTER_CORRECTION_AUTO_POST": "true",
            "KUMAMOTO_DISASTER_X_POST_ENABLED": "true",
            "KUMAMOTO_DISASTER_THREADS_POST_ENABLED": "true",
            "POST_ENABLED": "true", "X_POST_ENABLED": "true",
            "THREADS_POST_ENABLED": "true",
        }
        with patch.dict(os.environ, env):
            first = disaster.auto_publish_corrections(
                result["snapshot_id"], x_client=x_client,
                threads_client=threads_client, path=self.db)
            second = disaster.auto_publish_corrections(
                result["snapshot_id"], x_client=x_client,
                threads_client=threads_client, path=self.db)
        self.assertEqual(first["external_posts"], 2)
        self.assertEqual(second["external_posts"], 0)

    def test_86_phase_c_enabled_switches_pass_quality_gate(self):
        old, new, delta = self.pair()
        candidates = disaster.make_candidates(new, delta)
        visuals = disaster.render_visuals(new, delta, self.root)
        with patch.dict(os.environ, {
            "KUMAMOTO_DISASTER_PHASE": "C",
            "KUMAMOTO_DISASTER_PUBLISH_ENABLED": "true",
            "KUMAMOTO_DISASTER_AUTO_POST_ENABLED": "true",
            "KUMAMOTO_DISASTER_AUTO_PUBLISH_VERIFIED_ONLY": "true",
            "KUMAMOTO_DISASTER_CORRECTION_ENABLED": "true",
            "KUMAMOTO_DISASTER_CORRECTION_AUTO_POST": "true",
        }):
            report = disaster.quality_report(
                new, delta, candidates, visuals, directory=self.root)
        self.assertNotIn("auto_post_gate_valid", report["failed"])
        self.assertNotIn(
            "correction_auto_post_gate_valid", report["failed"])


if __name__ == "__main__":
    unittest.main()
