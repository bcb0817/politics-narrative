import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from metrics_db import init_db
from short_video_factory.workflow import ShortVideoFactory
from short_video_media_server import issue, lookup, revoke


class ShortVideoFactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "metrics.db"
        self.out = Path(self.tmp.name) / "output"
        self.old = dict(os.environ)
        os.environ["SHORT_VIDEO_OUTPUT_DIR"] = str(self.out)
        os.environ["SHORT_VIDEO_OPERATION_PHASE"] = "A"
        os.environ["SHORT_VIDEO_AUTO_PUBLISH_ENABLED"] = "false"
        os.environ["SHORT_VIDEO_TTS_PROVIDER"] = "mock"
        init_db(self.db)
        self.factory = ShortVideoFactory(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                """INSERT INTO integrated_research_runs
                   (run_id,generated_at,source_family_count,topic_count,
                    eligible_topic_count,status,created_at)
                   VALUES ('fixture','2026-07-31T10:00:00+09:00',3,1,1,
                           'completed','2026-07-31T10:00:00+09:00')""")
            conn.execute(
                """INSERT INTO integrated_research_topics
                   (run_id,topic_key,title,fact_summary,confidence,
                    source_family_count,evidence_count,change_status,
                    post_eligible,posting_value_score,correction_status,
                    created_at,updated_at)
                   VALUES ('fixture','fixture-policy','制度変更の検証',
                           '政府資料で制度変更が確認された',0.95,3,3,'new',
                           1,9.0,'current',
                           '2026-07-31T10:00:00+09:00',
                           '2026-07-31T10:00:00+09:00')""")
            topic_id = conn.execute(
                "SELECT id FROM integrated_research_topics").fetchone()[0]
            for index in range(3):
                conn.execute(
                    """INSERT INTO integrated_research_evidence
                       (topic_id,provider,evidence_type,source_id,source_url,
                        title,summary,reliability,is_deleted,created_at)
                       VALUES (?,?,?,?,?,?,?,?,0,?)""",
                    (topic_id, f"official-{index}", "primary", f"source-{index}",
                     f"https://example.invalid/{index}", f"資料{index}",
                     f"政府の一次資料で変更点{index}を確認", .95,
                     "2026-07-31T10:00:00+09:00"))
            conn.commit()
        self.topic_id = topic_id

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old)
        self.tmp.cleanup()

    def test_schema_is_idempotent_and_candidate_is_eligible(self):
        ShortVideoFactory(self.db)
        result = self.factory.candidates()
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["candidates"][0]["eligible"])

    def test_fixture_pipeline_stops_before_external_publish(self):
        created = self.factory.project_create(self.topic_id)
        video_id = created["video_id"]
        self.factory.script_generate(video_id)
        self.assertTrue(self.factory.script_check(video_id)["passed"])
        self.assertTrue(Path(self.factory.audio_generate(video_id)["path"]).is_file())
        self.assertEqual(self.factory.captions_generate(video_id)["cue_count"], 5)
        self.assertEqual(len(self.factory.visual_plan(video_id)["assets"]), 5)
        result = self.factory.publish(video_id, "x", confirm=True, dry_run=False)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("phase_d_required", result["blocking_reasons"])
        self.assertEqual(result["external_writes"], 0)

    def test_queue_is_idempotent_and_emergency_stop_blocks(self):
        video_id = self.factory.project_create(self.topic_id)["video_id"]
        self.factory.queue(video_id)
        self.factory.queue(video_id)
        with closing(sqlite3.connect(self.db)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM short_video_publication_queue").fetchone()[0]
        self.assertEqual(count, 2)
        self.factory.emergency_stop()
        plan = self.factory.publish_plan(video_id)
        self.assertIn("emergency_stopped", plan["blocking_reasons"])

    def _unlock_phase_d_fixture(self, video_id):
        os.environ["SHORT_VIDEO_OPERATION_PHASE"] = "D"
        os.environ["SHORT_VIDEO_AUTO_PUBLISH_ENABLED"] = "true"
        os.environ["SHORT_VIDEO_X_AUTO_PUBLISH"] = "true"
        os.environ["SHORT_VIDEO_THREADS_AUTO_PUBLISH"] = "true"
        os.environ["SHORT_VIDEO_YOUTUBE_AUTO_PUBLISH"] = "true"
        os.environ["SHORT_VIDEO_INSTAGRAM_AUTO_PUBLISH"] = "true"
        os.environ["API_KEY"] = "fixture"
        os.environ["API_KEY_SECRET"] = "fixture"
        os.environ["ACCESS_TOKEN"] = "fixture"
        os.environ["ACCESS_TOKEN_SECRET"] = "fixture"
        os.environ["THREADS_ACCESS_TOKEN"] = "fixture"
        os.environ["THREADS_USER_ID"] = "fixture"
        os.environ["YOUTUBE_ACCESS_TOKEN"] = "fixture"
        os.environ["INSTAGRAM_ACCESS_TOKEN"] = "fixture"
        os.environ["INSTAGRAM_USER_ID"] = "fixture"
        master = self.out / video_id / "renders" / "master.mp4"
        master.parent.mkdir(parents=True, exist_ok=True)
        master.write_bytes(b"fixture mp4")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                """UPDATE short_video_projects SET master_video_path=?,
                   quality_score=9.5,safety_score=9.5,publish_eligible=1
                   WHERE video_id=?""", (str(master), video_id))
            for index in range(30):
                conn.execute(
                    """INSERT INTO short_video_publications
                       (video_id,platform,external_post_id,status,created_at,updated_at)
                       VALUES (?,?,?,?,?,?)""",
                    (f"sample-{index}", "x", f"post-{index}", "published",
                     "2026-07-31T10:00:00+09:00",
                     "2026-07-31T10:00:00+09:00"))
            for index in range(10):
                conn.execute(
                    """INSERT INTO short_video_quality_checks
                       (video_id,check_type,passed,score,details_json,checked_at)
                       VALUES (?,?,?,?,?,?)""",
                    (f"safe-{index}", "final", 1, 9.5, "{}",
                     f"2026-07-31T10:{index:02d}:00+09:00"))
            conn.commit()

    def test_x_provider_is_wired_after_all_phase_d_gates(self):
        video_id = self.factory.project_create(self.topic_id)["video_id"]
        self.factory.script_generate(video_id)
        self._unlock_phase_d_fixture(video_id)
        client = Mock()
        client.upload.return_value = "media-1"
        client.publish.return_value = "tweet-1"
        with patch("crosspost.XVideoClient", return_value=client):
            result = self.factory.publish(
                video_id, "x", confirm=True, dry_run=False)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["external_post_id"], "tweet-1")
        client.upload.assert_called_once()
        client.publish.assert_called_once()

    def test_threads_provider_uses_video_container_and_public_https_url(self):
        video_id = self.factory.project_create(self.topic_id)["video_id"]
        self.factory.script_generate(video_id)
        self._unlock_phase_d_fixture(video_id)
        client = Mock()
        client.create_container.return_value = {"id": "container-1"}
        client.container_status.return_value = {"status": "FINISHED"}
        client.publish_container.return_value = {"id": "thread-1"}
        with patch("threads_api.ThreadsClient", return_value=client):
            result = self.factory.publish(
                video_id, "threads", confirm=True, dry_run=False,
                public_url="https://media.example.invalid/video.mp4")
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["external_post_id"], "thread-1")
        client.create_container.assert_called_once()
        self.assertEqual(
            client.create_container.call_args.kwargs["media_type"], "VIDEO")

    def test_public_media_token_is_scoped_and_revocable(self):
        target = self.out / "fixture.mp4"
        target.write_bytes(b"fixture mp4")
        os.environ["SHORT_VIDEO_PUBLIC_MEDIA_BASE_URL"] = (
            "https://media.example.invalid")
        issued = issue("fixture-video", target, 15, self.db)
        self.assertNotIn("fixture.mp4", issued["public_url"])
        found = lookup(issued["route"], self.db)
        self.assertEqual(found[0], target.resolve())
        self.assertEqual(revoke("fixture-video", self.db), 1)
        self.assertIsNone(lookup(issued["route"], self.db))

    def test_youtube_provider_is_wired_after_phase_d_gates(self):
        video_id = self.factory.project_create(self.topic_id)["video_id"]
        self.factory.script_generate(video_id)
        self._unlock_phase_d_fixture(video_id)
        client = Mock()
        client.upload.return_value = "youtube-1"
        with patch("crosspost.YouTubeClient", return_value=client):
            result = self.factory.publish(
                video_id, "youtube", confirm=True, dry_run=False)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["external_post_id"], "youtube-1")
        client.upload.assert_called_once()

    def test_instagram_provider_uses_public_reel_container(self):
        video_id = self.factory.project_create(self.topic_id)["video_id"]
        self.factory.script_generate(video_id)
        self._unlock_phase_d_fixture(video_id)
        client = Mock()
        client.create_reel.return_value = "container-ig"
        client.container_status.return_value = {"status_code": "FINISHED"}
        client.publish.return_value = "instagram-1"
        with patch("crosspost.InstagramClient", return_value=client):
            result = self.factory.publish(
                video_id, "instagram", confirm=True, dry_run=False,
                public_url="https://media.example.invalid/video.mp4")
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["external_post_id"], "instagram-1")
        client.create_reel.assert_called_once()

    def test_queue_applies_platform_offsets(self):
        video_id = self.factory.project_create(self.topic_id)["video_id"]
        os.environ["SHORT_VIDEO_X_OFFSET_MINUTES"] = "15"
        os.environ["SHORT_VIDEO_THREADS_OFFSET_MINUTES"] = "30"
        result = self.factory.queue(video_id, ("x", "threads"))
        x_time = datetime.fromisoformat(result["scheduled_at"]["x"])
        threads_time = datetime.fromisoformat(result["scheduled_at"]["threads"])
        self.assertEqual(
            int((threads_time - x_time).total_seconds() / 60), 15)

    def test_metrics_sync_preserves_nulls_and_records_due_window(self):
        video_id = self.factory.project_create(self.topic_id)["video_id"]
        published_at = (
            datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(hours=3)
        ).isoformat()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                """INSERT INTO short_video_publications
                   (video_id,platform,external_post_id,status,published_at,
                    created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (video_id, "youtube", "youtube-metric", "published",
                 published_at, published_at, published_at))
            conn.commit()
        payload = {
            "statistics": {
                "viewCount": "120", "likeCount": "7", "commentCount": "2",
            }}
        with patch.object(
            self.factory, "_fetch_platform_metrics", return_value=payload
        ):
            result = self.factory.metrics_sync(video_id, dry_run=False)
        self.assertEqual(result["synced"], 2)
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute(
                """SELECT measurement_window,views,likes,replies,shares
                   FROM short_video_metrics ORDER BY measurement_window"""
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1:4], (120, 7, 2))
        self.assertIsNone(rows[0][4])

    def test_queue_worker_dry_run_never_writes_externally(self):
        video_id = self.factory.project_create(self.topic_id)["video_id"]
        self.factory.queue(
            video_id, ("x",),
            scheduled_at=(
                datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(minutes=1)
            ).isoformat())
        result = self.factory.run_queue(live=False)
        self.assertEqual(result["due"], 1)
        self.assertEqual(result["external_writes"], 0)

    def test_policy_hold_keeps_queue_for_later_retry(self):
        video_id = self.factory.project_create(self.topic_id)["video_id"]
        self.factory.script_generate(video_id)
        self.factory.queue(
            video_id, ("x",),
            scheduled_at=(
                datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(minutes=1)
            ).isoformat())
        result = self.factory.run_queue(live=True)
        self.assertEqual(result["results"][0]["status"], "blocked")
        with closing(sqlite3.connect(self.db)) as conn:
            status, retry_count = conn.execute(
                """SELECT status,retry_count
                   FROM short_video_publication_queue"""
            ).fetchone()
        self.assertEqual(status, "queued")
        self.assertEqual(retry_count, 0)


if __name__ == "__main__":
    unittest.main()
