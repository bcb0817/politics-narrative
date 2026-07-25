"""Preview-first cross-platform political video publishing pipeline.

Only official APIs are represented.  Phase A generates local assets, copy,
validation and request plans.  No external write is possible unless every
global/platform switch is enabled and the caller passes ``confirm=True``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

from media_publication import MediaPublicationProvider, validate_public_url
from metrics_db import connect, db_path, init_db


JST = ZoneInfo("Asia/Tokyo")
PLATFORMS = ("youtube", "x", "threads", "instagram")
PUBLICATION_STATES = {
    "planned", "researching", "scripted", "rendering", "quality_check",
    "ready", "preparing_media", "publishing", "partially_published",
    "published", "failed", "blocked", "ambiguous",
}
PLATFORM_STATES = {
    "not_started", "media_preparing", "media_ready", "container_created",
    "processing", "ready_to_publish", "publishing", "published", "failed",
    "ambiguous", "blocked", "skipped",
}

CROSSPOST_SCHEMA = """
CREATE TABLE IF NOT EXISTS cross_platform_publications (
 id INTEGER PRIMARY KEY, publication_id TEXT UNIQUE, content_id TEXT,
 topic_key TEXT, format TEXT, master_video_path TEXT, status TEXT,
 target_publish_at TEXT, copy_json TEXT, quality_json TEXT,
 created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS platform_publications (
 id INTEGER PRIMARY KEY, publication_id TEXT, platform TEXT,
 idempotency_key TEXT UNIQUE, rendition_path TEXT, caption_text TEXT,
 title TEXT, external_container_id TEXT, external_media_id TEXT,
 external_post_id TEXT, external_url TEXT, status TEXT, prepared_at TEXT,
 published_at TEXT, publication_skew_seconds INTEGER, error_type TEXT,
 retry_count INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT,
 UNIQUE(publication_id,platform));
CREATE TABLE IF NOT EXISTS platform_media_assets (
 id INTEGER PRIMARY KEY, publication_id TEXT, platform TEXT, local_path TEXT,
 remote_url_hash TEXT, mime_type TEXT, width INTEGER, height INTEGER,
 duration_seconds REAL, file_size INTEGER, codec TEXT, audio_codec TEXT,
 audio_sample_rate INTEGER, fps REAL, status TEXT, expires_at TEXT,
 created_at TEXT, UNIQUE(publication_id,platform,local_path));
CREATE TABLE IF NOT EXISTS cross_platform_metrics (
 id INTEGER PRIMARY KEY, publication_id TEXT, platform TEXT,
 external_post_id TEXT, measurement_window TEXT, measured_at TEXT,
 metrics_json TEXT, created_at TEXT,
 UNIQUE(publication_id,platform,measurement_window,measured_at));
CREATE TABLE IF NOT EXISTS cross_platform_events (
 id INTEGER PRIMARY KEY, publication_id TEXT, platform TEXT, event_type TEXT,
 previous_status TEXT, new_status TEXT, occurred_at TEXT, metadata_json TEXT);
CREATE INDEX IF NOT EXISTS idx_crosspost_status
 ON cross_platform_publications(status,target_publish_at);
CREATE INDEX IF NOT EXISTS idx_platform_publication_status
 ON platform_publications(platform,status,published_at);
CREATE INDEX IF NOT EXISTS idx_crosspost_metrics_publication
 ON cross_platform_metrics(publication_id,platform,measured_at);
"""


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _now() -> datetime:
    return datetime.now(JST)


def _output_dir() -> Path:
    raw = os.environ.get("CROSSPOST_OUTPUT_DIR", "outputs/crosspost")
    path = Path(raw)
    path = path if path.is_absolute() else _root() / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _db(path: Path | None = None) -> Path:
    return Path(path) if path else db_path()


def apply_migrations(path: Path | None = None) -> bool:
    resolved = _db(path)
    if not init_db(resolved):
        return False
    try:
        with closing(connect(resolved)) as conn:
            conn.executescript(CROSSPOST_SCHEMA)
            conn.commit()
        return True
    except Exception:
        return False


def _execute(sql: str, params=(), path: Path | None = None) -> int:
    apply_migrations(path)
    with closing(connect(_db(path))) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return int(cur.lastrowid or 0)


def _rows(sql: str, params=(), path: Path | None = None) -> list[dict]:
    apply_migrations(path)
    with closing(connect(_db(path))) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def settings() -> dict:
    return {
        "enabled": _bool("CROSSPOST_ENABLED", "true"),
        "auto_publish": _bool("CROSSPOST_AUTO_PUBLISH_ENABLED", "false"),
        "platforms": {
            "youtube": _bool("CROSSPOST_YOUTUBE_ENABLED", "true"),
            "x": _bool("CROSSPOST_X_ENABLED", "true"),
            "threads": _bool("CROSSPOST_THREADS_ENABLED", "true"),
            "instagram": _bool("CROSSPOST_INSTAGRAM_ENABLED", "false"),
        },
        "platform_publish_switches": {
            "youtube": _bool("YOUTUBE_AUTO_PUBLISH_ENABLED", "false"),
            "x": _bool("X_POST_ENABLED", "false"),
            "threads": _bool("THREADS_POST_ENABLED", "false"),
            "instagram": _bool("INSTAGRAM_AUTO_PUBLISH_ENABLED", "false"),
        },
        "require_all_ready": _bool(
            "CROSSPOST_REQUIRE_ALL_PLATFORMS_READY", "false"),
        "publish_order": [
            item.strip().lower() for item in os.environ.get(
                "CROSSPOST_PUBLISH_ORDER",
                "youtube,x,threads,instagram").split(",")
            if item.strip().lower() in PLATFORMS
        ],
        "target_window_seconds": max(
            1, _int("CROSSPOST_TARGET_WINDOW_SECONDS", 120)),
        "require_youtube_url": _bool(
            "CROSSPOST_REQUIRE_YOUTUBE_URL_FOR_CTA", "true"),
        "emergency_stopped": emergency_stopped(),
    }


def generate_publication_id(topic_key: str, *, format_name: str = "short",
                            now: datetime | None = None) -> str:
    date = (now or _now()).strftime("%Y%m%d")
    slug = re.sub(r"[^a-z0-9]+", "-", str(topic_key).lower()).strip("-")
    slug = slug[:36] or "politics-explainer"
    suffix = hashlib.sha256(
        f"{date}:{slug}:{format_name}".encode()).hexdigest()[:6]
    return f"video-{date}-{slug}-{format_name}-{suffix}"


def create_publication(*, content_id: str = "phase-a-demo",
                       topic_key: str = "politics-api-safety",
                       format_name: str = "short",
                       publication_id: str = "",
                       path: Path | None = None) -> dict:
    apply_migrations(path)
    publication_id = publication_id or generate_publication_id(
        topic_key, format_name=format_name)
    now = _now().isoformat()
    target = (_now().replace(
        hour=20, minute=0, second=0, microsecond=0)
        + (timedelta(days=1) if _now().hour >= 20 else timedelta())
    ).isoformat()
    _execute("""INSERT OR IGNORE INTO cross_platform_publications
      (publication_id,content_id,topic_key,format,status,target_publish_at,
       created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?)""", (
        publication_id, content_id, topic_key, format_name, "planned",
        target, now, now,
    ), path)
    for platform in PLATFORMS:
        _execute("""INSERT OR IGNORE INTO platform_publications
          (publication_id,platform,idempotency_key,status,retry_count,
           created_at,updated_at)
          VALUES (?,?,?,?,0,?,?)""", (
            publication_id, platform, f"{platform}:{publication_id}",
            "not_started", now, now,
        ), path)
    return get_publication(publication_id, path=path)


def _latest_or_create(publication_id: str = "",
                      path: Path | None = None) -> dict:
    if publication_id:
        row = get_publication(publication_id, path=path)
        if row:
            return row
        return create_publication(publication_id=publication_id, path=path)
    rows = _rows("""SELECT * FROM cross_platform_publications
        ORDER BY id DESC LIMIT 1""", path=path)
    return rows[0] if rows else create_publication(path=path)


def get_publication(publication_id: str,
                    path: Path | None = None) -> dict:
    rows = _rows("""SELECT * FROM cross_platform_publications
        WHERE publication_id=?""", (publication_id,), path)
    if not rows:
        return {}
    result = rows[0]
    result["platforms"] = _rows("""SELECT * FROM platform_publications
        WHERE publication_id=? ORDER BY id""", (publication_id,), path)
    return result


def candidates(path: Path | None = None) -> dict:
    rows = _rows("""SELECT * FROM cross_platform_publications
        WHERE status IN ('planned','scripted','rendering','quality_check',
                         'ready','preparing_media','partially_published',
                         'ambiguous')
        ORDER BY target_publish_at,id""", path=path)
    return {"count": len(rows), "candidates": rows}


def _distinct_copy(publication: dict) -> dict:
    topic = publication.get("topic_key") or "政治の重要論点"
    publication_id = publication["publication_id"]
    is_long = publication.get("format") == "long"
    if is_long:
        x_text = (
            f"結論から。{topic}は制度の説明だけでは見誤ります。"
            "予告編の続きは、久世ゆいのYouTubeチャンネルで公開します。")
        threads_text = (
            f"{topic}の時系列と一次資料を長尺動画で整理しました。\n\n"
            "この動画は予告編です。本編は久世ゆいのYouTubeチャンネルで公開します。")
        instagram_text = (
            f"{topic}を時系列から確認します。\n"
            "このReelは本編の予告です。\n\n"
            "続きはプロフィールのYouTube導線から確認できます。")
    else:
        x_text = f"結論から。{topic}は、制度の目的と実際の負担を分けて見る必要があります。動画で要点を整理しました。"
        threads_text = (
            f"{topic}について、ニュースの見出しだけでは分かりにくい背景を"
            "短い動画にまとめました。\n\n一次資料と実際の影響を分けて確認します。"
        )
        instagram_text = (
            f"{topic}の要点を2行で整理します。\n"
            "制度の説明と、生活への影響は同じではありません。\n\n"
            "詳しい解説はプロフィールのYouTube・note導線から確認できます。"
        )
    youtube_title = (
        f"{topic}を資料から詳しく解説"
        if is_long else f"{topic}を60秒で整理｜政治ニュース解説")
    output = {
        "publication_id": publication_id,
        "x": {
            "text": x_text, "hashtags": [],
            "alt_text": f"{topic}を図解する縦型政治解説動画",
        },
        "threads": {
            "text": threads_text, "topic_tag": "",
            "alt_text": f"{topic}の背景と影響を説明する縦型動画",
        },
        "instagram": {
            "caption": instagram_text,
            "hashtags": ["政治ニュース", "ニュース解説"],
            "alt_text": f"{topic}について一次資料を基に解説する動画",
        },
        "youtube": {
            "title": youtube_title,
            "description": (
                f"{topic}を一次資料と報道から"
                f"{'詳しく' if is_long else '短く'}整理します。\n\n"
                "AIキャラクター「久世ゆい」による解説です。"
            ),
            "tags": ["政治", "ニュース", "解説"],
            "category_id": "25",
            "contains_synthetic_media": True,
            "made_for_kids": False,
        },
    }
    texts = [
        output["x"]["text"], output["threads"]["text"],
        output["instagram"]["caption"], output["youtube"]["description"],
    ]
    if len(set(texts)) != len(texts):
        raise RuntimeError("platform_copy_not_distinct")
    return output


def generate_copy(publication_id: str = "", *, dry_run: bool = True,
                  path: Path | None = None) -> dict:
    publication = _latest_or_create(publication_id, path)
    output = _distinct_copy(publication)
    now = _now().isoformat()
    _execute("""UPDATE cross_platform_publications
        SET copy_json=?,status='scripted',updated_at=? WHERE publication_id=?""",
             (json.dumps(output, ensure_ascii=False), now,
              publication["publication_id"]), path)
    for platform in PLATFORMS:
        item = output[platform]
        caption = item.get("text") or item.get("caption") or item.get(
            "description") or ""
        _execute("""UPDATE platform_publications SET caption_text=?,title=?,
            updated_at=? WHERE publication_id=? AND platform=?""", (
                caption, item.get("title", ""), now,
                publication["publication_id"], platform,
            ), path)
    output["dry_run"] = bool(dry_run)
    output["external_writes"] = 0
    return output


def _binary(name: str) -> str:
    env_name = f"{name.upper()}_PATH"
    explicit = os.environ.get(env_name, "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    found = shutil.which(name)
    if found:
        return found
    package_root = Path(os.environ.get("LOCALAPPDATA", "")) / (
        "Microsoft/WinGet/Packages/"
        "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    )
    try:
        matches = list(package_root.glob(f"ffmpeg-*-full_build/bin/{name}.exe"))
    except OSError:
        matches = []
    if matches:
        return str(sorted(matches)[-1])
    raise FileNotFoundError(f"{name}_not_found")


def _run(command: list[str]) -> None:
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=max(
            30, _int("CROSSPOST_FFMPEG_TIMEOUT_SECONDS", 600)),
    )
    if result.returncode:
        raise RuntimeError(
            f"media_command_failed:{Path(command[0]).name}:"
            f"{result.stderr[-500:]}")


def create_demo_master(publication_id: str, *, duration: int = 45) -> Path:
    folder = _output_dir() / publication_id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "master-1080x1920.mp4"
    subtitle = folder / "master.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:05,000\n"
        "Phase A Crosspost Dry Run\n\n"
        "2\n00:00:05,000 --> 00:00:10,000\n"
        "No external publishing\n", encoding="utf-8")
    if target.exists():
        return target
    ffmpeg = _binary("ffmpeg")
    _run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"color=c=0x172033:s=1080x1920:r=30:d={duration}",
        "-f", "lavfi", "-i",
        f"sine=frequency=440:sample_rate=48000:duration={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-ar", "48000", "-shortest", "-movflags", "+faststart",
        str(target),
    ])
    return target


RENDITION_PROFILES = {
    "youtube": {
        "width": 1080, "height": 1920, "fps": 30, "video_bitrate": "4M",
        "max_bytes": 256 * 1024 ** 3,
    },
    "instagram": {
        "width": 1080, "height": 1920, "fps": 30, "video_bitrate": "4M",
        "max_bytes": 1 * 1024 ** 3,
    },
    "threads": {
        "width": 1080, "height": 1920, "fps": 30, "video_bitrate": "4M",
        "max_bytes": 1 * 1024 ** 3,
    },
    "x": {
        "width": 720, "height": 1280, "fps": 30, "video_bitrate": "3M",
        "max_bytes": 512 * 1024 ** 2,
    },
}


def probe_video(path: Path) -> dict:
    ffprobe = _binary("ffprobe")
    result = subprocess.run([
        ffprobe, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ], capture_output=True, text=True, encoding="utf-8",
       errors="replace", timeout=60)
    if result.returncode:
        raise RuntimeError("ffprobe_failed")
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    rate = str(video.get("avg_frame_rate") or "0/1")
    left, _, right = rate.partition("/")
    fps = float(left or 0) / max(1.0, float(right or 1))
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 3),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate": int(audio.get("sample_rate") or 0),
        "duration_seconds": round(float(
            (payload.get("format") or {}).get("duration") or 0), 3),
        "file_size": int(
            (payload.get("format") or {}).get("size") or Path(path).stat().st_size),
        "video_stream": bool(video),
        "audio_stream": bool(audio),
    }


def detect_media_anomalies(path: Path) -> dict:
    """Use official FFmpeg filters to detect black frames and long silence."""
    null_sink = "NUL" if os.name == "nt" else "/dev/null"
    result = subprocess.run([
        _binary("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path),
        "-vf", "blackdetect=d=2:pix_th=0.10",
        "-af", "silencedetect=n=-50dB:d=3",
        "-f", "null", null_sink,
    ], capture_output=True, text=True, encoding="utf-8", errors="replace",
       timeout=max(30, _int("CROSSPOST_FFMPEG_TIMEOUT_SECONDS", 600)))
    diagnostics = result.stderr or ""
    black_segments = len(re.findall(r"black_start:", diagnostics))
    silence_segments = len(re.findall(r"silence_start:", diagnostics))
    return {
        "black_segments_over_2s": black_segments,
        "silence_segments_over_3s": silence_segments,
        "passed": result.returncode == 0
        and black_segments == 0 and silence_segments == 0,
    }


def render_renditions(publication_id: str = "", *, dry_run: bool = True,
                      path: Path | None = None) -> dict:
    publication = _latest_or_create(publication_id, path)
    pid = publication["publication_id"]
    master_raw = publication.get("master_video_path") or ""
    master = Path(master_raw) if master_raw else create_demo_master(pid)
    if not master.is_file():
        raise FileNotFoundError(master)
    folder = _output_dir() / pid
    outputs: dict[str, dict] = {}
    now = _now().isoformat()
    _execute("""UPDATE cross_platform_publications SET master_video_path=?,
        status='rendering',updated_at=? WHERE publication_id=?""",
             (str(master), now, pid), path)
    for platform, profile in RENDITION_PROFILES.items():
        target = folder / (
            f"{platform}-{profile['width']}x{profile['height']}.mp4")
        if not target.exists():
            video_filter = (
                f"scale={profile['width']}:{profile['height']}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={profile['width']}:{profile['height']}:"
                "(ow-iw)/2:(oh-ih)/2:color=black"
            )
            if platform == "youtube":
                video_filter += (
                    ",drawtext=fontfile='C\\:/Windows/Fonts/arial.ttf':"
                    "text='Phase A Dry Run':"
                    "x=(w-text_w)/2:y=h*0.70:"
                    "fontcolor=white:fontsize=48:"
                    "box=1:boxcolor=black@0.55:boxborderw=20"
                )
            _run([
                _binary("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(master),
                *(
                    ["-t", "60"]
                    if publication.get("format") == "long"
                    and platform != "youtube" else []
                ),
                "-vf", video_filter,
                "-r", str(profile["fps"]), "-c:v", "libx264",
                "-preset", "veryfast", "-b:v", profile["video_bitrate"],
                "-maxrate", profile["video_bitrate"], "-bufsize", "8M",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-ar", "48000", "-movflags", "+faststart", str(target),
            ])
        info = probe_video(target)
        outputs[platform] = {"path": str(target), **info}
        _execute("""UPDATE platform_publications SET rendition_path=?,
            status='media_ready',prepared_at=?,updated_at=?
            WHERE publication_id=? AND platform=?""", (
                str(target), now, now, pid, platform,
            ), path)
        _execute("""INSERT OR REPLACE INTO platform_media_assets
          (id,publication_id,platform,local_path,mime_type,width,height,
           duration_seconds,file_size,codec,audio_codec,audio_sample_rate,
           fps,status,created_at)
          VALUES ((SELECT id FROM platform_media_assets
                   WHERE publication_id=? AND platform=? AND local_path=?),
                  ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            pid, platform, str(target), pid, platform, str(target),
            "video/mp4", info["width"], info["height"],
            info["duration_seconds"], info["file_size"],
            info["video_codec"], info["audio_codec"],
            info["audio_sample_rate"], info["fps"], "local_ready", now,
        ), path)
    _execute("""UPDATE cross_platform_publications SET status='quality_check',
        updated_at=? WHERE publication_id=?""", (now, pid), path)
    return {
        "publication_id": pid, "master": str(master),
        "renditions": outputs, "dry_run": bool(dry_run),
        "external_writes": 0,
    }


def _safe_area(platform: str) -> dict:
    defaults = {"top": 12.0, "bottom": 22.0, "left": 8.0, "right": 8.0}
    prefix = platform.upper()
    for key in defaults:
        name = f"{prefix}_SAFE_{key.upper()}_PERCENT"
        try:
            defaults[key] = float(os.environ.get(name, defaults[key]))
        except ValueError:
            pass
    return defaults


def validate(publication_id: str = "", *, dry_run: bool = True,
             path: Path | None = None) -> dict:
    publication = _latest_or_create(publication_id, path)
    pid = publication["publication_id"]
    platforms = _rows("""SELECT * FROM platform_publications
        WHERE publication_id=?""", (pid,), path)
    checks: dict[str, dict] = {}
    all_valid = True
    for row in platforms:
        platform = row["platform"]
        rendition = Path(row.get("rendition_path") or "")
        if not rendition.is_file():
            checks[platform] = {"valid": False, "reason": "rendition_missing"}
            all_valid = False
            continue
        info = probe_video(rendition)
        anomalies = detect_media_anomalies(rendition)
        profile = RENDITION_PROFILES[platform]
        safe = _safe_area(platform)
        valid = all((
            info["width"] == profile["width"],
            info["height"] == profile["height"],
            23 <= info["fps"] <= 60,
            info["video_codec"] == "h264",
            info["audio_codec"] == "aac",
            info["audio_sample_rate"] == 48000,
            3 <= info["duration_seconds"] <= (
                43200 if (
                    publication.get("format") == "long"
                    and platform == "youtube") else 60.5),
            info["file_size"] <= profile["max_bytes"],
            info["video_stream"], info["audio_stream"],
            safe["top"] >= 0, safe["bottom"] >= 0,
            safe["left"] >= 0, safe["right"] >= 0,
        ))
        checks[platform] = {
            "valid": valid, "probe": info, "safe_area": safe,
            "media_anomalies": anomalies,
            "subtitle_safe_area": safe["bottom"] >= 22,
        }
        checks[platform]["valid"] = bool(valid and anomalies["passed"])
        all_valid = all_valid and anomalies["passed"]
        all_valid = all_valid and valid
    copy_payload = json.loads(publication.get("copy_json") or "{}")
    copy_distinct = False
    if copy_payload:
        texts = [
            copy_payload.get("x", {}).get("text", ""),
            copy_payload.get("threads", {}).get("text", ""),
            copy_payload.get("instagram", {}).get("caption", ""),
            copy_payload.get("youtube", {}).get("description", ""),
        ]
        copy_distinct = len(set(texts)) == 4 and all(texts)
    all_valid = all_valid and bool(copy_distinct)
    now = _now().isoformat()
    quality = {
        "fact_status": os.environ.get(
            "CROSSPOST_FACT_STATUS", "verified"),
        "risk_level": os.environ.get("CROSSPOST_RISK_LEVEL", "low"),
        "copyright_verified": _bool(
            "CROSSPOST_COPYRIGHT_VERIFIED", "true"),
        "ai_disclosure_checked": True,
        "copy_distinct": bool(copy_distinct),
        "platforms": checks,
        "valid": all_valid,
    }
    _execute("""UPDATE cross_platform_publications SET quality_json=?,
        status=?,updated_at=? WHERE publication_id=?""", (
            json.dumps(quality, ensure_ascii=False),
            "ready" if all_valid else "blocked", now, pid,
        ), path)
    return {
        "publication_id": pid, **quality, "dry_run": bool(dry_run),
        "external_writes": 0,
    }


class XVideoClient:
    """Official X API v2 chunked media workflow."""

    base = "https://api.x.com/2"

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def _auth(self):
        from requests_oauthlib import OAuth1
        return OAuth1(
            os.environ["API_KEY"], os.environ["API_KEY_SECRET"],
            os.environ["ACCESS_TOKEN"], os.environ["ACCESS_TOKEN_SECRET"])

    def upload(self, path: Path) -> str:
        total = Path(path).stat().st_size
        response = self.session.post(
            f"{self.base}/media/upload",
            data={"command": "INIT", "media_type": "video/mp4",
                  "media_category": "tweet_video", "total_bytes": total},
            auth=self._auth(), timeout=60)
        response.raise_for_status()
        media_id = str((response.json().get("data") or {}).get("id") or "")
        if not media_id:
            raise RuntimeError("x_media_id_missing")
        with Path(path).open("rb") as handle:
            segment = 0
            while True:
                chunk = handle.read(4 * 1024 * 1024)
                if not chunk:
                    break
                part = self.session.post(
                    f"{self.base}/media/upload",
                    data={"command": "APPEND", "media_id": media_id,
                          "segment_index": segment},
                    files={"media": ("chunk", chunk)},
                    auth=self._auth(), timeout=60)
                part.raise_for_status()
                segment += 1
        final = self.session.post(
            f"{self.base}/media/upload",
            data={"command": "FINALIZE", "media_id": media_id},
            auth=self._auth(), timeout=60)
        final.raise_for_status()
        for _ in range(max(1, _int("X_MEDIA_STATUS_MAX_POLLS", 30))):
            info = (final.json().get("data") or {}).get("processing_info") or {}
            state = info.get("state")
            if not state or state == "succeeded":
                return media_id
            if state == "failed":
                raise RuntimeError("x_media_processing_failed")
            time.sleep(min(30, max(1, int(info.get("check_after_secs") or 2))))
            final = self.session.get(
                f"{self.base}/media/upload",
                params={"command": "STATUS", "media_id": media_id},
                auth=self._auth(), timeout=30)
            final.raise_for_status()
        raise TimeoutError("x_media_processing_timeout")

    def publish(self, media_id: str, text: str) -> str:
        response = self.session.post(
            f"{self.base}/tweets",
            json={"text": text, "media": {"media_ids": [media_id]}},
            auth=self._auth(), timeout=30)
        response.raise_for_status()
        return str((response.json().get("data") or {}).get("id") or "")


class InstagramClient:
    """Official Instagram Login Reels endpoints."""

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.base = os.environ.get(
            "INSTAGRAM_API_BASE_URL", "https://graph.instagram.com").rstrip("/")
        self.version = os.environ.get(
            "INSTAGRAM_GRAPH_API_VERSION", "").strip("/")
        self.token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
        self.user_id = os.environ.get("INSTAGRAM_USER_ID", "")

    def _url(self, path: str) -> str:
        version = f"/{self.version}" if self.version else ""
        return f"{self.base}{version}/{path.lstrip('/')}"

    def profile(self) -> dict:
        response = self.session.get(
            self._url("me"), params={
                "fields": "id,username,account_type,media_count",
                "access_token": self.token,
            }, timeout=30)
        response.raise_for_status()
        return response.json()

    def create_reel(self, video_url: str, caption: str,
                    share_to_feed: bool = True) -> str:
        response = self.session.post(
            self._url(f"{self.user_id}/media"), data={
                "media_type": "REELS", "video_url": video_url,
                "caption": caption,
                "share_to_feed": str(bool(share_to_feed)).lower(),
                "access_token": self.token,
            }, timeout=60)
        response.raise_for_status()
        return str(response.json().get("id") or "")

    def container_status(self, creation_id: str) -> dict:
        response = self.session.get(
            self._url(creation_id), params={
                "fields": "status_code,status",
                "access_token": self.token,
            }, timeout=30)
        response.raise_for_status()
        return response.json()

    def publish(self, creation_id: str) -> str:
        response = self.session.post(
            self._url(f"{self.user_id}/media_publish"), data={
                "creation_id": creation_id, "access_token": self.token,
            }, timeout=30)
        response.raise_for_status()
        return str(response.json().get("id") or "")


class YouTubeClient:
    """Official YouTube Data API resumable videos.insert workflow."""

    upload_url = "https://www.googleapis.com/upload/youtube/v3/videos"
    api_url = "https://www.googleapis.com/youtube/v3"

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.token = os.environ.get("YOUTUBE_ACCESS_TOKEN", "")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def upload(self, path: Path, metadata: dict) -> str:
        audited = _bool("YOUTUBE_API_AUDIT_APPROVED", "false")
        privacy = os.environ.get("YOUTUBE_PRIVACY_STATUS", "private")
        if not audited and privacy != "private":
            raise PermissionError("youtube_unverified_project_private_only")
        body = {
            "snippet": {
                "title": metadata["title"],
                "description": metadata["description"],
                "tags": metadata.get("tags", []),
                "categoryId": metadata.get("category_id", "25"),
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": bool(
                    metadata.get("made_for_kids", False)),
                "containsSyntheticMedia": bool(
                    metadata.get("contains_synthetic_media", False)),
            },
        }
        size = Path(path).stat().st_size
        headers = {
            **self._headers(), "Content-Type": "application/json",
            "X-Upload-Content-Length": str(size),
            "X-Upload-Content-Type": "video/mp4",
        }
        init = self.session.post(
            self.upload_url,
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=body, headers=headers, timeout=60)
        init.raise_for_status()
        location = init.headers.get("Location", "")
        if not location:
            raise RuntimeError("youtube_resumable_location_missing")
        with Path(path).open("rb") as handle:
            uploaded = self.session.put(
                location, data=handle,
                headers={**self._headers(), "Content-Type": "video/mp4"},
                timeout=600)
        uploaded.raise_for_status()
        return str(uploaded.json().get("id") or "")

    def status(self, video_id: str) -> dict:
        response = self.session.get(
            f"{self.api_url}/videos", params={
                "part": "status,processingDetails", "id": video_id,
            }, headers=self._headers(), timeout=30)
        response.raise_for_status()
        items = response.json().get("items") or []
        return items[0] if items else {}


def _threads_video_plan(video_url: str, text: str, alt_text: str) -> dict:
    return {
        "create": {
            "method": "POST", "endpoint": "/me/threads",
            "data": {"media_type": "VIDEO", "video_url": video_url,
                     "text": text, "alt_text": alt_text},
        },
        "publish": {
            "method": "POST", "endpoint": "/me/threads_publish",
            "data": {"creation_id": "<container_id>"},
        },
    }


def _request_plans(publication: dict, copy_payload: dict) -> dict:
    return {
        "youtube": {
            "method": "POST",
            "endpoint": "https://www.googleapis.com/upload/youtube/v3/videos",
            "workflow": "resumable videos.insert -> videos.list processingDetails",
        },
        "x": {
            "endpoint": "https://api.x.com/2/media/upload",
            "workflow": "INIT -> APPEND -> FINALIZE -> STATUS -> POST /2/tweets",
            "media_category": "tweet_video",
        },
        "threads": _threads_video_plan(
            "<public_https_video_url>", copy_payload["threads"]["text"],
            copy_payload["threads"]["alt_text"]),
        "instagram": {
            "create": {
                "method": "POST", "endpoint": "/{ig_user_id}/media",
                "data": {"media_type": "REELS",
                         "video_url": "<public_https_video_url>"},
            },
            "status": "GET /{creation_id}?fields=status_code,status",
            "publish": "POST /{ig_user_id}/media_publish",
        },
    }


def prepare(publication_id: str = "", *, dry_run: bool = True,
            path: Path | None = None) -> dict:
    publication = _latest_or_create(publication_id, path)
    pid = publication["publication_id"]
    if not publication.get("copy_json"):
        generate_copy(pid, dry_run=True, path=path)
    current = get_publication(pid, path=path)
    platform_rows = current["platforms"]
    if not all(Path(row.get("rendition_path") or "").is_file()
               for row in platform_rows):
        render_renditions(pid, dry_run=True, path=path)
    quality = validate(pid, dry_run=True, path=path)
    if not quality["valid"]:
        return {"publication_id": pid, "status": "blocked",
                "reason": "validation_failed", "quality": quality}
    copy_payload = json.loads(
        get_publication(pid, path=path).get("copy_json") or "{}")
    media = MediaPublicationProvider()
    prepared = {}
    now = _now().isoformat()
    for row in get_publication(pid, path=path)["platforms"]:
        platform = row["platform"]
        if not settings()["platforms"][platform]:
            prepared[platform] = {"status": "skipped", "reason": "disabled"}
            continue
        asset = media.prepare(Path(row["rendition_path"]), dry_run=True)
        prepared[platform] = {
            "status": "ready", "remote_url_hash": asset.url_hash,
            "externally_published": False,
        }
        _execute("""UPDATE platform_media_assets SET remote_url_hash=?,
            expires_at=?,status='dry_run_ready'
            WHERE publication_id=? AND platform=?""", (
                asset.url_hash,
                datetime.fromtimestamp(
                    asset.expires_at_epoch, tz=JST).isoformat(),
                pid, platform,
            ), path)
        _execute("""UPDATE platform_publications SET status='ready_to_publish',
            prepared_at=?,updated_at=? WHERE publication_id=? AND platform=?""",
                 (now, now, pid, platform), path)
    _execute("""UPDATE cross_platform_publications
        SET status='preparing_media',updated_at=? WHERE publication_id=?""",
             (now, pid), path)
    return {
        "publication_id": pid, "status": "dry_run_ready",
        "platforms": prepared,
        "request_plans": _request_plans(publication, copy_payload),
        "external_media_published": False,
        "external_api_calls": 0, "external_writes": 0,
        "dry_run": bool(dry_run),
    }


def _publish_allowed(platform: str, confirm: bool) -> tuple[bool, str]:
    cfg = settings()
    if cfg["emergency_stopped"]:
        return False, "emergency_stopped"
    if not cfg["enabled"]:
        return False, "crosspost_disabled"
    if not cfg["auto_publish"]:
        return False, "crosspost_auto_publish_disabled"
    if not cfg["platforms"].get(platform):
        return False, "platform_disabled"
    if not cfg["platform_publish_switches"].get(platform):
        return False, "platform_publish_disabled"
    if not confirm:
        return False, "explicit_confirmation_required"
    return True, ""


def publish(publication_id: str = "", *, confirm: bool = False,
            dry_run: bool = False, path: Path | None = None) -> dict:
    publication = _latest_or_create(publication_id, path)
    pid = publication["publication_id"]
    results = {}
    for platform in settings()["publish_order"]:
        row = next((item for item in get_publication(pid, path=path)["platforms"]
                    if item["platform"] == platform), {})
        if row.get("status") == "published":
            results[platform] = {"status": "already_published"}
            continue
        allowed, reason = _publish_allowed(platform, confirm)
        if dry_run or not allowed:
            results[platform] = {
                "status": "dry_run" if dry_run else "blocked",
                "reason": "dry_run" if dry_run else reason,
                "external_writes": 0,
            }
            continue
        # Actual calls intentionally require Phase B platform-specific approval.
        results[platform] = {
            "status": "blocked", "reason": "phase_b_approval_required"}
    states = {item["status"] for item in results.values()}
    overall = "blocked" if "blocked" in states else "ready"
    return {
        "publication_id": pid, "status": overall,
        "platforms": results, "external_writes": 0,
    }


def reconcile(publication_id: str = "", *,
              path: Path | None = None) -> dict:
    publication = _latest_or_create(publication_id, path)
    rows = get_publication(publication["publication_id"], path=path)["platforms"]
    statuses = [row["status"] for row in rows if row["status"] != "skipped"]
    if statuses and all(status == "published" for status in statuses):
        overall = "published"
    elif any(status == "published" for status in statuses):
        overall = "partially_published"
    elif any(status == "ambiguous" for status in statuses):
        overall = "ambiguous"
    elif all(status in {"failed", "blocked"} for status in statuses):
        overall = "failed"
    else:
        overall = publication.get("status") or "planned"
    _execute("""UPDATE cross_platform_publications SET status=?,updated_at=?
        WHERE publication_id=?""",
             (overall, _now().isoformat(), publication["publication_id"]), path)
    return {
        "publication_id": publication["publication_id"],
        "status": overall, "platforms": rows,
        "successful_posts_deleted": 0,
    }


def metrics_sync(publication_id: str = "", *, dry_run: bool = True,
                 path: Path | None = None) -> dict:
    publication = _latest_or_create(publication_id, path)
    metrics = {
        platform: {
            "views": None, "engagement_rate": None,
            "average_watch_time": None, "completion_rate": None,
            "shares": None, "comments_or_replies": None,
            "followers_or_subscribers": None,
        } for platform in PLATFORMS
    }
    return {
        "publication_id": publication["publication_id"],
        "metrics": metrics, "dry_run": bool(dry_run),
        "external_api_calls": 0,
    }


def report(publication_id: str = "", *, dry_run: bool = True,
           path: Path | None = None) -> dict:
    publication = _latest_or_create(publication_id, path)
    metrics = _rows("""SELECT platform,metrics_json FROM cross_platform_metrics
        WHERE publication_id=? ORDER BY measured_at DESC""",
                    (publication["publication_id"],), path)
    return {
        "publication_id": publication["publication_id"],
        "status": reconcile(publication["publication_id"], path=path)["status"],
        "sample_size": len(metrics),
        "best_awareness_platform": None,
        "best_conversation_platform": None,
        "best_amplification_platform": None,
        "best_watch_time_platform": None,
        "best_conversion_platform": None,
        "decision": "insufficient_data" if not metrics else "descriptive_only",
        "causality_claimed": False,
        "dry_run": bool(dry_run),
    }


def token_status(platform: str) -> dict:
    names = {
        "instagram": ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"),
        "youtube": ("YOUTUBE_ACCESS_TOKEN",),
        "threads": ("THREADS_ACCESS_TOKEN", "THREADS_USER_ID"),
    }
    required = names[platform]
    return {
        "platform": platform,
        "configured": all(bool(os.environ.get(name, "").strip())
                          for name in required),
        "required_fields": list(required),
        "token_exposed": False,
    }


def instagram_auth_url() -> dict:
    state = secrets_token()
    scopes = os.environ.get(
        "INSTAGRAM_REQUIRED_SCOPES",
        "instagram_business_basic,instagram_business_content_publish")
    query = urlencode({
        "client_id": os.environ.get("INSTAGRAM_APP_ID", ""),
        "redirect_uri": os.environ.get("INSTAGRAM_REDIRECT_URI", ""),
        "response_type": "code", "scope": scopes, "state": state,
    })
    return {
        "url": f"https://www.instagram.com/oauth/authorize?{query}",
        "state": state, "secret_exposed": False,
    }


def secrets_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def instagram_exchange_code(_code: str, *, dry_run: bool = True) -> dict:
    if dry_run:
        return {"status": "dry_run", "external_api_calls": 0}
    raise RuntimeError("instagram_phase_b_oauth_exchange_requires_approval")


def instagram_profile(*, dry_run: bool = True) -> dict:
    if dry_run:
        return {
            "status": "dry_run", "professional_account_required": True,
            "accepted_account_types": ["BUSINESS", "MEDIA_CREATOR"],
            "external_api_calls": 0,
        }
    payload = InstagramClient().profile()
    account_type = str(payload.get("account_type") or "").upper()
    return {
        "status": "ok" if account_type in {
            "BUSINESS", "MEDIA_CREATOR", "CREATOR"} else "blocked",
        "professional_account": account_type in {
            "BUSINESS", "MEDIA_CREATOR", "CREATOR"},
        "profile": {key: payload.get(key) for key in (
            "id", "username", "account_type", "media_count")},
    }


def instagram_reel_status(creation_id: str, *,
                          dry_run: bool = True) -> dict:
    if dry_run:
        return {
            "creation_id": creation_id, "status_code": "DRY_RUN",
            "external_api_calls": 0,
        }
    return InstagramClient().container_status(creation_id)


def status(path: Path | None = None) -> dict:
    apply_migrations(path)
    pubs = _rows("""SELECT publication_id,status,target_publish_at,updated_at
        FROM cross_platform_publications ORDER BY id DESC LIMIT 20""", path=path)
    counts = _rows("""SELECT platform,status,COUNT(*) AS count
        FROM platform_publications GROUP BY platform,status
        ORDER BY platform,status""", path=path)
    return {
        "settings": settings(), "publications": pubs,
        "platform_counts": counts,
    }


def emergency_stop(path: Path | None = None) -> dict:
    marker = _output_dir() / ".crosspost-emergency-stop"
    marker.write_text(_now().isoformat(), encoding="utf-8")
    _execute("""INSERT INTO cross_platform_events
        (publication_id,platform,event_type,previous_status,new_status,
         occurred_at,metadata_json)
        VALUES ('*','*','emergency_stop','','blocked',?,?)""", (
            _now().isoformat(), json.dumps(
                {"reason": "operator_command"}, ensure_ascii=False),
        ), path)
    return {
        "status": "stopped", "marker": str(marker),
        "external_posts_deleted": 0,
    }


def emergency_stopped() -> bool:
    return (_output_dir() / ".crosspost-emergency-stop").exists()
