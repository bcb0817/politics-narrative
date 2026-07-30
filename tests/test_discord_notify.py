import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import discord_notify  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=204):
        self.status_code = status_code


class DiscordNotifyTests(unittest.TestCase):
    def test_disabled_does_not_send(self):
        with patch.dict(os.environ, {
            "DISCORD_NOTIFICATIONS_ENABLED": "false",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        }, clear=False), patch("discord_notify.requests.post") as post:
            self.assertFalse(discord_notify.notify("startup", "test"))
            post.assert_not_called()

    def test_success_payload_disables_mentions(self):
        env = {
            "DISCORD_NOTIFICATIONS_ENABLED": "true",
            "DISCORD_NOTIFY_POST_SUCCESS": "true",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "discord_notify.requests.post", return_value=FakeResponse()
        ) as post:
            sent = discord_notify.notify_post_success({
                "tweet_id": "123",
                "tweet_text": "@everyone test",
                "post_type": "issue_diagram",
                "effective_score": 8.1,
            })
        self.assertTrue(sent)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        fields = payload["embeds"][0]["fields"]
        self.assertEqual([field["name"] for field in fields], ["X投稿"])
        self.assertIn("https://x.com/i/web/status/123", fields[0]["value"])

    def test_http_error_is_non_fatal(self):
        env = {
            "DISCORD_NOTIFICATIONS_ENABLED": "true",
            "DISCORD_NOTIFY_ERROR": "true",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "discord_notify.requests.post", return_value=FakeResponse(500)
        ):
            self.assertFalse(discord_notify.notify_error("daemon", "boom"))

    def test_secrets_are_redacted_from_logs(self):
        env = {
            "OPENAI_API_KEY": "sk-" + "test-secret-1234567890",
            "DISCORD_WEBHOOK_URL":
                "https://discord.com/api/" +
                "webhooks/123456789/secret-token-value",
        }
        with patch.dict(os.environ, env, clear=False):
            cleaned = discord_notify.sanitize(
                f"OPENAI_API_KEY={env['OPENAI_API_KEY']} {env['DISCORD_WEBHOOK_URL']}")
        self.assertNotIn("secret-token-value", cleaned)
        self.assertNotIn("sk-test-secret", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_log_source_is_whitelisted(self):
        sent, count = discord_notify.notify_log_excerpt("../.env", force=True)
        self.assertFalse(sent)
        self.assertEqual(count, 0)

    def test_note_draft_uses_dedicated_webhook(self):
        env = {
            "NOTE_DRAFT_DISCORD_ENABLED": "true",
            "NOTE_DRAFT_DISCORD_WEBHOOK_URL": "https://discord.invalid/note-draft",
            "NOTE_DRAFT_DISCORD_WEBHOOK_USERNAME": "note bot",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "discord_notify.requests.post", return_value=FakeResponse()
        ) as post:
            self.assertTrue(discord_notify.notify_note_draft_ready({
                "title": "下書き",
                "summary": "本文要約",
                "status": "draft",
            }))
            self.assertEqual(post.call_args.args[0], env["NOTE_DRAFT_DISCORD_WEBHOOK_URL"])
            self.assertEqual(post.call_args.kwargs["json"]["username"], "note bot")
            self.assertEqual(post.call_args.kwargs["json"]["allowed_mentions"], {"parse": []})

    def test_note_draft_webhook_is_redacted(self):
        secret = (
            "https://discord.com/api/" +
            "webhooks/123456789/note-secret-token"
        )
        with patch.dict(os.environ, {
            "NOTE_DRAFT_DISCORD_WEBHOOK_URL": secret,
        }, clear=False):
            cleaned = discord_notify.sanitize(
                f"NOTE_DRAFT_DISCORD_WEBHOOK_URL={secret}")
            self.assertNotIn(secret, cleaned)

    def test_note_draft_disabled_does_not_send(self):
        with patch.dict(os.environ, {
            "NOTE_DRAFT_DISCORD_ENABLED": "false",
            "NOTE_DRAFT_DISCORD_WEBHOOK_URL": "https://discord.invalid/note-draft",
        }, clear=False), patch("discord_notify.requests.post") as post:
            self.assertFalse(discord_notify.notify_note_draft_ready({"title": "draft"}))
            post.assert_not_called()

    def test_note_draft_reports_only_amazon_result_counts(self):
        rendered = discord_notify._note_summary({
            "title": "下書き",
            "status": "draft",
            "amazon_item_count": 3,
            "amazon_manual_required": 2,
            "amazon_paapi_ready": 1,
            "affiliate_url":
                "https://www.amazon.co.jp/dp/example?tag=secret-22",
            "tracking_id": "secret-22",
        })
        self.assertIn("関連書籍候補:** 3件", rendered)
        self.assertIn("Amazonリンク作成待ち:** 2件", rendered)
        self.assertIn("公式API取得済み:** 1件", rendered)
        self.assertNotIn("https://", rendered)
        self.assertNotIn("secret-22", rendered)

    def test_run_result_contains_only_outcome(self):
        env = {
            "DISCORD_NOTIFICATIONS_ENABLED": "true",
            "DISCORD_NOTIFY_RUN_LOG": "true",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        }
        record = {
            "decision": "skip",
            "reason": "no_news",
            "openai_model": "internal-model",
            "effective_score": 1.2,
            "ban_risk": 3,
            "slot_key": "internal-slot",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "discord_notify.requests.post", return_value=FakeResponse()
        ) as post:
            self.assertTrue(discord_notify.notify_run_log(record))
        embed = post.call_args.kwargs["json"]["embeds"][0]
        rendered = json.dumps(embed, ensure_ascii=False)
        self.assertIn("今回は投稿なし", rendered)
        self.assertIn("投稿価値のあるニュース", rendered)
        self.assertNotIn("internal-model", rendered)
        self.assertNotIn("internal-slot", rendered)
        self.assertNotIn("BANリスク", rendered)
        self.assertNotIn("品質スコア", rendered)

    def test_no_new_attempt_does_not_notify_successful_run(self):
        env = {
            "DISCORD_NOTIFICATIONS_ENABLED": "true",
            "DISCORD_NOTIFY_RUN_LOG": "true",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "attempts.jsonl"
            path.write_text("", encoding="utf-8")
            with patch.dict(os.environ, env, clear=False), patch(
                "discord_notify.requests.post"
            ) as post:
                self.assertFalse(discord_notify.notify_attempt_since(
                    path, 0, exit_code=0))
            post.assert_not_called()

    def test_post_attempt_does_not_duplicate_post_success_notice(self):
        env = {
            "DISCORD_NOTIFICATIONS_ENABLED": "true",
            "DISCORD_NOTIFY_RUN_LOG": "true",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "attempts.jsonl"
            path.write_text(
                json.dumps({"decision": "post", "reason": "success"}) + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, env, clear=False), patch(
                "discord_notify.requests.post"
            ) as post:
                self.assertFalse(discord_notify.notify_attempt_since(
                    path, 0, exit_code=0))
            post.assert_not_called()

    def test_log_command_sends_counts_not_raw_lines(self):
        env = {
            "DISCORD_NOTIFICATIONS_ENABLED": "true",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        }
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            (directory / "bot.log").write_text(
                "[INFO] private detailed diagnostic\n"
                "[WARN] retry detail\n"
                "[ERROR] stack trace detail\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, env, clear=False), patch(
                "discord_notify.requests.post", return_value=FakeResponse()
            ) as post:
                sent, count = discord_notify.notify_log_excerpt(
                    "bot", lines=40, log_dir=directory, force=True)
        self.assertTrue(sent)
        self.assertEqual(count, 3)
        rendered = json.dumps(
            post.call_args.kwargs["json"], ensure_ascii=False)
        self.assertIn("ログ確認結果", rendered)
        self.assertIn("エラーが見つかりました", rendered)
        self.assertNotIn("private detailed diagnostic", rendered)
        self.assertNotIn("stack trace detail", rendered)

    def test_strategy_notification_contains_result_only(self):
        env = {
            "DISCORD_NOTIFICATIONS_ENABLED": "true",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "discord_notify.requests.post", return_value=FakeResponse()
        ) as post:
            sent = discord_notify.notify_review_strategy_result(
                {
                    "activated": True,
                    "reason": "validated_strategy_activated",
                    "policy": {"experiment_name": "number_hook"},
                },
                prior_evaluation={
                    "status": "insufficient_data",
                    "treatment_count": 2,
                    "control_count": 1,
                },
            )
        self.assertTrue(sent)
        rendered = json.dumps(
            post.call_args.kwargs["json"], ensure_ascii=False)
        self.assertIn("ChatGPT投稿方針を更新", rendered)
        self.assertNotIn("raw prompt", rendered)

    def test_threads_research_contains_public_result_and_analysis_only(self):
        env = {
            "DISCORD_NOTIFICATIONS_ENABLED": "true",
            "DISCORD_NOTIFY_THREADS_RESEARCH": "true",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        }
        report = {
            "lookback_hours": 24,
            "search_run_count": 1,
            "result_count": 4,
            "unique_post_count": 3,
            "searches": [{"query": "政治", "result_count": 4}],
            "representative_posts": [{
                "text": "社会保険料をめぐる議論",
                "permalink": "https://www.threads.net/@example/post/1",
                "username_hash": "must-not-be-sent",
            }],
            "top_entities": [{
                "entity": "社会保険料",
                "trend_score": 72.5,
                "state": "rising",
                "post_count": 3,
                "eligible_for_post": True,
            }],
            "eligible_entity_count": 1,
            "access_token": "must-not-be-sent",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "discord_notify.requests.post", return_value=FakeResponse()
        ) as post:
            self.assertTrue(discord_notify.notify_threads_research(report))
        rendered = json.dumps(
            post.call_args.kwargs["json"], ensure_ascii=False)
        self.assertIn("社会保険料をめぐる議論", rendered)
        self.assertIn("72.5点", rendered)
        self.assertIn("公式・報道照合あり", rendered)
        self.assertNotIn("must-not-be-sent", rendered)

    def test_x_research_contains_analysis_but_not_internal_data(self):
        env = {
            "DISCORD_NOTIFICATIONS_ENABLED": "true",
            "DISCORD_NOTIFY_X_RESEARCH": "true",
            "DISCORD_WEBHOOK_URL": "https://discord.invalid/webhook",
        }
        report = {
            "provider": "xAI X Search",
            "lookback_minutes": 360,
            "query_count": 1,
            "resource_count": 1,
            "topic_count": 1,
            "queries": ["社会保険料"],
            "topics": [{
                "topic_key": "社会保険料",
                "attention_score": 8.2,
                "velocity_score": 7.1,
                "main_claims": ["負担が重い"],
                "counter_claims": ["財源も確認すべき"],
                "representative_post_ids": ["1234567890"],
                "externally_corroborated": True,
                "author_id": "must-not-be-sent",
            }],
            "corroborated_topic_count": 1,
            "api_key": "must-not-be-sent",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "discord_notify.requests.post", return_value=FakeResponse()
        ) as post:
            self.assertTrue(discord_notify.notify_x_research(report))
        rendered = json.dumps(
            post.call_args.kwargs["json"], ensure_ascii=False)
        self.assertIn("負担が重い", rendered)
        self.assertIn("財源も確認すべき", rendered)
        self.assertIn("8.2", rendered)
        self.assertIn("https://x.com/i/web/status/1234567890", rendered)
        self.assertNotIn("must-not-be-sent", rendered)


if __name__ == "__main__":
    unittest.main()
