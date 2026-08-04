"""SQLite persistence for the short-video factory."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from metrics_db import connect, db_path, init_db

JST = ZoneInfo("Asia/Tokyo")

SCHEMA = """
CREATE TABLE IF NOT EXISTS short_video_projects (
 id INTEGER PRIMARY KEY, video_id TEXT UNIQUE, topic_id INTEGER,
 content_packet_id TEXT, topic_key TEXT, title TEXT, angle TEXT,
 phase TEXT, status TEXT, candidate_score REAL, quality_score REAL,
 safety_score REAL, publish_eligible INTEGER DEFAULT 0,
 master_video_path TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS short_video_scripts (
 id INTEGER PRIMARY KEY, video_id TEXT, version INTEGER, hook TEXT,
 narration TEXT, scenes_json TEXT, duration_target_seconds INTEGER,
 source_topic_updated_at TEXT, status TEXT, created_at TEXT,
 UNIQUE(video_id,version));
CREATE TABLE IF NOT EXISTS short_video_claims (
 id INTEGER PRIMARY KEY, video_id TEXT, claim_text TEXT, claim_type TEXT,
 evidence_urls_json TEXT, verified INTEGER, verification_reason TEXT,
 created_at TEXT);
CREATE TABLE IF NOT EXISTS short_video_audio_assets (
 id INTEGER PRIMARY KEY, video_id TEXT, provider TEXT, local_path TEXT,
 duration_seconds REAL, sample_rate INTEGER, status TEXT, created_at TEXT,
 UNIQUE(video_id,local_path));
CREATE TABLE IF NOT EXISTS short_video_visual_assets (
 id INTEGER PRIMARY KEY, video_id TEXT, asset_type TEXT, scene_index INTEGER,
 local_path TEXT, plan_json TEXT, status TEXT, created_at TEXT,
 UNIQUE(video_id,asset_type,scene_index));
CREATE TABLE IF NOT EXISTS short_video_render_jobs (
 id INTEGER PRIMARY KEY, video_id TEXT, master_path TEXT, command_json TEXT,
 ffmpeg_available INTEGER, status TEXT, error_type TEXT,
 started_at TEXT, completed_at TEXT);
CREATE TABLE IF NOT EXISTS short_video_quality_checks (
 id INTEGER PRIMARY KEY, video_id TEXT, check_type TEXT, passed INTEGER,
 score REAL, details_json TEXT, checked_at TEXT,
 UNIQUE(video_id,check_type));
CREATE TABLE IF NOT EXISTS short_video_publication_queue (
 id INTEGER PRIMARY KEY, video_id TEXT, platform TEXT, scheduled_at TEXT,
 idempotency_key TEXT UNIQUE, status TEXT, retry_count INTEGER DEFAULT 0,
 last_error_type TEXT, created_at TEXT, updated_at TEXT,
 UNIQUE(video_id,platform));
CREATE TABLE IF NOT EXISTS short_video_publications (
 id INTEGER PRIMARY KEY, video_id TEXT, platform TEXT,
 external_container_id TEXT, external_post_id TEXT, external_url TEXT,
 status TEXT, published_at TEXT, metrics_due_at TEXT, error_type TEXT,
 created_at TEXT, updated_at TEXT, UNIQUE(video_id,platform));
CREATE TABLE IF NOT EXISTS short_video_metrics (
 id INTEGER PRIMARY KEY, video_id TEXT, platform TEXT,
 measurement_window TEXT, measured_at TEXT, views INTEGER, likes INTEGER,
 replies INTEGER, reposts INTEGER, shares INTEGER, watch_seconds REAL,
 completion_rate REAL, raw_json TEXT,
 UNIQUE(video_id,platform,measurement_window));
CREATE TABLE IF NOT EXISTS short_video_experiments (
 id INTEGER PRIMARY KEY, experiment_id TEXT, video_id TEXT, platform TEXT,
 variant TEXT, hypothesis TEXT, status TEXT, result_json TEXT, created_at TEXT,
 UNIQUE(experiment_id,video_id,platform,variant));
CREATE TABLE IF NOT EXISTS short_video_weekly_reviews (
 id INTEGER PRIMARY KEY, week_start TEXT UNIQUE, generated_at TEXT,
 sample_count INTEGER, summary_json TEXT, recommendations_json TEXT,
 status TEXT);
CREATE TABLE IF NOT EXISTS short_video_stage_events (
 id INTEGER PRIMARY KEY, video_id TEXT, stage TEXT, status TEXT,
 detail_json TEXT, occurred_at TEXT);
CREATE TABLE IF NOT EXISTS short_video_system_state (
 state_key TEXT PRIMARY KEY, state_value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS short_video_public_media_tokens (
 id INTEGER PRIMARY KEY, video_id TEXT, token_hash TEXT UNIQUE,
 local_path TEXT, expires_at TEXT, revoked_at TEXT, download_count INTEGER DEFAULT 0,
 last_downloaded_at TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_short_video_project_status
 ON short_video_projects(status,created_at);
CREATE INDEX IF NOT EXISTS idx_short_video_queue_due
 ON short_video_publication_queue(status,scheduled_at);
"""


def resolve_db(path: Path | None = None) -> Path:
    return Path(path) if path else db_path()


def migrate(path: Path | None = None) -> bool:
    target = resolve_db(path)
    if not init_db(target):
        return False
    with closing(connect(target)) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            """INSERT OR IGNORE INTO short_video_system_state
               (state_key,state_value,updated_at) VALUES ('emergency_stop','false',?)""",
            (datetime.now(JST).isoformat(),),
        )
        conn.commit()
    return True


def rows(sql: str, params=(), path: Path | None = None) -> list[dict]:
    migrate(path)
    with closing(connect(resolve_db(path))) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def write(sql: str, params=(), path: Path | None = None) -> int:
    migrate(path)
    with closing(connect(resolve_db(path))) as conn:
        cursor = conn.execute(sql, params)
        conn.commit()
        return int(cursor.lastrowid or 0)


def event(video_id: str, stage: str, status: str, detail: dict,
          path: Path | None = None) -> None:
    write(
        """INSERT INTO short_video_stage_events
           (video_id,stage,status,detail_json,occurred_at) VALUES (?,?,?,?,?)""",
        (video_id, stage, status, json.dumps(detail, ensure_ascii=False),
         datetime.now(JST).isoformat()),
        path,
    )


def api_event(task_type: str, operation: str, success: bool = True,
              cost_usd: float = 0.0, metadata: dict | None = None,
              provider: str = "local", endpoint: str = "local",
              path: Path | None = None) -> None:
    """Record short-video cost without inventing non-local API spend."""
    write(
        """INSERT INTO api_usage_events
           (timestamp,provider,operation,model_or_endpoint,resource_count,
            input_tokens,cached_input_tokens,output_tokens,estimated_cost_usd,
            success,fallback_used,error_type,metadata_json,task_type_source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (datetime.now(JST).isoformat(), provider, operation, endpoint, 1,
         0, 0, 0, float(cost_usd), int(success), 0,
         None if success else "short_video_stage_failed",
         json.dumps(metadata or {}, ensure_ascii=False), task_type),
        path,
    )
