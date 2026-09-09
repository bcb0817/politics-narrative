"""Safe public-media abstraction for Meta video ingestion.

Phase A never starts this server or exposes a file.  The server is provided so
that a later, explicitly approved Tailscale Funnel deployment can expose one
allow-listed file behind an unguessable token without directory listing.
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import requests


@dataclass(frozen=True)
class PublishedMedia:
    provider: str
    public_url: str
    url_hash: str
    expires_at_epoch: int
    externally_published: bool


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def validate_public_url(url: str, *, session=None, fetch: bool = False) -> dict:
    """Validate a Meta-fetchable URL without returning secrets in diagnostics."""
    parsed = urlsplit(str(url or ""))
    result = {
        "valid": False,
        "https": parsed.scheme.lower() == "https",
        "host_present": bool(parsed.hostname),
        "credentials_absent": not (parsed.username or parsed.password),
        "content_type": None,
        "content_length": None,
        "range_supported": None,
        "external_fetch_tested": False,
    }
    if not all((
        result["https"], result["host_present"],
        result["credentials_absent"],
    )):
        return result
    if fetch:
        client = session or requests.Session()
        response = client.head(
            url, allow_redirects=True,
            timeout=float(os.environ.get("MEDIA_FETCH_TEST_TIMEOUT_SECONDS", "15")),
        )
        result["external_fetch_tested"] = True
        result["content_type"] = response.headers.get("Content-Type", "").split(";")[0]
        result["content_length"] = response.headers.get("Content-Length")
        result["range_supported"] = (
            "bytes" in response.headers.get("Accept-Ranges", "").lower()
        )
        result["valid"] = (
            200 <= response.status_code < 400
            and result["content_type"] == "video/mp4"
        )
        return result
    result["valid"] = True
    return result


class MediaPublicationProvider:
    """Prepare a URL while keeping Phase A completely local."""

    def prepare(self, path: Path, *, dry_run: bool = True) -> PublishedMedia:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        provider = os.environ.get("MEDIA_PUBLICATION_PROVIDER", "").strip().lower()
        ttl = max(30, int(os.environ.get(
            "MEDIA_SIGNED_URL_TTL_MINUTES", "120")))
        expires = int(time.time()) + ttl * 60
        if dry_run:
            token = hashlib.sha256(
                f"{resolved.name}:{resolved.stat().st_size}".encode()
            ).hexdigest()[:24]
            return PublishedMedia(
                provider=provider or "dry_run",
                public_url=f"https://dry-run.invalid/media/{token}.mp4",
                url_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at_epoch=expires,
                externally_published=False,
            )
        if not _bool("CROSSPOST_AUTO_PUBLISH_ENABLED"):
            raise PermissionError("crosspost_auto_publish_disabled")
        if provider not in {"s3", "r2", "funnel", "custom"}:
            raise RuntimeError("media_publication_provider_not_configured")
        if provider == "custom":
            base = os.environ.get("MEDIA_PUBLIC_BASE_URL", "").strip().rstrip("/")
            if not base:
                raise RuntimeError("media_public_base_url_missing")
            token = secrets.token_urlsafe(24)
            url = f"{base}/{token}/{resolved.name}"
            return PublishedMedia(
                provider=provider,
                public_url=url,
                url_hash=hashlib.sha256(url.encode()).hexdigest(),
                expires_at_epoch=expires,
                externally_published=False,
            )
        raise RuntimeError(
            f"{provider}_upload_requires_phase_b_provider_adapter")


class SingleFileMediaServer:
    """Serve one MP4 only; intended to sit behind an approved Funnel."""

    def __init__(self, path: Path, token: str | None = None,
                 host: str = "127.0.0.1", port: int = 0,
                 ttl_seconds: int = 7200):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self.token = token or secrets.token_urlsafe(32)
        self.host = host
        self.port = int(port)
        self.expires_at = time.time() + max(60, int(ttl_seconds))
        self.download_count = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def route(self) -> str:
        return f"/media/{self.token}.mp4"

    def start(self) -> int:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SingleFileMedia/1"

            def log_message(self, _format, *_args):
                return

            def _allowed(self) -> bool:
                request_path = urlsplit(self.path).path
                return (
                    time.time() <= owner.expires_at
                    and hmac.compare_digest(request_path, owner.route)
                    and ".." not in request_path
                    and "\\" not in request_path
                )

            def _headers(self, status: int, length: int = 0):
                self.send_response(status)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "private, max-age=60")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(length))
                self.end_headers()

            def do_HEAD(self):
                if not self._allowed():
                    self._headers(404)
                    return
                self._headers(200, owner.path.stat().st_size)

            def do_GET(self):
                if not self._allowed():
                    self._headers(404)
                    return
                size = owner.path.stat().st_size
                start, end = 0, size - 1
                range_header = self.headers.get("Range", "")
                if range_header.startswith("bytes="):
                    raw = range_header[6:].split(",", 1)[0]
                    left, _, right = raw.partition("-")
                    try:
                        start = int(left or 0)
                        end = min(int(right or end), end)
                    except ValueError:
                        self._headers(416)
                        return
                    if start < 0 or end < start or start >= size:
                        self._headers(416)
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(end - start + 1))
                    self.end_headers()
                else:
                    self._headers(200, size)
                with owner.path.open("rb") as handle:
                    handle.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = handle.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                owner.download_count += 1

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


def safe_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"
