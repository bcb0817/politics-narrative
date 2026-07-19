import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from x_attention import (  # noqa: E402
    aggregate_attention,
    build_search_queries,
    final_news_score,
    match_topics_to_rss,
    post_spam_penalty,
)
import news  # noqa: E402
import post  # noqa: E402


RSS_XML = """<?xml version='1.0' encoding='UTF-8'?>
<rss><channel><item><title>消費税減税法案を国会で審議</title>
<link>https://example.test/article</link><pubDate>Sun, 19 Jul 2026 10:00:00 GMT</pubDate>
<description>政府と各党が税制改正を議論</description></item></channel></rss>""".encode("utf-8")


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return RSS_XML


class XAttentionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)
        self.query = {"label": "消費税", "query": "消費税", "terms": ["消費税"]}

    def _posts(self, authors=("a", "b", "c")):
        return [
            {
                "text": "消費税の法案を国会で審議",
                "author_id": author,
                "author_followers": 100,
                "created_at": self.now - timedelta(minutes=30),
                "likes": 20,
                "reposts": 5,
                "replies": 2,
                "quotes": 1,
            }
            for author in authors
        ]

    def test_x_search_disabled_returns_rss_only(self):
        feeds = [{"name": "信頼できる報道", "url": "https://example.test/rss"}]
        with patch.dict(os.environ, {"X_SEARCH_ENABLED": "false"}, clear=False), \
                patch.object(news, "RSS_FEEDS", feeds), \
                patch.object(news.urllib.request, "urlopen", return_value=FakeResponse()):
            rows = news.fetch_all_items(include_x=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["discovered_via"], ["rss"])

    def test_x_search_failure_falls_back_to_rss(self):
        feeds = [{"name": "信頼できる報道", "url": "https://example.test/rss"}]
        with patch.dict(os.environ, {"X_SEARCH_ENABLED": "true"}, clear=False), \
                patch.object(news, "RSS_FEEDS", feeds), \
                patch.object(news.urllib.request, "urlopen", return_value=FakeResponse()), \
                patch.object(news, "fetch_x_search_topics", side_effect=RuntimeError("rate limited")):
            rows = news.fetch_all_items(include_x=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["x_attention_score"], 0.0)

    def test_attention_changes_final_news_score(self):
        low = final_news_score(7, 8, 0, 9, 0.25)
        high = final_news_score(7, 8, 9, 9, 0.25)
        self.assertGreater(high, low)

    def test_nonpolitical_fresh_item_is_not_forced_into_candidates(self):
        rows = post.prefilter_news([{
            "title": "浴室の換気扇を長持ちさせる方法",
            "summary": "住宅設備の日常的な手入れを紹介",
            "url": "https://example.test/lifestyle",
            "source_name": "Yahoo!ニュース政治",
            "pub_date": "",
            "x_attention_score": 0,
        }], top_n=1)
        self.assertEqual(rows, [])

    def test_single_account_does_not_qualify(self):
        topics = aggregate_attention(
            self._posts(("same", "same", "same")), self.query,
            min_unique_accounts=3, min_post_count=3, now_utc=self.now,
        )
        self.assertEqual(topics, [])

    def test_unverified_x_topic_is_not_a_candidate(self):
        topics = aggregate_attention(self._posts(), self.query, now_utc=self.now)
        self.assertTrue(topics)
        self.assertEqual(match_topics_to_rss([], topics), [])
        violations = post._candidate_quality_violations(
            {"tweet_text": "🚨確認前の話題\n\n📌事実として断定しません。十分な長さの確認文です。"},
            {"title": "未確認情報", "discovered_via": ["x_search"]},
        )
        self.assertIn("unverified_x_claim", violations)

    def test_rss_corroboration_receives_attention_metadata(self):
        topics = aggregate_attention(self._posts(), self.query, now_utc=self.now)
        rss = [{"title": "消費税減税法案を審議", "summary": "消費税を国会で議論"}]
        matched = match_topics_to_rss(rss, topics)
        self.assertEqual(matched[0]["discovered_via"], ["rss", "x_search"])
        self.assertGreater(matched[0]["x_attention_score"], 0)
        self.assertEqual(matched[0]["x_unique_accounts"], 3)

    def test_duplicate_and_engagement_bait_are_penalized(self):
        clean = post_spam_penalty({"text": "政策の論点を整理", "author_followers": 100})
        spam = post_spam_penalty(
            {"text": "拡散希望 リポストお願いします", "author_post_count": 6, "author_followers": 0},
            duplicate_ratio=0.8,
        )
        self.assertLess(spam, clean)

    def test_dynamic_queries_are_limited_to_five(self):
        rows = [{"title": f"『政策会議{i}』を政府が開催"} for i in range(20)]
        queries = build_search_queries(rows, max_queries=99)
        self.assertLessEqual(len(queries), 5)

    def test_x_search_results_are_saved_to_required_paths(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"STATE_DIR": tmp}, clear=False):
            news._save_x_search_results(
                [{"topic_key": "消費税減税", "x_attention_score": 8.4}],
                [{"label": "税制", "dynamic": False}],
            )
            latest = Path(tmp) / "x_search_latest.json"
            history = list((Path(tmp) / "x_search_history").glob("*.jsonl"))
            self.assertTrue(latest.exists())
            self.assertEqual(len(history), 1)
            self.assertEqual(json.loads(latest.read_text(encoding="utf-8"))["topic_count"], 1)

    def test_secret_is_not_written_to_x_error_log(self):
        secret = "secret-bearer-value"

        class FailingTweepy:
            @staticmethod
            def Client(**kwargs):
                raise RuntimeError(f"failed with {kwargs['bearer_token']}")

        output = io.StringIO()
        with patch.dict(os.environ, {
            "X_SEARCH_ENABLED": "true",
            "X_BEARER_TOKEN": secret,
        }, clear=False), patch.dict(sys.modules, {"tweepy": FailingTweepy}), redirect_stdout(output):
            self.assertEqual(news.fetch_x_search_topics([]), [])
        self.assertNotIn(secret, output.getvalue())


if __name__ == "__main__":
    unittest.main()
