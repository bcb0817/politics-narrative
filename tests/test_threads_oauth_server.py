import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import metrics_db
import threads_api
import threads_oauth_server


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def signed_request(user_id="user-1", secret="app-secret",
                   algorithm="HMAC-SHA256") -> str:
    payload = b64url(json.dumps({
        "algorithm": algorithm, "user_id": user_id,
    }, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(
        secret.encode("utf-8"), payload.encode("ascii"),
        hashlib.sha256).digest()
    return f"{b64url(signature)}.{payload}"


class ThreadsOAuthServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "metrics.sqlite3"
        self.env = patch.dict(os.environ, {
            "STATE_DIR": self.tmp.name,
            "THREADS_APP_ID": "app-id",
            "THREADS_APP_SECRET": "app-secret",
            "THREADS_REDIRECT_URI":
                "https://bot.example/threads/callback",
            "THREADS_PUBLIC_BASE_URL": "https://bot.example",
            "THREADS_USER_ID": "user-1",
            "THREADS_USERNAME": "yui",
            "THREADS_ACCESS_TOKEN": "access-secret",
            "THREADS_TOKEN_EXPIRES_AT": "2026-12-01T00:00:00+09:00",
            "THREADS_POST_ENABLED": "true",
            "THREADS_CALLBACK_TRUST_PROXY": "false",
        }, clear=False)
        self.env.start()
        metrics_db.apply_additive_migrations(self.path)
        self.app = threads_oauth_server.create_app(
            path=self.path, require_https=False)
        self.client = self.app.test_client()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def add_post(self):
        metrics_db.write(
            """INSERT INTO threads_posts
               (client_post_key,threads_user_id,status,created_at,updated_at)
               VALUES ('key-1','user-1','published','now','now')""",
            path=self.path)

    def test_01_signed_request_is_verified(self):
        result = threads_oauth_server.verify_signed_request(
            signed_request(), "app-secret")
        self.assertEqual(result["user_id"], "user-1")

    def test_02_bad_signature_is_rejected(self):
        with self.assertRaises(ValueError):
            threads_oauth_server.verify_signed_request(
                signed_request(secret="wrong"), "app-secret")

    def test_03_wrong_algorithm_is_rejected(self):
        with self.assertRaises(ValueError):
            threads_oauth_server.verify_signed_request(
                signed_request(algorithm="HMAC-SHA1"), "app-secret")

    def test_04_missing_user_is_rejected(self):
        with self.assertRaises(ValueError):
            threads_oauth_server.verify_signed_request(
                signed_request(user_id=""), "app-secret")

    def test_05_state_is_stored_as_hash_only(self):
        state = threads_api.create_oauth_state(self.path)
        with closing(metrics_db.connect(self.path)) as conn:
            row = conn.execute(
                "SELECT state_hash FROM threads_oauth_states").fetchone()
        self.assertNotEqual(row["state_hash"], state)
        self.assertEqual(row["state_hash"], hashlib.sha256(
            state.encode()).hexdigest())

    def test_06_state_is_one_time(self):
        state = threads_api.create_oauth_state(self.path)
        self.assertTrue(threads_api.consume_oauth_state(state, self.path))
        self.assertFalse(threads_api.consume_oauth_state(state, self.path))

    def test_07_callback_requires_code_and_state(self):
        response = self.client.get("/threads/callback")
        self.assertEqual(response.status_code, 400)

    def test_08_callback_rejects_unknown_state(self):
        response = self.client.get(
            "/threads/callback?code=code-secret&state=wrong")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(b"code-secret", response.data)

    def test_09_callback_exchanges_code_after_state_check(self):
        state = threads_api.create_oauth_state(self.path)
        with patch.object(threads_api, "exchange_code") as exchange:
            response = self.client.get(
                f"/threads/callback?code=code-secret&state={state}")
        self.assertEqual(response.status_code, 200)
        exchange.assert_called_once_with("code-secret", path=self.path)
        self.assertNotIn(b"code-secret", response.data)

    def test_10_callback_exchange_failure_is_generic(self):
        state = threads_api.create_oauth_state(self.path)
        with patch.object(
                threads_api, "exchange_code",
                side_effect=RuntimeError("contains-secret")):
            response = self.client.get(
                f"/threads/callback?code=code-secret&state={state}")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(b"contains-secret", response.data)

    def test_11_deauthorize_rejects_unsigned_request(self):
        response = self.client.post(
            "/threads/deauthorize", data={"signed_request": "invalid"})
        self.assertEqual(response.status_code, 400)

    def test_12_deauthorize_stops_posting_and_clears_credentials(self):
        with patch.object(threads_api, "_update_env") as update:
            response = self.client.post("/threads/deauthorize", data={
                "signed_request": signed_request()})
        self.assertEqual(response.status_code, 200)
        updates = [call.args[0] for call in update.call_args_list]
        self.assertTrue(any(
            item.get("THREADS_POST_ENABLED") == "false"
            for item in updates))
        self.assertTrue(any(
            item.get("THREADS_ACCESS_TOKEN") == ""
            for item in updates))
        self.assertNotIn(b"access-secret", response.data)

    def test_13_deauthorize_does_not_detach_history(self):
        self.add_post()
        with patch.object(threads_api, "_update_env"):
            self.client.post("/threads/deauthorize", data={
                "signed_request": signed_request()})
        with closing(metrics_db.connect(self.path)) as conn:
            user = conn.execute(
                "SELECT threads_user_id FROM threads_posts").fetchone()[0]
        self.assertEqual(user, "user-1")

    def test_14_data_deletion_detaches_post_history(self):
        self.add_post()
        with patch.object(threads_api, "_update_env"):
            response = self.client.post("/threads/data-deletion", data={
                "signed_request": signed_request()})
        self.assertEqual(response.status_code, 200)
        with closing(metrics_db.connect(self.path)) as conn:
            user = conn.execute(
                "SELECT threads_user_id FROM threads_posts").fetchone()[0]
        self.assertIsNone(user)

    def test_15_data_deletion_returns_confirmation(self):
        with patch.object(threads_api, "_update_env"):
            response = self.client.post("/threads/data-deletion", data={
                "signed_request": signed_request()})
        payload = response.get_json()
        self.assertRegex(payload["confirmation_code"], r"^[a-f0-9]{32}$")
        self.assertTrue(payload["url"].startswith(
            "https://bot.example/threads/data-deletion?code="))

    def test_16_confirmation_code_is_stored_as_hash(self):
        with patch.object(threads_api, "_update_env"):
            response = self.client.post("/threads/data-deletion", data={
                "signed_request": signed_request()})
        code = response.get_json()["confirmation_code"]
        with closing(metrics_db.connect(self.path)) as conn:
            stored = conn.execute(
                "SELECT confirmation_hash FROM threads_deletion_receipts"
            ).fetchone()[0]
        self.assertNotEqual(stored, code)
        self.assertEqual(stored, hashlib.sha256(code.encode()).hexdigest())

    def test_17_deletion_status_works(self):
        with patch.object(threads_api, "_update_env"):
            response = self.client.post("/threads/data-deletion", data={
                "signed_request": signed_request()})
        code = response.get_json()["confirmation_code"]
        status = self.client.get(
            f"/threads/data-deletion?code={code}")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["status"], "completed")

    def test_18_unknown_deletion_status_is_404(self):
        response = self.client.get(
            "/threads/data-deletion?code=unknown")
        self.assertEqual(response.status_code, 404)

    def test_19_https_is_required_by_default(self):
        app = threads_oauth_server.create_app(path=self.path)
        response = app.test_client().get("/threads/callback")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "https_required")

    def test_20_proxy_https_is_accepted_only_when_trusted(self):
        with patch.dict(os.environ, {
            "THREADS_CALLBACK_TRUST_PROXY": "true"}):
            app = threads_oauth_server.create_app(path=self.path)
            response = app.test_client().get(
                "/threads/callback",
                headers={"X-Forwarded-Proto": "https"})
        self.assertNotEqual(response.get_json(silent=True), {
            "error": "https_required"})

    def test_21_security_headers_disable_storage(self):
        response = self.client.get("/threads/callback")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_22_complete_meta_urls(self):
        self.assertEqual(threads_oauth_server.endpoint_urls(), {
            "oauth_redirect_url":
                "https://bot.example/threads/callback",
            "deauthorize_callback_url":
                "https://bot.example/threads/deauthorize",
            "data_deletion_request_url":
                "https://bot.example/threads/data-deletion",
            "privacy_policy_url":
                "https://bot.example/threads/privacy",
        })

    def test_23_public_base_must_be_https(self):
        with patch.dict(os.environ, {
            "THREADS_PUBLIC_BASE_URL": "http://bot.example"}):
            with self.assertRaises(ValueError):
                threads_oauth_server.endpoint_urls()

    def test_24_database_has_callback_tables(self):
        with closing(sqlite3.connect(self.path)) as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("threads_oauth_states", tables)
        self.assertIn("threads_deletion_receipts", tables)

    def test_25_expired_state_is_rejected(self):
        now = datetime.now(threads_api.JST)
        state = threads_api.create_oauth_state(self.path, now=now)
        self.assertFalse(threads_api.consume_oauth_state(
            state, self.path, now=now + timedelta(minutes=11)))

    def test_26_waitress_trusts_only_local_proxy_when_enabled(self):
        with patch.dict(os.environ, {
            "THREADS_CALLBACK_TRUST_PROXY": "true"}):
            self.assertEqual(
                threads_oauth_server._waitress_proxy_options(),
                {
                    "trusted_proxy": "127.0.0.1",
                    "trusted_proxy_headers": {"x-forwarded-proto"},
                },
            )
        with patch.dict(os.environ, {
            "THREADS_CALLBACK_TRUST_PROXY": "false"}):
            self.assertEqual(
                threads_oauth_server._waitress_proxy_options(), {})

    def test_27_tailscale_proxy_without_forwarded_proto_is_accepted(self):
        with patch.dict(os.environ, {
            "THREADS_CALLBACK_TRUST_PROXY": "true"}):
            app = threads_oauth_server.create_app(path=self.path)
            accepted = app.test_client().get(
                "/threads/callback",
                base_url="http://bot.example")
        self.assertNotEqual(accepted.get_json(silent=True), {
            "error": "https_required"})

    def test_28_explicit_http_forwarding_is_rejected(self):
        with patch.dict(os.environ, {
            "THREADS_CALLBACK_TRUST_PROXY": "true"}):
            app = threads_oauth_server.create_app(path=self.path)
            response = app.test_client().get(
                "/threads/callback",
                base_url="http://bot.example",
                headers={"X-Forwarded-Proto": "http"})
        self.assertEqual(response.get_json(), {
            "error": "https_required"})

    def test_29_privacy_policy_is_public_and_secret_free(self):
        response = self.client.get("/threads/privacy")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        self.assertIn(
            b"Kuze Yui Threads Integration Privacy Policy", response.data)
        self.assertNotIn(b"access-secret", response.data)
        self.assertNotIn(b"app-secret", response.data)

    def test_30_privacy_contact_is_escaped(self):
        with patch.dict(os.environ, {
            "THREADS_PRIVACY_CONTACT": "<script>alert(1)</script>"}):
            response = self.client.get("/threads/privacy")
        self.assertNotIn(b"<script>", response.data)
        self.assertIn(b"&lt;script&gt;", response.data)


if __name__ == "__main__":
    unittest.main()
