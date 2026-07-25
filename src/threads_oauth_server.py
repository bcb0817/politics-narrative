"""Public Meta Threads OAuth callbacks with strict secret redaction."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request

import threads_api


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _max_request_bytes() -> int:
    try:
        return max(1024, min(
            65536, int(os.environ.get(
                "THREADS_CALLBACK_MAX_REQUEST_BYTES", "8192"))))
    except ValueError:
        return 8192


def _decode_urlsafe(value: str) -> bytes:
    if not value or len(value) > _max_request_bytes():
        raise ValueError("invalid_signed_request")
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def verify_signed_request(signed_request: str,
                          app_secret: str) -> dict[str, Any]:
    """Verify Meta's base64url payload using HMAC-SHA256."""
    if not app_secret or not signed_request or "." not in signed_request:
        raise ValueError("invalid_signed_request")
    encoded_signature, encoded_payload = signed_request.split(".", 1)
    try:
        supplied = _decode_urlsafe(encoded_signature)
        payload_bytes = _decode_urlsafe(encoded_payload)
    except (ValueError, UnicodeError, base64.binascii.Error):
        raise ValueError("invalid_signed_request") from None
    expected = hmac.new(
        app_secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("invalid_signed_request")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("invalid_signed_request") from None
    if not isinstance(payload, dict):
        raise ValueError("invalid_signed_request")
    algorithm = str(payload.get("algorithm") or "HMAC-SHA256").upper()
    if algorithm != "HMAC-SHA256":
        raise ValueError("unsupported_signed_request_algorithm")
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id or len(user_id) > 256:
        raise ValueError("signed_request_user_missing")
    return payload


def _public_base_url() -> str:
    value = threads_api.settings()["public_base_url"]
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https" or not parsed.netloc
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        or parsed.username or parsed.password
    ):
        raise ValueError("THREADS_PUBLIC_BASE_URL must be public HTTPS")
    return value.rstrip("/")


def endpoint_urls() -> dict:
    base = _public_base_url()
    return {
        "oauth_redirect_url": f"{base}/threads/callback",
        "deauthorize_callback_url": f"{base}/threads/deauthorize",
        "data_deletion_request_url": f"{base}/threads/data-deletion",
    }


def _request_is_https() -> bool:
    if request.is_secure:
        return True
    if _bool("THREADS_CALLBACK_TRUST_PROXY", "false"):
        forwarded = request.headers.get("X-Forwarded-Proto", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip().lower() == "https"
        # Tailscale Funnel terminates public HTTPS and proxies to the
        # loopback-only backend without promising X-Forwarded-Proto.
        return True
    return False


def _waitress_proxy_options() -> dict[str, Any]:
    """Trust only the local reverse proxy when explicitly enabled."""
    if not _bool("THREADS_CALLBACK_TRUST_PROXY", "false"):
        return {}
    return {
        "trusted_proxy": "127.0.0.1",
        "trusted_proxy_headers": {"x-forwarded-proto"},
    }


def _signed_user() -> str:
    signed = request.form.get("signed_request", "")
    payload = verify_signed_request(
        signed, threads_api.settings()["app_secret"])
    return str(payload["user_id"])


def create_app(path: Path | None = None,
               require_https: bool | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=_max_request_bytes(),
        PROPAGATE_EXCEPTIONS=False,
    )
    require_https = (
        _bool("THREADS_CALLBACK_REQUIRE_HTTPS", "true")
        if require_https is None else require_https
    )
    app.logger.disabled = True
    logging.getLogger("werkzeug").disabled = True

    @app.before_request
    def enforce_https():
        if require_https and not _request_is_https():
            return jsonify({"error": "https_required"}), 400
        return None

    @app.after_request
    def secure_headers(response: Response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.get("/threads/callback")
    def oauth_callback():
        code = request.args.get("code", "")
        state = request.args.get("state", "")
        if not code or len(code) > 4096 or not threads_api.consume_oauth_state(
                state, path=path):
            return Response(
                "Threads authorization could not be verified.",
                status=400, mimetype="text/plain")
        try:
            threads_api.exchange_code(code, path=path)
        except Exception:
            return Response(
                "Threads authorization failed. Start a new authorization.",
                status=502, mimetype="text/plain")
        return Response(
            "Threads authorization completed. You may close this window.",
            status=200, mimetype="text/plain")

    @app.post("/threads/deauthorize")
    def deauthorize():
        try:
            user_id = _signed_user()
            threads_api._update_env({"THREADS_POST_ENABLED": "false"})
            result = threads_api.clear_user_credentials(
                user_id, detach_history=False, path=path)
        except ValueError:
            return jsonify({"error": "invalid_signed_request"}), 400
        except Exception:
            return jsonify({"error": "deauthorization_failed"}), 503
        return jsonify({
            "status": "deauthorized",
            "threads_posting_disabled": True,
            "credentials_cleared": result["credentials_cleared"],
        })

    @app.post("/threads/data-deletion")
    def data_deletion():
        try:
            user_id = _signed_user()
            threads_api._update_env({"THREADS_POST_ENABLED": "false"})
            threads_api.clear_user_credentials(
                user_id, detach_history=True, path=path)
            confirmation = threads_api.create_deletion_receipt(
                user_id, path=path)
            status_url = (
                f"{_public_base_url()}/threads/data-deletion"
                f"?code={confirmation}"
            )
        except ValueError:
            return jsonify({"error": "invalid_signed_request"}), 400
        except Exception:
            return jsonify({"error": "data_deletion_failed"}), 503
        return jsonify({
            "url": status_url,
            "confirmation_code": confirmation,
        })

    @app.get("/threads/data-deletion")
    def data_deletion_status():
        result = threads_api.deletion_status(
            request.args.get("code", ""), path=path)
        status = 200 if result["found"] else 404
        return jsonify(result), status

    return app


def run_server() -> None:
    """Run behind a trusted HTTPS reverse proxy; never enable Flask debug."""
    from waitress import serve

    host = os.environ.get("THREADS_CALLBACK_HOST", "127.0.0.1")
    if (
        _bool("THREADS_CALLBACK_TRUST_PROXY", "false")
        and host not in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ValueError(
            "THREADS_CALLBACK_TRUST_PROXY requires a loopback-only host")
    try:
        port = int(os.environ.get("THREADS_CALLBACK_PORT", "8787"))
    except ValueError:
        port = 8787
    logging.getLogger("waitress").setLevel(logging.ERROR)
    logging.getLogger("waitress.queue").setLevel(logging.ERROR)
    serve(
        create_app(), host=host, port=port, threads=4,
        clear_untrusted_proxy_headers=True,
        **_waitress_proxy_options(),
    )
