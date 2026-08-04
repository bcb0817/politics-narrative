import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import integrated_research
from metrics_db import apply_additive_migrations, apply_threads_full_migrations


JST = ZoneInfo("Asia/Tokyo")


class IntegratedResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.temp.name) / "metrics.db"
        apply_additive_migrations(self.path)
        apply_threads_full_migrations(self.path)
        self.now = datetime(2026, 7, 31, 9, 0, tzinfo=JST)
        self.item = {
            "title": "政府が新しい子育て支援制度を公表",
            "summary": "政府の公式資料によると、対象世帯と開始時期が示された。",
            "link": "https://example.go.jp/policy/childcare",
            "source": "政府公式",
            "source_type": "government_official",
            "pub_date": self.now.isoformat(),
            "discovered_via": ["rss", "xai"],
            "verified": True,
            "source_reliability_score": 10,
            "xai_topic_match": True,
            "xai_attention_score": 7.0,
            "xai_velocity_score": 5.0,
            "xai_news_match_confidence": 0.9,
            "xai_discovered_at": self.now.isoformat(),
            "xai_topic_metadata": {
                "topic_key": "新しい子育て支援制度",
                "stance_summary": ["負担軽減を評価する声"],
                "counterargument_summary": ["対象範囲が狭いとの指摘"],
                "representative_post_ids": ["100", "101"],
                "evidence_count": 3,
                "unique_source_estimate": 2,
                "search_confidence": 0.8,
            },
        }
        self.topics = [{
            "topic_key": "新しい子育て支援制度",
            "main_claims": ["負担軽減を評価する声"],
            "counter_claims": ["対象範囲が狭いとの指摘"],
            "representative_post_ids": ["100", "101"],
            "search_confidence": 0.8,
        }]
        self.env = {
            "INTEGRATED_RESEARCH_ENABLED": "true",
            "INTEGRATED_RESEARCH_POST_ENABLED": "true",
            "INTEGRATED_RESEARCH_MIN_SOURCE_FAMILIES": "2",
            "INTEGRATED_RESEARCH_MIN_EVIDENCE": "2",
            "INTEGRATED_RESEARCH_MIN_CONFIDENCE": "0.65",
            "INTEGRATED_RESEARCH_MIN_POSTING_VALUE_SCORE": "6.0",
            "INTEGRATED_RESEARCH_MAX_POST_CANDIDATES_PER_RUN": "1",
            "INTEGRATED_RESEARCH_DISCORD_ENABLED": "false",
            "SEMANTIC_TOPIC_COOLDOWN_HOURS": "72",
        }

    def tearDown(self):
        self.temp.cleanup()

    def build(self, item=None, topics=None):
        with patch.dict(os.environ, self.env, clear=False):
            return integrated_research.build_integrated_research_candidates(
                [item or dict(self.item)],
                topics or list(self.topics),
                path=self.path,
                now=self.now,
            )

    def count(self, table):
        with sqlite3.connect(self.path) as conn:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_build_persists_auditable_result_and_candidate(self):
        candidates = self.build()
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertIn("【公式情報】", candidate["summary"])
        self.assertIn("【X上の主な論点】", candidate["summary"])
        self.assertIn("【反対論・留意点】", candidate["summary"])
        self.assertTrue(candidate["verified"])
        self.assertEqual(candidate["post_type_hint"],
                         "steelman_counterargument")
        self.assertEqual(self.count("integrated_research_runs"), 1)
        self.assertEqual(self.count("integrated_research_topics"), 1)
        self.assertEqual(self.count("integrated_research_evidence"), 3)

    def test_insufficient_evidence_is_saved_but_not_posted(self):
        self.env["INTEGRATED_RESEARCH_MIN_EVIDENCE"] = "5"
        candidates = self.build()
        self.assertEqual(candidates, [])
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                """SELECT post_eligible,decision_reason
                   FROM integrated_research_topics""").fetchone()
        self.assertEqual(row[0], 0)
        self.assertIn("insufficient_evidence", row[1])

    def test_posting_can_be_disabled_without_disabling_database(self):
        self.env["INTEGRATED_RESEARCH_POST_ENABLED"] = "false"
        self.assertEqual(self.build(), [])
        self.assertEqual(self.count("integrated_research_runs"), 1)
        self.assertEqual(self.count("integrated_research_topics"), 1)

    def test_published_candidate_is_not_returned_again(self):
        candidate = self.build()[0]
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """UPDATE integrated_research_topics
                   SET candidate_news_id=99,x_post_id='posted-1' WHERE id=?""",
                (candidate["integrated_research_topic_id"],),
            )
        self.assertEqual(self.build(), [])

    def test_unchanged_new_run_is_not_another_post_candidate(self):
        self.assertEqual(len(self.build()), 1)
        later = dict(self.item)
        later["xai_discovered_at"] = "2026-07-31T18:00:00+09:00"
        self.now = datetime(2026, 7, 31, 18, 0, tzinfo=JST)
        self.assertEqual(self.build(item=later), [])
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                """SELECT change_status,decision_reason
                   FROM integrated_research_topics
                   ORDER BY id DESC LIMIT 1""").fetchone()
        self.assertEqual(row[0], "unchanged")
        self.assertIn("no_material_change", row[1])

    def test_threads_match_becomes_reaction_evidence(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO threads_search_results
                   (threads_post_id,text,permalink,is_verified,last_seen_at)
                   VALUES (?,?,?,?,?)""",
                (
                    "thread-1", "子育て支援制度の対象範囲を広げてほしい",
                    "https://threads.net/post/thread-1", 0,
                    self.now.isoformat(),
                ),
            )
        self.build()
        with sqlite3.connect(self.path) as conn:
            count = conn.execute(
                """SELECT COUNT(*) FROM integrated_research_evidence
                   WHERE provider='threads_search'""").fetchone()[0]
        self.assertEqual(count, 1)

    def test_status_reports_integrated_tables(self):
        self.build()
        result = integrated_research.status(self.path)
        self.assertEqual(result["counts"]["integrated_research_runs"], 1)
        self.assertEqual(result["counts"]["integrated_research_topics"], 1)
        self.assertEqual(result["counts"]["integrated_research_evidence"], 3)
        self.assertEqual(result["counts"]["integrated_research_decisions"], 1)

    def test_analysis_fields_and_decision_are_persisted(self):
        self.build()
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                """SELECT claim_classification_json,contradictions_json,
                          anger_summary,posting_value_score,change_summary,
                          cache_status FROM integrated_research_topics"""
            ).fetchone()
            decision = conn.execute(
                """SELECT decision,reason FROM integrated_research_decisions"""
            ).fetchone()
        self.assertIn('"fact"', row[0])
        self.assertIn("policy_disagreement", row[1])
        self.assertGreater(row[3], 6)
        self.assertEqual(row[4], "初回観測")
        self.assertEqual(row[5], "fresh")
        self.assertEqual(decision[0], "eligible")

    def test_history_outcomes_export_dashboard_audit_and_restore(self):
        candidate = self.build()[0]
        topic_id = candidate["integrated_research_topic_id"]
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """UPDATE integrated_research_topics
                   SET x_post_id='x-1',threads_post_id='t-1' WHERE id=?""",
                (topic_id,),
            )
            conn.execute(
                """INSERT INTO post_metrics
                   (tweet_id,measurement_window,impressions,likes,reposts)
                   VALUES ('x-1','24h',120,8,2)"""
            )
            conn.execute(
                """INSERT INTO threads_metrics
                   (threads_post_id,measurement_window,views,likes,reposts)
                   VALUES ('t-1','24h',80,6,1)"""
            )
            conn.commit()
        history = integrated_research.history(limit=5, path=self.path)
        self.assertEqual(history["count"], 1)
        self.assertEqual(len(history["topics"][0]["decisions"]), 1)
        outcome = integrated_research.outcomes(30, self.path)
        self.assertEqual(outcome["x_impressions"], 120)
        self.assertEqual(outcome["threads_views"], 80)
        export_path = Path(self.temp.name) / "research.json"
        exported = integrated_research.export_results(
            "json", 30, export_path, self.path)
        self.assertTrue(Path(exported["output"]).exists())
        dashboard = integrated_research.render_dashboard(
            30, Path(self.temp.name) / "dashboard.html", self.path)
        self.assertTrue(Path(dashboard["output"]).exists())
        self.assertEqual(integrated_research.audit(
            path=self.path)["status"], "ok")
        self.assertTrue(
            integrated_research.validate_backup_restore(self.path)["ok"])

    def test_correction_deletion_and_retention_preserve_audit_history(self):
        topic_id = self.build()[0]["integrated_research_topic_id"]
        correction = integrated_research.record_correction(
            topic_id, "訂正後の公式事実", "公式訂正", True, self.path)
        self.assertEqual(correction["status"], "applied")
        deleted = integrated_research.mark_source_deleted(
            "xai_x_search", "100", True, self.path)
        self.assertEqual(deleted["marked_deleted"], 1)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """UPDATE integrated_research_evidence
                   SET created_at='2020-01-01T00:00:00+09:00',
                       is_deleted=0 WHERE provider='xai_x_search'"""
            )
            conn.commit()
        result = integrated_research.retention(30, True, self.path)
        self.assertGreaterEqual(result["redacted"], 1)
        self.assertEqual(self.count("integrated_research_topics"), 1)
        self.assertEqual(self.count("integrated_research_corrections"), 1)

    def test_backfill_is_audit_only_and_never_posts(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """INSERT INTO xai_discovery_runs
                   (run_id,completed_at,status) VALUES ('old-run',?,'success')""",
                (self.now.isoformat(),),
            )
            conn.execute(
                """INSERT INTO xai_discovery_topics
                   (run_id,topic_key,stance_summary_json,
                    counterargument_summary_json,evidence_count,
                    search_confidence,created_at)
                   VALUES ('old-run','過去政策','["意見"]','["反対"]',2,0.7,?)""",
                (self.now.isoformat(),),
            )
            conn.commit()
        preview = integrated_research.backfill(10, False, self.path)
        self.assertEqual(preview["imported"], 0)
        applied = integrated_research.backfill(10, True, self.path)
        self.assertEqual(applied["imported"], 1)
        self.assertEqual(applied["external_posts"], 0)
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                """SELECT post_eligible,decision_reason
                   FROM integrated_research_topics""").fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], "historical_backfill_no_auto_post")


if __name__ == "__main__":
    unittest.main()
