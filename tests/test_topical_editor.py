"""Fixed-clock, isolated-DB regression of the live editorial route. No paid requests."""
import copy
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import topical_editor as editor

NOW = datetime(2026, 9, 8, 3, tzinfo=timezone.utc)
SOURCE = ("鉄道会社は10月から通勤定期の申請手続きを変更する。対象はオンラインで申請する利用者で、窓口の申請方法は変わらない。"
          "変更は申請方法に限られ、定期券の運賃は据え置かれる。受付開始は10月1日である。") * 3
TEXT = "通勤定期のオンライン申請が10月から変更に。窓口の申請方法は変わりません。\n\n変わるのは申請手続きで、定期券の運賃は据え置き。オンライン利用者が対象です。"


def item(**kwargs):
    return {"title": "通勤定期の申請手続きが変更", "summary": SOURCE[:160],
            "url": "https://news.web.nhk/newsweb/test", "pub_date": (NOW-timedelta(hours=2)).isoformat(),
            "source_name": "NHK経済", **kwargs}


def draft(text=TEXT, reason="none"):
    return {"text": text, "link_reason": reason, "uncertainty": "",
            "claims": [{"claim": "申請方法が変わり運賃は据え置き", "evidence_quote": "変更は申請方法に限られ、定期券の運賃は据え置かれる。"}]}


def review(**kwargs):
    return {"supported": True, "conditions_preserved": True, "safe": True, "one_message": True,
            "link_necessary": False, "clarity": 8, "usefulness": 8, "factuality": 9, "reason": "確認済み", **kwargs}


class TopicalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/"metrics.db"
        self.env = patch.dict(os.environ, {"TOPICAL_EDITOR_ENABLED": "true", "STATE_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def generate(self, **kwargs):
        return editor.generate(item(), [], now=NOW, path=self.path, client=Mock(), fetch=lambda *a, **k: SOURCE, **kwargs)

    def test_selection_is_broad_and_missing_demand_stays_null(self):
        selected = editor.select([item(title="電池の新技術を研究", summary="科学技術の研究")], [], now=NOW)
        self.assertEqual(selected[0]["genre"], "技術・科学")
        self.assertIsNone(selected[0]["reach_priority"]["features"]["observed_demand"])

    def test_expired_future_unknown_untrusted_and_high_risk_rejected(self):
        for row in [item(pub_date=""), item(pub_date=(NOW+timedelta(hours=1)).isoformat()),
                    item(pub_date=(NOW-timedelta(hours=25)).isoformat()), item(url="https://localhost/a"),
                    item(url="https://news.web.nhk.evil.test/a"), item(title="地震の避難情報")]:
            with self.subTest(row=row):
                self.assertEqual(editor.select([row], [], now=NOW), [])

    def test_duplicates_rejected_before_generation(self):
        self.assertEqual(editor.select([item()], [{"source_url": item()["url"]}], now=NOW), [])
        self.assertEqual(editor.select([item()], [{"title": item()["title"]}], now=NOW), [])

    def test_weighted_length_and_links(self):
        self.assertEqual(editor.weighted_length("あ"*140), 280)
        self.assertEqual(editor.weighted_length("a"*257+"https://example.com/test"), 280)
        with self.assertRaises(ValueError):
            editor.check_draft(draft("あ"*141), SOURCE, item(), True)

    def test_unknown_safety_and_quality_fail_closed(self):
        for change in [{"safe": None}, {"supported": False}, {"clarity": None},
                       {"clarity": float("nan")}, {"clarity": True}, {"factuality": 11}]:
            self.assertIsNone(editor.review_score(review(**change)))

    def test_anchor_must_exist_and_filler_rejected(self):
        bad = draft(); bad["claims"][0]["evidence_quote"] = "資料に存在しない引用です"
        with self.assertRaisesRegex(ValueError, "evidence"):
            editor.check_draft(bad, SOURCE, item(), True)
        with self.assertRaisesRegex(ValueError, "filler"):
            editor.check_draft(draft(TEXT+"どう思いますか"), SOURCE, item(), True)

    def test_links_are_sparse_and_justified_not_forbidden(self):
        text = TEXT+"\n"+item()["url"]
        self.assertEqual(editor.check_draft(draft(text,"action"), SOURCE, item(), True), text)
        with self.assertRaises(ValueError):
            editor.check_draft(draft(text,"action"), SOURCE, item(), False)
        self.assertFalse(editor.link_slot([{"tweet_text":"https://example.com/a"}]))
        self.assertTrue(editor.link_slot([{"tweet_text":"plain"}]*9))
        self.assertFalse(editor.public_text_allowed(text))
        context = {"prompt_version": editor.settings()["version"], "openai_model": editor.MODEL,
                   "link_approved": True, "tweet_text": text, "source_url": item()["url"]}
        self.assertTrue(editor.public_text_allowed(text, context))
        self.assertFalse(editor.public_text_allowed(text+" changed", context))

    def test_two_astra_stages_and_cache_no_rebill(self):
        with patch.object(editor, "call_astra", side_effect=[draft(), review()]) as call:
            first = self.generate()
            again = self.generate()
        self.assertEqual(call.call_count, 2)
        self.assertEqual(first, again)
        self.assertEqual(first[0]["openai_model"], "gpt-6-astra")
        self.assertEqual(first[0]["threads_text"], TEXT)
        self.assertEqual(first[0]["source_snapshot"], SOURCE)

    def test_incomplete_generation_claim_prevents_retry(self):
        with patch.object(editor, "call_astra", side_effect=RuntimeError("unknown")) as call:
            with self.assertRaises(RuntimeError): self.generate()
            self.assertEqual(self.generate(), [])
        self.assertEqual(call.call_count, 1)
        # A failed top-ranked source must not starve all other eligible inventory.
        other = item(url="https://news.web.nhk/newsweb/other", title="通信サービスの料金変更")
        selected = editor.select([item(),other],[],now=NOW,blocked=editor.blocked_keys(self.path))
        self.assertEqual(selected[0]["url"],other["url"])

    def test_new_experiments_do_not_reuse_political_learning(self):
        cfg = editor.experiment_settings()
        self.assertEqual(len(cfg["experiments"]), 2)
        self.assertFalse(cfg["learning"]["enabled"])
        self.assertEqual(cfg["version"], editor.settings()["version"])

    def test_parallel_generation_claims_only_one_topic(self):
        from metrics_db import init_db
        init_db(self.path)
        with patch.object(editor, "call_astra", side_effect=[draft(), review()]) as call:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.generate(), range(2)))
        self.assertEqual(call.call_count, 2)
        self.assertTrue(any(results))

    def test_review_rejection_is_not_replaced_by_fallback(self):
        with patch.object(editor, "call_astra", side_effect=[draft(), review(safe=False)]):
            with self.assertRaisesRegex(ValueError, "review_failed"): self.generate()

    def test_other_writer_refused(self):
        cfg = copy.deepcopy(editor.settings()); cfg["writer_model"] = "gpt-5-mini"
        with self.assertRaisesRegex(ValueError, "astra_required"): self.generate(cfg=cfg)

    def test_sdk_uses_exact_astra_and_durable_budget(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(status="completed", output_text='{"ok":true}',
                                      usage=SimpleNamespace(input_tokens=100, output_tokens=20))
        with patch("api_budget.reserve", return_value=(7,"")) as reserve, patch("api_budget.finalize") as finalize:
            editor.call_astra(client, "test", {}, editor.REVIEW_SCHEMA, cfg=editor.settings(), path=self.path)
        self.assertEqual(client.responses.create.call_args.kwargs["model"], editor.MODEL)
        self.assertEqual(reserve.call_args.args[:3], ("openai", "post_generation", editor.MODEL))
        finalize.assert_called_once()

    def test_budget_denial_never_calls_sdk(self):
        client = Mock()
        with patch("api_budget.reserve", return_value=(None,"budget_guard")):
            with self.assertRaisesRegex(RuntimeError,"budget_guard"):
                editor.call_astra(client,"test",{},editor.REVIEW_SCHEMA,cfg=editor.settings(),path=self.path)
        client.responses.create.assert_not_called()

    def test_lost_response_keeps_reservation(self):
        client = Mock(); client.responses.create.side_effect = TimeoutError()
        with patch("api_budget.reserve",return_value=(7,"")), patch("api_budget.finalize") as finalize:
            with self.assertRaisesRegex(RuntimeError,"no_retry"):
                editor.call_astra(client,"test",{},editor.REVIEW_SCHEMA,cfg=editor.settings(),path=self.path)
        finalize.assert_not_called()

    def test_existing_production_generator_routes_to_astra(self):
        import post
        with patch.object(editor,"generate",return_value=[]) as generate, \
             patch.object(post,"_generate_candidates_existing") as legacy, \
             patch.object(post,"get_jst_now",return_value=(NOW,"fixed")):
            post.generate_candidates(item())
        generate.assert_called_once(); legacy.assert_not_called()
        self.assertEqual(post._load_cached_candidates(item()), [])
        self.assertLess(post.effective_score({"overall":10}, []),0)

    def test_main_reaches_existing_publication_boundary_without_posting(self):
        import post
        with ExitStack() as stack:
            for name, value in {
                "get_jst_now": (NOW,"fixed"), "load_post_history": [], "load_recent_topics": [],
                "find_catch_up_slot": ("12:00","2026-09-08_12:00",NOW,[NOW],[NOW]),
                "_load_json": [], "topic_cooldown_skip_reason": "", "phase_daily_limit_reached": False,
                "cost_forecast": {"restriction_level":0}, "insert_news": 1,
            }.items():
                stack.enter_context(patch.object(post,name,return_value=value))
            stack.enter_context(patch.object(post,"POST_ENABLED",False))
            stack.enter_context(patch.object(post,"record_local_event"))
            stack.enter_context(patch("candidate_inventory.available",return_value=[item()]))
            stack.enter_context(patch.object(editor,"call_astra",side_effect=[draft(),review()]))
            stack.enter_context(patch("openai.OpenAI"))
            stack.enter_context(patch("article_content.fetch_article_text",return_value=SOURCE))
            send = stack.enter_context(patch.object(post,"post_to_x"))
            attempts = stack.enter_context(patch.object(post,"log_attempt"))
            stack.enter_context(patch.object(post,"log"))
            post.main()
        send.assert_not_called()
        self.assertEqual(attempts.call_args.args[0]["reason"], "post_disabled")

    def test_forced_publication_is_refused(self):
        import post
        with patch.dict(os.environ,{"FORCE_POST":"true"}), patch.object(post,"gather_candidate_news") as gather:
            post.main()
        gather.assert_not_called()


if __name__ == "__main__": unittest.main()
