import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import metrics_db  # noqa: E402
import x_research_analysis as analysis  # noqa: E402
import local_bot  # noqa: E402


JST = ZoneInfo("Asia/Tokyo")


class FakeClient:
    def __init__(self, first_error=None):
        self.calls = []
        self.first_error = first_error

    def create_tweet(self, **kwargs):
        self.calls.append(kwargs)
        if self.first_error and len(self.calls) == 1:
            raise self.first_error
        return SimpleNamespace(data={"id": str(1000 + len(self.calls))})


class FakeResponse:
    status_code = 400


class LongPostRejected(Exception):
    response = FakeResponse()


class XResearchAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "metrics.db"
        metrics_db.init_db(self.path)
        analysis.migrate(self.path)
        self.now = datetime(2026, 7, 31, 12, 30, tzinfo=JST)
        self.env = {
            "X_RESEARCH_ANALYSIS_ENABLED": "true",
            "X_RESEARCH_ANALYSIS_AUTO_PUBLISH_ENABLED": "true",
            "X_RESEARCH_PREMIUM_LONG_POST_ENABLED": "true",
            "X_RESEARCH_THREAD_FALLBACK_ENABLED": "true",
            "X_RESEARCH_ANALYSIS_DAILY_LIMIT": "2",
            "X_RESEARCH_ANALYSIS_MIN_INTERVAL_MINUTES": "180",
            "X_RESEARCH_ANALYSIS_MIN_CONFIDENCE": "0.65",
            "X_RESEARCH_ANALYSIS_MIN_EVIDENCE": "2",
            "X_RESEARCH_ANALYSIS_MIN_POSTING_VALUE": "6.0",
            "X_RESEARCH_LONG_POST_MAX_CHARS": "4000",
            "X_RESEARCH_THREAD_CHARS": "260",
            "X_RESEARCH_THREAD_MAX_POSTS": "8",
            "POST_ENABLED": "true",
        }
        self._seed()

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self):
        with closing(metrics_db.connect(self.path)) as conn:
            conn.execute(
                """INSERT INTO integrated_research_runs
                   (run_id,xai_run_id,generated_at,source_family_count,
                    topic_count,eligible_topic_count,status,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    "integrated-test", "xai-test", self.now.isoformat(),
                    2, 2, 1, "success", self.now.isoformat(),
                ),
            )
            conn.execute(
                """INSERT INTO integrated_research_topics
                   (id,run_id,topic_key,title,fact_summary,main_claims_json,
                    counterclaims_json,confidence,source_family_count,
                    evidence_count,change_status,post_eligible,
                    decision_reason,correction_status,deleted_source_count,
                    posting_value_score,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    1, "integrated-test", "社会保険料", "社会保険料の議論",
                    "政府が制度資料を公開した。",
                    json.dumps(["負担が重いとの意見"], ensure_ascii=False),
                    json.dumps(["財源も確認すべきとの意見"], ensure_ascii=False),
                    0.82, 2, 3, "changed", 1,
                    "eligible_for_standard_pipeline", "current", 0, 8.3,
                    self.now.isoformat(), self.now.isoformat(),
                ),
            )
            conn.execute(
                """INSERT INTO integrated_research_topics
                   (id,run_id,topic_key,title,fact_summary,main_claims_json,
                    counterclaims_json,confidence,source_family_count,
                    evidence_count,change_status,post_eligible,
                    decision_reason,correction_status,deleted_source_count,
                    posting_value_score,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    2, "integrated-test", "未確認", "未確認情報",
                    "確認できていない。",
                    "[]", "[]", 0.2, 1, 1, "new", 0,
                    "low_confidence", "current", 0, 2.0,
                    self.now.isoformat(), self.now.isoformat(),
                ),
            )
            for index, url in enumerate((
                "https://example.go.jp/source-1",
                "https://example.go.jp/source-2",
            ), 1):
                conn.execute(
                    """INSERT INTO integrated_research_evidence
                       (topic_id,provider,evidence_type,source_id,source_url,
                        title,summary,reliability,observed_at,canonical_url,
                        content_hash,freshness_status,is_deleted,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                    (
                        1, "official_rss", "official", f"source-{index}",
                        url, f"公式資料{index}", "要約", 0.95,
                        self.now.isoformat(), url, f"hash-{index}",
                        "fresh", self.now.isoformat(),
                    ),
                )
            conn.commit()

    def test_prepare_uses_only_verified_topics(self):
        with patch.dict(os.environ, self.env, clear=False):
            result = analysis.prepare(self.path, now=self.now)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(result["topics"]), 1)
        self.assertIn("社会保険料の議論", result["text"])
        self.assertIn("一次資料", result["text"])
        self.assertNotIn("未確認情報", result["text"])
        self.assertIn("反応・意見", result["text"])

    def test_dry_run_never_calls_x(self):
        client = FakeClient()
        with patch.dict(os.environ, self.env, clear=False):
            result = analysis.publish(
                confirm=True, dry_run=True, path=self.path,
                now=self.now, client=client)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["external_writes"], 0)
        self.assertEqual(client.calls, [])

    def test_premium_long_post_is_recorded(self):
        client = FakeClient()
        with patch.dict(os.environ, self.env, clear=False), patch(
            "x_research_analysis._reserve_posts", return_value=(123, "")
        ), patch("x_research_analysis.finalize"), patch(
            "discord_notify.notify_post_success", return_value=True
        ):
            result = analysis.publish(
                confirm=True, dry_run=False, path=self.path,
                now=self.now, client=client)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["delivery_mode"], "premium_long_post")
        self.assertEqual(result["external_writes"], 1)
        self.assertEqual(len(client.calls), 1)
        with closing(metrics_db.connect(self.path)) as conn:
            state = conn.execute(
                "SELECT status FROM x_research_analysis_posts"
            ).fetchone()[0]
            topic_post = conn.execute(
                "SELECT x_post_id FROM integrated_research_topics WHERE id=1"
            ).fetchone()[0]
        self.assertEqual(state, "published")
        self.assertEqual(topic_post, "1001")

    def test_known_long_post_rejection_falls_back_to_thread(self):
        client = FakeClient(first_error=LongPostRejected())
        with patch.dict(os.environ, self.env, clear=False), patch(
            "x_research_analysis._reserve_posts", return_value=(123, "")
        ), patch("x_research_analysis.finalize"), patch(
            "discord_notify.notify_post_success", return_value=True
        ):
            result = analysis.publish(
                confirm=True, dry_run=False, path=self.path,
                now=self.now, client=client)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["delivery_mode"], "thread")
        self.assertGreater(result["external_writes"], 1)
        self.assertNotIn("in_reply_to_tweet_id", client.calls[1])
        self.assertIn("in_reply_to_tweet_id", client.calls[2])

    def test_ambiguous_error_never_falls_back(self):
        client = FakeClient(first_error=RuntimeError("network"))
        with patch.dict(os.environ, self.env, clear=False), patch(
            "x_research_analysis._reserve_posts", return_value=(123, "")
        ), patch("x_research_analysis.finalize"):
            result = analysis.publish(
                confirm=True, dry_run=False, path=self.path,
                now=self.now, client=client)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["external_writes"], 0)
        self.assertEqual(len(client.calls), 1)

    def test_same_run_is_not_published_twice(self):
        with closing(metrics_db.connect(self.path)) as conn:
            conn.execute(
                """INSERT INTO x_research_analysis_posts
                   (source_run_id,content_hash,text,status,delivery_mode,
                    tweet_ids_json,included_topic_ids_json,failure_reason,
                    created_at,published_at,updated_at)
                   VALUES (?,?,?,'published','premium_long_post','[]','[]','',
                           ?,?,?)""",
                (
                    "integrated-test", "hash", "text",
                    self.now.isoformat(), self.now.isoformat(),
                    self.now.isoformat(),
                ),
            )
            conn.commit()
        with patch.dict(os.environ, self.env, clear=False):
            result = analysis.prepare(self.path, now=self.now)
        self.assertEqual(result["status"], "already_processed")

    def test_thread_chunks_respect_configured_limit(self):
        chunks = analysis._split_thread("長い説明です。" * 100, 260)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 260 for chunk in chunks))

    def test_daemon_schedules_analysis_after_noon_search(self):
        env = {
            **self.env,
            "X_RESEARCH_ANALYSIS_SCHEDULE": "12:25,18:25",
            "ENGAGEMENT_QUEUE_SCHEDULE": "12:20,20:20",
        }
        now = datetime(2026, 7, 31, 12, 21, tzinfo=JST)
        with patch.dict(os.environ, env, clear=False):
            next_at, event = local_bot.next_auxiliary_event(now)
        self.assertEqual(event, "x_research_analysis")
        self.assertEqual(next_at.strftime("%H:%M"), "12:25")


if __name__ == "__main__":
    unittest.main()
