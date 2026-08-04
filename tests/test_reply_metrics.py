import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import metrics_db
import post_experiments
import reply_metrics


class FakeReplyClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def search_recent_tweets(self, **kwargs):
        return SimpleNamespace(data=[
            SimpleNamespace(
                id="reply-1", author_id="author-secret",
                conversation_id="post-1",
                created_at="2026-07-29T01:00:00Z",
                text="誰がいくら負担するのですか？",
            ),
        ])


class PaginatedReplyClient:
    calls = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).calls = []

    def search_recent_tweets(self, **kwargs):
        type(self).calls.append(kwargs)
        token = kwargs.get("pagination_token")
        if token is None:
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        id="reply-1", author_id="author-1",
                        created_at="2026-07-29T01:00:00Z", text="返信1"),
                    SimpleNamespace(
                        id="reply-2", author_id="author-2",
                        created_at="2026-07-29T01:01:00Z", text="返信2"),
                ],
                meta={"next_token": "page-2"},
            )
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    id="reply-3", author_id="author-3",
                    created_at="2026-07-29T01:02:00Z", text="返信3"),
            ],
            meta={},
        )


class ReplyMetricTests(unittest.TestCase):
    def test_read_only_collection_hashes_author_and_classifies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.db"
            metrics_db.init_db(path)
            post_experiments.apply_migrations(path)
            metrics_db.write(
                """INSERT INTO published_posts
                   (tweet_id,text,posted_at)
                   VALUES ('post-1','本文','2026-07-29T00:00:00Z')""",
                path=path,
            )
            with patch.dict(os.environ, {"BEARER_TOKEN": "secret"}), \
                    patch.object(reply_metrics, "reserve",
                                 return_value=(1, None)), \
                    patch.object(reply_metrics, "finalize"), \
                    patch.object(reply_metrics, "estimate_x",
                                 return_value=0.01):
                result = reply_metrics.collect_x_replies(
                    path=path, client_factory=FakeReplyClient)
            with closing(post_experiments.connect(path)) as connection:
                row = connection.execute(
                    "SELECT * FROM post_reply_events").fetchone()

        self.assertEqual(result["status"], "working_live")
        self.assertEqual(result["external_writes"], 0)
        self.assertNotEqual(row["author_hash"], "author-secret")
        self.assertEqual(
            row["reply_classification"], "specific_question")

    def test_missing_bearer_token_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.db"
            metrics_db.init_db(path)
            post_experiments.apply_migrations(path)
            metrics_db.write(
                """INSERT INTO published_posts
                   (tweet_id,text,posted_at)
                   VALUES ('post-1','本文','2026-07-29T00:00:00Z')""",
                path=path,
            )
            with patch.dict(os.environ, {"BEARER_TOKEN": ""}):
                result = reply_metrics.collect_x_replies(path=path)
        self.assertEqual(result["status"], "missing_credentials")

    def test_pagination_uses_next_token_and_stops_without_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.db"
            metrics_db.init_db(path)
            post_experiments.apply_migrations(path)
            metrics_db.write(
                """INSERT INTO published_posts
                   (tweet_id,text,posted_at)
                   VALUES ('post-1','本文','2026-07-29T00:00:00Z')""",
                path=path,
            )
            with patch.dict(os.environ, {"BEARER_TOKEN": "secret"}), \
                    patch.object(reply_metrics, "reserve",
                                 return_value=(1, None)), \
                    patch.object(reply_metrics, "finalize"), \
                    patch.object(reply_metrics, "estimate_x",
                                 return_value=0.01):
                result = reply_metrics.collect_x_replies(
                    path=path, replies_per_post=200,
                    client_factory=PaginatedReplyClient)

        self.assertEqual(result["replies_received"], 3)
        self.assertEqual(result["pages_requested"], 2)
        self.assertNotIn(
            "pagination_token", PaginatedReplyClient.calls[0])
        self.assertEqual(
            PaginatedReplyClient.calls[1]["pagination_token"], "page-2")

    def test_pagination_is_bounded_to_five_pages(self):
        class EndlessClient:
            calls = 0

            def __init__(self, **kwargs):
                type(self).calls = 0

            def search_recent_tweets(self, **kwargs):
                type(self).calls += 1
                number = type(self).calls
                return SimpleNamespace(
                    data=[SimpleNamespace(
                        id=f"reply-{number}", author_id=f"author-{number}",
                        created_at="", text="返信")],
                    meta={"next_token": f"page-{number + 1}"},
                )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.db"
            metrics_db.init_db(path)
            post_experiments.apply_migrations(path)
            metrics_db.write(
                """INSERT INTO published_posts
                   (tweet_id,text,posted_at)
                   VALUES ('post-1','本文','2026-07-29T00:00:00Z')""",
                path=path,
            )
            with patch.dict(os.environ, {"BEARER_TOKEN": "secret"}), \
                    patch.object(reply_metrics, "reserve",
                                 return_value=(1, None)), \
                    patch.object(reply_metrics, "finalize"), \
                    patch.object(reply_metrics, "estimate_x",
                                 return_value=0.01):
                result = reply_metrics.collect_x_replies(
                    path=path, replies_per_post=500,
                    max_pages_per_post=99, client_factory=EndlessClient)

        self.assertEqual(result["pages_requested"], 5)
        self.assertEqual(EndlessClient.calls, 5)
        self.assertEqual(result["max_pages_per_post"], 5)


if __name__ == "__main__":
    unittest.main()
