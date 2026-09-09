"""Token-scoped MP4 server for a Tailscale Funnel reverse proxy."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
from contextlib import closing
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from metrics_db import connect, db_path  # noqa: E402
from short_video_factory.repository import migrate  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue(video_id: str, local_path: Path, ttl_minutes: int = 180,
          path: Path | None = None) -> dict:
    resolved = Path(local_path).resolve()
    if not resolved.is_file() or resolved.suffix.lower() != ".mp4":
        raise FileNotFoundError("publishable_mp4_missing")
    migrate(path)
    token = secrets.token_urlsafe(32)
    now = datetime.now(JST)
    expires = now + timedelta(minutes=max(15, int(ttl_minutes)))
    with closing(connect(path or db_path())) as conn:
        conn.execute(
            """INSERT INTO short_video_public_media_tokens
               (video_id,token_hash,local_path,expires_at,created_at)
               VALUES (?,?,?,?,?)""",
            (video_id, _hash(token), str(resolved), expires.isoformat(),
             now.isoformat()))
        conn.commit()
    base = os.environ.get("SHORT_VIDEO_PUBLIC_MEDIA_BASE_URL", "").rstrip("/")
    route = f"/short-media/{token}.mp4"
    return {
        "video_id": video_id, "route": route,
        "public_url": f"{base}{route}" if base else "",
        "expires_at": expires.isoformat(),
        "token_hash": _hash(token),
    }


def revoke(video_id: str, path: Path | None = None) -> int:
    migrate(path)
    with closing(connect(path or db_path())) as conn:
        cursor = conn.execute(
            """UPDATE short_video_public_media_tokens SET revoked_at=?
               WHERE video_id=? AND revoked_at IS NULL""",
            (datetime.now(JST).isoformat(), video_id))
        conn.commit()
        return int(cursor.rowcount)


def lookup(request_path: str, path: Path | None = None) -> tuple[Path, int] | None:
    clean = urlsplit(request_path).path
    name = clean.rsplit("/", 1)[-1]
    if not name.endswith(".mp4") or ".." in clean or "\\" in clean:
        return None
    token = name[:-4]
    if not token:
        return None
    migrate(path)
    now = datetime.now(JST)
    with closing(connect(path or db_path())) as conn:
        rows = conn.execute(
            """SELECT id,token_hash,local_path,expires_at FROM
               short_video_public_media_tokens
               WHERE revoked_at IS NULL AND expires_at>?""",
            (now.isoformat(),)).fetchall()
        for row in rows:
            if hmac.compare_digest(row["token_hash"], _hash(token)):
                target = Path(row["local_path"]).resolve()
                if target.is_file() and target.suffix.lower() == ".mp4":
                    return target, int(row["id"])
    return None


def run(host: str = "127.0.0.1", port: int = 8766,
        path: Path | None = None) -> None:
    class Handler(BaseHTTPRequestHandler):
        server_version = "KuzeShortMedia/1"

        def log_message(self, _format, *_args):
            return

        def _resolve(self):
            return lookup(self.path, path)

        def _headers(self, status: int, length: int = 0,
                     content_range: str = ""):
            self.send_response(status)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=60")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(length))
            if content_range:
                self.send_header("Content-Range", content_range)
            self.end_headers()

        def do_HEAD(self):
            found = self._resolve()
            if not found:
                self._headers(404)
                return
            self._headers(200, found[0].stat().st_size)

        def do_GET(self):
            found = self._resolve()
            if not found:
                self._headers(404)
                return
            target, row_id = found
            size = target.stat().st_size
            start, end, status = 0, size - 1, 200
            raw_range = self.headers.get("Range", "")
            if raw_range.startswith("bytes="):
                left, _, right = raw_range[6:].split(",", 1)[0].partition("-")
                try:
                    start = int(left or 0)
                    end = min(int(right or end), end)
                except ValueError:
                    self._headers(416)
                    return
                if start < 0 or end < start or start >= size:
                    self._headers(416)
                    return
                status = 206
            content_range = (
                f"bytes {start}-{end}/{size}" if status == 206 else "")
            self._headers(status, end - start + 1, content_range)
            with target.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            with closing(connect(path or db_path())) as conn:
                conn.execute(
                    """UPDATE short_video_public_media_tokens
                       SET download_count=download_count+1,last_downloaded_at=?
                       WHERE id=?""",
                    (datetime.now(JST).isoformat(), row_id))
                conn.commit()

    migrate(path)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    print(json.dumps({"status": "started", "host": host, "port": int(port)}))
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    run(args.host, args.port)
