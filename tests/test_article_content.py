import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from article_content import (  # noqa: E402
    enrich_article,
    extract_article_text,
    fetch_article_text,
    safe_public_url,
)


class Response:
    headers = {"Content-Type": "text/html; charset=utf-8"}
    content = b"<article><p>Government policy details are confirmed.</p></article>"
    text = content.decode()

    def raise_for_status(self):
        return None


class ArticleContentTests(unittest.TestCase):
    def test_extracts_article_paragraphs_not_navigation(self):
        text = extract_article_text(
            "<nav><p>navigation should be ignored</p></nav>"
            "<article><h1>政策案</h1><p>政府は制度案を公表しました。</p>"
            "<p>対象と施行日は資料に記載されています。</p></article>")
        self.assertNotIn("navigation", text)
        self.assertIn("政府は制度案を公表しました", text)

    def test_rejects_private_and_credential_urls(self):
        self.assertFalse(safe_public_url("http://127.0.0.1/private"))
        self.assertFalse(safe_public_url("http://user:pass@example.com/"))
        self.assertTrue(safe_public_url("https://example.com/policy"))

    def test_fetch_cache_prevents_second_request(self):
        calls = []

        def get(*args, **kwargs):
            calls.append(args[0])
            return Response()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = fetch_article_text(
                "https://example.com/a", root=root, request_get=get)
            second = fetch_article_text(
                "https://example.com/a", root=root,
                request_get=lambda *a, **k: self.fail("cache miss"))
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)

    def test_social_context_is_bounded_and_marks_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            value = enrich_article({
                "url": "https://example.com/a",
                "article_text": "本文",
                "social_posts": [{
                    "platform": "x", "text": "話題の論点",
                    "url": "https://x.com/example/status/1",
                    "verified": False,
                }],
            }, root=Path(directory))
        self.assertEqual(value["article_text"], "本文")
        self.assertFalse(value["social_context"][0]["verified"])


if __name__ == "__main__":
    unittest.main()
