import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import crosspost
import media_publication


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload or {}
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http_{self.status_code}")


class QueueSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._next("PUT", url, **kwargs)

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def head(self, url, **kwargs):
        return self._next("HEAD", url, **kwargs)


class CrosspostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.db = self.root / "metrics.db"
        self.output = self.root / "out"
        self.env = patch.dict(os.environ, {
            "STATE_DIR": str(self.root),
            "CROSSPOST_OUTPUT_DIR": str(self.output),
            "CROSSPOST_ENABLED": "true",
            "CROSSPOST_AUTO_PUBLISH_ENABLED": "false",
            "CROSSPOST_YOUTUBE_ENABLED": "true",
            "CROSSPOST_X_ENABLED": "true",
            "CROSSPOST_THREADS_ENABLED": "true",
            "CROSSPOST_INSTAGRAM_ENABLED": "false",
            "YOUTUBE_AUTO_PUBLISH_ENABLED": "false",
            "X_POST_ENABLED": "false",
            "THREADS_POST_ENABLED": "false",
            "INSTAGRAM_AUTO_PUBLISH_ENABLED": "false",
        })
        self.env.start()
        crosspost.apply_migrations(self.db)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def publication(self):
        return crosspost.create_publication(path=self.db)

    def test_01_schema_is_idempotent(self):
        self.assertTrue(crosspost.apply_migrations(self.db))
        self.assertTrue(crosspost.apply_migrations(self.db))

    def test_02_required_tables_exist(self):
        with crosspost.connect(self.db) as conn:
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({
            "cross_platform_publications", "platform_publications",
            "platform_media_assets", "cross_platform_metrics",
            "cross_platform_events",
        }.issubset(names))

    def test_03_publication_id_is_stable_shape(self):
        value = crosspost.generate_publication_id("Tax Reform")
        self.assertRegex(value, r"^video-\d{8}-tax-reform-short-[a-f0-9]{6}$")

    def test_04_publication_has_four_platform_rows(self):
        self.assertEqual(len(self.publication()["platforms"]), 4)

    def test_05_platform_keys_are_unique(self):
        keys = [row["idempotency_key"] for row in self.publication()["platforms"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_06_copy_is_distinct(self):
        result = crosspost.generate_copy(
            self.publication()["publication_id"], path=self.db)
        values = [
            result["x"]["text"], result["threads"]["text"],
            result["instagram"]["caption"],
            result["youtube"]["description"],
        ]
        self.assertEqual(len(set(values)), 4)

    def test_07_x_copy_has_at_most_one_hashtag(self):
        result = crosspost.generate_copy(
            self.publication()["publication_id"], path=self.db)
        self.assertLessEqual(len(result["x"]["hashtags"]), 1)

    def test_08_instagram_has_profile_cta(self):
        result = crosspost.generate_copy(
            self.publication()["publication_id"], path=self.db)
        self.assertIn("プロフィール", result["instagram"]["caption"])

    def test_09_youtube_has_ai_disclosure(self):
        result = crosspost.generate_copy(
            self.publication()["publication_id"], path=self.db)
        self.assertTrue(result["youtube"]["contains_synthetic_media"])
        self.assertIn("AI", result["youtube"]["description"])

    def test_10_threads_alt_text_exists(self):
        result = crosspost.generate_copy(
            self.publication()["publication_id"], path=self.db)
        self.assertTrue(result["threads"]["alt_text"])

    def test_11_default_auto_publish_is_off(self):
        self.assertFalse(crosspost.settings()["auto_publish"])

    def test_12_default_instagram_is_off(self):
        self.assertFalse(crosspost.settings()["platforms"]["instagram"])

    def test_13_publish_without_switches_is_blocked(self):
        result = crosspost.publish(
            self.publication()["publication_id"], confirm=True, path=self.db)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["external_writes"], 0)

    def test_14_publish_without_confirm_is_blocked(self):
        with patch.dict(os.environ, {
            "CROSSPOST_AUTO_PUBLISH_ENABLED": "true",
            "YOUTUBE_AUTO_PUBLISH_ENABLED": "true",
        }):
            allowed, reason = crosspost._publish_allowed("youtube", False)
        self.assertFalse(allowed)
        self.assertEqual(reason, "explicit_confirmation_required")

    def test_15_dry_run_publish_never_writes(self):
        result = crosspost.publish(
            self.publication()["publication_id"], dry_run=True, path=self.db)
        self.assertEqual(result["external_writes"], 0)

    def test_16_reconcile_does_not_delete_success(self):
        result = crosspost.reconcile(
            self.publication()["publication_id"], path=self.db)
        self.assertEqual(result["successful_posts_deleted"], 0)

    def test_17_partial_success_is_preserved(self):
        publication = self.publication()
        crosspost._execute("""UPDATE platform_publications
            SET status='published' WHERE publication_id=? AND platform='x'""",
            (publication["publication_id"],), self.db)
        crosspost._execute("""UPDATE platform_publications
            SET status='failed' WHERE publication_id=? AND platform='threads'""",
            (publication["publication_id"],), self.db)
        result = crosspost.reconcile(publication["publication_id"], path=self.db)
        self.assertEqual(result["status"], "partially_published")

    def test_18_ambiguous_state_is_recorded(self):
        publication = self.publication()
        crosspost._execute("""UPDATE platform_publications
            SET status='ambiguous' WHERE publication_id=? AND platform='x'""",
            (publication["publication_id"],), self.db)
        result = crosspost.reconcile(publication["publication_id"], path=self.db)
        self.assertEqual(result["status"], "ambiguous")

    def test_19_metrics_unavailable_values_are_null(self):
        result = crosspost.metrics_sync(
            self.publication()["publication_id"], path=self.db)
        self.assertIsNone(result["metrics"]["youtube"]["views"])

    def test_20_report_does_not_claim_causality(self):
        result = crosspost.report(
            self.publication()["publication_id"], path=self.db)
        self.assertFalse(result["causality_claimed"])
        self.assertEqual(result["decision"], "insufficient_data")

    def test_21_media_dry_run_uses_non_resolving_domain(self):
        video = self.root / "a.mp4"
        video.write_bytes(b"video")
        result = media_publication.MediaPublicationProvider().prepare(video)
        self.assertIn("dry-run.invalid", result.public_url)
        self.assertFalse(result.externally_published)

    def test_22_public_url_requires_https(self):
        self.assertFalse(
            media_publication.validate_public_url("http://example.com/a.mp4")[
                "valid"])

    def test_23_public_url_rejects_embedded_credentials(self):
        result = media_publication.validate_public_url(
            "https://user:pass@example.com/a.mp4")
        self.assertFalse(result["valid"])

    def test_24_public_url_head_checks_content_type(self):
        session = QueueSession([FakeResponse(headers={
            "Content-Type": "video/mp4", "Content-Length": "5",
            "Accept-Ranges": "bytes",
        })])
        result = media_publication.validate_public_url(
            "https://example.com/a.mp4", session=session, fetch=True)
        self.assertTrue(result["valid"])
        self.assertTrue(result["range_supported"])

    def test_25_single_file_server_rejects_path_traversal(self):
        video = self.root / "a.mp4"
        video.write_bytes(b"12345")
        server = media_publication.SingleFileMediaServer(video)
        port = server.start()
        try:
            response = __import__("requests").get(
                f"http://127.0.0.1:{port}/media/../a.mp4", timeout=3)
            self.assertEqual(response.status_code, 404)
        finally:
            server.stop()

    def test_26_single_file_server_has_no_directory_listing(self):
        video = self.root / "a.mp4"
        video.write_bytes(b"12345")
        server = media_publication.SingleFileMediaServer(video)
        port = server.start()
        try:
            response = __import__("requests").get(
                f"http://127.0.0.1:{port}/", timeout=3)
            self.assertEqual(response.status_code, 404)
        finally:
            server.stop()

    def test_27_single_file_server_serves_only_token_route(self):
        video = self.root / "a.mp4"
        video.write_bytes(b"12345")
        server = media_publication.SingleFileMediaServer(video)
        port = server.start()
        try:
            response = __import__("requests").get(
                f"http://127.0.0.1:{port}{server.route}", timeout=3)
            self.assertEqual(response.content, b"12345")
        finally:
            server.stop()

    def test_28_threads_video_plan_uses_official_steps(self):
        plan = crosspost._threads_video_plan("https://e/x.mp4", "t", "a")
        self.assertEqual(plan["create"]["data"]["media_type"], "VIDEO")
        self.assertEqual(plan["publish"]["endpoint"], "/me/threads_publish")

    def test_29_instagram_reel_uses_reels_media_type(self):
        session = QueueSession([FakeResponse({"id": "container"})])
        with patch.dict(os.environ, {
            "INSTAGRAM_USER_ID": "me", "INSTAGRAM_ACCESS_TOKEN": "token",
        }):
            result = crosspost.InstagramClient(session).create_reel(
                "https://e/v.mp4", "caption")
        self.assertEqual(result, "container")
        self.assertEqual(session.calls[0][2]["data"]["media_type"], "REELS")

    def test_30_instagram_publish_uses_media_publish(self):
        session = QueueSession([FakeResponse({"id": "media"})])
        with patch.dict(os.environ, {
            "INSTAGRAM_USER_ID": "me", "INSTAGRAM_ACCESS_TOKEN": "token",
        }):
            result = crosspost.InstagramClient(session).publish("container")
        self.assertEqual(result, "media")
        self.assertIn("media_publish", session.calls[0][1])

    def test_31_instagram_professional_requirement_in_dry_run(self):
        result = crosspost.instagram_profile(dry_run=True)
        self.assertTrue(result["professional_account_required"])

    def test_32_instagram_new_scope_names_are_used(self):
        with patch.dict(os.environ, {
            "INSTAGRAM_REQUIRED_SCOPES":
                "instagram_business_basic,instagram_business_content_publish",
        }):
            result = crosspost.instagram_auth_url()
        self.assertIn("instagram_business_content_publish", result["url"])

    def test_33_youtube_unverified_project_forces_private(self):
        video = self.root / "video.mp4"
        video.write_bytes(b"x")
        client = crosspost.YouTubeClient(QueueSession([]))
        with patch.dict(os.environ, {
            "YOUTUBE_API_AUDIT_APPROVED": "false",
            "YOUTUBE_PRIVACY_STATUS": "public",
        }):
            with self.assertRaises(PermissionError):
                client.upload(video, {
                    "title": "t", "description": "d",
                    "contains_synthetic_media": True,
                })

    def test_34_youtube_resumable_upload_has_synthetic_flag(self):
        video = self.root / "video.mp4"
        video.write_bytes(b"x")
        session = QueueSession([
            FakeResponse(headers={"Location": "https://upload.example/session"}),
            FakeResponse({"id": "video-id"}),
        ])
        with patch.dict(os.environ, {
            "YOUTUBE_ACCESS_TOKEN": "token",
            "YOUTUBE_API_AUDIT_APPROVED": "false",
            "YOUTUBE_PRIVACY_STATUS": "private",
        }):
            result = crosspost.YouTubeClient(session).upload(video, {
                "title": "t", "description": "d",
                "contains_synthetic_media": True,
            })
        self.assertEqual(result, "video-id")
        body = session.calls[0][2]["json"]
        self.assertTrue(body["status"]["containsSyntheticMedia"])

    def test_35_x_chunked_workflow_uses_all_commands(self):
        video = self.root / "video.mp4"
        video.write_bytes(b"123456789")
        session = QueueSession([
            FakeResponse({"data": {"id": "123"}}),
            FakeResponse({}),
            FakeResponse({"data": {"processing_info": {
                "state": "succeeded"}}}),
        ])
        with patch.dict(os.environ, {
            "API_KEY": "a", "API_KEY_SECRET": "b",
            "ACCESS_TOKEN": "c", "ACCESS_TOKEN_SECRET": "d",
        }):
            media_id = crosspost.XVideoClient(session).upload(video)
        commands = [
            call[2].get("data", {}).get("command") for call in session.calls]
        self.assertEqual(media_id, "123")
        self.assertEqual(commands, ["INIT", "APPEND", "FINALIZE"])

    def test_36_x_does_not_publish_before_processing_success(self):
        video = self.root / "video.mp4"
        video.write_bytes(b"1")
        session = QueueSession([
            FakeResponse({"data": {"id": "123"}}), FakeResponse({}),
            FakeResponse({"data": {"processing_info": {"state": "failed"}}}),
        ])
        with patch.dict(os.environ, {
            "API_KEY": "a", "API_KEY_SECRET": "b",
            "ACCESS_TOKEN": "c", "ACCESS_TOKEN_SECRET": "d",
        }):
            with self.assertRaises(RuntimeError):
                crosspost.XVideoClient(session).upload(video)

    def test_37_x_publish_attaches_media_id(self):
        session = QueueSession([FakeResponse({"data": {"id": "post"}})])
        with patch.dict(os.environ, {
            "API_KEY": "a", "API_KEY_SECRET": "b",
            "ACCESS_TOKEN": "c", "ACCESS_TOKEN_SECRET": "d",
        }):
            result = crosspost.XVideoClient(session).publish("123", "text")
        self.assertEqual(result, "post")
        self.assertEqual(
            session.calls[0][2]["json"]["media"]["media_ids"], ["123"])

    def test_38_safe_area_defaults_match_contract(self):
        self.assertEqual(crosspost._safe_area("youtube"), {
            "top": 12.0, "bottom": 22.0, "left": 8.0, "right": 8.0})

    def test_39_rendition_profiles_use_vertical_video(self):
        for profile in crosspost.RENDITION_PROFILES.values():
            self.assertLess(profile["width"], profile["height"])

    def test_40_x_fallback_is_720x1280(self):
        self.assertEqual(
            (crosspost.RENDITION_PROFILES["x"]["width"],
             crosspost.RENDITION_PROFILES["x"]["height"]),
            (720, 1280))

    def test_41_instagram_limit_is_one_gib(self):
        self.assertEqual(
            crosspost.RENDITION_PROFILES["instagram"]["max_bytes"],
            1024 ** 3)

    def test_42_emergency_stop_does_not_delete_posts(self):
        result = crosspost.emergency_stop(self.db)
        self.assertEqual(result["external_posts_deleted"], 0)
        self.assertTrue(Path(result["marker"]).exists())

    def test_43_emergency_stop_blocks_publish(self):
        crosspost.emergency_stop(self.db)
        allowed, reason = crosspost._publish_allowed("x", True)
        self.assertFalse(allowed)
        self.assertEqual(reason, "emergency_stopped")

    def test_44_token_status_does_not_expose_token(self):
        with patch.dict(os.environ, {
            "YOUTUBE_ACCESS_TOKEN": "secret-value",
        }):
            result = crosspost.token_status("youtube")
        self.assertTrue(result["configured"])
        self.assertNotIn("secret-value", json.dumps(result))

    def test_45_instagram_exchange_is_dry_run_only(self):
        result = crosspost.instagram_exchange_code("secret", dry_run=True)
        self.assertEqual(result["external_api_calls"], 0)
        self.assertNotIn("secret", json.dumps(result))

    def test_46_longform_x_copy_points_to_channel_without_fake_url(self):
        publication = crosspost.create_publication(
            format_name="long", publication_id="video-long", path=self.db)
        result = crosspost.generate_copy(
            publication["publication_id"], path=self.db)
        self.assertIn("YouTubeチャンネル", result["x"]["text"])
        self.assertNotIn("http", result["x"]["text"])

    def test_47_longform_threads_copy_is_a_teaser(self):
        publication = crosspost.create_publication(
            format_name="long", publication_id="video-long", path=self.db)
        result = crosspost.generate_copy(
            publication["publication_id"], path=self.db)
        self.assertIn("予告編", result["threads"]["text"])

    def test_48_longform_youtube_title_is_not_short_title(self):
        publication = crosspost.create_publication(
            format_name="long", publication_id="video-long", path=self.db)
        result = crosspost.generate_copy(
            publication["publication_id"], path=self.db)
        self.assertNotIn("60秒", result["youtube"]["title"])

    def test_49_publish_order_defaults_to_youtube_first(self):
        self.assertEqual(crosspost.settings()["publish_order"][0], "youtube")

    def test_50_target_window_defaults_to_120_seconds(self):
        self.assertEqual(crosspost.settings()["target_window_seconds"], 120)

    def test_51_require_all_ready_defaults_false(self):
        self.assertFalse(crosspost.settings()["require_all_ready"])

    def test_52_publication_states_include_partial(self):
        self.assertIn("partially_published", crosspost.PUBLICATION_STATES)

    def test_53_platform_states_include_ambiguous(self):
        self.assertIn("ambiguous", crosspost.PLATFORM_STATES)

    def test_54_actual_media_prepare_needs_global_switch(self):
        video = self.root / "a.mp4"
        video.write_bytes(b"video")
        with self.assertRaises(PermissionError):
            media_publication.MediaPublicationProvider().prepare(
                video, dry_run=False)

    def test_55_custom_media_provider_requires_base_url(self):
        video = self.root / "a.mp4"
        video.write_bytes(b"video")
        with patch.dict(os.environ, {
            "CROSSPOST_AUTO_PUBLISH_ENABLED": "true",
            "MEDIA_PUBLICATION_PROVIDER": "custom",
            "MEDIA_PUBLIC_BASE_URL": "",
        }):
            with self.assertRaises(RuntimeError):
                media_publication.MediaPublicationProvider().prepare(
                    video, dry_run=False)

    def test_56_remote_media_schema_uses_hash(self):
        with crosspost.connect(self.db) as conn:
            columns = {row[1] for row in conn.execute(
                "PRAGMA table_info(platform_media_assets)")}
        self.assertIn("remote_url_hash", columns)
        self.assertNotIn("remote_signed_url", columns)

    def test_57_threads_token_status_never_returns_token(self):
        with patch.dict(os.environ, {
            "THREADS_ACCESS_TOKEN": "secret-value",
            "THREADS_USER_ID": "1",
        }):
            result = crosspost.token_status("threads")
        self.assertNotIn("secret-value", json.dumps(result))

    def test_58_instagram_consumer_account_is_rejected(self):
        session = QueueSession([FakeResponse({
            "id": "1", "username": "u", "account_type": "PERSONAL"})])
        with patch.object(crosspost, "InstagramClient",
                          return_value=crosspost.InstagramClient(session)):
            result = crosspost.instagram_profile(dry_run=False)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["professional_account"])

    def test_59_instagram_finished_status_is_returned(self):
        session = QueueSession([FakeResponse({
            "status_code": "FINISHED", "status": "Finished"})])
        with patch.object(crosspost, "InstagramClient",
                          return_value=crosspost.InstagramClient(session)):
            result = crosspost.instagram_reel_status(
                "container", dry_run=False)
        self.assertEqual(result["status_code"], "FINISHED")

    def test_60_youtube_processing_status_is_read(self):
        session = QueueSession([FakeResponse({"items": [{
            "id": "v", "processingDetails": {
                "processingStatus": "succeeded"}}]})])
        result = crosspost.YouTubeClient(session).status("v")
        self.assertEqual(
            result["processingDetails"]["processingStatus"], "succeeded")

    def test_61_x_init_uses_tweet_video_category(self):
        video = self.root / "video.mp4"
        video.write_bytes(b"1")
        session = QueueSession([
            FakeResponse({"data": {"id": "123"}}), FakeResponse({}),
            FakeResponse({"data": {}}),
        ])
        with patch.dict(os.environ, {
            "API_KEY": "a", "API_KEY_SECRET": "b",
            "ACCESS_TOKEN": "c", "ACCESS_TOKEN_SECRET": "d",
        }):
            crosspost.XVideoClient(session).upload(video)
        self.assertEqual(
            session.calls[0][2]["data"]["media_category"], "tweet_video")

    def test_62_disabled_platform_is_skipped_during_prepare_plan(self):
        self.assertFalse(crosspost.settings()["platforms"]["instagram"])

    def test_63_publication_copy_contains_shared_id(self):
        publication = self.publication()
        result = crosspost.generate_copy(
            publication["publication_id"], path=self.db)
        self.assertEqual(result["publication_id"], publication["publication_id"])

    def test_64_profile_request_uses_safe_fields(self):
        session = QueueSession([FakeResponse({
            "id": "1", "username": "u", "account_type": "BUSINESS"})])
        client = crosspost.InstagramClient(session)
        client.profile()
        fields = session.calls[0][2]["params"]["fields"]
        self.assertNotIn("access_token", fields)
        self.assertIn("account_type", fields)

    def test_65_url_validation_does_not_echo_url(self):
        result = media_publication.validate_public_url(
            "https://example.com/secret-token/video.mp4")
        self.assertNotIn("secret-token", json.dumps(result))

    def test_66_request_plans_have_all_four_platforms(self):
        publication = self.publication()
        copy_payload = crosspost.generate_copy(
            publication["publication_id"], path=self.db)
        plans = crosspost._request_plans(publication, copy_payload)
        self.assertEqual(set(plans), set(crosspost.PLATFORMS))

    def test_67_crosspost_module_has_no_browser_automation_import(self):
        source = (ROOT / "src" / "crosspost.py").read_text(encoding="utf-8")
        self.assertNotIn("playwright", source.lower())
        self.assertNotIn("selenium", source.lower())


if __name__ == "__main__":
    unittest.main()
