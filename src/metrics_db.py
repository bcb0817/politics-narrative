"""Fault-tolerant SQLite storage shared by phases 1-3."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from xai_cost import ticks_to_usd

JST = ZoneInfo("Asia/Tokyo")

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_candidates (
 id INTEGER PRIMARY KEY, source_type TEXT, source_name TEXT, source_url TEXT, title TEXT,
 summary TEXT, published_at TEXT, fetched_at TEXT, topic_key TEXT, genre TEXT,
 source_reliability_score REAL, freshness_score REAL, x_attention_score REAL,
 final_news_score REAL, verified INTEGER, verification_reason TEXT, is_major_update INTEGER,
 metadata_json TEXT, discovered_via_json TEXT, xai_topic_match INTEGER DEFAULT 0,
 xai_attention_score REAL, xai_velocity_score REAL, xai_discovered_at TEXT,
 xai_cost_allocated_usd REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS generated_posts (
 id INTEGER PRIMARY KEY, news_candidate_id INTEGER, prompt_version TEXT, model TEXT,
 post_type TEXT, hook_type TEXT, critique_axis TEXT, text TEXT, quality_score REAL,
 ban_risk REAL, decision TEXT, decision_reason TEXT, created_at TEXT,
 input_tokens INTEGER, cached_input_tokens INTEGER, output_tokens INTEGER,
 estimated_cost_usd REAL);
CREATE TABLE IF NOT EXISTS published_posts (
 id INTEGER PRIMARY KEY, generated_post_id INTEGER, tweet_id TEXT UNIQUE, text TEXT,
 posted_at TEXT, topic_key TEXT, post_type TEXT, hook_type TEXT, critique_axis TEXT,
 model TEXT, prompt_version TEXT, is_breaking INTEGER, discovered_via_json TEXT,
 xai_topic_match INTEGER DEFAULT 0, xai_attention_score REAL, xai_velocity_score REAL,
 xai_discovered_at TEXT, xai_cost_allocated_usd REAL DEFAULT 0,
 digest_type TEXT, digest_date TEXT, included_topic_keys_json TEXT,
 primary_topic_key TEXT, posted_hour_jst INTEGER,
 followers_before_window INTEGER, followers_after_window INTEGER,
 estimated_follower_delta REAL, conversion_confidence TEXT);
CREATE TABLE IF NOT EXISTS post_metrics (
 id INTEGER PRIMARY KEY, tweet_id TEXT, measurement_window TEXT, measured_at TEXT,
 impressions INTEGER, likes INTEGER, reposts INTEGER, replies INTEGER, quotes INTEGER,
 bookmarks INTEGER, profile_clicks INTEGER, url_clicks INTEGER, engagement_rate REAL,
 impressions_per_hour REAL, UNIQUE(tweet_id, measurement_window));
CREATE TABLE IF NOT EXISTS daily_reviews (
 id INTEGER PRIMARY KEY, review_date TEXT UNIQUE, generated_at TEXT, review_model TEXT,
 top_posts_json TEXT, bottom_posts_json TEXT, winning_patterns_json TEXT,
 losing_patterns_json TEXT, recommendations_json TEXT, input_tokens INTEGER,
 output_tokens INTEGER, estimated_cost_usd REAL);
CREATE TABLE IF NOT EXISTS weekly_reviews (
 id INTEGER PRIMARY KEY, week_start TEXT, week_end TEXT, generated_at TEXT,
 review_model TEXT, summary_json TEXT, media_expansion_json TEXT,
 recommendations_json TEXT, estimated_cost_usd REAL,
 UNIQUE(week_start, week_end));
CREATE TABLE IF NOT EXISTS api_usage_events (
 id INTEGER PRIMARY KEY, timestamp TEXT, provider TEXT, operation TEXT,
 model_or_endpoint TEXT, resource_count INTEGER, input_tokens INTEGER,
 cached_input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL,
 success INTEGER, fallback_used INTEGER, error_type TEXT, metadata_json TEXT,
 task_type_source TEXT);
CREATE TABLE IF NOT EXISTS prompt_versions (
 id INTEGER PRIMARY KEY, version TEXT UNIQUE, created_at TEXT, system_prompt_hash TEXT,
 description TEXT, is_active INTEGER);
CREATE TABLE IF NOT EXISTS xai_usage_events (
 id INTEGER PRIMARY KEY, request_id TEXT UNIQUE, timestamp TEXT, model TEXT, operation TEXT,
 schedule_slot TEXT, input_tokens INTEGER, output_tokens INTEGER,
 cached_input_tokens INTEGER, tool_call_count INTEGER, successful_tool_call_count INTEGER,
 cost_in_usd_ticks INTEGER, actual_cost_usd REAL, estimated_cost_usd REAL,
 cost_source TEXT, cache_used INTEGER DEFAULT 0, success INTEGER,
 error_type TEXT, metadata_json TEXT);
CREATE TABLE IF NOT EXISTS engagement_queue (
 id INTEGER PRIMARY KEY, queue_type TEXT, source_post_id TEXT, author_handle TEXT,
 author_type TEXT, topic_key TEXT, source_verified INTEGER, reason_selected TEXT,
 content_json TEXT, risk_flags_json TEXT, status TEXT, created_at TEXT,
 updated_at TEXT, expires_at TEXT, source_author TEXT, candidate_created_at TEXT,
 approved_at TEXT, posted_manually_at TEXT, manual_post_id TEXT, manual_post_url TEXT,
 selected_option TEXT, operator_notes TEXT, result_measurement_due_at TEXT,
 result_collected_at TEXT, UNIQUE(queue_type, source_post_id));
CREATE TABLE IF NOT EXISTS engagement_results (
 id INTEGER PRIMARY KEY, queue_id INTEGER, queue_type TEXT, manual_post_id TEXT,
 measurement_window TEXT, measured_at TEXT, impressions INTEGER, likes INTEGER,
 reposts INTEGER, replies INTEGER, quotes INTEGER, profile_clicks INTEGER,
 estimated_follower_delta REAL, source TEXT, metadata_json TEXT,
 UNIQUE(queue_id, measurement_window));
CREATE TABLE IF NOT EXISTS post_style_experiments (
 id INTEGER PRIMARY KEY, tweet_id TEXT, post_type TEXT, hook_type TEXT,
 is_exploration INTEGER, experiment_name TEXT, created_at TEXT, result_json TEXT);
CREATE TABLE IF NOT EXISTS openai_batch_jobs (
 id INTEGER PRIMARY KEY, batch_id TEXT UNIQUE, custom_id TEXT UNIQUE, task_type TEXT,
 model TEXT, status TEXT, input_file_id TEXT, output_file_id TEXT, error_file_id TEXT,
 reservation_id INTEGER, target_json_path TEXT, submitted_at TEXT, completed_at TEXT,
 result_json TEXT, error_type TEXT, metadata_json TEXT);
CREATE TABLE IF NOT EXISTS follower_snapshots (
 id INTEGER PRIMARY KEY, captured_at TEXT UNIQUE, followers_count INTEGER,
 following_count INTEGER, posts_count INTEGER, source TEXT, estimated INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS conversion_events (
 id INTEGER PRIMARY KEY, occurred_at TEXT, source TEXT, campaign TEXT, content_id TEXT,
 event_type TEXT, value REAL, metadata_json TEXT, event_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS conversion_event_quarantine (
 id INTEGER PRIMARY KEY, imported_at TEXT, source_file TEXT, row_json TEXT,
 rejection_reason TEXT, event_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS quality_eval_runs (
 id INTEGER PRIMARY KEY, run_at TEXT, mode TEXT, prompt_version TEXT, fixture_count INTEGER,
 passed_count INTEGER, failed_count INTEGER, average_scores_json TEXT,
 pass_rate REAL, automatic_disqualifications_json TEXT,
 estimated_cost_usd REAL, result_path TEXT);
CREATE TABLE IF NOT EXISTS content_pipeline_runs (
 id INTEGER PRIMARY KEY, week_start TEXT UNIQUE, generated_at TEXT,
 source_weekly_report TEXT, shorts_json TEXT, note_articles_json TEXT,
 openai_cost_usd REAL, status TEXT, manifest_path TEXT, metadata_json TEXT);
CREATE TABLE IF NOT EXISTS budget_change_events (
 id INTEGER PRIMARY KEY, changed_at TEXT UNIQUE, previous_total_budget_usd REAL,
 new_total_budget_usd REAL, previous_provider_budgets_json TEXT,
 new_provider_budgets_json TEXT, reason TEXT, metadata_json TEXT);
CREATE TABLE IF NOT EXISTS note_drafts (
 id INTEGER PRIMARY KEY, content_id TEXT UNIQUE, title TEXT, slug TEXT,
 article_type TEXT, status TEXT, generated_at TEXT, target_publish_date TEXT,
 published_at TEXT, note_url TEXT, prompt_version TEXT, model TEXT,
 character_count INTEGER, reading_minutes INTEGER, primary_topic_key TEXT,
 included_topic_keys_json TEXT, source_news_ids_json TEXT,
 source_x_post_ids_json TEXT, primary_sources_json TEXT,
 secondary_sources_json TEXT, draft_path TEXT,
 discord_notification_status TEXT, discord_message_id TEXT,
 input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL,
 quality_score REAL, safety_score REAL, cover_path TEXT, cover_status TEXT,
 cover_width INTEGER, cover_height INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS note_generation_runs (
 id INTEGER PRIMARY KEY, run_at TEXT, schedule_type TEXT,
 target_article_type TEXT, selected_topic TEXT, selection_reason TEXT,
 status TEXT, content_id TEXT, model TEXT, input_tokens INTEGER,
 output_tokens INTEGER, estimated_cost_usd REAL, error_type TEXT,
 metadata_json TEXT);
CREATE TABLE IF NOT EXISTS human_reviews (
 id INTEGER PRIMARY KEY, content_type TEXT, content_id TEXT, reviewed_at TEXT,
 scores_json TEXT, should_post INTEGER, notes TEXT, reviewer TEXT,
 UNIQUE(content_type, content_id, reviewed_at));
CREATE TABLE IF NOT EXISTS post_quality_dimensions (
 id INTEGER PRIMARY KEY, tweet_id TEXT UNIQUE, factuality_score REAL,
 relevance_score REAL, logic_score REAL, originality_score REAL,
 natural_japanese_score REAL, safety_score REAL, anger_score REAL,
 personal_attack_score REAL, partisan_bias_score REAL, claim_risk TEXT,
 trust_score REAL, correction_required INTEGER DEFAULT 0,
 manual_delete_required INTEGER DEFAULT 0, follow_conversion_estimate REAL,
 winner_types_json TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_usage_month ON api_usage_events(timestamp, provider);
CREATE INDEX IF NOT EXISTS idx_metrics_window ON post_metrics(tweet_id, measurement_window);
CREATE INDEX IF NOT EXISTS idx_xai_usage_month ON xai_usage_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_queue_status ON engagement_queue(queue_type, status);
CREATE INDEX IF NOT EXISTS idx_engagement_results_queue ON engagement_results(queue_id, measurement_window);
CREATE INDEX IF NOT EXISTS idx_openai_batch_status ON openai_batch_jobs(status, submitted_at);
CREATE INDEX IF NOT EXISTS idx_follower_captured ON follower_snapshots(captured_at);
CREATE INDEX IF NOT EXISTS idx_conversion_occurred ON conversion_events(occurred_at, event_type);
CREATE INDEX IF NOT EXISTS idx_quality_eval_run ON quality_eval_runs(run_at, prompt_version);
CREATE INDEX IF NOT EXISTS idx_human_review_content ON human_reviews(content_type, content_id);
CREATE INDEX IF NOT EXISTS idx_note_draft_status ON note_drafts(status, generated_at);
CREATE INDEX IF NOT EXISTS idx_note_generation_run ON note_generation_runs(run_at, status);
"""


def db_path(state_dir: Path | None = None) -> Path:
    if state_dir is None:
        root = Path(__file__).resolve().parent.parent
        raw = os.environ.get("STATE_DIR", "data")
        state_dir = Path(raw) if Path(raw).is_absolute() else root / raw
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "bot_metrics.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    resolved = Path(path) if path is not None else db_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: Path | None = None) -> bool:
    try:
        with closing(connect(path)) as conn:
            conn.executescript(SCHEMA)
            columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(xai_usage_events)")
            }
            if "request_id" in columns:
                conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_xai_request_id ON xai_usage_events(request_id)""")
            conn.commit()
        return True
    except sqlite3.Error as exc:
        print(f"SQLite unavailable; continuing with JSON ({type(exc).__name__})")
        return False


def write(sql: str, params=(), path: Path | None = None) -> int | None:
    for attempt in range(2):
        try:
            with closing(connect(path)) as conn:
                cur = conn.execute(sql, params)
                conn.commit()
                return int(cur.lastrowid or 0)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt == 0:
                time.sleep(0.05)
                continue
            print(f"SQLite write skipped; continuing ({type(exc).__name__})")
            return None
        except sqlite3.Error as exc:
            print(f"SQLite write skipped; continuing ({type(exc).__name__})")
            return None


def insert_news(item: dict, path: Path | None = None) -> int | None:
    discovered = list(dict.fromkeys(item.get("discovered_via") or [
        item.get("source_type") or "rss"
    ]))
    return write("""INSERT INTO news_candidates
      (source_type,source_name,source_url,title,summary,published_at,fetched_at,topic_key,genre,
       source_reliability_score,freshness_score,x_attention_score,final_news_score,verified,
       verification_reason,is_major_update,metadata_json,discovered_via_json,xai_topic_match,
       xai_attention_score,xai_velocity_score,xai_discovered_at,xai_cost_allocated_usd)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        item.get("source_type") or ",".join(item.get("discovered_via") or ["rss"]), item.get("source_name") or item.get("source", ""),
        item.get("url") or item.get("link", ""), item.get("title", ""), item.get("summary", ""),
        item.get("pub_date", ""), datetime.now(JST).isoformat(), item.get("topic_key", ""),
        item.get("genre", ""), item.get("source_reliability_score", 0), item.get("freshness_score", 0),
        item.get("x_attention_score", 0), item.get("final_news_score", 0),
        int(bool(item.get("verified") or item.get("externally_corroborated") or "rss" in (item.get("discovered_via") or []))),
        item.get("verification_reason", ""), int(bool(item.get("is_major_update"))),
        json.dumps(item, ensure_ascii=False, default=str),
        json.dumps(discovered, ensure_ascii=False), int(bool(item.get("xai_topic_match"))),
        item.get("xai_attention_score", item.get("x_attention_score")),
        item.get("xai_velocity_score"), item.get("xai_discovered_at"),
        float(item.get("xai_cost_allocated_usd", 0) or 0)), path)


def insert_generated(news_id: int | None, post: dict, path: Path | None = None) -> int | None:
    scores = post.get("scores") or {}
    generated_id = write("""INSERT INTO generated_posts
      (news_candidate_id,prompt_version,model,post_type,hook_type,critique_axis,text,quality_score,
       ban_risk,decision,decision_reason,created_at,input_tokens,cached_input_tokens,output_tokens,estimated_cost_usd)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        news_id, post.get("prompt_version", "x-growth-quality-v2"), post.get("openai_model", ""), post.get("post_type", ""),
        post.get("hook_type", ""), post.get("critique_axis", ""), post.get("tweet_text", ""),
        post.get("quality_score", post.get("overall", 0)), scores.get("ban_risk", post.get("ban_risk", 0)),
        post.get("decision", "generated"), post.get("decision_reason", ""), datetime.now(JST).isoformat(),
        post.get("input_tokens", 0), post.get("cached_input_tokens", 0), post.get("output_tokens", 0),
        post.get("estimated_cost_usd", 0)), path)
    if generated_id is not None:
        apply_additive_migrations(path)
        write("""UPDATE generated_posts SET factuality_score=?,relevance_score=?,logic_score=?,
          originality_score=?,natural_japanese_score=?,safety_score=?,anger_score=?,
          personal_attack_score=?,partisan_bias_score=?,claim_risk=?,trust_score=?,
          correction_required=?,manual_delete_required=? WHERE id=?""", (
            post.get("factuality_score"), post.get("relevance_score"), post.get("logic_score"),
            post.get("originality_score"), post.get("natural_japanese_score"),
            post.get("safety_score"), post.get("anger_score"),
            post.get("personal_attack_score"), post.get("partisan_bias_score"),
            post.get("claim_risk"), post.get("trust_score"),
            int(bool(post.get("correction_required"))),
            int(bool(post.get("manual_delete_required") or post.get("manual_delete"))),
            generated_id,
        ), path)
    return generated_id


def insert_published(generated_id: int | None, row: dict, path: Path | None = None) -> int | None:
    discovered = list(dict.fromkeys(row.get("discovered_via") or []))
    apply_additive_migrations(path)
    return write("""INSERT OR IGNORE INTO published_posts
      (generated_post_id,tweet_id,text,posted_at,topic_key,post_type,hook_type,critique_axis,
       model,prompt_version,is_breaking,discovered_via_json,xai_topic_match,xai_attention_score,
       xai_velocity_score,xai_discovered_at,xai_cost_allocated_usd,digest_type,digest_date,
       included_topic_keys_json,primary_topic_key,posted_hour_jst)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        generated_id, str(row.get("tweet_id", "")), row.get("tweet_text", ""),
        row.get("posted_at_jst") or row.get("posted_at", ""), row.get("topic_key", ""),
        row.get("post_type", ""), row.get("hook_type", ""), row.get("critique_axis", ""),
        row.get("openai_model", ""), row.get("prompt_version", "x-growth-quality-v2"),
        int(row.get("post_type") == "breaking_news"), json.dumps(discovered, ensure_ascii=False),
        int(bool(row.get("xai_topic_match"))), row.get("xai_attention_score"),
        row.get("xai_velocity_score"), row.get("xai_discovered_at"),
        float(row.get("xai_cost_allocated_usd", 0) or 0), row.get("digest_type"),
        row.get("digest_date"), json.dumps(row.get("included_topic_keys") or [],
                                           ensure_ascii=False),
        row.get("primary_topic_key") or row.get("topic_key"),
        row.get("posted_hour_jst")), path)


def upsert_metric(row: dict, path: Path | None = None) -> int | None:
    return write("""INSERT OR IGNORE INTO post_metrics
      (tweet_id,measurement_window,measured_at,impressions,likes,reposts,replies,quotes,bookmarks,
       profile_clicks,url_clicks,engagement_rate,impressions_per_hour) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        str(row.get("tweet_id", "")), row.get("measurement_window", "latest"), row.get("measured_at", ""),
        row.get("impressions", 0), row.get("likes", 0), row.get("reposts", 0), row.get("replies", 0),
        row.get("quotes", 0), row.get("bookmarks", 0), row.get("profile_clicks", 0), row.get("url_clicks", 0),
        row.get("engagement_rate", 0), row.get("impressions_per_hour", 0)), path)


def table_counts(path: Path | None = None) -> dict:
    init_db(path)
    out = {}
    try:
        with closing(connect(path)) as conn:
            for name in ("news_candidates", "generated_posts", "published_posts", "post_metrics",
                         "daily_reviews", "weekly_reviews", "api_usage_events", "prompt_versions",
                         "xai_usage_events", "engagement_queue", "post_style_experiments",
                         "openai_batch_jobs", "follower_snapshots", "conversion_events",
                         "quality_eval_runs", "human_reviews", "post_quality_dimensions",
                         "engagement_results", "conversion_event_quarantine",
                         "content_pipeline_runs", "budget_change_events",
                         "note_drafts", "note_generation_runs"):
                out[name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    except sqlite3.Error:
        pass
    return out


def add_column_if_missing(table: str, column: str, declaration: str,
                          path: Path | None = None) -> bool:
    """Idempotently extend an existing table without replacing historical rows."""
    allowed = {
        "generated_posts": {
            "factuality_score": "REAL", "relevance_score": "REAL",
            "logic_score": "REAL", "originality_score": "REAL",
            "natural_japanese_score": "REAL", "safety_score": "REAL",
            "anger_score": "REAL", "personal_attack_score": "REAL",
            "partisan_bias_score": "REAL", "claim_risk": "TEXT",
            "trust_score": "REAL", "correction_required": "INTEGER DEFAULT 0",
            "manual_delete_required": "INTEGER DEFAULT 0",
        },
        "daily_reviews": {
            "four_axes_json": "TEXT", "winner_groups_json": "TEXT",
            "prompt_version_comparison_json": "TEXT", "local_only": "INTEGER DEFAULT 0",
        },
        "news_candidates": {
            "discovered_via_json": "TEXT", "xai_topic_match": "INTEGER DEFAULT 0",
            "xai_attention_score": "REAL", "xai_velocity_score": "REAL",
            "xai_discovered_at": "TEXT", "xai_cost_allocated_usd": "REAL DEFAULT 0",
        },
        "published_posts": {
            "discovered_via_json": "TEXT", "xai_topic_match": "INTEGER DEFAULT 0",
            "xai_attention_score": "REAL", "xai_velocity_score": "REAL",
            "xai_discovered_at": "TEXT", "xai_cost_allocated_usd": "REAL DEFAULT 0",
            "digest_type": "TEXT", "digest_date": "TEXT",
            "included_topic_keys_json": "TEXT", "primary_topic_key": "TEXT",
            "posted_hour_jst": "INTEGER", "followers_before_window": "INTEGER",
            "followers_after_window": "INTEGER", "estimated_follower_delta": "REAL",
            "conversion_confidence": "TEXT",
        },
        "xai_usage_events": {
            "request_id": "TEXT", "schedule_slot": "TEXT",
            "cached_input_tokens": "INTEGER DEFAULT 0",
            "successful_tool_call_count": "INTEGER DEFAULT 0",
            "estimated_cost_usd": "REAL DEFAULT 0", "cost_source": "TEXT",
            "cache_used": "INTEGER DEFAULT 0",
        },
        "engagement_queue": {
            "source_author": "TEXT", "candidate_created_at": "TEXT",
            "approved_at": "TEXT", "posted_manually_at": "TEXT",
            "manual_post_id": "TEXT", "manual_post_url": "TEXT",
            "selected_option": "TEXT", "operator_notes": "TEXT",
            "result_measurement_due_at": "TEXT", "result_collected_at": "TEXT",
        },
        "quality_eval_runs": {
            "pass_rate": "REAL",
            "automatic_disqualifications_json": "TEXT",
        },
        "conversion_events": {"event_key": "TEXT"},
        "api_usage_events": {"task_type_source": "TEXT"},
        "note_drafts": {
            "cover_path": "TEXT", "cover_status": "TEXT",
            "cover_width": "INTEGER", "cover_height": "INTEGER",
        },
    }
    if declaration != allowed.get(table, {}).get(column):
        raise ValueError("unsupported migration")
    try:
        with closing(connect(path)) as conn:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column in existing:
                return False
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            conn.commit()
        return True
    except sqlite3.Error:
        return False


def apply_additive_migrations(path: Path | None = None) -> dict:
    """Apply all additive migrations safely on every startup."""
    init_db(path)
    migrations = {
        "generated_posts": {
            "factuality_score": "REAL", "relevance_score": "REAL",
            "logic_score": "REAL", "originality_score": "REAL",
            "natural_japanese_score": "REAL", "safety_score": "REAL",
            "anger_score": "REAL", "personal_attack_score": "REAL",
            "partisan_bias_score": "REAL", "claim_risk": "TEXT",
            "trust_score": "REAL", "correction_required": "INTEGER DEFAULT 0",
            "manual_delete_required": "INTEGER DEFAULT 0",
        },
        "daily_reviews": {
            "four_axes_json": "TEXT", "winner_groups_json": "TEXT",
            "prompt_version_comparison_json": "TEXT", "local_only": "INTEGER DEFAULT 0",
        },
        "news_candidates": {
            "discovered_via_json": "TEXT", "xai_topic_match": "INTEGER DEFAULT 0",
            "xai_attention_score": "REAL", "xai_velocity_score": "REAL",
            "xai_discovered_at": "TEXT", "xai_cost_allocated_usd": "REAL DEFAULT 0",
        },
        "published_posts": {
            "discovered_via_json": "TEXT", "xai_topic_match": "INTEGER DEFAULT 0",
            "xai_attention_score": "REAL", "xai_velocity_score": "REAL",
            "xai_discovered_at": "TEXT", "xai_cost_allocated_usd": "REAL DEFAULT 0",
            "digest_type": "TEXT", "digest_date": "TEXT",
            "included_topic_keys_json": "TEXT", "primary_topic_key": "TEXT",
            "posted_hour_jst": "INTEGER", "followers_before_window": "INTEGER",
            "followers_after_window": "INTEGER", "estimated_follower_delta": "REAL",
            "conversion_confidence": "TEXT",
        },
        "xai_usage_events": {
            "request_id": "TEXT", "schedule_slot": "TEXT",
            "cached_input_tokens": "INTEGER DEFAULT 0",
            "successful_tool_call_count": "INTEGER DEFAULT 0",
            "estimated_cost_usd": "REAL DEFAULT 0", "cost_source": "TEXT",
            "cache_used": "INTEGER DEFAULT 0",
        },
        "engagement_queue": {
            "source_author": "TEXT", "candidate_created_at": "TEXT",
            "approved_at": "TEXT", "posted_manually_at": "TEXT",
            "manual_post_id": "TEXT", "manual_post_url": "TEXT",
            "selected_option": "TEXT", "operator_notes": "TEXT",
            "result_measurement_due_at": "TEXT", "result_collected_at": "TEXT",
        },
        "quality_eval_runs": {
            "pass_rate": "REAL",
            "automatic_disqualifications_json": "TEXT",
        },
        "conversion_events": {"event_key": "TEXT"},
        "api_usage_events": {"task_type_source": "TEXT"},
        "note_drafts": {
            "cover_path": "TEXT", "cover_status": "TEXT",
            "cover_width": "INTEGER", "cover_height": "INTEGER",
        },
    }
    added = {
        table: [
            column for column, declaration in columns.items()
            if add_column_if_missing(table, column, declaration, path)
        ]
        for table, columns in migrations.items()
    }
    try:
        with closing(connect(path)) as conn:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_xai_request_id "
                "ON xai_usage_events(request_id)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_conversion_event_key "
                "ON conversion_events(event_key)"
            )
            conn.execute("""UPDATE xai_usage_events
                SET request_id='legacy-xai-' || id WHERE request_id IS NULL OR request_id=''""")
            conn.execute("""UPDATE xai_usage_events SET
                cached_input_tokens=COALESCE(cached_input_tokens,0),
                successful_tool_call_count=CASE
                    WHEN success=1 AND COALESCE(successful_tool_call_count,0)=0
                    THEN COALESCE(tool_call_count,0)
                    ELSE COALESCE(successful_tool_call_count,0) END,
                estimated_cost_usd=COALESCE(estimated_cost_usd,actual_cost_usd,0),
                cost_source=CASE WHEN COALESCE(cost_in_usd_ticks,0)>0
                    THEN 'actual' ELSE COALESCE(cost_source,'estimated') END,
                cache_used=COALESCE(cache_used,0)""")
            # Import legacy xAI-only diagnostics that were recorded solely in the
            # generic table. Matching radar rows are intentionally not duplicated.
            legacy = conn.execute("""SELECT * FROM api_usage_events
                WHERE provider='xai' ORDER BY id""").fetchall()
            for row in legacy:
                match = conn.execute("""SELECT 1 FROM xai_usage_events
                    WHERE operation=? AND ABS(COALESCE(actual_cost_usd,0)-?)<0.000000001
                    AND ABS(strftime('%s',timestamp)-strftime('%s',?))<180 LIMIT 1""",
                    (row["operation"], float(row["estimated_cost_usd"] or 0),
                     row["timestamp"])).fetchone()
                if match:
                    continue
                metadata = json.loads(row["metadata_json"] or "{}")
                ticks = int(metadata.get("cost_in_usd_ticks", 0) or 0)
                actual = ticks_to_usd(ticks) if ticks else float(
                    row["estimated_cost_usd"] or 0)
                conn.execute("""INSERT OR IGNORE INTO xai_usage_events
                    (request_id,timestamp,model,operation,schedule_slot,input_tokens,
                     output_tokens,cached_input_tokens,tool_call_count,
                     successful_tool_call_count,cost_in_usd_ticks,actual_cost_usd,
                     estimated_cost_usd,cost_source,cache_used,success,error_type,metadata_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    f"legacy-api-xai-{row['id']}", row["timestamp"],
                    row["model_or_endpoint"] or "unknown", row["operation"], "",
                    int(row["input_tokens"] or 0), int(row["output_tokens"] or 0),
                    int(row["cached_input_tokens"] or 0), int(row["resource_count"] or 0),
                    int(row["resource_count"] or 0) if row["success"] else 0, ticks,
                    actual, float(row["estimated_cost_usd"] or 0),
                    "actual" if ticks else "estimated", 0, int(row["success"] or 0),
                    row["error_type"] or "", json.dumps({
                        **metadata, "legacy_api_usage_event_id": row["id"]
                    }, ensure_ascii=False)))
            conn.commit()
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        pass
    return added


def record_budget_change(event: dict, path: Path | None = None) -> int | None:
    """Persist a configuration change without mutating historical usage."""
    init_db(path)
    return write("""INSERT OR IGNORE INTO budget_change_events
      (changed_at,previous_total_budget_usd,new_total_budget_usd,
       previous_provider_budgets_json,new_provider_budgets_json,reason,metadata_json)
      VALUES (?,?,?,?,?,?,?)""", (
        event.get("changed_at", datetime.now(JST).isoformat()),
        float(event.get("previous_total_budget_usd", 0) or 0),
        float(event.get("new_total_budget_usd", 0) or 0),
        json.dumps(event.get("previous_provider_budgets", {}), ensure_ascii=False),
        json.dumps(event.get("new_provider_budgets", {}), ensure_ascii=False),
        str(event.get("reason", "")),
        json.dumps(event.get("metadata", {}), ensure_ascii=False),
    ), path)


def migrate_json_state(state_dir: Path, path: Path | None = None) -> dict:
    """Idempotently seed SQLite from preserved JSON history."""
    path = path or db_path(state_dir)
    init_db(path)
    migrated = {"published_posts": 0, "prompt_versions": 0, "openai_usage": 0}
    try:
        history = json.loads((state_dir / "posted_urls.json").read_text(encoding="utf-8"))
    except Exception:
        history = []
    for row in history if isinstance(history, list) else []:
        before = table_counts(path).get("published_posts", 0)
        insert_published(None, row, path)
        after = table_counts(path).get("published_posts", 0)
        migrated["published_posts"] += max(0, after - before)
    version = os.environ.get("PROMPT_VERSION", "x-growth-quality-v2")
    value = write("""INSERT OR IGNORE INTO prompt_versions
      (version,created_at,system_prompt_hash,description,is_active) VALUES (?,?,?,?,1)""",
      (version, datetime.now(JST).isoformat(), "runtime", "Current prompt configuration"), path)
    migrated["prompt_versions"] = int(value is not None)
    try:
        usage = json.loads((state_dir / "openai_usage.json").read_text(encoding="utf-8"))
        month = str(usage.get("month", ""))
        with closing(connect(path)) as conn:
            exists = conn.execute("""SELECT 1 FROM api_usage_events
                WHERE provider='openai' AND operation='legacy_json_import' AND timestamp LIKE ? LIMIT 1""",
                (month + "%",)).fetchone()
        if not exists and float(usage.get("estimated_cost_usd", 0) or 0) > 0:
            timestamp = f"{month}-01T00:00:00+09:00"
            write("""INSERT INTO api_usage_events
              (timestamp,provider,operation,model_or_endpoint,resource_count,input_tokens,cached_input_tokens,
               output_tokens,estimated_cost_usd,success,fallback_used,error_type,metadata_json)
               VALUES (?,?,?,?,?,?,?,?,?,1,0,'',?)""", (
                timestamp, "openai", "legacy_json_import", "legacy_aggregate", 0,
                int(usage.get("input_tokens", 0) or 0), int(usage.get("cached_input_tokens", 0) or 0),
                int(usage.get("output_tokens", 0) or 0), float(usage.get("estimated_cost_usd", 0) or 0),
                json.dumps({"source": "data/openai_usage.json"})), path)
            migrated["openai_usage"] = 1
    except Exception:
        pass
    return migrated
