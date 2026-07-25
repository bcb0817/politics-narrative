import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import metrics_db
import threads_api


class FakeThreadsClient:
    def __init__(self):
        self.created = []
        self.published = []
        self.insight_calls = []

    def create_container(self, text):
        self.created.append(text)
        return {"id": "creation-1"}

    def publish_container(self, creation_id):
        self.published.append(creation_id)
        return {"id": "thread-1"}

    def insights(self, post_id):
        self.insight_calls.append(post_id)
        return {"data": [
            {"name": "views", "values": [{"value": 100}]},
            {"name": "likes", "values": [{"value": 10}]},
            {"name": "replies", "values": [{"value": 2}]},
            {"name": "reposts", "values": [{"value": 1}]},
            {"name": "quotes", "values": [{"value": 1}]},
            {"name": "shares", "values": [{"value": 3}]},
        ]}


class ThreadsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "metrics.sqlite3"
        self.env = patch.dict(os.environ, {
            "STATE_DIR": self.tmp.name,
            "THREADS_ENABLED": "true",
            "THREADS_POST_ENABLED": "false",
            "THREADS_ACCESS_TOKEN": "",
            "THREADS_USER_ID": "",
            "THREADS_APP_ID": "",
            "THREADS_APP_SECRET": "",
            "THREADS_REDIRECT_URI": "",
            "THREADS_PUBLIC_BASE_URL": "",
            "THREADS_TOKEN_EXPIRES_AT": "",
            "OPENAI_API_KEY": "",
        }, clear=False)
        self.env.start()
        metrics_db.apply_additive_migrations(self.path)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def source(self):
        return {
            "verified": 1, "source_reliability_score": 9,
            "title": "政府が新制度の実施方針を公表",
            "summary": "対象者と費用負担、実施時期について公式資料が公表されました。",
            "topic_key": "policy-a",
        }

    def add_x_source(self, tweet_id="x1", verified=1, hours_ago=2):
        now = datetime.now(threads_api.JST)
        news_id = metrics_db.write(
            """INSERT INTO news_candidates
               (source_type,source_name,source_url,title,summary,published_at,
                fetched_at,topic_key,source_reliability_score,
                final_news_score,verified)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("official", "政府", "https://example.go.jp/a", self.source()["title"],
             self.source()["summary"], now.isoformat(), now.isoformat(),
             "policy-a", 9, 9, verified), self.path)
        generated_id = metrics_db.write(
            """INSERT INTO generated_posts
               (news_candidate_id,text,quality_score,ban_risk,decision,created_at)
               VALUES (?,?,?,?,?,?)""",
            (news_id, "X向けの短い投稿", 9, 0, "post", now.isoformat()),
            self.path)
        metrics_db.write(
            """INSERT INTO published_posts
               (generated_post_id,tweet_id,text,posted_at,topic_key,post_type)
               VALUES (?,?,?,?,?,?)""",
            (generated_id, tweet_id, "X向けの短い投稿",
             (now - timedelta(hours=hours_ago)).isoformat(),
             "policy-a", "news"), self.path)
        return tweet_id

    # Configuration and safety defaults
    def test_01_enabled_default(self):
        self.assertTrue(threads_api.settings()["enabled"])

    def test_02_posting_default_off(self):
        self.assertFalse(threads_api.settings()["post_enabled"])

    def test_03_platform_limit_is_500(self):
        self.assertEqual(threads_api.settings()["platform_limit"], 500)

    def test_04_platform_limit_is_clamped(self):
        with patch.dict(os.environ, {"THREADS_PLATFORM_LIMIT_CHARS": "999"}):
            self.assertEqual(threads_api.settings()["platform_limit"], 500)

    def test_05_only_initial_scopes_are_accepted(self):
        with patch.dict(os.environ, {
            "THREADS_OAUTH_SCOPES":
                "threads_basic,threads_manage_replies,threads_content_publish"}):
            self.assertEqual(threads_api.settings()["scopes"], (
                "threads_basic", "threads_content_publish"))

    def test_06_auto_reply_is_off(self):
        with patch.dict(os.environ, {"THREADS_AUTO_REPLY_ENABLED": "true"}):
            self.assertFalse(threads_api.settings()["auto_reply_enabled"])

    def test_07_auto_quote_is_off(self):
        self.assertFalse(threads_api.settings()["auto_quote_enabled"])

    def test_08_auto_repost_is_off(self):
        self.assertFalse(threads_api.settings()["auto_repost_enabled"])

    def test_09_auto_follow_is_off(self):
        self.assertFalse(threads_api.settings()["auto_follow_enabled"])

    def test_10_auto_like_is_off(self):
        self.assertFalse(threads_api.settings()["auto_like_enabled"])

    def test_11_copy_x_text_is_off(self):
        with patch.dict(os.environ, {"THREADS_COPY_X_TEXT": "true"}):
            self.assertFalse(threads_api.settings()["copy_x_text"])

    def test_12_daily_max_is_clamped_to_three(self):
        with patch.dict(os.environ, {"THREADS_DAILY_POST_MAX": "20"}):
            self.assertEqual(threads_api.settings()["daily_max"], 3)

    def test_13_default_schedule_has_three_slots(self):
        self.assertEqual(len(threads_api.settings()["schedule"]), 3)

    def test_14_default_interval_is_180(self):
        self.assertEqual(threads_api.settings()["min_interval_minutes"], 180)

    def test_15_default_cooldown_is_eight_hours(self):
        self.assertEqual(threads_api.settings()["topic_cooldown_hours"], 8)

    # Content policy
    def test_16_local_text_is_not_x_copy(self):
        text = threads_api._local_text(self.source())
        self.assertNotEqual(text, "X向けの短い投稿")

    def test_17_local_text_is_within_limit(self):
        self.assertLessEqual(len(threads_api._local_text(self.source())), 450)

    def test_18_question_variant_ends_with_question(self):
        self.assertTrue(threads_api._local_text(
            self.source(), question=True).endswith("？"))

    def test_19_similarity_identical_is_one(self):
        self.assertEqual(threads_api.similarity_to_x("同じ", "同じ"), 1.0)

    def test_20_similarity_different_is_low(self):
        self.assertLess(threads_api.similarity_to_x("政策の説明", "スポーツ"), .5)

    def test_21_unverified_source_is_rejected(self):
        source = self.source()
        source["verified"] = 0
        check = threads_api.quality_check(
            threads_api._local_text(source), source, "別のX文")
        self.assertIn("unverified_source", check["reasons"])

    def test_22_internal_label_is_rejected(self):
        text = threads_api._local_text(self.source()) + " quality_score"
        self.assertIn("internal_label", threads_api.quality_check(
            text, self.source(), "X文")["reasons"])

    def test_23_personal_attack_is_rejected(self):
        text = threads_api._local_text(self.source()) + " 売国奴"
        self.assertIn("personal_attack", threads_api.quality_check(
            text, self.source(), "X文")["reasons"])

    def test_24_excess_emoji_is_rejected(self):
        text = threads_api._local_text(self.source()) + " 🚨🌷"
        self.assertIn("too_many_emoji", threads_api.quality_check(
            text, self.source(), "X文")["reasons"])

    def test_25_excess_hashtags_are_rejected(self):
        text = threads_api._local_text(self.source()) + " #政治 #行政"
        self.assertIn("too_many_hashtags", threads_api.quality_check(
            text, self.source(), "X文")["reasons"])

    # OAuth/token
    def test_26_auth_url_requires_configuration(self):
        with self.assertRaises(ValueError):
            threads_api.authorization_url(self.path)

    def test_27_auth_url_has_only_requested_scopes(self):
        with patch.dict(os.environ, {
            "THREADS_APP_ID": "app",
            "THREADS_REDIRECT_URI":
                "https://localhost/threads/callback",
            "THREADS_PUBLIC_BASE_URL": "https://localhost"}):
            result = threads_api.authorization_url(self.path)
        self.assertEqual(result["requested_scopes"], list(
            threads_api.INITIAL_SCOPES))

    def test_28_token_status_hides_token(self):
        with patch.dict(os.environ, {"THREADS_ACCESS_TOKEN": "secret"}):
            result = threads_api.token_status()
        self.assertNotIn("access_token", result)
        self.assertNotIn("secret", json.dumps(result))

    def test_29_refresh_missing_token_is_safe(self):
        self.assertEqual(
            threads_api.refresh_token(path=self.path)["reason"],
            "token_missing")

    def test_30_refresh_failure_disables_threads_only(self):
        failing = Mock()
        failing.refresh.side_effect = RuntimeError("bad")
        with patch.dict(os.environ, {
            "THREADS_ACCESS_TOKEN": "secret",
            "THREADS_POST_ENABLED": "true"}):
            with patch.object(threads_api, "_update_env") as update:
                result = threads_api.refresh_token(
                    failing, force=True, path=self.path)
        self.assertTrue(result["threads_posting_disabled"])
        self.assertFalse(result["x_bot_affected"])
        update.assert_called_once_with({"THREADS_POST_ENABLED": "false"})

    # Generation
    def test_31_disabled_generation_skips(self):
        with patch.dict(os.environ, {"THREADS_ENABLED": "false"}):
            self.assertEqual(
                threads_api.generate(dry_run=True, path=self.path)["reason"],
                "threads_disabled")

    def test_32_no_source_skips(self):
        self.assertEqual(
            threads_api.generate(dry_run=True, path=self.path)["reason"],
            "no_eligible_verified_source")

    def test_33_unverified_x_source_skips(self):
        self.add_x_source(verified=0)
        self.assertEqual(
            threads_api.generate(dry_run=True, path=self.path)["reason"],
            "no_eligible_verified_source")

    def test_34_too_recent_x_source_skips(self):
        self.add_x_source(hours_ago=0)
        self.assertEqual(
            threads_api.generate(dry_run=True, path=self.path)["reason"],
            "no_eligible_verified_source")

    def test_35_dry_run_makes_zero_api_calls(self):
        self.add_x_source()
        result = threads_api.generate(dry_run=True, path=self.path)
        self.assertEqual(result["threads_api_calls"], 0)
        self.assertFalse(result["threads_published"])
        self.assertEqual(result["x_writes"], 0)

    def test_36_dry_run_saves_source_content_id(self):
        self.add_x_source("123")
        result = threads_api.generate(dry_run=True, path=self.path)
        self.assertEqual(result["source_content_id"], "x-123")

    def test_37_drafts_are_persisted(self):
        self.add_x_source()
        threads_api.generate(dry_run=True, path=self.path)
        self.assertEqual(len(threads_api.drafts(path=self.path)), 1)

    # Publish and two-step API
    def draft(self):
        self.add_x_source()
        return threads_api.generate(dry_run=True, path=self.path)["draft_id"]

    def test_38_posting_off_makes_no_call(self):
        client = FakeThreadsClient()
        result = threads_api.publish(self.draft(), client, self.path)
        self.assertEqual(result["threads_api_calls"], 0)
        self.assertEqual(client.created, [])

    def test_39_credentials_are_required(self):
        with patch.dict(os.environ, {"THREADS_POST_ENABLED": "true"}):
            result = threads_api.publish(self.draft(), path=self.path)
        self.assertEqual(result["reason"], "threads_credentials_missing")

    def test_40_two_step_publish(self):
        client = FakeThreadsClient()
        with patch.dict(os.environ, {
            "THREADS_POST_ENABLED": "true",
            "THREADS_ACCESS_TOKEN": "token",
            "THREADS_USER_ID": "user"}):
            result = threads_api.publish(
                self.draft(), client=client, path=self.path)
        self.assertTrue(result["published"])
        self.assertEqual(result["threads_api_calls"], 2)
        self.assertEqual(client.published, ["creation-1"])

    def test_41_duplicate_is_blocked(self):
        draft_id = self.draft()
        client = FakeThreadsClient()
        with patch.dict(os.environ, {
            "THREADS_POST_ENABLED": "true",
            "THREADS_ACCESS_TOKEN": "token",
            "THREADS_USER_ID": "user"}):
            threads_api.publish(draft_id, client=client, path=self.path)
            result = threads_api.publish(
                draft_id, client=client, path=self.path)
        self.assertEqual(result["reason"], "duplicate_client_post_key")

    def test_42_timeout_becomes_ambiguous(self):
        client = Mock()
        client.create_container.side_effect = threads_api.requests.Timeout()
        with patch.dict(os.environ, {
            "THREADS_POST_ENABLED": "true",
            "THREADS_ACCESS_TOKEN": "token",
            "THREADS_USER_ID": "user"}):
            result = threads_api.publish(
                self.draft(), client=client, path=self.path)
        self.assertEqual(result["status"], "ambiguous")

    def test_43_schedule_mismatch_skips(self):
        now = datetime(2026, 7, 25, 9, 0, tzinfo=threads_api.JST)
        self.assertEqual(
            threads_api.run_scheduled(path=self.path, now=now)["reason"],
            "not_scheduled_slot")

    # Metrics/comparison/reporting
    def test_44_metric_parser_keeps_missing_as_null(self):
        values = threads_api._metric_values({
            "data": [{"name": "views", "values": [{"value": 10}]}]})
        self.assertEqual(values["views"], 10)
        self.assertIsNone(values["likes"])

    def test_45_metrics_require_token(self):
        self.assertEqual(
            threads_api.collect_metrics(path=self.path)["reason"],
            "token_missing")

    def test_46_comparison_needs_three_samples(self):
        result = threads_api.platform_comparison(self.path)
        self.assertEqual(result["decision"], "insufficient_data")

    def test_47_status_shows_posting_off(self):
        result = threads_api.status(self.path)
        self.assertFalse(result["posting_enabled"])
        self.assertFalse(result["automatic_replies"])

    def test_48_schema_has_all_four_tables(self):
        with closing(sqlite3.connect(self.path)) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({
            "threads_generation_runs", "threads_posts",
            "threads_metrics", "threads_token_events",
        }.issubset(names))

    def test_49_official_api_call_count_is_recorded(self):
        response = Mock()
        response.json.return_value = {"id": "me"}
        session = Mock()
        session.request.return_value = response
        client = threads_api.ThreadsClient(session=session, path=self.path)
        client._request("GET", "v1.0/me")
        with closing(metrics_db.connect(self.path)) as conn:
            row = conn.execute(
                """SELECT provider,resource_count,estimated_cost_usd
                   FROM api_usage_events WHERE provider='threads'""").fetchone()
        self.assertEqual((row["provider"], row["resource_count"]), ("threads", 1))
        self.assertEqual(row["estimated_cost_usd"], 0)

    def test_50_get_timeout_retries_once(self):
        response = Mock()
        response.json.return_value = {"id": "me"}
        session = Mock()
        session.request.side_effect = [threads_api.requests.Timeout(), response]
        client = threads_api.ThreadsClient(session=session, path=self.path)
        self.assertEqual(client._request("GET", "v1.0/me")["id"], "me")
        self.assertEqual(session.request.call_count, 2)

    def test_51_post_timeout_is_not_retried(self):
        session = Mock()
        session.request.side_effect = threads_api.requests.Timeout()
        client = threads_api.ThreadsClient(session=session, path=self.path)
        with self.assertRaises(threads_api.requests.Timeout):
            client._request("POST", "v1.0/me/threads")
        self.assertEqual(session.request.call_count, 1)

    def test_52_same_closing_is_not_used_three_times(self):
        for _ in range(2):
            metrics_db.write(
                """INSERT INTO threads_generation_runs
                   (decision,question_included,created_at)
                   VALUES ('generated',1,?)""",
                (datetime.now(threads_api.JST).isoformat(),), self.path)
        self.assertFalse(threads_api._avoid_repeated_closing(True, self.path))

    def test_53_code_exchange_saves_expiry_without_returning_token(self):
        client = Mock()
        client.exchange_code.return_value = {
            "access_token": "sensitive-token", "user_id": "u1",
            "username": "yui", "expires_in": 3600,
        }
        with patch.dict(os.environ, {
            "THREADS_APP_ID": "app", "THREADS_APP_SECRET": "secret",
            "THREADS_REDIRECT_URI": "https://localhost/callback"}):
            with patch.object(threads_api, "_update_env") as update:
                result = threads_api.exchange_code(
                    "code", client=client, path=self.path)
        self.assertTrue(result["token_saved"])
        self.assertNotIn("access_token", result)
        self.assertIn("THREADS_TOKEN_EXPIRES_AT", update.call_args.args[0])

    def test_54_token_is_due_seven_days_before_expiry(self):
        expires = (
            datetime.now(threads_api.JST) + timedelta(days=6)
        ).isoformat()
        with patch.dict(os.environ, {
            "THREADS_ACCESS_TOKEN": "token",
            "THREADS_TOKEN_EXPIRES_AT": expires}):
            self.assertTrue(threads_api.token_status()["refresh_required"])

    def test_55_published_ids_are_saved(self):
        client = FakeThreadsClient()
        with patch.dict(os.environ, {
            "THREADS_POST_ENABLED": "true",
            "THREADS_ACCESS_TOKEN": "token",
            "THREADS_USER_ID": "user"}):
            threads_api.publish(self.draft(), client=client, path=self.path)
        with closing(metrics_db.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT creation_id,threads_post_id FROM threads_posts"
            ).fetchone()
        self.assertEqual((row["creation_id"], row["threads_post_id"]),
                         ("creation-1", "thread-1"))

    def test_56_daily_limit_blocks_fourth_post(self):
        draft_id = self.draft()
        now = datetime.now(threads_api.JST)
        for i in range(3):
            metrics_db.write(
                """INSERT INTO threads_posts
                   (client_post_key,status,published_at,created_at)
                   VALUES (?, 'published', ?, ?)""",
                (f"old-{i}", now.isoformat(), now.isoformat()), self.path)
        with patch.dict(os.environ, {
            "THREADS_POST_ENABLED": "true",
            "THREADS_ACCESS_TOKEN": "token",
            "THREADS_USER_ID": "user"}):
            result = threads_api.publish(
                draft_id, client=FakeThreadsClient(), path=self.path, now=now)
        self.assertEqual(result["reason"], "threads_daily_limit")

    def test_57_topic_cooldown_blocks_candidate(self):
        self.add_x_source()
        now = datetime.now(threads_api.JST)
        metrics_db.write(
            """INSERT INTO threads_posts
               (client_post_key,topic_key,status,published_at,created_at)
               VALUES ('old','policy-a','published',?,?)""",
            (now.isoformat(), now.isoformat()), self.path)
        self.assertEqual(
            threads_api.generate(dry_run=True, path=self.path)["reason"],
            "no_eligible_verified_source")

    def test_58_three_metric_windows_are_unique(self):
        now = datetime.now(threads_api.JST)
        metrics_db.write(
            """INSERT INTO threads_posts
               (client_post_key,threads_post_id,status,published_at,created_at)
               VALUES ('metrics','thread-m','published',?,?)""",
            ((now - timedelta(hours=73)).isoformat(), now.isoformat()),
            self.path)
        with patch.dict(os.environ, {"THREADS_ACCESS_TOKEN": "token"}):
            first = threads_api.collect_metrics(
                FakeThreadsClient(), self.path, now)
            second = threads_api.collect_metrics(
                FakeThreadsClient(), self.path, now)
        self.assertEqual(first["collected"], 3)
        self.assertEqual(second["collected"], 0)

    def test_59_phase_a_run_saves_preview_not_post(self):
        self.add_x_source()
        now = datetime.now(threads_api.JST).replace(
            hour=8, minute=30, second=0, microsecond=0)
        result = threads_api.run_scheduled(path=self.path, now=now)
        self.assertEqual(result["status"], "preview_saved")
        self.assertFalse(result["threads_published"])


if __name__ == "__main__":
    unittest.main()
