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
            "INTEGRATED_RESEARCH_MAX_POST_CANDIDATES_PER_RUN": "1",
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


if __name__ == "__main__":
    unittest.main()
