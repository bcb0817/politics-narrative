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
import threads_full_api as full


ALL_SCOPES = ",".join(threads_api.KNOWN_SCOPES)


class FakeFullClient:
    def __init__(self):
        self.calls = []

    def profile(self):
        self.calls.append("profile")
        return {
            "id": "u1", "username": "yui", "name": "久世ゆい",
            "is_verified": False, "threads_biography": "政治ニュース",
        }

    def own_posts(self, **kwargs):
        self.calls.append(("posts", kwargs))
        return [{
            "id": "p1", "text": "政策ニュース", "timestamp": "2026-07-25T01:00:00Z",
            "owner": {"id": "u1"}, "media_type": "CAROUSEL",
            "children": {"data": [
                {"id": "c1", "media_type": "IMAGE"},
                {"id": "c2", "media_type": "VIDEO"},
            ]},
            "poll_attachment": {"option_a": "A", "option_b": "B"},
        }]

    def replies(self, post_id, **kwargs):
        self.calls.append(("replies", post_id))
        return [{
            "id": "r1", "text": "なぜですか？", "username": "reader",
            "timestamp": "2026-07-25T02:00:00Z",
            "root_post": {"id": post_id}, "is_reply_owned_by_me": False,
        }]

    def own_replies(self, **kwargs):
        self.calls.append("own_replies")
        return []

    def mentions(self, **kwargs):
        self.calls.append("mentions")
        return [{
            "id": "m1", "text": "@yui 確認してください",
            "username": "reader", "timestamp": "2026-07-25T02:00:00Z",
        }]

    def insights(self, post_id):
        self.calls.append(("insights", post_id))
        return {"data": [
            {"name": "views", "values": [{"value": 100}]},
            {"name": "likes", "values": [{"value": 10}]},
            {"name": "replies", "values": [{"value": 2}]},
        ]}

    def account_insights(self, metrics, breakdown=""):
        self.calls.append(("account", metrics, breakdown))
        if breakdown:
            return {"data": [{
                "name": "follower_demographics",
                "total_value": {"breakdowns": [{
                    "dimension_keys": [breakdown],
                    "results": [{"dimension_values": ["JP"], "value": 5}],
                }]},
            }]}
        return {"data": [{
            "name": "views", "period": "day",
            "values": [{"value": 20, "end_time": "2026-07-25T00:00:00Z"}],
        }]}

    def keyword_search(self, *args, **kwargs):
        self.calls.append(("search", args, kwargs))
        return [{
            "id": "s1", "text": "社会保険料の議論",
            "username": "a", "timestamp": "2026-07-25T01:00:00Z",
            "media_type": "TEXT_POST", "has_replies": True,
            "is_verified": True,
        }]

    def publishing_limit(self):
        self.calls.append("quota")
        return {"data": [{
            "quota_usage": 225,
            "config": {"quota_total": 250, "quota_duration": 86400},
            "reply_quota_usage": 10,
            "reply_config": {"quota_total": 1000, "quota_duration": 86400},
        }]}

    def container_status(self, container_id):
        return {"id": container_id, "status": "FINISHED"}

    def create_container(self, text, **options):
        self.calls.append(("create", text, options))
        return {"id": "container-1"}

    def publish_container(self, creation_id):
        self.calls.append(("publish", creation_id))
        return {"id": "published-1"}

    def repost(self, post_id):
        self.calls.append(("repost", post_id))
        return {"id": "repost-1"}

    def delete(self, post_id):
        self.calls.append(("delete", post_id))
        return {"success": True, "deleted_id": post_id}

    def manage_reply(self, reply_id, hide):
        self.calls.append(("manage", reply_id, hide))
        return {"success": True}

    def location(self, location_id):
        self.calls.append(("location", location_id))
        return {"id": location_id, "name": "国会議事堂", "country": "JP"}


class ThreadsFullApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "metrics.sqlite3"
        self.env = patch.dict(os.environ, {
            "STATE_DIR": self.tmp.name,
            "THREADS_ENABLED": "true",
            "THREADS_POST_ENABLED": "false",
            "THREADS_ACCESS_TOKEN": "token",
            "THREADS_USER_ID": "u1",
            "THREADS_OAUTH_SCOPES": ALL_SCOPES,
            "THREADS_PROFILE_SYNC_ENABLED": "true",
            "THREADS_SYNC_POSTS_ENABLED": "true",
            "THREADS_SYNC_REPLIES_ENABLED": "true",
            "THREADS_SYNC_MENTIONS_ENABLED": "true",
            "THREADS_POST_INSIGHTS_ENABLED": "true",
            "THREADS_ACCOUNT_INSIGHTS_ENABLED": "true",
            "THREADS_QUOTA_SYNC_ENABLED": "true",
            "THREADS_KEYWORD_SEARCH_ENABLED": "true",
            "THREADS_TOPIC_TAG_SEARCH_ENABLED": "true",
            "THREADS_TEXT_POST_ENABLED": "true",
            "THREADS_IMAGE_POST_ENABLED": "false",
            "THREADS_VIDEO_POST_ENABLED": "false",
            "THREADS_CAROUSEL_POST_ENABLED": "false",
            "THREADS_POLL_POST_ENABLED": "false",
            "THREADS_GHOST_POST_ENABLED": "false",
            "THREADS_LOCATION_TAGGING_ENABLED": "false",
        }, clear=False)
        self.env.start()
        metrics_db.apply_threads_full_migrations(self.path)
        self.client = FakeFullClient()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def table_names(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}

    def add_post(self, post_id="p1", hours=200):
        now = datetime.now(threads_api.JST)
        metrics_db.write("""INSERT INTO threads_posts
          (client_post_key,threads_post_id,threads_user_id,text,status,
           published_at,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?)""", (
            "key-" + post_id, post_id, "u1", "本文", "published",
            (now - timedelta(hours=hours)).isoformat(),
            now.isoformat(), now.isoformat(),
        ), self.path)

    def test_61_schema_has_all_required_tables(self):
        expected = {
            "threads_profiles", "threads_post_children",
            "threads_post_insights", "threads_account_insights",
            "threads_follower_demographics", "threads_replies",
            "threads_mentions", "threads_search_queries",
            "threads_search_results", "threads_search_result_matches",
            "threads_trend_snapshots", "threads_trend_entities",
            "threads_reply_analyses", "threads_action_drafts",
            "threads_action_events", "threads_api_calls",
            "threads_api_quotas", "threads_containers",
            "threads_locations", "threads_poll_snapshots",
            "threads_sync_cursors", "threads_daily_reports",
            "threads_weekly_reports",
        }
        self.assertTrue(expected.issubset(self.table_names()))

    def test_62_migration_is_idempotent(self):
        self.assertTrue(metrics_db.apply_threads_full_migrations(self.path))

    def test_63_permissions_hide_token(self):
        result = full.permissions()
        self.assertNotIn("access_token", result)
        self.assertNotIn('"token"', json.dumps(result))

    def test_64_permissions_show_all_known_scopes(self):
        self.assertEqual(
            len(full.permissions()["permissions"]), len(threads_api.KNOWN_SCOPES))

    def test_65_permission_missing_is_reported(self):
        with patch.dict(os.environ, {
            "THREADS_OAUTH_SCOPES": "threads_basic"}):
            self.assertIn(
                "threads_keyword_search", full.permissions()["missing"])

    def test_66_profile_dry_run_has_no_call(self):
        result = full.profile_sync(self.client, dry_run=True, path=self.path)
        self.assertEqual((result["api_calls"], self.client.calls), (0, []))

    def test_67_profile_sync_saves_profile(self):
        full.profile_sync(self.client, path=self.path)
        with closing(metrics_db.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT username FROM threads_profiles").fetchone()[0],
                "yui")

    def test_68_post_sync_saves_post(self):
        result = full.sync_posts(self.client, path=self.path)
        self.assertEqual(result["saved"], 1)

    def test_69_post_sync_saves_children(self):
        result = full.sync_posts(self.client, path=self.path)
        self.assertEqual(result["children_saved"], 2)

    def test_70_post_sync_saves_poll(self):
        result = full.sync_posts(self.client, path=self.path)
        self.assertEqual(result["poll_snapshots_saved"], 1)

    def test_71_post_sync_is_duplicate_safe(self):
        full.sync_posts(self.client, path=self.path)
        full.sync_posts(self.client, path=self.path)
        with closing(metrics_db.connect(self.path)) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM threads_posts").fetchone()[0],
                1)

    def test_72_reply_dry_run_has_no_call(self):
        self.assertEqual(
            full.sync_replies(self.client, dry_run=True, path=self.path)["api_calls"],
            0)

    def test_73_reply_sync_saves_tree(self):
        self.add_post()
        result = full.sync_replies(self.client, path=self.path)
        self.assertEqual(result["saved"], 1)

    def test_74_reply_permission_missing_skips_without_call(self):
        with patch.dict(os.environ, {
            "THREADS_OAUTH_SCOPES": "threads_basic"}):
            result = full.sync_replies(self.client, path=self.path)
        self.assertEqual(result["reason"], "permission_missing")
        self.assertEqual(self.client.calls, [])

    def test_75_mention_sync_saves(self):
        self.assertEqual(
            full.sync_mentions(self.client, path=self.path)["saved"], 1)

    def test_76_post_insight_zero_denominator_is_null(self):
        self.add_post()
        self.client.insights = lambda _post: {
            "data": [{"name": "views", "values": [{"value": 0}]}]}
        full.collect_post_insights(self.client, path=self.path)
        with closing(metrics_db.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT engagement_rate FROM threads_post_insights").fetchone()
        self.assertIsNone(row[0])

    def test_77_post_insights_save_required_and_optional_windows(self):
        self.add_post()
        result = full.collect_post_insights(self.client, path=self.path)
        self.assertEqual(result["saved"], 6)
        self.assertEqual(result["api_calls"], 1)
        self.assertEqual(
            self.client.calls.count(("insights", "p1")), 1)

    def test_77a_post_insights_complete_post_makes_no_api_call(self):
        self.add_post()
        first = full.collect_post_insights(self.client, path=self.path)
        self.client.calls.clear()
        second = full.collect_post_insights(self.client, path=self.path)
        self.assertEqual(first["saved"], 6)
        self.assertEqual(second, {
            "status": "completed", "api_calls": 0, "saved": 0,
            "skipped": 1, "failed": 0,
        })
        self.assertEqual(self.client.calls, [])

    def test_77b_post_insights_reuses_payload_for_missing_due_windows(self):
        self.add_post()
        measured = datetime.now(threads_api.JST).isoformat()
        metrics_db.write("""INSERT INTO threads_post_insights
          (threads_post_id,measurement_window,measured_at,created_at,updated_at)
          VALUES (?,?,?,?,?)""", ("p1", "15m", measured, measured, measured),
                         self.path)
        result = full.collect_post_insights(self.client, path=self.path)
        self.assertEqual(
            (result["api_calls"], result["saved"], result["failed"]),
            (1, 5, 0))
        self.assertEqual(
            self.client.calls.count(("insights", "p1")), 1)

    def test_77c_post_insights_preserves_missing_metrics_as_null(self):
        self.add_post()
        self.client.insights = lambda _post: {
            "data": [{"name": "views", "values": [{"value": 100}]}]}
        full.collect_post_insights(self.client, path=self.path)
        with closing(metrics_db.connect(self.path)) as conn:
            row = conn.execute(
                """SELECT likes,replies,reposts,quotes,shares
                   FROM threads_post_insights LIMIT 1""").fetchone()
        self.assertEqual(tuple(row), (None, None, None, None, None))

    def test_78_account_insights_save(self):
        result = full.collect_account_insights(self.client, path=self.path)
        self.assertGreaterEqual(result["account_metrics_saved"], 1)

    def test_79_demographics_save(self):
        result = full.collect_account_insights(self.client, path=self.path)
        self.assertEqual(result["demographics_saved"], 4)

    def test_80_invalid_search_type_is_rejected(self):
        self.assertEqual(
            full.search("政治", search_type="BAD", path=self.path)["status"],
            "rejected")

    def test_81_search_dry_run_has_no_call(self):
        result = full.search(
            "政治", dry_run=True, client=self.client, path=self.path)
        self.assertEqual((result["api_calls"], self.client.calls), (0, []))

    def test_82_search_result_is_saved(self):
        result = full.search("政治", client=self.client, path=self.path)
        self.assertEqual(result["result_count"], 1)

    def test_83_search_cache_avoids_second_call(self):
        full.search("政治", client=self.client, path=self.path)
        result = full.search("政治", client=self.client, path=self.path)
        self.assertEqual(result["status"], "cached")

    def test_84_tag_search_uses_tag_mode(self):
        full.search(
            "選挙", search_mode="TAG", client=self.client, path=self.path)
        self.assertEqual(self.client.calls[0][2]["search_mode"], "TAG")

    def test_84b_search_period_is_part_of_cache_key(self):
        full.search(
            "政治", since="2026-07-20T00:00:00Z",
            client=self.client, path=self.path)
        result = full.search(
            "政治", since="2026-07-21T00:00:00Z",
            client=self.client, path=self.path)
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(self.client.calls), 2)

    def test_84c_empty_result_is_not_api_failure_and_is_cached(self):
        client = FakeFullClient()
        client.keyword_search = Mock(return_value=[])
        first = full.search("該当なし", client=client, path=self.path)
        second = full.search("該当なし", client=client, path=self.path)
        self.assertEqual(first["status"], "empty")
        self.assertEqual(first["result_count"], 0)
        self.assertEqual(second["status"], "cached")
        self.assertEqual(second["cached_result_status"], "empty")

    def test_84d_api_failure_is_persisted_separately(self):
        client = FakeFullClient()
        client.keyword_search = Mock(side_effect=RuntimeError("offline"))
        result = full.search("障害", client=client, path=self.path)
        self.assertEqual(result["status"], "failing")
        self.assertIsNone(result["result_count"])
        with closing(metrics_db.connect(self.path)) as connection:
            row = connection.execute("""SELECT status,error_class,result_count,
              live_or_cached FROM threads_search_queries
              ORDER BY id DESC LIMIT 1""").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_class"], "RuntimeError")
        self.assertIsNone(row["result_count"])
        self.assertEqual(row["live_or_cached"], "live")

    def test_84e_missing_token_is_distinct_from_missing_scope(self):
        with patch.dict(os.environ, {"THREADS_ACCESS_TOKEN": ""}):
            result = full.search("政治", client=self.client, path=self.path)
        self.assertEqual(result["status"], "missing_credentials")

    def test_84f_pagination_metadata_counts_pages(self):
        client = threads_api.ThreadsClient(path=self.path)
        payloads = iter([
            {
                "data": [{"id": "1"}],
                "paging": {"cursors": {"after": "next-1"}},
            },
            {"data": [{"id": "2"}], "paging": {"cursors": {}}},
        ])
        client._request = Mock(side_effect=lambda *args, **kwargs: next(payloads))
        result = client.keyword_search("政治", return_metadata=True)
        self.assertEqual(result["page_count"], 2)
        self.assertEqual([row["id"] for row in result["data"]], ["1", "2"])
        self.assertFalse(result["pagination_truncated"])

    def test_85_trend_without_sample_is_insufficient(self):
        self.assertEqual(
            full.trends(path=self.path)["status"], "insufficient_data")

    def test_86_trend_is_local_relative_not_global(self):
        now = datetime.now(threads_api.JST).isoformat()
        for index in range(3):
            metrics_db.write("""INSERT INTO threads_search_results
              (threads_post_id,username_hash,text,timestamp,first_seen_at,
               last_seen_at,created_at,updated_at,source)
              VALUES (?,?,?,?,?,?,?,?,?)""", (
                f"s{index}", f"u{index}", "社会保険料 政策",
                now, now, now, now, now, "test"), self.path)
        result = full.trends(path=self.path)
        self.assertTrue(result["not_official_global_ranking"])

    def test_86b_new_search_sends_one_discord_research_summary(self):
        client = FakeFullClient()
        client.keyword_search = Mock(return_value=[
            {
                "id": "r1", "text": "社会保険料 改革の議論",
                "username": "a", "timestamp": datetime.now(
                    threads_api.JST).isoformat(),
                "permalink": "https://www.threads.net/@a/post/r1",
                "media_type": "TEXT_POST", "is_verified": True,
            },
            {
                "id": "r2", "text": "社会保険料 改革への意見",
                "username": "b", "timestamp": datetime.now(
                    threads_api.JST).isoformat(),
                "permalink": "https://www.threads.net/@b/post/r2",
                "media_type": "TEXT_POST",
            },
        ])
        full.search("社会保険料", client=client, path=self.path)
        with patch(
            "discord_notify.notify_threads_research", return_value=True
        ) as notify:
            first = full.trends(path=self.path)
            second = full.trends(path=self.path)
        self.assertTrue(first["discord_sent"])
        self.assertFalse(second["discord_sent"])
        notify.assert_called_once()
        report = notify.call_args.args[0]
        self.assertEqual(report["search_run_count"], 1)
        self.assertEqual(report["unique_post_count"], 2)
        self.assertEqual(
            report["representative_posts"][0]["text"],
            "社会保険料 改革の議論",
        )

    def test_86c_threads_research_dry_run_does_not_notify(self):
        full.search("政治", client=self.client, path=self.path)
        with patch("discord_notify.notify_threads_research") as notify:
            result = full.trends(dry_run=True, path=self.path)
        self.assertFalse(result["discord_sent"])
        notify.assert_not_called()

    def test_86d_empty_research_result_is_not_notified_twice(self):
        client = FakeFullClient()
        client.keyword_search = Mock(return_value=[])
        full.search("該当なし", client=client, path=self.path)
        with patch(
            "discord_notify.notify_threads_research", return_value=True
        ) as notify:
            first = full.trends(path=self.path)
            second = full.trends(path=self.path)
        self.assertTrue(first["discord_sent"])
        self.assertFalse(second["discord_sent"])
        notify.assert_called_once()

    def test_87_reply_analysis_does_not_treat_opposition_as_spam(self):
        result = full.analyze_reply("r1", "この政策には反対です", self.path)
        self.assertEqual(result["spam_score"], 0)

    def test_88_low_confidence_requires_review(self):
        result = full.analyze_reply("r2", "読みました", self.path)
        self.assertTrue(result["review_required"])

    def test_89_quota_90_percent_stops_writes(self):
        result = full.quota_status(self.client, path=self.path)
        self.assertFalse(result["external_writes_allowed"])

    def test_90_container_status_accepts_official_state(self):
        result = full.container_status("c1", self.client, self.path)
        self.assertEqual(result["status"], "FINISHED")

    def test_91_image_requires_public_https(self):
        result = full.validate_format({
            "media_type": "IMAGE", "image_url": "C:\\local.jpg"})
        self.assertIn("public_https_image_url_required", result["errors"])

    def test_92_carousel_requires_two_children(self):
        result = full.validate_format({
            "media_type": "CAROUSEL", "children": ["one"]})
        self.assertIn("carousel_requires_2_to_20_children", result["errors"])

    def test_93_poll_and_link_are_exclusive(self):
        result = full.validate_format({
            "media_type": "TEXT", "text": "test",
            "poll_attachment": {"option_a": "a", "option_b": "b"},
            "link_attachment": "https://example.com"})
        self.assertIn("poll_and_link_are_mutually_exclusive", result["errors"])

    def test_94_reply_draft_makes_no_external_write(self):
        result = full.reply_draft("r1", path=self.path)
        self.assertEqual(result["external_writes"], 0)

    def test_95_action_without_confirm_is_blocked(self):
        draft = full.reply_draft("r1", path=self.path)
        result = full.apply_action(
            draft["draft_id"], client=self.client, path=self.path)
        self.assertEqual(result["reason"], "confirm_required")

    def test_96_confirm_still_requires_feature_flag(self):
        draft = full.reply_draft("r1", path=self.path)
        with patch.dict(os.environ, {"THREADS_POST_ENABLED": "true"}):
            result = full.apply_action(
                draft["draft_id"], confirm=True,
                client=self.client, path=self.path)
        self.assertEqual(result["reason"], "action_disabled")

    def test_97_confirm_and_flags_publish_reply_in_two_steps(self):
        draft = full.reply_draft("r1", path=self.path)
        with patch.dict(os.environ, {
            "THREADS_POST_ENABLED": "true",
            "THREADS_AUTO_REPLY_ENABLED": "true"}):
            result = full.apply_action(
                draft["draft_id"], confirm=True,
                client=self.client, path=self.path)
        self.assertEqual((result["status"], result["external_writes"]),
                         ("success", 2))

    def test_98_ordinary_criticism_is_not_hide_candidate(self):
        metrics_db.write("""INSERT INTO threads_replies
          (reply_id,text,created_at,updated_at) VALUES ('r1','反対です',?,?)""",
                         ("now", "now"), self.path)
        result = full.moderation_draft("r1", "hide", self.path)
        self.assertEqual(result["status"], "rejected")

    def test_99_delete_rejects_non_owned_post(self):
        result = full.direct_action_draft(
            "delete", "other", reason="test", path=self.path)
        self.assertEqual(result["reason"], "not_owned_local_post")

    def test_100_user_delete_requires_confirm(self):
        result = full.user_data_delete("u1", path=self.path)
        self.assertEqual(result["reason"], "confirm_required")

    def test_101_retention_keeps_recent_data(self):
        now = datetime.now(threads_api.JST).isoformat()
        metrics_db.write("""INSERT INTO threads_mentions
          (mention_id,timestamp,created_at,updated_at)
          VALUES ('m1',?,?,?)""", (now, now, now), self.path)
        result = full.data_retention_run(self.path)
        self.assertEqual(result["deleted"]["threads_mentions"], 0)

    def test_102_full_sync_dry_run_has_no_external_actions(self):
        result = full.full_sync(dry_run=True, path=self.path)
        self.assertEqual(result["external_write_actions"], 0)


if __name__ == "__main__":
    unittest.main()
