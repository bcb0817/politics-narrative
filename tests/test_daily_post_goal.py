import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import daily_post_goal as goal
import discord_notify
from metrics_db import init_db

JST = ZoneInfo("Asia/Tokyo")


class DailyPostGoalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "metrics.db"
        self.logs = self.root / "logs"
        self.logs.mkdir()
        init_db(self.db)
        self.now = datetime(2026, 8, 2, 20, 0, tzinfo=JST)

    def tearDown(self):
        self.temp.cleanup()

    def insert_x(self, count):
        connection = sqlite3.connect(self.db)
        for index in range(count):
            connection.execute(
                "INSERT INTO published_posts(tweet_id,posted_at,text) VALUES(?,?,?)",
                (f"x-{index}", f"2026-08-02T{index:02d}:00:00+09:00", "post"))
        connection.commit()
        connection.close()

    def test_reports_twenty_post_goal_without_double_counting_threads(self):
        self.insert_x(18)
        connection = sqlite3.connect(self.db)
        connection.execute(
            """INSERT INTO threads_posts(client_post_key,threads_post_id,status,published_at)
               VALUES('key','thread-1','published','2026-08-02T01:00:00+09:00')""")
        connection.commit()
        connection.close()
        report = goal.build_report(
            path=self.db, log_dir=self.logs, now=self.now, target=20)
        self.assertEqual(report["actual"]["x"], 18)
        self.assertEqual(report["actual"]["threads"], 1)
        self.assertEqual(report["achievement"]["shortfall"], 2)
        self.assertIn("pace_forecast", report["analysis"])

    def test_previous_calendar_day_can_be_reviewed(self):
        self.insert_x(3)
        report = goal.build_report(
            path=self.db, log_dir=self.logs, now=self.now,
            report_date=datetime(2026, 8, 1, tzinfo=JST).date(), target=20)
        self.assertEqual(report["report_date"], "2026-08-01")
        self.assertEqual(report["actual"]["x"], 0)
        self.assertEqual(report["achievement"]["shortfall"], 20)

    def test_skip_reasons_drive_remediation_without_lowering_quality(self):
        record = {"ts_jst": self.now.isoformat(), "decision": "skip",
                  "reason": "no_qualified_news"}
        (self.logs / "post_attempts.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
        report = goal.build_report(
            path=self.db, log_dir=self.logs, now=self.now, target=20)
        self.assertEqual(report["analysis"]["primary_reason"], "no_qualified_news")
        self.assertTrue(any("公式" in row["action"] for row in report["remediation"]))
        self.assertFalse(report["safety"]["quality_threshold_changed"])

    def test_save_writes_dated_and_latest_reports(self):
        report = goal.build_report(
            path=self.db, log_dir=self.logs, now=self.now, target=20)
        saved = goal.save_report(report, self.root / "out")
        self.assertTrue(saved.exists())
        self.assertTrue((self.root / "out" / "latest.json").exists())

    def test_unmet_goal_applies_bounded_remediation(self):
        report = goal.build_report(
            path=self.db, log_dir=self.logs, now=self.now, target=20)
        policy = goal.apply_remediation(report, self.root / "goal", now=self.now)
        self.assertTrue(policy["active"])
        self.assertEqual(policy["target"], 20)
        self.assertTrue(policy["quality_threshold_locked"])
        self.assertTrue(policy["safety_gate_locked"])
        self.assertTrue(policy["duplicate_gate_locked"])
        self.assertTrue(policy["budget_gate_locked"])
        self.assertEqual(
            goal.load_active_remediation(self.root / "goal", now=self.now), policy)

    def test_discord_summary_does_not_include_raw_attempts(self):
        report = goal.build_report(
            path=self.db, log_dir=self.logs, now=self.now, target=20)
        with patch("discord_notify.notify", return_value=True) as notify:
            self.assertTrue(discord_notify.notify_daily_post_goal(report))
        fields = notify.call_args.kwargs["fields"]
        self.assertNotIn("attempts", fields)

    def test_registration_script_is_whatif_by_default(self):
        text = (ROOT / "production" / "register_daily_post_goal_task.ps1").read_text(
            encoding="utf-8")
        self.assertIn("if (-not $Apply)", text)
        self.assertIn('Registered = 0', text)
        self.assertNotIn("Start-ScheduledTask", text)


if __name__ == "__main__":
    unittest.main()
