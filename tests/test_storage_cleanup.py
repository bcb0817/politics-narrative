import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import local_bot  # noqa: E402
from storage_cleanup import run_cleanup  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")


class StorageCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 7, 29, 12, 0, tzinfo=JST)
        self.env = patch.dict(os.environ, {
            "STORAGE_CLEANUP_ENABLED": "true",
            "STORAGE_POLITICS_CACHE_DAYS": "2",
            "STORAGE_ARTICLE_CACHE_DAYS": "2",
            "STORAGE_SEARCH_HISTORY_DAYS": "45",
            "STORAGE_OPENAI_BATCH_DAYS": "30",
            "STORAGE_PREVIEW_DAYS": "14",
            "STORAGE_FAILED_DRAFT_DAYS": "30",
            "STORAGE_LOG_RETENTION_DAYS": "30",
            "STORAGE_LOG_MAX_MB": "1",
            "STORAGE_PYCACHE_DAYS": "2",
            "STORAGE_CLEANUP_MAX_DELETIONS": "5000",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def make_file(self, relative: str, *, age_days: int, size: int = 1) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        timestamp = (self.now - timedelta(days=age_days)).timestamp()
        os.utime(path, (timestamp, timestamp))
        return path

    def test_dry_run_reports_without_deleting(self):
        old = self.make_file(
            "data/politics_analysis_cache/old.json", age_days=3)
        result = run_cleanup(self.root, dry_run=True, now=self.now)
        self.assertTrue(old.exists())
        self.assertEqual(result["deleted_files"], 1)
        self.assertEqual(result["items"][0]["action"], "would_delete")

    def test_apply_deletes_only_expired_allowlisted_files(self):
        old = self.make_file(
            "data/article_content_cache/old.json", age_days=3)
        recent = self.make_file(
            "data/article_content_cache/recent.json", age_days=1)
        result = run_cleanup(self.root, dry_run=False, now=self.now)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertEqual(result["deleted_files"], 1)

    def test_expired_candidate_cache_is_deleted(self):
        old = self.make_file(
            "data/politics_candidate_cache/old.json", age_days=3)
        result = run_cleanup(self.root, dry_run=False, now=self.now)
        self.assertFalse(old.exists())
        self.assertEqual(result["deleted_files"], 1)

    def test_state_database_and_backups_are_never_deleted(self):
        protected = [
            self.make_file("data/bot_metrics.db", age_days=400),
            self.make_file("data/posted_urls.json", age_days=400),
            self.make_file("backups/full-backup.zip", age_days=400),
            self.make_file("outputs/note/drafts/draft.md", age_days=400),
            self.make_file(
                "outputs/disaster_updates/event/visual.png", age_days=400),
            self.make_file(
                "outputs/special_research/report.md", age_days=400),
            self.make_file(
                "outputs/crosspost/publication/video.mp4", age_days=400),
        ]
        run_cleanup(self.root, dry_run=False, now=self.now)
        self.assertTrue(all(path.exists() for path in protected))

    def test_old_preview_and_failed_draft_are_removed(self):
        preview = self.make_file(
            "outputs/previews/2026-01-01/a.txt", age_days=20)
        failed = self.make_file(
            "outputs/note/failed/old/a.md", age_days=40)
        run_cleanup(self.root, dry_run=False, now=self.now)
        self.assertFalse(preview.exists())
        self.assertFalse(failed.exists())

    def test_symlink_is_never_followed(self):
        outside = self.make_file("outside/keep.txt", age_days=100)
        cache = self.root / "data" / "article_content_cache"
        cache.mkdir(parents=True)
        try:
            (cache / "link").symlink_to(outside)
        except OSError:
            self.skipTest("symlink unavailable")
        run_cleanup(self.root, dry_run=False, now=self.now)
        self.assertTrue(outside.exists())

    def test_active_large_log_is_rotated_not_deleted(self):
        active = self.make_file(
            "logs/bot.log", age_days=0, size=1024 * 1024 + 1)
        result = run_cleanup(self.root, dry_run=False, now=self.now)
        self.assertFalse(active.exists())
        self.assertEqual(result["rotated_files"], 1)
        self.assertTrue(list((self.root / "logs").glob("bot.log.*")))

    def test_old_rotated_log_is_deleted(self):
        old = self.make_file(
            "logs/bot.log.20260101-000000", age_days=31)
        run_cleanup(self.root, dry_run=False, now=self.now)
        self.assertFalse(old.exists())

    def test_deletion_cap_is_enforced(self):
        with patch.dict(os.environ, {
            "STORAGE_CLEANUP_MAX_DELETIONS": "2"}, clear=False):
            files = [
                self.make_file(
                    f"data/politics_analysis_cache/{index}.json",
                    age_days=5)
                for index in range(5)
            ]
            result = run_cleanup(self.root, dry_run=False, now=self.now)
        self.assertTrue(result["limit_reached"])
        self.assertEqual(sum(not path.exists() for path in files), 2)

    def test_daily_cleanup_is_part_of_daemon_schedule(self):
        now = datetime(2026, 7, 29, 3, 0, tzinfo=JST)
        with patch.dict(os.environ, {
            "STORAGE_CLEANUP_ENABLED": "true",
            "STORAGE_CLEANUP_SCHEDULE": "03:30",
            "ENGAGEMENT_QUEUE_SCHEDULE": "12:20",
            "FOLLOWER_SNAPSHOT_SCHEDULE": "23:55",
        }, clear=False):
            scheduled, event = local_bot.next_auxiliary_event(now)
        self.assertEqual(event, "storage_cleanup")
        self.assertEqual(scheduled.strftime("%H:%M"), "03:30")


if __name__ == "__main__":
    unittest.main()
