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

import measurement_jobs
import metrics_db

JST = ZoneInfo("Asia/Tokyo")


class MeasurementJobTests(unittest.TestCase):
    def seed(self, path, posted):
        metrics_db.init_db(path)
        metrics_db.write(
            """INSERT INTO published_posts(tweet_id,text,posted_at)
               VALUES (?,?,?)""", ("post-1", "text", posted.isoformat()),
            path=path)

    def test_plans_all_six_follower_windows_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.db"
            now = datetime(2026, 7, 29, 12, tzinfo=JST)
            self.seed(path, now - timedelta(hours=80))
            first = measurement_jobs.ensure_follower_plans(path, now)
            second = measurement_jobs.ensure_follower_plans(path, now)
            with closing(metrics_db.connect(path)) as connection:
                windows = {row[0] for row in connection.execute(
                    "SELECT measurement_window FROM follower_snapshot_plans")}
        self.assertEqual(first["created"], 6)
        self.assertEqual(second["created"], 0)
        self.assertEqual(
            windows, {"publish", "15m", "1h", "6h", "24h", "72h"})

    def test_status_is_local_and_explicit_about_missing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.db"
            now = datetime(2026, 7, 29, 12, tzinfo=JST)
            self.seed(path, now)
            with patch.dict(os.environ, {
                "API_KEY": "", "API_KEY_SECRET": "", "ACCESS_TOKEN": "",
                "ACCESS_TOKEN_SECRET": "", "BEARER_TOKEN": "",
            }, clear=False):
                result = measurement_jobs.status(path, now)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["external_request_made"])
        self.assertEqual(result["x_owned_metrics"], "missing_credentials")
        self.assertEqual(result["x_replies"], "missing_credentials")

    def test_default_run_is_dry_run_and_calls_no_collectors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.db"
            now = datetime(2026, 7, 29, 12, tzinfo=JST)
            self.seed(path, now)
            with patch.object(
                    measurement_jobs, "collect_post_metrics") as collect:
                result = measurement_jobs.run(path=path, now=now)
        collect.assert_not_called()
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["executed"])

    def test_status_separates_eligible_from_irrecoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.db"
            now = datetime(2026, 7, 29, 12, tzinfo=JST)
            self.seed(path, now - timedelta(hours=80))
            measurement_jobs.ensure_follower_plans(path, now)
            result = measurement_jobs.status(path, now)
        self.assertEqual(result["plans"]["eligible_now"], 0)
        self.assertEqual(result["plans"]["overdue_unrecoverable"], 6)

    def test_execute_marks_old_plans_missed_without_follower_api(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.db"
            now = datetime(2026, 7, 29, 12, tzinfo=JST)
            self.seed(path, now - timedelta(hours=80))
            with patch.object(
                    measurement_jobs, "collect_post_metrics",
                    return_value={"collected": 0}), patch(
                    "growth_tracking.capture_follower_snapshot") as capture, patch(
                    "reply_metrics.collect_x_replies",
                    return_value={"collected": 0}):
                measurement_jobs.run(path=path, now=now, execute=True)
            with closing(metrics_db.connect(path)) as connection:
                rows = connection.execute(
                    """SELECT status,snapshot_id,error_class
                       FROM follower_snapshot_plans""").fetchall()
        capture.assert_not_called()
        self.assertEqual({row["status"] for row in rows}, {"missed"})
        self.assertTrue(all(row["snapshot_id"] is None for row in rows))
        self.assertTrue(all(
            row["error_class"] == "measurement_window_expired"
            for row in rows))

    def test_existing_snapshot_reconciles_only_within_tolerance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.db"
            now = datetime(2026, 7, 29, 12, tzinfo=JST)
            self.seed(path, now - timedelta(hours=80))
            measurement_jobs.ensure_follower_plans(path, now)
            metrics_db.write(
                """INSERT INTO follower_snapshots
                   (captured_at,followers_count,source,estimated)
                   VALUES (?,?,?,?)""",
                ((now - timedelta(hours=80)).isoformat(), 100,
                 "x_owned_read", 0), path=path)
            with patch.object(
                    measurement_jobs, "collect_post_metrics",
                    return_value={"collected": 0}), patch(
                    "growth_tracking.capture_follower_snapshot") as capture, patch(
                    "reply_metrics.collect_x_replies",
                    return_value={"collected": 0}):
                measurement_jobs.run(path=path, now=now, execute=True)
            with closing(metrics_db.connect(path)) as connection:
                rows = connection.execute(
                    """SELECT measurement_window,status,snapshot_id
                       FROM follower_snapshot_plans""").fetchall()
        capture.assert_not_called()
        completed = [row for row in rows if row["status"] == "complete"]
        self.assertEqual(
            [row["measurement_window"] for row in completed], ["publish"])
        self.assertIsNotNone(completed[0]["snapshot_id"])
        self.assertEqual(
            sum(row["status"] == "missed" for row in rows), 5)

    def test_current_capture_completes_only_the_eligible_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.db"
            now = datetime(2026, 7, 29, 12, tzinfo=JST)
            self.seed(path, now)

            def capture(**kwargs):
                metrics_db.write(
                    """INSERT INTO follower_snapshots
                       (captured_at,followers_count,source,estimated)
                       VALUES (?,?,?,?)""",
                    (now.isoformat(), 101, "x_owned_read", 0), path=path)
                return {"captured": True, "followers_count": 101}

            with patch.object(
                    measurement_jobs, "collect_post_metrics",
                    return_value={"collected": 0}), patch(
                    "growth_tracking.capture_follower_snapshot",
                    side_effect=capture) as capture_mock, patch(
                    "reply_metrics.collect_x_replies",
                    return_value={"collected": 0}):
                result = measurement_jobs.run(
                    path=path, now=now, execute=True)
            with closing(metrics_db.connect(path)) as connection:
                rows = connection.execute(
                    """SELECT measurement_window,status,snapshot_id
                       FROM follower_snapshot_plans
                       ORDER BY due_at""").fetchall()
        capture_mock.assert_called_once()
        self.assertEqual(result["follower_plans"]["eligible_for_capture"], 1)
        complete = [row for row in rows if row["status"] == "complete"]
        self.assertEqual(
            [row["measurement_window"] for row in complete], ["publish"])
        self.assertIsNotNone(complete[0]["snapshot_id"])
        self.assertEqual(sum(row["status"] == "pending" for row in rows), 5)

    def test_tolerance_is_configurable_and_bounded(self):
        with patch.dict(os.environ, {
                "MEASUREMENT_FOLLOWER_TOLERANCE_MINUTES": "30"}):
            self.assertEqual(
                measurement_jobs._follower_tolerance(), timedelta(minutes=30))
        with patch.dict(os.environ, {
                "MEASUREMENT_FOLLOWER_TOLERANCE_MINUTES": "9999"}):
            self.assertEqual(
                measurement_jobs._follower_tolerance(), timedelta(minutes=180))

    def test_reconcile_dry_run_reports_changes_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.db"
            now = datetime(2026, 7, 29, 12, tzinfo=JST)
            self.seed(path, now - timedelta(hours=80))
            with patch.object(
                    measurement_jobs, "collect_post_metrics") as metrics, patch(
                    "growth_tracking.capture_follower_snapshot") as follower, patch(
                    "reply_metrics.collect_x_replies") as replies:
                result = measurement_jobs.reconcile_local(
                    path=path, now=now)
            with closing(metrics_db.connect(path)) as connection:
                table_exists = connection.execute(
                    """SELECT 1 FROM sqlite_master
                       WHERE type='table'
                         AND name='follower_snapshot_plans'""").fetchone()
        metrics.assert_not_called()
        follower.assert_not_called()
        replies.assert_not_called()
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["applied"])
        self.assertEqual(result["external_api_calls"], 0)
        self.assertEqual(result["new_plans_would_create"], 6)
        self.assertEqual(result["new_plans_created"], 0)
        self.assertEqual(result["historical_would_mark_missed"], 6)
        self.assertIsNone(table_exists)

    def test_reconcile_apply_only_updates_local_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.db"
            now = datetime(2026, 7, 29, 12, tzinfo=JST)
            self.seed(path, now - timedelta(hours=80))
            metrics_db.write(
                """INSERT INTO follower_snapshots
                   (captured_at,followers_count,source,estimated)
                   VALUES (?,?,?,?)""",
                ((now - timedelta(hours=80)).isoformat(), 100,
                 "x_owned_read", 0), path=path)
            with patch.object(
                    measurement_jobs, "collect_post_metrics") as metrics, patch(
                    "growth_tracking.capture_follower_snapshot") as follower, patch(
                    "reply_metrics.collect_x_replies") as replies:
                result = measurement_jobs.reconcile_local(
                    path=path, now=now, apply=True)
            with closing(metrics_db.connect(path)) as connection:
                counts = {
                    row["status"]: row["n"] for row in connection.execute(
                        """SELECT status,COUNT(*) AS n
                           FROM follower_snapshot_plans GROUP BY status""")
                }
        metrics.assert_not_called()
        follower.assert_not_called()
        replies.assert_not_called()
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["external_api_calls"], 0)
        self.assertEqual(result["new_plans_created"], 6)
        self.assertEqual(result["historical_marked_missed"], 5)
        self.assertEqual(result["existing_snapshots_reconciled"], 1)
        self.assertEqual(counts, {"complete": 1, "missed": 5})


if __name__ == "__main__":
    unittest.main()
