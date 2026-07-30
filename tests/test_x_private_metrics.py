import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / ".venv" / "Lib" / "site-packages"
sys.path[:0] = [str(SITE), str(ROOT / "src"), str(ROOT)]

import metrics_db
import post_metrics


JST = ZoneInfo("Asia/Tokyo")


class PublicOnlyClient:
    last_fields = []

    def __init__(self, **kwargs):
        pass

    def get_tweets(self, ids, **kwargs):
        type(self).last_fields = list(kwargs.get("tweet_fields") or [])
        return SimpleNamespace(data=[
            SimpleNamespace(
                id=tweet_id,
                public_metrics={
                    "like_count": 0,
                    "retweet_count": 0,
                    "reply_count": 0,
                    "quote_count": 0,
                    "bookmark_count": 0,
                },
            )
            for tweet_id in ids
        ])


class PrivateZeroClient:
    def __init__(self, **kwargs):
        pass

    def get_tweets(self, ids, **kwargs):
        return SimpleNamespace(data=[
            SimpleNamespace(
                id=tweet_id,
                public_metrics={
                    "like_count": 0,
                    "retweet_count": 0,
                    "reply_count": 0,
                    "quote_count": 0,
                    "bookmark_count": 0,
                },
                non_public_metrics={
                    "impression_count": 0,
                    "user_profile_clicks": 0,
                    "url_link_clicks": 0,
                },
                organic_metrics={},
            )
            for tweet_id in ids
        ])


class XPrivateMetricsTests(unittest.TestCase):
    def _collect(self, path, client):
        now = datetime.now(JST)
        history = [{
            "tweet_id": "tweet-1",
            "posted_at_jst": (now - timedelta(hours=2)).isoformat(),
        }]
        with patch.dict(os.environ, {
            "POST_METRICS_ENABLED": "true",
            "X_OWNED_READ_MAX_PER_DAY": "24",
        }):
            result = post_metrics.collect(history, now, client, path)
        with closing(metrics_db.connect(path)) as connection:
            rows = [
                dict(row) for row in connection.execute(
                    "SELECT * FROM post_metrics ORDER BY measurement_window"
                )
            ]
        return result, rows

    def test_public_only_response_preserves_missing_private_metrics_as_null(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            result, rows = self._collect(
                Path(directory) / "metrics.db", PublicOnlyClient
            )

        self.assertEqual(result["collected"], 2)
        self.assertIn("non_public_metrics", PublicOnlyClient.last_fields)
        self.assertIn("organic_metrics", PublicOnlyClient.last_fields)
        self.assertTrue(rows)
        for row in rows:
            self.assertIsNone(row["impressions"])
            self.assertIsNone(row["profile_clicks"])
            self.assertIsNone(row["url_clicks"])
            self.assertEqual(row["public_metrics_available"], 1)
            self.assertEqual(row["private_metrics_available"], 0)
            self.assertIsNone(row["profile_clicks_source"])

    def test_private_measured_zero_is_distinct_from_missing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            result, rows = self._collect(
                Path(directory) / "metrics.db", PrivateZeroClient
            )

        self.assertEqual(result["collected"], 2)
        for row in rows:
            self.assertEqual(row["impressions"], 0)
            self.assertEqual(row["profile_clicks"], 0)
            self.assertEqual(row["url_clicks"], 0)
            self.assertEqual(row["private_metrics_available"], 1)
            self.assertEqual(
                row["profile_clicks_source"], "non_public_metrics"
            )
            fields = json.loads(row["metric_fields_json"])
            self.assertIn(
                "user_profile_clicks", fields["non_public_metrics"]
            )

    def test_legacy_private_metric_zeros_are_migrated_to_null(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "legacy.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("""CREATE TABLE post_metrics (
                    id INTEGER PRIMARY KEY,
                    tweet_id TEXT,
                    measurement_window TEXT,
                    measured_at TEXT,
                    impressions INTEGER,
                    likes INTEGER,
                    reposts INTEGER,
                    replies INTEGER,
                    quotes INTEGER,
                    bookmarks INTEGER,
                    profile_clicks INTEGER,
                    url_clicks INTEGER,
                    engagement_rate REAL,
                    impressions_per_hour REAL,
                    UNIQUE(tweet_id, measurement_window)
                )""")
                connection.execute("""INSERT INTO post_metrics (
                    tweet_id, measurement_window, impressions,
                    profile_clicks, url_clicks
                ) VALUES ('legacy-1', '24h', 123, 0, 0)""")
                connection.commit()

            self.assertTrue(metrics_db.init_db(path))
            with closing(metrics_db.connect(path)) as connection:
                row = connection.execute(
                    "SELECT * FROM post_metrics WHERE tweet_id='legacy-1'"
                ).fetchone()

        self.assertIsNone(row["profile_clicks"])
        self.assertIsNone(row["url_clicks"])
        self.assertEqual(row["profile_clicks_source"], "legacy_not_requested")
        self.assertEqual(row["private_metrics_available"], 0)
        self.assertEqual(row["impressions_source"], "legacy_public_metrics")


if __name__ == "__main__":
    unittest.main()
