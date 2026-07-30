import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import post  # noqa: E402


class PoliticsPostIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.news = {
            "title": "政府が法案を提出",
            "summary": "政府は制度改正法案を国会へ提出した。",
            "url": "https://example.test/bill",
            "source_name": "官公庁",
        }

    def test_fallback_reduces_candidates_then_uses_minimum_mode(self):
        calls = []

        def staged(news, history, **kwargs):
            calls.append((kwargs.get("candidate_count"),
                          kwargs.get("minimum_mode")))
            if len(calls) < 3:
                raise ValueError("stage failed")
            return [{"tweet_text": "最低限候補"}]

        with patch.dict(os.environ, {
            "POLITICS_ENABLE_MULTI_STAGE_GENERATION": "true",
            "POLITICS_FALLBACK_TO_LEGACY": "true",
            "POLITICS_MAX_API_CALLS_PER_ARTICLE": "12",
        }, clear=False), patch.object(
            post, "_multistage_candidate", side_effect=staged
        ), patch.object(post, "load_post_history", return_value=[]):
            result = post.generate_candidates(self.news)
        self.assertEqual(result[0]["tweet_text"], "最低限候補")
        self.assertEqual(calls, [(None, False), (3, False), (1, True)])

    def test_legacy_is_last_fallback(self):
        def failed(news, history, **kwargs):
            kwargs["call_budget"]["count"] += 1
            raise ValueError("failed")

        with patch.dict(os.environ, {
            "POLITICS_ENABLE_MULTI_STAGE_GENERATION": "true",
            "POLITICS_FALLBACK_TO_LEGACY": "true",
            "POLITICS_MAX_API_CALLS_PER_ARTICLE": "12",
        }, clear=False), patch.object(
            post, "_multistage_candidate", side_effect=failed
        ), patch.object(
            post, "_generate_candidates_legacy",
            return_value=[{"tweet_text": "legacy"}],
        ) as legacy, patch.object(post, "load_post_history", return_value=[]):
            result = post.generate_candidates(self.news)
        self.assertEqual(result[0]["tweet_text"], "legacy")
        legacy.assert_called_once()

    def test_api_limit_skips_without_legacy_overrun(self):
        def consumes_budget(news, history, **kwargs):
            budget = kwargs["call_budget"]
            budget["count"] = budget["max"]
            raise RuntimeError("politics_api_call_limit")

        with patch.dict(os.environ, {
            "POLITICS_ENABLE_MULTI_STAGE_GENERATION": "true",
            "POLITICS_FALLBACK_TO_LEGACY": "true",
            "POLITICS_MAX_API_CALLS_PER_ARTICLE": "2",
        }, clear=False), patch.object(
            post, "_multistage_candidate", side_effect=consumes_budget
        ), patch.object(
            post, "_generate_candidates_legacy",
        ) as legacy, patch.object(post, "load_post_history", return_value=[]):
            result = post.generate_candidates(self.news)
        self.assertEqual(result, [])
        legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
