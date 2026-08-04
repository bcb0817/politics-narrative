"""Phase A-only performance experiments: audit, features, backtests and plans."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import statistics
import tempfile
import uuid
import random
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
PHASE = "A"
SPECIFIC_REPLIES = {
    "specific_question", "substantive_agreement", "substantive_disagreement",
    "additional_evidence", "personal_experience",
}
TOXIC_REPLIES = {
    "party_attack", "personal_attack", "attribute_attack", "insult_only",
    "spam", "off_topic",
}
PREDICTION_FEATURES = (
    "hook_strength", "specificity", "new_information_value",
    "quoteability", "fact_strength",
)
BINARY_FEATURES = (
    "specific_number_present", "specific_actor_present", "cost_bearer_present",
    "decision_maker_present", "human_stake_present", "contrast_present",
    "surprise_present", "comparison_present", "future_scenario_present",
    "counterargument_present", "concrete_question_present",
    "memorable_line_present", "generic_conclusion_flag",
)
CONTINUOUS_FEATURES = (
    "character_count", "average_sentence_length", "abstract_term_ratio",
    "noun_density", "previous_post_interval_hours",
    "previous_24h_normalized_impressions",
    "same_hook_recent_count", "same_structure_recent_count",
    "semantic_similarity_recent",
)
ANALYSIS_OUTCOMES = (
    "raw_impressions", "normalized_impressions", "profile_click_rate",
    "quote_rate", "repost_rate", "engagement_rate",
)
SOCIAL_SECURITY_FIXTURE = {
    "content_id": "fixture-social-security-r6",
    "topic_key": "social-security-benefits-r6",
    "text": (
        "138兆3019億円。令和6年度の社会保障給付費は3年ぶりに増えた。\n"
        "前年度比でおよそ2兆6700億円増。年金・医療・介護に支払われた。\n\n"
        "問題は金額だけではない。\n"
        "どの制度を維持し、どこを見直すかの決定根拠が見えないまま増え続けることだ。"
    ),
    "fact_packet": {
        "title": "令和6年度の社会保障給付費は138兆3019億円",
        "summary": (
            "3年ぶりに増加し、前年度比およそ2兆6700億円増。"
            "年金・医療・介護への給付。制度ごとの維持・見直し判断は別途検証が必要。"
        ),
        "numbers": ["138兆3019億円", "およそ2兆6700億円", "3年ぶり"],
        "sources": ["確認済みfact packet"],
        "prohibited_inferences": ["根拠のない一人あたり換算"],
    },
}


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _int(name: str, default: int, low: int = 0, high: int = 100000) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def settings() -> dict:
    return {
        "enabled": _bool("POST_EXPERIMENTS_ENABLED", "true"),
        "auto_publish": False,
        "min_sample": _int("POST_EXPERIMENT_MIN_SAMPLE_SIZE", 20, 2),
        "recommended_sample": _int(
            "POST_EXPERIMENT_RECOMMENDED_SAMPLE_SIZE", 50, 2),
        "rollout_sample": _int(
            "POST_EXPERIMENT_FULL_ROLLOUT_SAMPLE_SIZE", 100, 2),
        "normal_variants": _int(
            "POST_EXPERIMENT_MIN_VARIANT_CANDIDATES_NORMAL", 3, 3, 7),
        "important_variants": _int(
            "POST_EXPERIMENT_MIN_VARIANT_CANDIDATES_IMPORTANT", 5, 5, 7),
        "max_similarity": float(os.environ.get(
            "POST_EXPERIMENT_MAX_CANDIDATE_SIMILARITY", ".78")),
        "prediction_limit": min(5, _int(
            "POST_EXPERIMENT_PREDICTION_FEATURE_LIMIT", 5, 1, 5)),
    }


SCHEMA = """
CREATE TABLE IF NOT EXISTS post_performance_features (
 id INTEGER PRIMARY KEY, post_id TEXT, content_id TEXT, platform TEXT,
 topic_key TEXT, category TEXT, post_type TEXT, hook_type TEXT,
 angle_type TEXT, structure_type TEXT, ending_type TEXT,
 model TEXT, published_at TEXT, weekday TEXT, publish_hour INTEGER,
 character_count INTEGER, line_count INTEGER, sentence_count INTEGER,
 media_present INTEGER, url_present INTEGER, breaking_flag INTEGER,
 digest_flag INTEGER, source_count INTEGER, source_quality_score REAL,
 topic_demand_score REAL, news_freshness_hours REAL,
 x_search_post_count INTEGER, x_search_velocity REAL,
 x_search_unique_authors INTEGER, x_search_engagement REAL,
 official_news_count INTEGER, audience_relevance_score REAL,
 specific_number_present INTEGER, specific_actor_present INTEGER,
 cost_bearer_present INTEGER, decision_maker_present INTEGER,
 human_stake_present INTEGER, contrast_present INTEGER, surprise_present INTEGER,
 comparison_present INTEGER, future_scenario_present INTEGER,
 counterargument_present INTEGER, concrete_question_present INTEGER,
 memorable_line_present INTEGER, abstract_term_ratio REAL,
 average_sentence_length REAL, longest_sentence_length INTEGER,
 reader_effect TEXT, target_audience TEXT,
 question_count INTEGER, emoji_count INTEGER, hashtag_count INTEGER,
 sensational_term_count INTEGER, generic_conclusion_flag INTEGER,
 problem_wa_pattern INTEGER, should_end_pattern INTEGER,
 same_hook_recent_count INTEGER, same_structure_recent_count INTEGER,
 same_ending_recent_count INTEGER, semantic_similarity_recent REAL,
 noun_density REAL, previous_post_interval_hours REAL,
 previous_24h_normalized_impressions REAL, major_news_flag INTEGER,
 topic_competition_score REAL, topic_saturation_score REAL,
 ban_risk REAL, defamation_risk REAL, unsupported_number_flag INTEGER,
 unsupported_claim_flag INTEGER, generalization_risk REAL,
 mob_targeting_risk REAL,
 feature_json TEXT, created_at TEXT, updated_at TEXT,
 UNIQUE(post_id,platform));
CREATE TABLE IF NOT EXISTS post_performance_outcomes (
 id INTEGER PRIMARY KEY, post_id TEXT, captured_at TEXT,
 measurement_window TEXT, followers_at_publish INTEGER,
 impressions INTEGER, views INTEGER, likes INTEGER, reposts INTEGER,
 quotes INTEGER, replies INTEGER, bookmarks INTEGER, shares INTEGER,
 profile_clicks INTEGER, link_clicks INTEGER, follows INTEGER,
 video_views INTEGER, video_completion REAL,
 like_rate REAL, repost_rate REAL, quote_rate REAL, reply_rate REAL,
 bookmark_rate REAL, share_rate REAL, profile_click_rate REAL,
 follow_conversion_rate REAL, engagement_rate REAL,
 specific_reply_rate REAL, outcome_json TEXT, created_at TEXT,
 UNIQUE(post_id,measurement_window));
CREATE TABLE IF NOT EXISTS post_experiments (
 id INTEGER PRIMARY KEY, experiment_id TEXT UNIQUE, topic_key TEXT,
 content_id TEXT, platform TEXT, fact_packet_hash TEXT,
 control_candidate_id TEXT, variant_candidate_ids_json TEXT,
 assignment_group TEXT, assignment_stratum TEXT,
 prediction_version TEXT, selection_status TEXT,
 published_candidate_id TEXT, published_candidate_type TEXT,
 result_status TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS post_candidate_predictions (
 id INTEGER PRIMARY KEY, experiment_id TEXT, candidate_id TEXT,
 candidate_type TEXT, hook_strength REAL, specificity REAL,
 new_information_value REAL, quoteability REAL, fact_strength REAL,
 predicted_performance_score REAL, prediction_json TEXT, created_at TEXT,
 UNIQUE(experiment_id,candidate_id));
CREATE TABLE IF NOT EXISTS post_experiment_results (
 id INTEGER PRIMARY KEY, experiment_id TEXT, candidate_id TEXT,
 candidate_type TEXT, sample_group TEXT,
 normalized_impression_score REAL, normalized_profile_click_score REAL,
 normalized_follow_score REAL, normalized_quote_score REAL,
 result_json TEXT, evaluated_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS post_feature_correlations (
 id INTEGER PRIMARY KEY, feature_name TEXT, outcome_name TEXT,
 sample_size INTEGER, pearson_correlation REAL, spearman_correlation REAL,
 confidence_low REAL, confidence_high REAL,
 analysis_period_start TEXT, analysis_period_end TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS post_experiment_recommendations (
 id INTEGER PRIMARY KEY, analysis_period_start TEXT, analysis_period_end TEXT,
 recommendation_type TEXT, feature_name TEXT, control_metric REAL,
 variant_metric REAL, sample_size INTEGER, confidence REAL,
 recommendation TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS post_experiment_snapshots (
 id INTEGER PRIMARY KEY, experiment_id TEXT, snapshot_type TEXT,
 snapshot_hash TEXT, payload_json TEXT, source_timestamp TEXT,
 captured_at TEXT, created_at TEXT,
 UNIQUE(experiment_id,snapshot_type));
CREATE TABLE IF NOT EXISTS post_reply_events (
 id INTEGER PRIMARY KEY, platform TEXT, root_post_id TEXT, reply_id TEXT,
 author_hash TEXT, reply_text TEXT, reply_classification TEXT,
 replied_at TEXT, source TEXT, collected_at TEXT,
 UNIQUE(platform,reply_id));
CREATE TABLE IF NOT EXISTS post_experiment_approvals (
 id INTEGER PRIMARY KEY, experiment_id TEXT, candidate_id TEXT,
 decision TEXT, approved_by TEXT, reason TEXT, decided_at TEXT,
 UNIQUE(experiment_id,candidate_id));
CREATE TABLE IF NOT EXISTS post_experiment_publications (
 id INTEGER PRIMARY KEY, experiment_id TEXT, candidate_id TEXT,
 candidate_type TEXT, platform TEXT, post_id TEXT, published_at TEXT,
 link_status TEXT, created_at TEXT,
 UNIQUE(platform,post_id), UNIQUE(experiment_id,candidate_id));
CREATE INDEX IF NOT EXISTS idx_post_features_topic
 ON post_performance_features(platform,topic_key,post_type);
CREATE INDEX IF NOT EXISTS idx_post_outcomes_window
 ON post_performance_outcomes(measurement_window,captured_at);
CREATE INDEX IF NOT EXISTS idx_post_reply_root
 ON post_reply_events(platform,root_post_id,replied_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_post_experiment_one_fact_platform
 ON post_experiments(fact_packet_hash,platform)
 WHERE selection_status IN ('assigned','approved','published');
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def resolve_db_path(root: Path) -> tuple[Path, bool]:
    """Use the live DB when available, otherwise a local analysis shadow.

    WSL cannot read a SQLite WAL that is held by the Windows bot process. The
    shadow keeps dry-run/audit usable without touching, stopping, or replacing
    that live database.
    """
    live = root / "data" / "bot_metrics.db"
    try:
        with closing(connect(live)) as conn:
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()
        return live, False
    except sqlite3.Error:
        shadow_dir = Path(tempfile.gettempdir()) / "politics-post-experiments"
        shadow_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
        shadow = shadow_dir / f"bot_metrics-{digest}.db"
        if live.exists() and (
                not shadow.exists() or live.stat().st_mtime > shadow.stat().st_mtime):
            shutil.copy2(live, shadow)
        if not shadow.exists():
            raise
        return shadow, True


def apply_migrations(path: Path) -> bool:
    try:
        with closing(connect(path)) as conn:
            conn.executescript(SCHEMA)
            columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(post_performance_features)")
            }
            for name, declaration in {
                "model": "TEXT",
                "published_at": "TEXT",
                "weekday": "TEXT",
                "publish_hour": "INTEGER",
                "url_present": "INTEGER",
                "digest_flag": "INTEGER",
                "source_count": "INTEGER",
                "x_search_post_count": "INTEGER",
                "x_search_velocity": "REAL",
                "x_search_unique_authors": "INTEGER",
                "x_search_engagement": "REAL",
                "official_news_count": "INTEGER",
                "audience_relevance_score": "REAL",
                "longest_sentence_length": "INTEGER",
                "reader_effect": "TEXT",
                "target_audience": "TEXT",
                "question_count": "INTEGER",
                "emoji_count": "INTEGER",
                "hashtag_count": "INTEGER",
                "sensational_term_count": "INTEGER",
                "problem_wa_pattern": "INTEGER",
                "should_end_pattern": "INTEGER",
                "noun_density": "REAL",
                "previous_post_interval_hours": "REAL",
                "previous_24h_normalized_impressions": "REAL",
                "major_news_flag": "INTEGER",
                "topic_competition_score": "REAL",
                "topic_saturation_score": "REAL",
                "ban_risk": "REAL",
                "defamation_risk": "REAL",
                "unsupported_number_flag": "INTEGER",
                "unsupported_claim_flag": "INTEGER",
                "generalization_risk": "REAL",
                "mob_targeting_risk": "REAL",
            }.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE post_performance_features "
                        f"ADD COLUMN {name} {declaration}")
            outcome_columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(post_performance_outcomes)")
            }
            for name, declaration in {
                "video_views": "INTEGER",
                "video_completion": "REAL",
            }.items():
                if name not in outcome_columns:
                    conn.execute(
                        f"ALTER TABLE post_performance_outcomes "
                        f"ADD COLUMN {name} {declaration}")
            experiment_columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(post_experiments)")
            }
            if "assignment_stratum" not in experiment_columns:
                conn.execute(
                    "ALTER TABLE post_experiments ADD COLUMN assignment_stratum TEXT")
            conn.commit()
        return True
    except sqlite3.Error:
        return False


def safe_rate(numerator: Any, denominator: Any) -> float | None:
    if denominator is None:
        return None
    try:
        denominator = float(denominator)
        if denominator <= 0 or numerator is None:
            return None
        return float(numerator) / denominator
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def classify_reply(text: str) -> str:
    text = str(text or "").strip()
    if not text:
        return "unknown"
    # Classify the target of abuse before the generic insult bucket.  This
    # keeps party/attribute attacks observable as separate safety outcomes.
    if re.search(
            r"(自民|立憲|維新|共産|公明|国民民主|れいわ|参政)"
            r".{0,12}(信者|工作員|ゴミ|無能|売国)", text):
        return "party_attack"
    if re.search(r"(民族|国籍|人種|宗教).{0,8}(劣等|排除|追放)", text):
        return "attribute_attack"
    if re.search(
            r"(お前|こいつ|あいつ|投稿者|管理人).{0,8}"
            r"(死ね|消えろ|馬鹿|バカ|クズ|ゴミ|無能)", text):
        return "personal_attack"
    if re.search(r"(死ね|消えろ|売国|非国民|馬鹿|バカ|クズ|ゴミ|無能)", text):
        return "insult_only"
    if re.search(r"(http\S+){2,}|副業|投資で稼", text):
        return "spam"
    if text.endswith(("？", "?")) and len(text) >= 12:
        return "specific_question"
    if re.search(r"(資料|統計|データ|根拠|出典).{0,20}(では|によると)", text):
        return "additional_evidence"
    if re.search(r"(私|うち|職場|家族|現場).{0,20}(経験|負担|困)", text):
        return "personal_experience"
    if re.search(r"(賛成|同意|その通り).{0,20}(理由|なぜなら|ただ)", text):
        return "substantive_agreement"
    if re.search(r"(反対|違う|異論).{0,20}(理由|なぜなら|ただ)", text):
        return "substantive_disagreement"
    return "unknown"


def specific_reply_rate(classifications: list[str]) -> float | None:
    valid = [value for value in classifications if value != "spam"]
    if not valid:
        return None
    return sum(value in SPECIFIC_REPLIES for value in valid) / len(valid)


def deduplicated_reply_classes(rows: list[dict]) -> list[str]:
    """Count at most one reply per author, keeping the earliest response."""
    ordered = sorted(
        rows, key=lambda row: str(
            row.get("replied_at") or row.get("timestamp") or ""))
    seen: set[str] = set()
    classes = []
    for row in ordered:
        # Some APIs omit an author ID for deleted/restricted users.  Falling
        # back to the reply ID avoids collapsing all such replies into one
        # fictitious responder while still never storing a raw identity.
        author = str(
            row.get("author_hash") or row.get("username_hash")
            or f"reply:{row.get('reply_id') or id(row)}")
        if author in seen:
            continue
        seen.add(author)
        classification = str(row.get("reply_classification") or "")
        allowed = SPECIFIC_REPLIES | TOXIC_REPLIES | {"unknown"}
        classes.append(
            classification if classification in allowed
            else classify_reply(row.get("reply_text") or row.get("text")))
    return classes


def _noun_density(text: str) -> tuple[float | None, str]:
    """Use Sudachi when installed, otherwise a deterministic Japanese proxy."""
    try:
        from sudachipy import dictionary, tokenizer
        mode = tokenizer.Tokenizer.SplitMode.C
        tokens = [
            token for token in dictionary.Dictionary().create().tokenize(
                text, mode)
            if token.surface().strip()
        ]
        if not tokens:
            return None, "sudachi"
        nouns = sum(token.part_of_speech()[0] == "名詞" for token in tokens)
        return nouns / len(tokens), "sudachi"
    except (ImportError, OSError, RuntimeError):
        tokens = re.findall(r"[一-龥]{2,}|[ァ-ヶー]{2,}|[A-Za-z]{2,}", text)
        all_terms = re.findall(
            r"[一-龥ぁ-んァ-ヶーA-Za-z0-9]+", text)
        return (
            len(tokens) / len(all_terms) if all_terms else None,
            "regex_proxy_v1",
        )


def topic_demand_score(signals: dict) -> float:
    """Demand only; source truth/safety never enters this value."""
    velocity = min(1, max(0, float(signals.get("x_search_velocity", 0) or 0) / 10))
    authors = min(1, max(0, float(signals.get("unique_author_count", 0) or 0) / 20))
    official = min(1, max(0, float(signals.get("official_news_count", 0) or 0) / 5))
    engagement = min(1, max(0, float(signals.get("engagement_velocity", 0) or 0) / 100))
    relevance = min(1, max(0, float(signals.get("audience_relevance", 0) or 0) / 10))
    freshness_hours = max(0, float(signals.get("news_freshness_hours", 24) or 24))
    freshness = max(0, 1 - freshness_hours / 24)
    return round(10 * (
        .25 * velocity + .15 * authors + .10 * official
        + .20 * engagement + .15 * relevance + .15 * freshness), 4)


def posts_per_hour(post_count: Any, elapsed_hours: Any) -> float | None:
    return safe_rate(post_count, elapsed_hours)


def _sentences(text: str) -> list[str]:
    return [value.strip() for value in re.split(
        r"(?<=[。！？!?])\s*", str(text or "")) if value.strip()]


def _similarity(left: str, right: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(
        None, re.sub(r"\s+", "", left), re.sub(r"\s+", "", right)).ratio()


def _framing_text(text: str, packet: dict) -> str:
    value = text
    for common in (packet.get("title"), packet.get("summary"),
                   *(packet.get("numbers") or [])):
        if common:
            value = value.replace(str(common), "")
    return re.sub(r"[\s。、！？?]+", "", value)


def infer_hook(text: str) -> str:
    first = _sentences(text)[0] if _sentences(text) else text[:80]
    if re.search(r"\d", first):
        return "数字先出し"
    if re.search(r"(一方|対して|しかし|なのに|ではなく)", first):
        return "対比"
    if first.endswith(("？", "?")):
        return "疑問"
    if re.search(r"(反論|もちろん|確かに)", first):
        return "反論先出し"
    if re.search(r"(朝|昼|夜|現場|家庭|会社|国会)", first):
        return "場面描写"
    return "結論先出し"


def infer_angle(text: str) -> str:
    rules = (
        ("誰が払う", r"(負担者|納税者|保険料|誰が払)"),
        ("誰が決めた", r"(決定者|決めた|意思決定|責任主体)"),
        ("約束と実績", r"(約束|公約|実績|以前は)"),
        ("一人あたり", r"(一人あたり|1人あたり)"),
        ("比較", r"(比較|対して|よりも|一方)"),
        ("未来予測", r"(将来|10年後|今後\d+年)"),
        ("説明不足", r"(説明不足|根拠が見え|説明すべき)"),
        ("改善策", r"(見直し|改善|公開すべき|明示すべき)"),
    )
    return next((name for name, pattern in rules if re.search(pattern, text)), "その他")


def infer_structure(text: str) -> str:
    if re.search(r"\d", text) and re.search(r"(内訳|→|増|減|合計)", text):
        return "数字分解型"
    if re.search(r"(しかし|ではなく|本当の|実際は)", text):
        return "反転型"
    if re.search(r"(責任|決めた|決定)", text):
        return "責任型"
    if re.search(r"(もちろん|確かに|反論)", text):
        return "反論型"
    if text.rstrip().endswith(("？", "?")):
        return "質問型"
    if re.search(r"(将来|10年後)", text):
        return "未来型"
    if re.search(r"(現場|家庭|会社|国会)", text):
        return "場面型"
    return "一撃型"


def extract_features(row: dict, recent: list[dict] | None = None) -> dict:
    recent = recent or []
    text = str(row.get("text") or row.get("tweet_text") or "")
    sentences = _sentences(text)
    lines = text.splitlines() or [text]
    words = re.findall(r"[一-龥ぁ-んァ-ヶA-Za-z0-9]+", text)
    abstract = re.findall(
        r"(課題|重要|議論|検討|対応|必要|適切|様々|社会全体|今後)", text)
    hook = row.get("hook_type") or infer_hook(text)
    structure = row.get("structure_type") or infer_structure(text)
    ending = (
        "question" if text.rstrip().endswith(("？", "?"))
        else "should" if re.search(r"(べき|必要だ)[。]?$", text)
        else "assertion")
    same_hook = sum((item.get("hook_type") or infer_hook(
        str(item.get("text") or ""))) == hook for item in recent[-20:])
    same_structure = sum((item.get("structure_type") or infer_structure(
        str(item.get("text") or ""))) == structure for item in recent[-10:])
    same_ending = sum(str(item.get("ending_type") or "") == ending
                      for item in recent[-10:])
    published = row.get("published_at") or row.get("posted_at") or row.get("created_at")
    try:
        dt = datetime.fromisoformat(str(published).replace("Z", "+00:00"))
        dt = dt.astimezone(JST) if dt.tzinfo else dt.replace(tzinfo=JST)
    except (ValueError, TypeError):
        dt = None
    current_id = str(row.get("post_id") or row.get("tweet_id") or row.get("id") or "")
    eligible_recent = []
    for item in recent:
        item_id = str(item.get("post_id") or item.get("tweet_id") or item.get("id") or "")
        item_time = item.get("published_at") or item.get("posted_at") or item.get("created_at")
        try:
            item_dt = datetime.fromisoformat(str(item_time).replace("Z", "+00:00"))
            item_dt = item_dt.astimezone(JST) if item_dt.tzinfo else item_dt.replace(tzinfo=JST)
        except (ValueError, TypeError):
            item_dt = None
        if item_id != current_id and (dt is None or item_dt is None or item_dt < dt):
            eligible_recent.append(item)
    semantic = max((_similarity(text, str(item.get("text") or item.get("tweet_text") or ""))
                    for item in eligible_recent[-20:]), default=0.0)
    previous = eligible_recent[-1] if eligible_recent else None
    previous_interval = None
    if dt and previous:
        previous_time = (
            previous.get("published_at") or previous.get("posted_at")
            or previous.get("created_at"))
        try:
            previous_dt = datetime.fromisoformat(
                str(previous_time).replace("Z", "+00:00"))
            previous_dt = (
                previous_dt.astimezone(JST) if previous_dt.tzinfo
                else previous_dt.replace(tzinfo=JST))
            previous_interval = max(
                0.0, (dt - previous_dt).total_seconds() / 3600)
        except (ValueError, TypeError):
            pass
    topic_key = str(row.get("topic_key") or "")
    same_topic_recent = sum(
        bool(topic_key)
        and str(item.get("topic_key") or "") == topic_key
        for item in eligible_recent[-20:])
    noun_density, noun_density_method = _noun_density(text)
    source_count = int(row.get("source_count", 1) or 1)
    freshness = row.get("news_freshness_hours")
    demand_signals = {
        "x_search_velocity": (
            row.get("x_search_velocity")
            if row.get("x_search_velocity") is not None
            else row.get("xai_velocity_score")
            if row.get("xai_velocity_score") is not None
            else posts_per_hour(
                row.get("x_search_post_count"), row.get("x_search_window_hours")) or 0),
        "unique_author_count": row.get("x_search_unique_authors", 0),
        "official_news_count": row.get("official_news_count", source_count),
        "engagement_velocity": row.get("x_search_engagement", 0),
        "audience_relevance": row.get("audience_relevance_score", 0),
        "news_freshness_hours": freshness if freshness is not None else 24,
    }
    has_demand_signal = any(row.get(name) is not None for name in (
        "x_search_velocity", "xai_velocity_score", "x_search_post_count",
        "x_search_unique_authors", "official_news_count",
        "x_search_engagement", "audience_relevance_score"))
    features = {
        "post_id": current_id,
        "content_id": str(row.get("content_id") or row.get("generated_post_id") or ""),
        "platform": str(row.get("platform") or "x"),
        "topic_key": topic_key,
        "category": str(row.get("category") or row.get("genre") or ""),
        "post_type": str(row.get("post_type") or ""),
        "model": str(row.get("model") or ""),
        "published_at": dt.isoformat() if dt else None,
        "weekday": dt.strftime("%a") if dt else None,
        "publish_hour": dt.hour if dt else None,
        "character_count": len(text), "line_count": len(lines),
        "sentence_count": len(sentences), "media_present": bool(row.get("media_present")),
        "url_present": bool(re.search(r"https?://\S+", text)),
        "breaking_flag": bool(row.get("breaking_flag") or row.get("is_breaking")),
        "digest_flag": bool(row.get("digest_flag") or row.get("digest_type")),
        "source_count": source_count,
        "source_quality_score": row.get("source_quality_score", row.get("source_reliability_score")),
        "topic_demand_score": (
            topic_demand_score(demand_signals) if has_demand_signal else None),
        "x_search_post_count": row.get("x_search_post_count"),
        "x_search_velocity": demand_signals["x_search_velocity"],
        "x_search_unique_authors": demand_signals["unique_author_count"],
        "x_search_engagement": demand_signals["engagement_velocity"],
        "official_news_count": demand_signals["official_news_count"],
        "news_freshness_hours": freshness,
        "topic_competition_score": (
            row.get("topic_competition_score")
            if row.get("topic_competition_score") is not None
            else min(10.0, (
                float(row.get("x_search_post_count") or 0) / 10
                + float(row.get("x_search_unique_authors") or 0) / 20
            )) if has_demand_signal else None),
        "topic_saturation_score": (
            same_topic_recent / max(1, min(20, len(eligible_recent)))),
        "audience_relevance_score": demand_signals["audience_relevance"],
        "hook_type": hook, "angle_type": row.get("angle_type") or infer_angle(text),
        "post_structure": structure, "structure_type": structure,
        "ending_type": ending,
        "reader_effect": row.get("reader_effect"), "target_audience": row.get("target_audience"),
        "specific_number_present": bool(re.search(r"\d", text)),
        "specific_actor_present": bool(re.search(r"(政府|国会|内閣|省|庁|党|知事|市長|首相)", text)),
        "cost_bearer_present": bool(re.search(r"(負担者|納税者|現役世代|家計|企業|保険料)", text)),
        "decision_maker_present": bool(re.search(r"(決めた|決定|閣議|国会|政府|自治体)", text)),
        "human_stake_present": bool(re.search(r"(家計|生活|患者|高齢者|子育て|企業|現場|世帯)", text)),
        "contrast_present": bool(re.search(r"(一方|対して|しかし|なのに|ではなく)", text)),
        "surprise_present": bool(re.search(r"(実は|意外|逆に|ところが)", text)),
        "comparison_present": bool(re.search(r"(比較|より|一方|対して|倍|差)", text)),
        "future_scenario_present": bool(re.search(r"(将来|10年後|今後\d+年|続けば)", text)),
        "counterargument_present": bool(re.search(r"(もちろん|確かに|反論|一理)", text)),
        "concrete_question_present": bool(
            text.rstrip().endswith(("？", "?")) and re.search(r"(誰|いつ|いくら|どこ|何を|どの)", text)),
        "memorable_line_present": any(
            10 <= len(value) <= 45 and re.search(r"(問題は|本音は|争点は|必要なのは)", value)
            for value in sentences),
        "abstract_term_ratio": len(abstract) / max(1, len(words)),
        "average_sentence_length": (
            sum(len(value) for value in sentences) / len(sentences)
            if sentences else None),
        "longest_sentence_length": max((len(value) for value in sentences), default=0),
        "noun_density": noun_density,
        "noun_density_method": noun_density_method,
        "question_count": len(re.findall(r"[？?]", text)),
        "emoji_count": len(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text)),
        "hashtag_count": len(re.findall(r"#[^\s#]+", text)),
        "sensational_term_count": len(re.findall(r"(激震|崩壊|終わり|ヤバい|衝撃|炎上)", text)),
        "generic_conclusion_flag": bool(re.search(
            r"(今後.*注目|慎重な議論|重要な課題|社会全体で考)", text)),
        "problem_wa_pattern": "問題は" in text,
        "should_end_pattern": bool(re.search(r"(べき|必要だ)[。]?$", text)),
        "same_hook_recent_count": same_hook,
        "same_structure_recent_count": same_structure,
        "same_ending_recent_count": same_ending,
        "semantic_similarity_recent": round(semantic, 4),
        "previous_post_interval_hours": previous_interval,
        "previous_24h_normalized_impressions": (
            previous.get("normalized_24h_impressions")
            if previous else None),
        "major_news_flag": bool(
            row.get("major_news_flag") or row.get("is_major_update")
            or row.get("is_breaking")),
        "ban_risk": row.get("ban_risk"), "defamation_risk": row.get("defamation_risk"),
        "unsupported_number_flag": bool(row.get("uses_unverified_number")),
        "unsupported_claim_flag": bool(row.get("unsupported_claim_flag")),
        "generalization_risk": row.get("generalization_risk"),
        "mob_targeting_risk": row.get("mob_targeting_risk"),
        "text": text,
    }
    return features


def outcome_record(row: dict) -> dict:
    impressions = row.get("impressions")
    views = row.get("views")
    denominator = impressions if impressions is not None else views
    replies = row.get("replies")
    follows = row.get("follows")
    record = dict(row)
    for metric, numerator in (
        ("like_rate", row.get("likes")), ("repost_rate", row.get("reposts")),
        ("quote_rate", row.get("quotes")), ("reply_rate", replies),
        ("bookmark_rate", row.get("bookmarks")), ("share_rate", row.get("shares")),
        ("profile_click_rate", row.get("profile_clicks")),
        ("follow_conversion_rate", follows),
    ):
        record[metric] = safe_rate(numerator, denominator)
    engagement_parts = [
        row.get(key) for key in ("likes", "reposts", "quotes", "replies",
                                 "bookmarks", "shares")
        if row.get(key) is not None
    ]
    record["engagement_rate"] = (
        safe_rate(sum(engagement_parts), denominator)
        if engagement_parts else None)
    classes = row.get("reply_classifications") or []
    record["specific_reply_rate"] = specific_reply_rate(classes)
    return record


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def load_source_data(db_path: Path, limit: int = 300) -> dict:
    """Read only fields that actually exist. Missing coverage remains explicit."""
    with closing(connect(db_path)) as conn:
        generated = _rows(conn, """
            SELECT g.*, n.topic_key, n.genre AS category, n.published_at AS news_published_at,
                   n.source_reliability_score, n.xai_velocity_score,
                   n.x_attention_score AS audience_relevance_score,
                   n.is_major_update,n.metadata_json AS news_metadata_json
            FROM generated_posts g LEFT JOIN news_candidates n
              ON n.id=g.news_candidate_id ORDER BY g.id DESC LIMIT ?""", (limit,))
        published = _rows(conn, """
            SELECT p.*, g.news_candidate_id, g.ban_risk,
                   n.genre AS category, n.published_at AS news_published_at,
                   n.source_reliability_score, n.xai_velocity_score,
                   n.x_attention_score AS audience_relevance_score,
                   n.is_major_update,n.metadata_json AS news_metadata_json
            FROM published_posts p LEFT JOIN generated_posts g
              ON g.id=p.generated_post_id LEFT JOIN news_candidates n
              ON n.id=g.news_candidate_id ORDER BY p.id DESC LIMIT ?""", (limit,))
        x_metrics = _rows(conn, """
            SELECT m.* FROM post_metrics m JOIN (
              SELECT tweet_id, measurement_window, MAX(measured_at) measured_at
              FROM post_metrics GROUP BY tweet_id,measurement_window
            ) z ON z.tweet_id=m.tweet_id AND z.measurement_window=m.measurement_window
              AND z.measured_at=m.measured_at""")
        followers = _rows(conn, "SELECT * FROM follower_snapshots ORDER BY captured_at")
        threads = _rows(conn, """
            SELECT p.*, m.measurement_window,m.measured_at,m.views,m.likes,m.replies,
                   m.reposts,m.quotes,m.shares
            FROM threads_posts p LEFT JOIN threads_metrics m
              ON m.threads_post_id=p.threads_post_id
            WHERE p.status='published' ORDER BY p.id DESC LIMIT ?""", (limit,))
        reply_events = _rows(conn, """
            SELECT platform,root_post_id,reply_id,author_hash,reply_text,
                   reply_classification,replied_at
            FROM post_reply_events ORDER BY replied_at""")
        thread_reply_events = _rows(conn, """
            SELECT 'threads' platform,r.root_post_id,
                   r.reply_id,r.username_hash author_hash,r.text reply_text,
                   a.intent reply_classification,r.timestamp replied_at
            FROM threads_replies r
            LEFT JOIN threads_reply_analyses a ON a.reply_id=r.reply_id""")
    for row in generated + published:
        try:
            metadata = json.loads(row.get("news_metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        row["x_search_post_count"] = metadata.get(
            "x_post_count", metadata.get("x_search_post_count"))
        row["x_search_unique_authors"] = metadata.get(
            "x_unique_accounts", metadata.get("x_search_unique_authors"))
        row["x_search_engagement"] = metadata.get("x_search_engagement")
        row["x_search_window_hours"] = metadata.get(
            "x_search_window_hours")
        row["topic_competition_score"] = metadata.get(
            "topic_competition_score")
    for row in published:
        row["platform"] = "x"
        if row.get("news_published_at") and row.get("posted_at"):
            try:
                row["news_freshness_hours"] = max(0, (
                    datetime.fromisoformat(str(row["posted_at"]).replace("Z", "+00:00"))
                    - datetime.fromisoformat(str(row["news_published_at"]).replace("Z", "+00:00"))
                ).total_seconds() / 3600)
            except (ValueError, TypeError):
                pass
    for row in generated:
        row["platform"] = "candidate"
    return {
        "generated": generated, "published": published, "x_metrics": x_metrics,
        "followers": followers, "threads": threads,
        "reply_events": reply_events + thread_reply_events,
    }


def _nearest_followers(snapshots: list[dict], published_at: Any) -> int | None:
    if not published_at:
        return None
    try:
        target = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        if target.tzinfo is None:
            target = target.replace(tzinfo=JST)
    except (ValueError, TypeError):
        return None
    best: tuple[float, int] | None = None
    for row in snapshots:
        try:
            dt = datetime.fromisoformat(str(row["captured_at"]).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=JST)
            value = (abs((dt - target).total_seconds()), int(row["followers_count"]))
            if best is None or value[0] < best[0]:
                best = value
        except (ValueError, TypeError, KeyError):
            continue
    return best[1] if best else None


def _followers_at_publish(post: dict, snapshots: list[dict],
                          published_at: Any) -> int | None:
    """Prefer an exact publish-time capture; proximity is only a legacy fallback."""
    exact = post.get("followers_at_publish")
    if exact is None:
        exact = post.get("followers_before_window")
    if exact is not None:
        try:
            return int(exact)
        except (TypeError, ValueError):
            pass
    return _nearest_followers(snapshots, published_at)


def persist_features(db_path: Path, records: list[dict]) -> int:
    columns = [
        "post_id", "content_id", "platform", "topic_key", "category", "post_type",
        "hook_type", "angle_type", "structure_type", "ending_type",
        "model", "published_at", "weekday", "publish_hour",
        "character_count", "line_count", "sentence_count", "media_present",
        "url_present", "breaking_flag", "digest_flag", "source_count",
        "source_quality_score", "topic_demand_score",
        "news_freshness_hours", "specific_number_present", "specific_actor_present",
        "x_search_post_count", "x_search_velocity", "x_search_unique_authors",
        "x_search_engagement", "official_news_count", "audience_relevance_score",
        "cost_bearer_present", "decision_maker_present", "human_stake_present",
        "contrast_present", "surprise_present", "comparison_present",
        "future_scenario_present", "counterargument_present",
        "concrete_question_present", "memorable_line_present",
        "abstract_term_ratio", "average_sentence_length", "longest_sentence_length",
        "reader_effect", "target_audience", "question_count", "emoji_count",
        "hashtag_count", "sensational_term_count", "generic_conclusion_flag",
        "problem_wa_pattern", "should_end_pattern",
        "same_hook_recent_count", "same_structure_recent_count",
        "same_ending_recent_count", "semantic_similarity_recent",
        "noun_density", "previous_post_interval_hours",
        "previous_24h_normalized_impressions", "major_news_flag",
        "topic_competition_score", "topic_saturation_score",
        "ban_risk", "defamation_risk", "unsupported_number_flag",
        "unsupported_claim_flag", "generalization_risk", "mob_targeting_risk",
    ]
    now = datetime.now(JST).isoformat()
    update_columns = [
        column for column in columns if column not in {
            "post_id", "platform", "created_at"}
    ]
    sql = f"""INSERT INTO post_performance_features
      ({','.join(columns)},feature_json,created_at,updated_at)
      VALUES ({','.join('?' for _ in columns)},?,?,?)
      ON CONFLICT(post_id,platform) DO UPDATE SET
      {','.join(f'{column}=excluded.{column}' for column in update_columns)},
      feature_json=excluded.feature_json,updated_at=excluded.updated_at"""
    with closing(connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        for record in records:
            values = [int(record.get(c)) if isinstance(record.get(c), bool)
                      else record.get(c) for c in columns]
            conn.execute(sql, values + [
                json.dumps(record, ensure_ascii=False, default=str), now, now])
        conn.commit()
    return len(records)


def persist_outcomes(db_path: Path, records: list[dict]) -> int:
    columns = [
        "post_id", "captured_at", "measurement_window", "followers_at_publish",
        "impressions", "views", "likes", "reposts", "quotes", "replies",
        "bookmarks", "shares", "profile_clicks", "link_clicks", "follows",
        "video_views", "video_completion",
        "like_rate", "repost_rate", "quote_rate", "reply_rate", "bookmark_rate",
        "share_rate", "profile_click_rate", "follow_conversion_rate",
        "engagement_rate", "specific_reply_rate",
    ]
    now = datetime.now(JST).isoformat()
    update_columns = [
        column for column in columns
        if column not in {"post_id", "measurement_window"}
    ]
    sql = f"""INSERT INTO post_performance_outcomes
      ({','.join(columns)},outcome_json,created_at)
      VALUES ({','.join('?' for _ in columns)},?,?)
      ON CONFLICT(post_id,measurement_window) DO UPDATE SET
      {','.join(f'{column}=excluded.{column}' for column in update_columns)},
      outcome_json=excluded.outcome_json,created_at=excluded.created_at"""
    with closing(connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        for record in records:
            conn.execute(sql, [record.get(c) for c in columns] + [
                json.dumps(record, ensure_ascii=False, default=str), now])
        conn.commit()
    return len(records)


def descriptive_stats(values: Iterable[Any], min_sample: int | None = None) -> dict:
    numbers = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    n = len(numbers)
    minimum = min_sample or settings()["min_sample"]
    if not n:
        return {"count": 0, "insufficient_sample": True}
    def quantile(q: float) -> float:
        pos = (n - 1) * q
        lo, hi = math.floor(pos), math.ceil(pos)
        return numbers[lo] if lo == hi else numbers[lo] + (numbers[hi] - numbers[lo]) * (pos - lo)
    mean = statistics.fmean(numbers)
    sd = statistics.stdev(numbers) if n > 1 else 0.0
    trim = max(0, math.floor(n * .1))
    trimmed = numbers[trim:n-trim] if trim and n > 2 * trim else numbers
    margin = 1.96 * sd / math.sqrt(n) if n > 1 else None
    return {
        "count": n, "mean": mean, "median": statistics.median(numbers),
        "p25": quantile(.25), "p75": quantile(.75),
        "top_25_mean": statistics.fmean(numbers[max(0, math.floor(n * .75)):]),
        "bottom_25_mean": statistics.fmean(numbers[:max(1, math.ceil(n * .25))]),
        "top_10_mean": statistics.fmean(numbers[max(0, math.floor(n * .9)):]),
        "bottom_10_mean": statistics.fmean(numbers[:max(1, math.ceil(n * .1))]),
        "trimmed_mean": statistics.fmean(trimmed), "stddev": sd,
        "confidence_low": mean - margin if margin is not None else None,
        "confidence_high": mean + margin if margin is not None else None,
        "minimum_sample_size": minimum, "insufficient_sample": n < minimum,
    }


def normalize_records(records: list[dict]) -> list[dict]:
    """Explainable peer normalization instead of an opaque predictive model."""
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for row in records:
        hour = row.get("publish_hour")
        hour_bucket = None if hour is None else int(hour) // 4
        demand = row.get("topic_demand_score")
        demand_bucket = None if demand is None else min(4, int(float(demand) // 2))
        freshness = row.get("news_freshness_hours")
        freshness_bucket = (
            None if freshness is None else
            "breaking" if float(freshness) <= 1 else
            "fresh" if float(freshness) <= 6 else
            "same_day" if float(freshness) <= 24 else "older")
        followers = row.get("followers_at_publish")
        follower_bucket = None if followers is None else int(float(followers) // 500)
        saturation = row.get("topic_saturation_score")
        saturation_bucket = None if saturation is None else int(float(saturation) * 4)
        previous_interval = row.get("previous_post_interval_hours")
        previous_interval_bucket = (
            None if previous_interval is None else
            "under_1h" if float(previous_interval) < 1 else
            "under_3h" if float(previous_interval) < 3 else
            "under_6h" if float(previous_interval) < 6 else "6h_plus")
        previous_outcome = row.get("previous_24h_normalized_impressions")
        previous_outcome_bucket = (
            None if previous_outcome is None else
            "low" if float(previous_outcome) < .75 else
            "typical" if float(previous_outcome) < 1.25 else "high")
        competition = row.get("topic_competition_score")
        competition_bucket = (
            None if competition is None else
            min(4, int(float(competition) // 2)))
        key = (row.get("platform"), row.get("category"), hour_bucket,
               row.get("weekday"), demand_bucket, bool(row.get("breaking_flag")),
               bool(row.get("media_present")), freshness_bucket, follower_bucket,
               saturation_bucket, bool(row.get("major_news_flag")),
               previous_interval_bucket, previous_outcome_bucket,
               competition_bucket)
        buckets[key].append(row)
    metric_map = {
        "impressions": "normalized_impression_score",
        "profile_click_rate": "normalized_profile_click_score",
        "follow_conversion_rate": "normalized_follow_score",
        "quote_rate": "normalized_quote_score",
    }
    result = []
    for peers in buckets.values():
        medians = {}
        for raw in metric_map:
            vals = [float(x[raw]) for x in peers if x.get(raw) is not None]
            medians[raw] = statistics.median(vals) if vals else None
        for row in peers:
            item = dict(row)
            for raw, normalized in metric_map.items():
                value, baseline = row.get(raw), medians[raw]
                item[normalized] = (
                    float(value) / baseline if value is not None and baseline not in (None, 0)
                    else None)
            result.append(item)
    return result


def _ranks(values: list[float]) -> list[float]:
    ordered = sorted((v, i) for i, v in enumerate(values))
    ranks = [0.0] * len(values)
    p = 0
    while p < len(ordered):
        q = p
        while q + 1 < len(ordered) and ordered[q + 1][0] == ordered[p][0]:
            q += 1
        rank = (p + q) / 2 + 1
        for _, idx in ordered[p:q + 1]:
            ranks[idx] = rank
        p = q + 1
    return ranks


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    lm, rm = statistics.fmean(left), statistics.fmean(right)
    numerator = sum((a-lm) * (b-rm) for a, b in zip(left, right))
    denominator = math.sqrt(sum((a-lm) ** 2 for a in left) * sum((b-rm) ** 2 for b in right))
    return numerator / denominator if denominator else None


def correlation(left: list[Any], right: list[Any], min_sample: int | None = None) -> dict:
    pairs = [(float(a), float(b)) for a, b in zip(left, right)
             if a is not None and b is not None]
    x, y = [p[0] for p in pairs], [p[1] for p in pairs]
    r = pearson(x, y)
    rho = pearson(_ranks(x), _ranks(y))
    n = len(pairs)
    lo = hi = None
    if r is not None and n > 3 and abs(r) < 1:
        z = math.atanh(r)
        margin = 1.96 / math.sqrt(n - 3)
        lo, hi = math.tanh(z - margin), math.tanh(z + margin)
    minimum = min_sample or settings()["min_sample"]
    magnitude_difference = (
        abs(r - rho) if r is not None and rho is not None else None)
    direction_agreement = (
        r * rho >= 0 if r is not None and rho is not None else None)
    classification = "insufficient_sample" if n < minimum else "unavailable"
    if n >= minimum and r is not None and rho is not None:
        if not direction_agreement:
            classification = "weak_or_inconclusive"
        elif abs(r) < .20 or abs(rho) < .20:
            classification = "weak_or_inconclusive"
        elif r > 0 and rho > 0:
            classification = "positive_associations"
        elif r < 0 and rho < 0:
            classification = "negative_associations"
    return {"sample_size": n, "pearson_correlation": r,
            "spearman_correlation": rho, "confidence_low": lo,
            "confidence_high": hi, "insufficient_sample": n < minimum,
            "direction_agreement": direction_agreement,
            "magnitude_difference": magnitude_difference,
            "outlier_sensitive": bool(
                magnitude_difference is not None and magnitude_difference >= .15),
            "interpretation": (
                "符号不一致のため結論不能" if direction_agreement is False else
                "PearsonとSpearmanの差が0.15以上。外れ値または非線形を確認"
                if magnitude_difference is not None and magnitude_difference >= .15 else
                "方向は整合" if direction_agreement else "算出不能"),
            "classification": classification}


def robust_slope(left: list[Any], right: list[Any],
                 max_pairs: int = 5000) -> float | None:
    """Deterministic Theil-Sen slope for an explainable robust trend."""
    pairs = [
        (float(a), float(b)) for a, b in zip(left, right)
        if a is not None and b is not None
        and math.isfinite(float(a)) and math.isfinite(float(b))
    ]
    slopes = []
    for index, (x1, y1) in enumerate(pairs):
        for x2, y2 in pairs[index + 1:]:
            if x2 != x1:
                slopes.append((y2 - y1) / (x2 - x1))
                if len(slopes) >= max_pairs:
                    return statistics.median(slopes)
    return statistics.median(slopes) if slopes else None


def permutation_importance(left: list[Any], right: list[Any],
                           iterations: int = 100) -> dict:
    """Univariate, deterministic MSE importance; never used for auto rollout."""
    pairs = [
        (float(a), float(b)) for a, b in zip(left, right)
        if a is not None and b is not None
        and math.isfinite(float(a)) and math.isfinite(float(b))
    ]
    if len(pairs) < settings()["min_sample"]:
        return {
            "sample_size": len(pairs), "importance": None,
            "insufficient_sample": True,
        }
    x = [pair[0] for pair in pairs]
    y = [pair[1] for pair in pairs]
    x_mean, y_mean = statistics.fmean(x), statistics.fmean(y)
    variance = sum((value - x_mean) ** 2 for value in x)
    if variance == 0:
        return {
            "sample_size": len(pairs), "importance": 0.0,
            "insufficient_sample": False,
        }
    slope = sum(
        (a - x_mean) * (b - y_mean) for a, b in pairs) / variance
    intercept = y_mean - slope * x_mean
    baseline = statistics.fmean(
        (actual - (intercept + slope * feature)) ** 2
        for feature, actual in pairs)
    rng = random.Random(20260729)
    increases = []
    for _ in range(max(10, min(500, iterations))):
        shuffled = list(x)
        rng.shuffle(shuffled)
        mse = statistics.fmean(
            (actual - (intercept + slope * feature)) ** 2
            for feature, actual in zip(shuffled, y))
        increases.append(mse - baseline)
    return {
        "sample_size": len(pairs),
        "importance": statistics.fmean(increases),
        "baseline_mse": baseline,
        "insufficient_sample": False,
    }


def _finite(values: Iterable[Any]) -> list[float]:
    result = []
    for value in values:
        try:
            number = float(value)
            if math.isfinite(number):
                result.append(number)
        except (TypeError, ValueError):
            pass
    return result


def cliffs_delta(left: list[Any], right: list[Any]) -> float | None:
    a, b = _finite(left), _finite(right)
    if not a or not b:
        return None
    return sum((x > y) - (x < y) for x in a for y in b) / (len(a) * len(b))


def mann_whitney_u(left: list[Any], right: list[Any]) -> tuple[float | None, float | None]:
    """Return U and a tie-corrected two-sided normal-approximation p value."""
    a, b = _finite(left), _finite(right)
    if not a or not b:
        return None, None
    combined = a + b
    ranks = _ranks(combined)
    u = sum(ranks[:len(a)]) - len(a) * (len(a) + 1) / 2
    n1, n2, n = len(a), len(b), len(combined)
    counts = Counter(combined)
    tie_term = sum(c ** 3 - c for c in counts.values())
    variance = n1 * n2 / 12 * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0
    if variance <= 0:
        return u, 1.0
    z = (u - n1 * n2 / 2) / math.sqrt(variance)
    p = math.erfc(abs(z) / math.sqrt(2))
    return u, p


def bootstrap_difference(left: list[Any], right: list[Any],
                         statistic=statistics.median,
                         iterations: int = 1000) -> tuple[float | None, float | None]:
    a, b = _finite(left), _finite(right)
    if not a or not b:
        return None, None
    rng = random.Random(20260729)
    samples = sorted(
        statistic(rng.choices(a, k=len(a))) - statistic(rng.choices(b, k=len(b)))
        for _ in range(iterations))
    return samples[int(iterations * .025)], samples[min(iterations - 1, int(iterations * .975))]


def select_measurement_window(rows: list[dict], window: str) -> tuple[list[dict], dict]:
    """Select exactly one latest capture per post for one explicit window."""
    allowed = [r for r in rows if str(r.get("measurement_window")) == window]
    selected: dict[tuple[str, str], dict] = {}
    for row in allowed:
        key = (str(row.get("platform") or ""), str(row.get("post_id") or ""))
        old = selected.get(key)
        if old is None or str(row.get("captured_at") or "") > str(old.get("captured_at") or ""):
            selected[key] = row
    all_posts = {(str(r.get("platform") or ""), str(r.get("post_id") or "")) for r in rows}
    return list(selected.values()), {
        "measurement_window": window, "available_posts": len(selected),
        "excluded_missing_window": len(all_posts - set(selected)),
        "duplicate_rows_removed": len(allowed) - len(selected),
    }


def _remove_outliers(rows: list[dict], outcome: str) -> list[dict]:
    values = sorted(_finite(r.get(outcome) for r in rows))
    if len(values) < 4:
        return list(rows)
    q1 = values[math.floor((len(values) - 1) * .25)]
    q3 = values[math.ceil((len(values) - 1) * .75)]
    low, high = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    return [r for r in rows if r.get(outcome) is not None and low <= float(r[outcome]) <= high]


def _stratified_direction_stable(rows: list[dict], feature: str,
                                 outcome: str, direction: int) -> bool:
    evaluated = 0
    for field in ("category", "breaking_flag", "publish_hour", "weekday",
                  "media_present"):
        groups: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            value = row.get(field)
            if field == "publish_hour" and value is not None:
                value = int(value) // 4
            groups[str(value)].append(row)
        for group in groups.values():
            result = correlation(
                [r.get(feature) for r in group], [r.get(outcome) for r in group],
                min_sample=5)
            value = result["spearman_correlation"]
            if not result["insufficient_sample"] and value is not None:
                evaluated += 1
                if value * direction < 0:
                    return False
    return evaluated > 0


def fact_packet_hash(packet: dict) -> str:
    canonical = json.dumps(packet, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _prediction(text: str, facts: dict) -> dict:
    scores = {
        "hook_strength": min(10, 3 + 2 * bool(re.search(r"\d", text))
                             + 2 * bool(re.search(r"(しかし|一方|問題は)", text))),
        "specificity": min(10, 2 + len(re.findall(r"\d+|政府|省|国会|家計|現役世代", text))),
        "new_information_value": min(10, 4 + 2 * bool(facts.get("numbers"))),
        "quoteability": min(10, 3 + 2 * ("問題は" in text) + 2 * (25 <= len(text) <= 280)),
        "fact_strength": min(10, 3 + min(7, len(facts.get("sources") or []))),
    }
    scores["predicted_performance_score"] = round(
        statistics.fmean(scores.values()), 3)
    return scores


def evaluate_candidate(text: str, packet: dict) -> dict:
    packet_text = json.dumps(packet, ensure_ascii=False)
    per_capita = bool(re.search(r"(一人|1人)あたり", text))
    unsupported_per_capita = per_capita and not re.search(
        r"(一人|1人)あたり", packet_text)
    safety_priority = bool(re.search(
        r"(災害|地震|津波|死亡|死者|裁判|司法|逮捕|起訴)", packet_text))
    sensational = bool(re.search(r"(激震|崩壊|終わり|ヤバい|衝撃|炎上)", text))
    boring = {
        "generic_conclusion": bool(re.search(
            r"(今後.*注目|慎重な議論|社会全体で考|重要な課題)", text)),
        "abstract_overload": len(re.findall(
            r"(課題|重要|議論|検討|対応|必要|適切)", text)) >= 3,
        "same_sentence_opening": len(re.findall(r"(?:^|\n)問題は", text)) >= 2,
        "unsupported_per_capita": unsupported_per_capita,
        "sensational_sensitive_topic": safety_priority and sensational,
    }
    factual = not unsupported_per_capita
    return {
        "fact_consistent": factual,
        "safety_priority": safety_priority,
        "boring_signals": boring,
        "eligible": factual and not any(boring.values()),
        "selection_reason": (
            "同一fact packet内で具体的な発見を提示"
            if factual and not any(boring.values()) else None),
        "rejection_reasons": [
            name for name, flagged in boring.items() if flagged],
    }


def generate_candidates(content: dict, important: bool = False, *,
                        client_factory=None,
                        budget_path: Path | None = None,
                        use_openai: bool = False) -> dict:
    packet = content.get("fact_packet") or {
        "title": content.get("title") or content.get("text") or "",
        "summary": content.get("summary") or "",
        "numbers": content.get("numbers") or [],
        "sources": content.get("sources") or [],
    }
    title = str(packet.get("title") or "このニュース")
    summary = str(packet.get("summary") or "")
    base = str(content.get("text") or f"{title}。{summary}").strip()
    angles = [
        ("数字の意味", f"{title}。数字の大きさより、何が変わる数字なのかが争点です。{summary}"),
        ("現役世代", f"{title}。現役世代の家計にどう届くのか。{summary}"),
        ("負担上限", f"{title}。問題は負担の上限と、誰が決めるかです。{summary}"),
        ("給付と負担非対称", f"{title}。給付と負担は同じ速さで動くのか。{summary}"),
        ("10年後", f"{title}。この仕組みが10年続けば、生活はどう変わるでしょうか。{summary}"),
        ("反論先出し", f"もちろん制度維持は必要です。しかし、{title}で負担者への説明は十分でしょうか。{summary}"),
        ("具体質問", f"{title}。誰が、いつ、いくら負担するのかを政府は示せますか。{summary}"),
    ]
    count = (
        7 if content.get("content_id") == SOCIAL_SECURITY_FIXTURE["content_id"]
        else settings()["important_variants"] if important
        else settings()["normal_variants"])
    generation = {"variants": None, "status": "disabled"}
    if use_openai:
        try:
            from experiment_ai import generate_fact_packet_variants
            generation = generate_fact_packet_variants(
                packet, count, client_factory=client_factory,
                budget_path=budget_path)
        except Exception as exc:
            generation = {"variants": None, "status": "fallback",
                          "error_type": type(exc).__name__}
    candidate_angles = (
        [(item["angle_type"], item["text"])
         for item in generation["variants"]] + angles
        if generation.get("variants") else angles
    )
    selected: list[dict] = []
    max_similarity = settings()["max_similarity"]
    for angle, text in candidate_angles:
        text = re.sub(r"\s+", " ", text).strip()
        framing = _framing_text(text, packet)
        semantic_signature = angle
        similarities = [
            _similarity(semantic_signature, item["semantic_signature"])
            for item in selected]
        maximum = max(similarities, default=0.0)
        if maximum > max_similarity:
            continue
        selected.append({"candidate_id": f"variant-{uuid.uuid4().hex[:12]}",
                         "candidate_type": "variant", "angle_type": angle,
                         "text": text, "prediction": _prediction(text, packet),
                         "framing_text": framing,
                         "semantic_signature": semantic_signature,
                         "semantic_similarity_to_variants": maximum,
                         **evaluate_candidate(text, packet)})
        if len(selected) >= count:
            break
    if len(selected) < count:
        raise ValueError(
            f"semantic diversity gate left {len(selected)} variants; required {count}")
    experiment_id = f"pexp-{uuid.uuid4().hex}"
    control = {"candidate_id": f"control-{uuid.uuid4().hex[:12]}",
               "candidate_type": "control", "angle_type": infer_angle(base),
               "text": base, "prediction": _prediction(base, packet),
               **evaluate_candidate(base, packet)}
    eligible = [item for item in selected if item["eligible"]]
    selected_id = max(
        eligible, key=lambda item: item["prediction"]["predicted_performance_score"]
    )["candidate_id"] if eligible else None
    return {"experiment_id": experiment_id,
            "topic_key": content.get("topic_key"), "content_id": content.get("content_id"),
            "fact_packet_hash": fact_packet_hash(packet), "fact_packet": packet,
            "search_snapshot": content.get("search_snapshot"),
            "control": control, "variants": selected, "phase": PHASE,
            "recommended_candidate_id": selected_id,
            "recommendation_is_auxiliary": True,
            "variant_generation": {
                key: value for key, value in generation.items()
                if key != "variants"
            },
            "auto_publish": False, "selection_status": "analysis_only"}


def persist_experiment(db_path: Path, experiment: dict) -> None:
    now = datetime.now(JST).isoformat()
    control, variants = experiment["control"], experiment["variants"]
    with closing(connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.execute("""INSERT OR REPLACE INTO post_experiments
          (experiment_id,topic_key,content_id,platform,fact_packet_hash,
           control_candidate_id,variant_candidate_ids_json,assignment_group,
           assignment_stratum,
           prediction_version,selection_status,result_status,created_at,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            experiment["experiment_id"], experiment.get("topic_key"),
            experiment.get("content_id"), "analysis", experiment["fact_packet_hash"],
            control["candidate_id"],
            json.dumps([v["candidate_id"] for v in variants]),
            None, None, "phase-a-v1", "analysis_only", "not_published", now, now))
        packet = experiment.get("fact_packet") or {}
        conn.execute("""INSERT OR REPLACE INTO post_experiment_snapshots
          (experiment_id,snapshot_type,snapshot_hash,payload_json,
           source_timestamp,captured_at,created_at)
          VALUES (?,?,?,?,?,?,?)""", (
            experiment["experiment_id"], "fact_packet",
            experiment["fact_packet_hash"],
            json.dumps(packet, ensure_ascii=False, sort_keys=True,
                       default=str),
            packet.get("source_timestamp") or packet.get("published_at"),
            now, now,
        ))
        search_snapshot = experiment.get("search_snapshot")
        if search_snapshot is not None:
            conn.execute("""INSERT OR REPLACE INTO post_experiment_snapshots
              (experiment_id,snapshot_type,snapshot_hash,payload_json,
               source_timestamp,captured_at,created_at)
              VALUES (?,?,?,?,?,?,?)""", (
                experiment["experiment_id"], "search_snapshot",
                fact_packet_hash(search_snapshot),
                json.dumps(search_snapshot, ensure_ascii=False,
                           sort_keys=True, default=str),
                search_snapshot.get("captured_at")
                if isinstance(search_snapshot, dict) else None,
                now, now,
            ))
        for item in [control] + variants:
            p = item["prediction"]
            conn.execute("""INSERT OR REPLACE INTO post_candidate_predictions
              (experiment_id,candidate_id,candidate_type,hook_strength,specificity,
               new_information_value,quoteability,fact_strength,
               predicted_performance_score,prediction_json,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                experiment["experiment_id"], item["candidate_id"],
                item["candidate_type"], p["hook_strength"], p["specificity"],
                p["new_information_value"], p["quoteability"], p["fact_strength"],
                p["predicted_performance_score"],
                json.dumps(p, ensure_ascii=False), now))
        conn.commit()


def _phase_b_candidate_ids(row: sqlite3.Row) -> tuple[str, str]:
    variants = json.loads(row["variant_candidate_ids_json"] or "[]")
    if not variants:
        raise ValueError("experiment_has_no_variant")
    # Candidate choice is stable even if candidate ordering changes later.
    variant = min(
        (str(value) for value in variants),
        key=lambda value: hashlib.sha256(value.encode()).hexdigest())
    return str(row["control_candidate_id"]), variant


def assign_phase_b(db_path: Path, experiment_id: str, platform: str,
                   stratum: str = "default") -> dict:
    """Deterministically assign one control/variant without publishing it."""
    if platform not in {"x", "threads"}:
        raise ValueError("platform must be x or threads")
    apply_migrations(db_path)
    now = datetime.now(JST).isoformat()
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM post_experiments WHERE experiment_id=?",
            (experiment_id,)).fetchone()
        if row is None:
            return {"status": "not_found", "experiment_id": experiment_id}
        conflict = conn.execute(
            """SELECT experiment_id FROM post_experiments
               WHERE experiment_id<>? AND fact_packet_hash=? AND platform=?
                 AND selection_status IN ('assigned','approved','published')
               LIMIT 1""",
            (experiment_id, row["fact_packet_hash"], platform)).fetchone()
        if conflict:
            return {"status": "blocked", "reason": "fact_already_assigned",
                    "conflicting_experiment_id": conflict["experiment_id"]}
        if row["assignment_group"] in {"control", "variant"}:
            candidate_ids = _phase_b_candidate_ids(row)
            candidate_id = candidate_ids[row["assignment_group"] == "variant"]
            return {"status": "assigned", "assignment_group": row["assignment_group"],
                    "candidate_id": candidate_id, "deterministic": True}
        counts = dict(conn.execute(
            """SELECT assignment_group,COUNT(*) count FROM post_experiments
               WHERE platform=? AND assignment_stratum=?
                 AND assignment_group IN ('control','variant')
               GROUP BY assignment_group""", (platform, str(stratum))).fetchall())
        control_count = int(counts.get("control", 0))
        variant_count = int(counts.get("variant", 0))
        if control_count != variant_count:
            group = "control" if control_count < variant_count else "variant"
        else:
            bucket_key = "|".join((
                str(row["fact_packet_hash"]), platform, str(stratum)))
            group = ("variant" if int(hashlib.sha256(
                bucket_key.encode()).hexdigest(), 16) % 2 else "control")
        control_id, variant_id = _phase_b_candidate_ids(row)
        candidate_id = variant_id if group == "variant" else control_id
        conn.execute(
            """UPDATE post_experiments SET platform=?,assignment_group=?,
               assignment_stratum=?,
               published_candidate_id=?,published_candidate_type=?,
               selection_status='assigned',result_status='awaiting_approval',
               updated_at=? WHERE experiment_id=?""",
            (platform, group, str(stratum), candidate_id, group, now, experiment_id))
        conn.commit()
    return {"status": "assigned", "assignment_group": group,
            "candidate_id": candidate_id, "deterministic": True,
            "human_approval_required": True, "external_posts": 0}


def approve_phase_b(db_path: Path, experiment_id: str, candidate_id: str,
                    approved_by: str, decision: str = "approved",
                    reason: str = "") -> dict:
    """Record an explicit human decision. This function cannot publish."""
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    if not str(approved_by).strip():
        raise ValueError("approved_by is required")
    apply_migrations(db_path)
    now = datetime.now(JST).isoformat()
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            """SELECT published_candidate_id,selection_status
               FROM post_experiments WHERE experiment_id=?""",
            (experiment_id,)).fetchone()
        if row is None:
            return {"status": "not_found"}
        if row["selection_status"] != "assigned":
            return {"status": "blocked", "reason": "assignment_required"}
        if str(row["published_candidate_id"]) != str(candidate_id):
            return {"status": "blocked", "reason": "candidate_not_assigned"}
        conn.execute(
            """INSERT INTO post_experiment_approvals
               (experiment_id,candidate_id,decision,approved_by,reason,decided_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(experiment_id,candidate_id) DO UPDATE SET
               decision=excluded.decision,approved_by=excluded.approved_by,
               reason=excluded.reason,decided_at=excluded.decided_at""",
            (experiment_id, candidate_id, decision, approved_by, reason, now))
        conn.execute(
            """UPDATE post_experiments SET selection_status=?,result_status=?,
               updated_at=? WHERE experiment_id=?""",
            ("approved" if decision == "approved" else "rejected",
             "approved_not_published" if decision == "approved" else "rejected",
             now, experiment_id))
        conn.commit()
    return {"status": decision, "experiment_id": experiment_id,
            "candidate_id": candidate_id, "external_posts": 0}


def link_phase_b_publication(db_path: Path, experiment_id: str,
                             candidate_id: str, platform: str, post_id: str,
                             published_at: str | None = None) -> dict:
    """Link an already-published post; never calls a platform API."""
    apply_migrations(db_path)
    now = datetime.now(JST).isoformat()
    with closing(connect(db_path)) as conn:
        row = conn.execute(
            """SELECT published_candidate_id,published_candidate_type,
                      selection_status FROM post_experiments
               WHERE experiment_id=?""", (experiment_id,)).fetchone()
        approval = conn.execute(
            """SELECT decision FROM post_experiment_approvals
               WHERE experiment_id=? AND candidate_id=?""",
            (experiment_id, candidate_id)).fetchone()
        if row is None:
            return {"status": "not_found"}
        if (row["selection_status"] != "approved" or approval is None
                or approval["decision"] != "approved"):
            return {"status": "blocked", "reason": "human_approval_required"}
        if str(row["published_candidate_id"]) != str(candidate_id):
            return {"status": "blocked", "reason": "candidate_not_assigned"}
        conn.execute(
            """INSERT INTO post_experiment_publications
               (experiment_id,candidate_id,candidate_type,platform,post_id,
                published_at,link_status,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (experiment_id, candidate_id, row["published_candidate_type"],
             platform, str(post_id), published_at or now, "linked", now))
        conn.execute(
            """UPDATE post_experiments SET selection_status='published',
               result_status='collecting',updated_at=? WHERE experiment_id=?""",
            (now, experiment_id))
        conn.commit()
    return {"status": "linked", "post_id": str(post_id),
            "external_posts": 0, "result_status": "collecting"}


def evaluate_phase_b_rollout(db_path: Path, minimum: int | None = None) -> dict:
    """Evaluate rollout eligibility; adoption always remains a human action."""
    minimum = minimum or settings()["rollout_sample"]
    apply_migrations(db_path)
    with closing(connect(db_path)) as conn:
        rows = _rows(conn, """
          SELECT e.assignment_group,o.impressions,o.profile_click_rate,
                 o.follow_conversion_rate,o.specific_reply_rate
          FROM post_experiments e
          JOIN post_experiment_publications p ON p.experiment_id=e.experiment_id
          JOIN post_performance_outcomes o ON o.post_id=p.post_id
          WHERE o.measurement_window='24h' AND e.assignment_group IN
                ('control','variant')""")
    groups = {name: [r for r in rows if r["assignment_group"] == name]
              for name in ("control", "variant")}
    def median(name: str, field: str) -> float | None:
        values = _finite(row.get(field) for row in groups[name])
        return statistics.median(values) if values else None
    ci, vi = median("control", "impressions"), median("variant", "impressions")
    cp, vp = median("control", "profile_click_rate"), median("variant", "profile_click_rate")
    impression_lift = safe_rate(vi - ci, ci) if vi is not None and ci is not None else None
    click_lift = safe_rate(vp - cp, cp) if vp is not None and cp is not None else None
    enough = all(len(groups[name]) >= minimum for name in groups)
    eligible = bool(enough and impression_lift is not None
                    and click_lift is not None
                    and impression_lift >= .15 and click_lift >= .15)
    return {"status": "eligible_for_human_review" if eligible else "not_eligible",
            "control_count": len(groups["control"]),
            "variant_count": len(groups["variant"]),
            "minimum_per_group": minimum, "impression_lift": impression_lift,
            "profile_click_lift": click_lift, "rollout_eligible": eligible,
            "auto_adopted": False, "human_approval_required": True}


def build_analysis(db_path: Path, window_days: int = 90,
                   measurement_window: str = "24h") -> dict:
    if measurement_window not in {"24h", "72h"}:
        raise ValueError("measurement_window must be '24h' or '72h'")
    with closing(connect(db_path)) as conn:
        joined = _rows(conn, """
          SELECT f.*,o.captured_at,o.impressions,o.profile_click_rate,o.follow_conversion_rate,
                 o.quote_rate,o.repost_rate,o.bookmark_rate,o.specific_reply_rate,
                 o.engagement_rate,o.followers_at_publish,o.measurement_window
          FROM post_performance_features f JOIN post_performance_outcomes o
            ON o.post_id=f.post_id
          """)
    cutoff = datetime.now(JST) - timedelta(days=max(1, window_days))
    dated = []
    for row in joined:
        captured = row.get("captured_at")
        try:
            dt = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
            dt = dt.astimezone(JST) if dt.tzinfo else dt.replace(tzinfo=JST)
            if dt < cutoff:
                continue
        except (ValueError, TypeError):
            pass
        dated.append(row)
    selected, coverage = select_measurement_window(dated, measurement_window)
    normalized = normalize_records(selected)
    for row in normalized:
        row["raw_impressions"] = row.get("impressions")
        row["normalized_impressions"] = row.get("normalized_impression_score")
    groups = {}
    for field in ("hook_type", "angle_type", "structure_type", "publish_hour",
                  "topic_demand_score"):
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in normalized:
            key: Any = row.get(field)
            if field == "topic_demand_score" and key is not None:
                key = (
                    "速報" if row.get("breaking_flag") else
                    "低需要" if float(key) < 3.34 else
                    "中需要" if float(key) < 6.67 else "高需要")
            if field == "publish_hour" and key is not None:
                hour = int(key)
                key = (
                    "朝" if 5 <= hour < 11 else "昼" if 11 <= hour < 15 else
                    "夕方" if 15 <= hour < 19 else "夜")
            grouped[str(key or "unknown")].append(row)
        groups[field] = {}
        for key, values in grouped.items():
            group_result = {"posts": len(values)}
            for metric in (
                "impressions", "profile_click_rate", "follow_conversion_rate",
                "quote_rate", "repost_rate", "bookmark_rate",
                "specific_reply_rate", "normalized_impression_score",
            ):
                stats = descriptive_stats(r.get(metric) for r in values)
                group_result.update({
                    f"{metric}_{name}": value for name, value in stats.items()})
            groups[field][key] = group_result
    feature_names = [
        "character_count", "topic_demand_score", "news_freshness_hours",
        "specific_number_present", "specific_actor_present", "cost_bearer_present",
        "decision_maker_present", "human_stake_present", "contrast_present",
        "surprise_present", "comparison_present", "future_scenario_present",
        "counterargument_present", "concrete_question_present",
        "memorable_line_present", "abstract_term_ratio",
        "average_sentence_length", "noun_density",
        "previous_post_interval_hours",
        "previous_24h_normalized_impressions",
        "topic_competition_score", "topic_saturation_score",
        "major_news_flag", "generic_conclusion_flag",
        "same_hook_recent_count", "same_structure_recent_count",
        "semantic_similarity_recent",
    ]
    correlations = {}
    for outcome in ANALYSIS_OUTCOMES:
        correlations[outcome] = {}
        for feature in feature_names:
            base = correlation(
                [r.get(feature) for r in normalized],
                [r.get(outcome) for r in normalized])
            trimmed = _remove_outliers(normalized, outcome)
            after = correlation(
                [r.get(feature) for r in trimmed],
                [r.get(outcome) for r in trimmed])
            base["after_outlier_removal"] = after
            base["direction_maintained_after_outliers"] = (
                base["pearson_correlation"] is not None
                and after["pearson_correlation"] is not None
                and base["pearson_correlation"] * after["pearson_correlation"] >= 0)
            direction = (
                1 if (base["spearman_correlation"] or 0) > 0 else -1)
            base["stratified_direction_stable"] = _stratified_direction_stable(
                normalized, feature, outcome, direction)
            base["robust_slope"] = robust_slope(
                [r.get(feature) for r in normalized],
                [r.get(outcome) for r in normalized])
            base["permutation_importance"] = permutation_importance(
                [r.get(feature) for r in normalized],
                [r.get(outcome) for r in normalized])
            correlations[outcome][feature] = base
    binary = []
    for outcome in ANALYSIS_OUTCOMES:
        for feature in BINARY_FEATURES:
            present = _finite(r.get(outcome) for r in normalized if bool(r.get(feature)))
            absent = _finite(r.get(outcome) for r in normalized if not bool(r.get(feature)))
            ps, ns = descriptive_stats(present), descriptive_stats(absent)
            u, p = mann_whitney_u(present, absent)
            ci_low, ci_high = bootstrap_difference(present, absent)
            binary.append({
                "measurement_window": measurement_window, "outcome": outcome,
                "feature": feature, "present_n": len(present), "absent_n": len(absent),
                "present_mean": ps.get("mean"), "absent_mean": ns.get("mean"),
                "present_median": ps.get("median"), "absent_median": ns.get("median"),
                "present_p25": ps.get("p25"), "present_p75": ps.get("p75"),
                "absent_p25": ns.get("p25"), "absent_p75": ns.get("p75"),
                "mean_difference": (
                    ps["mean"] - ns["mean"] if "mean" in ps and "mean" in ns else None),
                "median_difference": (
                    ps["median"] - ns["median"] if "median" in ps and "median" in ns else None),
                "cliffs_delta": cliffs_delta(present, absent),
                "mann_whitney_u": u, "p_value": p,
                "median_difference_ci_low": ci_low,
                "median_difference_ci_high": ci_high,
                "insufficient_sample": (
                    len(present) < settings()["min_sample"]
                    or len(absent) < settings()["min_sample"]),
            })
    continuous = []
    for outcome in ANALYSIS_OUTCOMES:
        for feature in CONTINUOUS_FEATURES:
            result = correlations[outcome][feature]
            pairs = sorted(
                (float(r[feature]), float(r[outcome])) for r in normalized
                if r.get(feature) is not None and r.get(outcome) is not None)
            quartile_medians = []
            for index in range(4):
                chunk = pairs[
                    math.floor(len(pairs) * index / 4):
                    math.floor(len(pairs) * (index + 1) / 4)]
                quartile_medians.append(
                    statistics.median(y for _, y in chunk) if chunk else None)
            continuous.append({
                "measurement_window": measurement_window, "outcome": outcome,
                "feature": feature, **{
                    k: result.get(k) for k in (
                        "sample_size", "pearson_correlation", "spearman_correlation",
                        "direction_agreement", "magnitude_difference",
                        "outlier_sensitive", "classification")},
                "pearson_after_outliers": result["after_outlier_removal"]["pearson_correlation"],
                "spearman_after_outliers": result["after_outlier_removal"]["spearman_correlation"],
                "q1_outcome_median": quartile_medians[0],
                "q2_outcome_median": quartile_medians[1],
                "q3_outcome_median": quartile_medians[2],
                "q4_outcome_median": quartile_medians[3],
                "linear": (
                    result["magnitude_difference"] is not None
                    and result["magnitude_difference"] < .15),
                "monotonic": result["direction_agreement"],
                "insufficient_sample": result["insufficient_sample"],
            })
    return {"records": normalized, "groups": groups,
            "correlations": correlations, "binary_comparisons": binary,
            "continuous_analysis": continuous, "coverage": coverage,
            "measurement_window": measurement_window, "window_days": window_days}


def persist_correlations(db_path: Path, correlations: dict,
                         window_days: int = 90) -> int:
    now = datetime.now(JST)
    start = now - timedelta(days=max(1, window_days))
    with closing(connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        flattened = [
            (outcome, feature, result)
            for outcome, features in correlations.items()
            for feature, result in features.items()]
        for outcome, feature, result in flattened:
            conn.execute("""INSERT INTO post_feature_correlations
              (feature_name,outcome_name,sample_size,pearson_correlation,
               spearman_correlation,confidence_low,confidence_high,
               analysis_period_start,analysis_period_end,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                feature, outcome, result["sample_size"],
                result["pearson_correlation"], result["spearman_correlation"],
                result["confidence_low"], result["confidence_high"],
                start.isoformat(), now.isoformat(), now.isoformat()))
        conn.commit()
    return len(flattened)


def persist_analysis_results(db_path: Path, analysis: dict,
                             experiments: list[dict]) -> dict:
    """Persist observed historical baselines and review-only recommendations."""
    now = datetime.now(JST)
    observed = {
        str(row.get("content_id")): row for row in analysis["records"]
        if row.get("content_id")
    }
    saved_results = 0
    weak_saved = 0
    start = now - timedelta(days=analysis.get("window_days", 90))
    with closing(connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        for experiment in experiments:
            row = observed.get(str(experiment.get("content_id")))
            if not row:
                continue
            conn.execute(
                "DELETE FROM post_experiment_results WHERE experiment_id=?",
                (experiment["experiment_id"],))
            conn.execute("""INSERT INTO post_experiment_results
              (experiment_id,candidate_id,candidate_type,sample_group,
               normalized_impression_score,normalized_profile_click_score,
               normalized_follow_score,normalized_quote_score,result_json,
               evaluated_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                experiment["experiment_id"],
                experiment["control"]["candidate_id"], "historical_baseline",
                row.get("measurement_window"),
                row.get("normalized_impression_score"),
                row.get("normalized_profile_click_score"),
                row.get("normalized_follow_score"),
                row.get("normalized_quote_score"),
                json.dumps(row, ensure_ascii=False, default=str),
                now.isoformat(), now.isoformat()))
            saved_results += 1
        primary = analysis["correlations"].get("normalized_impressions", {})
        for feature, result in primary.items():
            r = result.get("pearson_correlation")
            if result["insufficient_sample"] or r is None or abs(r) >= .05:
                continue
            conn.execute("""INSERT INTO post_experiment_recommendations
              (analysis_period_start,analysis_period_end,recommendation_type,
               feature_name,sample_size,confidence,recommendation,status,created_at)
              VALUES (?,?,?,?,?,?,?,?,?)""", (
                start.isoformat(), now.isoformat(), "feature_removal_candidate",
                feature, result["sample_size"],
                max(0.0, 1.0 - abs(r)), "実績相関が弱い。重複・収集コストを人間確認",
                "review_required", now.isoformat()))
            weak_saved += 1
        conn.commit()
    return {"experiment_results": saved_results,
            "recommendations": weak_saved}


def _csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\ufeffstatus\nno_data\n", encoding="utf-8")
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _group_rows(groups: dict[str, dict]) -> list[dict]:
    return [{"group": group, **stats} for group, stats in sorted(groups.items())]


def write_reports(root: Path, source: dict, analysis: dict,
                  experiments: list[dict], analysis_72h: dict | None = None) -> dict:
    output = root / "outputs" / "post_experiments" / "latest"
    output.mkdir(parents=True, exist_ok=True)
    counts = {key: len(value) for key, value in source.items()}
    available_windows = sorted({
        str(row.get("measurement_window")) for row in source["x_metrics"] + source["threads"]
        if row.get("measurement_window")
    })
    reply_available = bool(source.get("reply_events"))
    exact_follower_rows = sum(
        row.get("followers_before_window") is not None
        for row in source["published"])
    limitations = [
        "15分窓は既存DBに保存されている場合だけ分析対象",
        "Threadsにはprofile_clicks/follows/bookmarksがない",
        "過去分のバックテストは完全な当時時点再現ではない",
    ]
    if exact_follower_rows < counts["published"]:
        limitations.append(
            "投稿時フォロワー数の過去欠損分は最寄りスナップショットで近似")
    if not reply_available:
        limitations.append(
            "返信本文未収集の投稿はspecific_reply_rateがnull")
    coverage = {
        "generated_candidates": counts["generated"],
        "published_x": counts["published"], "published_threads_rows": counts["threads"],
        "metric_rows_x": counts["x_metrics"], "follower_snapshots": counts["followers"],
        "measurement_windows": available_windows,
        "profile_clicks_available": any(r.get("profile_clicks") is not None for r in source["x_metrics"]),
        "follows_available": any(
            row.get("estimated_follower_delta") is not None
            for row in source["published"]),
        "reply_text_available": reply_available,
        "reply_rows": len(source.get("reply_events", [])),
        "exact_follower_at_publish_rows": exact_follower_rows,
        "limitations": limitations,
    }
    (output / "overview.md").write_text(
        "# 投稿実績検証 Phase A\n\n"
        f"- 公開X投稿: {counts['published']}\n"
        f"- X実績行: {counts['x_metrics']}\n"
        f"- 抽出・結合済み: {len(analysis['records'])}\n"
        f"- ローカル実験: {len(experiments)}\n"
        "- 本番投稿・プロンプト更新・配分変更: 実施なし\n",
        encoding="utf-8")
    (output / "data_coverage.md").write_text(
        "# Data coverage\n\n```json\n" +
        json.dumps(coverage, ensure_ascii=False, indent=2) + "\n```\n",
        encoding="utf-8")
    primary = analysis["correlations"]["normalized_impressions"]
    feature_rows = [{"feature_name": key, **value} for key, value in primary.items()]
    _csv(output / "feature_summary.csv", feature_rows)
    _csv(output / "feature_performance.csv", feature_rows)
    _csv(output / "hook_performance.csv",
         _group_rows(analysis["groups"]["hook_type"]))
    _csv(output / "angle_performance.csv",
         _group_rows(analysis["groups"]["angle_type"]))
    _csv(output / "structure_performance.csv",
         _group_rows(analysis["groups"]["structure_type"]))
    _csv(output / "publish_time_performance.csv",
         _group_rows(analysis["groups"]["publish_hour"]))
    _csv(output / "topic_demand_performance.csv",
         _group_rows(analysis["groups"]["topic_demand_score"]))
    insufficient = len(analysis["records"]) < settings()["min_sample"]
    comparison = (
        "# Control vs variant\n\n"
        "Phase Aでは候補を公開していないため、公開実績によるcontrol/variant勝敗は未判定です。\n"
        "同一fact packet、1テーマ1候補、層化割当をPhase Bで使用します。\n")
    (output / "control_vs_variant.md").write_text(comparison, encoding="utf-8")
    def correlation_report(item: dict) -> str:
        window = item["measurement_window"]
        lines = [f"# 相関分析 {window}", "",
                 "分析単位: 1投稿1行。指定窓の最新取得値だけを使用し、他のmeasurement windowは混在させていません。",
                 "normalized_impressions = raw impressions / 同一(category、4時間帯、weekday、"
                 "breaking、media、需要、鮮度、フォロワー帯、飽和度) peer群のimpressions中央値。",
                 "需要欠損中は文章特徴の因果効果を断定しません。", ""]
        for outcome, features in item["correlations"].items():
            lines += [f"## 目的変数: {outcome}", "",
                      f"- measurement window: {window}",
                      f"- sample size（最大）: {len(item['records'])}", "",
                      "| feature | n | Pearson | Spearman | CI 95% | direction agreement | "
                      "magnitude difference | outlier sensitive | classification | interpretation |",
                      "|---|---:|---:|---:|---|---|---:|---|---|---|"]
            for feature, result in features.items():
                lines.append(
                    f"| {feature} | {result['sample_size']} | "
                    f"{result['pearson_correlation']} | {result['spearman_correlation']} | "
                    f"[{result['confidence_low']}, {result['confidence_high']}] | "
                    f"{result['direction_agreement']} | {result['magnitude_difference']} | "
                    f"{result['outlier_sensitive']} | {result['classification']} | "
                    f"{result['interpretation']} |")
            lines.append("")
        return "\n".join(lines) + "\n"
    (output / "prediction_correlation_24h.md").write_text(
        correlation_report(analysis), encoding="utf-8")
    (output / "prediction_correlation.md").write_text(
        correlation_report(analysis), encoding="utf-8")
    if analysis_72h is not None:
        (output / "prediction_correlation_72h.md").write_text(
            correlation_report(analysis_72h), encoding="utf-8")
    _csv(output / "binary_feature_comparison_24h.csv",
         analysis["binary_comparisons"])
    _csv(output / "continuous_feature_analysis_24h.csv",
         analysis["continuous_analysis"])
    social = generate_candidates(SOCIAL_SECURITY_FIXTURE, important=True)
    social_rows = [social["control"]] + social["variants"]
    social_text = "\n\n## 社会保障fixture\n\n" + "\n".join(
        f"- {item['candidate_type']} / {item['angle_type']}: "
        f"fact_consistent={item['fact_consistent']}, "
        f"score={item['prediction']['predicted_performance_score']}, "
        f"boring={json.dumps(item['boring_signals'], ensure_ascii=False)}, "
        f"eligible={item['eligible']}, "
        f"採用理由={item['selection_reason'] or '-'}, "
        f"不採用理由={','.join(item['rejection_reasons']) or '-'}"
        for item in social_rows)
    (output / "backtest_results.md").write_text(
        "# Backtest\n\n"
        f"{len(experiments)}テーマでcontrol 1件とvariant候補をローカル再生成しました。"
        "過去実績を候補生成入力へ渡していません。完全な時点再現ではないため勝者認定はしません。"
        + social_text + "\n",
        encoding="utf-8")
    ranked = sorted(feature_rows, key=lambda r: abs(r.get("pearson_correlation") or 0),
                    reverse=True)
    winners = [r for r in ranked if not r["insufficient_sample"]
               and (r.get("pearson_correlation") or 0) >= .20
               and (r.get("spearman_correlation") or 0) >= .20
               and r.get("direction_agreement")
               and r.get("direction_maintained_after_outliers")
               and r.get("stratified_direction_stable")]
    losers = [r for r in ranked if not r["insufficient_sample"]
              and (r.get("pearson_correlation") or 0) <= -.20
              and (r.get("spearman_correlation") or 0) <= -.20
              and r.get("direction_agreement")
              and r.get("direction_maintained_after_outliers")
              and r.get("stratified_direction_stable")]
    inconclusive = [r for r in ranked if r not in winners and r not in losers]
    weak = []
    for row in ranked:
        importance = row.get("permutation_importance") or {}
        baseline = importance.get("baseline_mse")
        relative_importance = (
            importance.get("importance") / baseline
            if importance.get("importance") is not None
            and baseline not in {None, 0} else None)
        row["relative_permutation_importance"] = relative_importance
        if (
            not row["insufficient_sample"]
            and abs(row.get("pearson_correlation") or 0) < .05
            and abs(row.get("spearman_correlation") or 0) < .05
            and (relative_importance is None
                 or relative_importance < .01)
        ):
            weak.append(row)
    (output / "winning_patterns.md").write_text(
        "# Winning pattern candidates\n\n" +
        ("\n".join(f"- {r['feature_name']}: Pearson={r['pearson_correlation']:.3f}, "
                   f"Spearman={r['spearman_correlation']:.3f}"
                   for r in winners) or "該当なし（層別で傾向が残ることは別途確認が必要）")
        + "\n", encoding="utf-8")
    (output / "losing_patterns.md").write_text(
        "# Losing pattern candidates\n\n" +
        ("\n".join(f"- {r['feature_name']}: Pearson={r['pearson_correlation']:.3f}, "
                   f"Spearman={r['spearman_correlation']:.3f}"
                   for r in losers) or "該当なし") + "\n",
        encoding="utf-8")
    (output / "inconclusive_patterns.md").write_text(
        "# Inconclusive patterns\n\n" +
        ("\n".join(f"- {r['feature_name']}: {r['classification']}"
                   for r in inconclusive) or "該当なし") + "\n", encoding="utf-8")
    outlier_lines = ["# 外れ値感度", ""]
    for row in analysis["continuous_analysis"]:
        outlier_lines.append(
            f"- {row['outcome']} / {row['feature']}: Pearson "
            f"{row['pearson_correlation']} → {row['pearson_after_outliers']}; "
            f"Spearman {row['spearman_correlation']} → {row['spearman_after_outliers']}")
    (output / "outlier_sensitivity.md").write_text(
        "\n".join(outlier_lines) + "\n", encoding="utf-8")
    records = analysis["records"]
    demand_n = sum(r.get("topic_demand_score") is not None for r in records)
    freshness_n = sum(r.get("news_freshness_hours") is not None for r in records)
    demand_reason = (
        "一部利用可能。ただし欠損行は需要シグナルの保存・結合が未接続、または元テーブルに値がない"
        if demand_n else
        "全件null。既存feature行への需要シグナルの保存・結合が未接続、または元テーブルに値がない")
    freshness_reason = (
        "利用可能" if freshness_n else
        "n=0。source published_atとpost published_atを結ぶ確実な保存値がなく、安全に復元不能")
    (output / "missing_feature_diagnosis.md").write_text(
        "# 欠損特徴の診断\n\n"
        f"- topic_demand_score: n={demand_n}; {demand_reason}\n"
        f"- news_freshness_hours: n={freshness_n}; {freshness_reason}\n\n"
        "news_freshness_hoursは post published_at - source published_at のみで計算します。"
        "source published_atが確認できない場合はnullとし、推測時刻は使いません。\n",
        encoding="utf-8")
    coverage_items = [analysis["coverage"]]
    if analysis_72h:
        coverage_items.append(analysis_72h["coverage"])
    (output / "measurement_window_coverage.md").write_text(
        "# Measurement window coverage\n\n```json\n"
        + json.dumps(coverage_items, ensure_ascii=False, indent=2)
        + "\n```\n", encoding="utf-8")
    sem = primary["semantic_similarity_recent"]
    def sem_result(rows: list[dict]) -> dict:
        return correlation(
            [r.get("semantic_similarity_recent") for r in rows],
            [r.get("normalized_impressions") for r in rows])
    sem_rows = [r for r in records if r.get("semantic_similarity_recent") is not None
                and r.get("normalized_impressions") is not None]
    sorted_outcome = sorted(
        sem_rows, key=lambda r: float(r["normalized_impressions"]))
    top_cut = math.floor(len(sorted_outcome) * .95)
    comparisons = {
        "全投稿": sem_result(sem_rows),
        "breaking投稿を除外": sem_result(
            [r for r in sem_rows if not r.get("breaking_flag")]),
        "上位5%外れ値を除外": sem_result(sorted_outcome[:top_cut]),
    }
    topic_counts = Counter(str(r.get("topic_key") or "") for r in sem_rows)
    comparisons["同一topic_key反復を除外"] = sem_result([
        r for r in sem_rows if not r.get("topic_key")
        or topic_counts[str(r.get("topic_key"))] == 1])
    for category in sorted({str(r.get("category") or "unknown") for r in sem_rows}):
        comparisons[f"category={category}"] = sem_result([
            r for r in sem_rows if str(r.get("category") or "unknown") == category])
    if analysis_72h:
        comparisons["72h固定"] = correlation(
            [r.get("semantic_similarity_recent") for r in analysis_72h["records"]],
            [r.get("normalized_impressions") for r in analysis_72h["records"]])
    comparison_lines = "\n".join(
        f"- {name}: Pearson={value['pearson_correlation']}, "
        f"Spearman={value['spearman_correlation']}, n={value['sample_size']}"
        for name, value in comparisons.items())
    (output / "semantic_similarity_investigation.md").write_text(
        "# semantic_similarity_recent 調査\n\n"
        + comparison_lines + "\n"
        "- 類似度対象は現在投稿より前に公開された別post_id最大20件です。自己比較と未来投稿を除外しました。\n"
        "- 同一ニュース継続と成功テンプレートは識別用の確実なラベルがなく、直接分離できません。\n"
        "- measurement windowは24hと72hを別々に比較し、混在させていません。\n"
        "- 原因を特定できないため推奨特徴には採用しません。\n",
        encoding="utf-8")
    recommended = winners[:5]
    (output / "recommended_features.md").write_text(
        "# Recommended prediction features\n\n" +
        ("\n".join(f"- {r['feature_name']}" for r in recommended)
         or "insufficient_sample: 現行5項目を補助値として維持") + "\n",
        encoding="utf-8")
    (output / "features_to_remove.md").write_text(
        "# Features to remove\n\n" +
        ("\n".join(
            f"- {r['feature_name']}: Pearson/Spearmanと"
            f"Permutation importanceが低い"
                   for r in weak) or "insufficient_sample") +
        "\n\n自動削除は行わず、収集コスト・重複・説明可能性も人間が確認します。\n",
        encoding="utf-8")
    phase_b = """# Phase B plan

- control 50% / variant 50%（初期案）
- 同一ニュースからはどちらか一方だけを公開
- category / topic_demand_bucket / publish_hour_bucket / breaking_flag / media_present で層化
- 最低100公開比較、中央値impressions +15%、profile click rate +15%、followと安全が非悪化で全面採用候補
- 強い成功: impressions +25%、quote +20%、profile click +20%、follow +10%
- 罵倒・党派攻撃・単発バズを成功扱いしない
- 人間承認なしにprompt、投稿比率、数、時刻、強度、テーマ配分を変更しない
"""
    (output / "phase_b_plan.md").write_text(phase_b, encoding="utf-8")
    quality = {
        "phase": PHASE, "generated_at": datetime.now(JST).isoformat(),
        "external_publish_attempted": False, "auto_prompt_update": False,
        "auto_schedule_change": False, "auto_ratio_change": False,
        "coverage": coverage, "analyzed_records": len(analysis["records"]),
        "insufficient_sample": insufficient, "prediction_feature_limit": 5,
        "phase_b_recommended": False,
        "phase_b_reason": "公開control/variant比較が100件未満",
        "social_security_fixture_variants": len(social["variants"]),
        "all_expected_reports_created": True,
        "measurement_windows_analyzed": [
            analysis["measurement_window"],
            *([analysis_72h["measurement_window"]] if analysis_72h else [])],
        "topic_demand_non_null": demand_n,
        "freshness_non_null": freshness_n,
    }
    (output / "quality_report.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "POST_PERFORMANCE_EXPERIMENT_AUDIT.md").write_text(
        "# 投稿実績・実験基盤監査\n\n"
        "## 利用可能データ\n\n```json\n" +
        json.dumps(coverage, ensure_ascii=False, indent=2) +
        "\n```\n\n## 方針\n\n欠損分母はnullのまま保存し、推測で補完しません。"
        "需要は真偽・重要性・安全性と分離します。Phase Aは外部投稿しません。\n",
        encoding="utf-8")
    (docs / "POST_AB_TEST_DESIGN.md").write_text(
        phase_b + "\n## 公平性\n\n同じfact_packet_hashを確認し、同じテーマの両案を"
        "公開しません。平均だけでなく中央値、分位点、trimmed mean、信頼区間を併記します。\n",
        encoding="utf-8")
    return quality


def audit(root: Path, limit: int = 300) -> dict:
    db_path, shadow = resolve_db_path(root)
    source = load_source_data(db_path, limit)
    return {**{key: len(value) for key, value in source.items()},
            "shadow_database": shadow}


def feature_extract(root: Path, limit: int = 300) -> dict:
    db_path, shadow = resolve_db_path(root)
    source = load_source_data(db_path, limit)
    rows = source["published"] + [{
        **r, "post_id": r.get("threads_post_id"), "platform": "threads",
        "posted_at": r.get("published_at"),
    } for r in source["threads"]]
    x_24h = {
        str(row.get("tweet_id")): row.get("impressions")
        for row in source["x_metrics"]
        if row.get("measurement_window") == "24h"
    }
    measured_24h = _finite(x_24h.values())
    x_24h_median = (
        statistics.median(measured_24h) if measured_24h else None)
    for row in rows:
        raw = x_24h.get(str(row.get("tweet_id")))
        row["normalized_24h_impressions"] = (
            float(raw) / x_24h_median
            if raw is not None and x_24h_median not in {None, 0}
            else None)
    records, recent = [], []
    for row in reversed(rows):
        record = extract_features(row, recent)
        records.append(record)
        recent.append({**row, **record, "text": row.get("text") or row.get("tweet_text")})
    return {"saved": persist_features(db_path, records), "records": records,
            "shadow_database": shadow}


def performance_sync(root: Path, limit: int = 300) -> dict:
    db_path, shadow = resolve_db_path(root)
    source = load_source_data(db_path, limit)
    published = {str(r.get("tweet_id")): r for r in source["published"]}
    replies_by_post: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for reply in source.get("reply_events", []):
        replies_by_post[(
            str(reply.get("platform") or ""),
            str(reply.get("root_post_id") or ""),
        )].append(reply)
    outcomes = []
    for row in source["x_metrics"]:
        post = published.get(str(row.get("tweet_id")), {})
        outcomes.append(outcome_record({
            **row, "post_id": row.get("tweet_id"),
            "captured_at": row.get("measured_at"),
            "link_clicks": row.get("url_clicks"),
            "followers_at_publish": _followers_at_publish(
                post, source["followers"], post.get("posted_at")),
            "follows": post.get("estimated_follower_delta"),
            "reply_classifications": deduplicated_reply_classes(
                replies_by_post.get(("x", str(row.get("tweet_id"))), [])),
        }))
    for row in source["threads"]:
        outcomes.append(outcome_record({
            **row, "post_id": row.get("threads_post_id"),
            "captured_at": row.get("measured_at"),
            "followers_at_publish": _nearest_followers(
                source["followers"], row.get("published_at")),
            "reply_classifications": deduplicated_reply_classes(
                replies_by_post.get(
                    ("threads", str(row.get("threads_post_id"))), [])),
        }))
    return {"saved": persist_outcomes(db_path, outcomes), "records": outcomes,
            "shadow_database": shadow}


def candidate_for_content(root: Path, content_id: str | None = None,
                          dry_run: bool = True) -> dict:
    db_path, shadow = resolve_db_path(root)
    if not content_id:
        experiment = generate_candidates(
            SOCIAL_SECURITY_FIXTURE, important=True, use_openai=True,
            budget_path=db_path)
        persist_experiment(db_path, experiment)
        experiment["dry_run"] = dry_run
        experiment["shadow_database"] = shadow
        return experiment
    source = load_source_data(db_path, 500)
    row = next((r for r in source["generated"] if str(r.get("id")) == str(content_id)), None)
    if row is None:
        row = next((r for r in source["published"]
                    if str(r.get("generated_post_id")) == str(content_id)), None)
    if row is None:
        raise ValueError(f"content_id not found: {content_id}")
    content = {**row, "content_id": str(content_id),
               "title": row.get("title") or row.get("text", "")[:80],
               "summary": row.get("summary") or row.get("text", ""),
               "search_snapshot": {
                   "x_search_post_count": row.get("x_search_post_count"),
                   "x_search_unique_authors": row.get(
                       "x_search_unique_authors"),
                   "x_search_velocity": row.get("x_search_velocity"),
                   "x_search_engagement": row.get("x_search_engagement"),
                   "captured_at": row.get("xai_discovered_at"),
               }}
    experiment = generate_candidates(
        content, bool(row.get("is_breaking") or row.get("is_major_update")),
        use_openai=True, budget_path=db_path)
    persist_experiment(db_path, experiment)
    experiment["dry_run"] = dry_run
    experiment["shadow_database"] = shadow
    return experiment


def backtest(root: Path, limit: int = 100, dry_run: bool = True) -> dict:
    db_path, shadow = resolve_db_path(root)
    source = load_source_data(db_path, limit)
    experiments = []
    for row in source["published"][:limit]:
        experiment = generate_candidates({
            **row, "content_id": str(row.get("generated_post_id") or row.get("id")),
            "title": str(row.get("text") or "")[:80],
            "summary": str(row.get("text") or ""),
            "search_snapshot": {
                "x_search_post_count": row.get("x_search_post_count"),
                "x_search_unique_authors": row.get(
                    "x_search_unique_authors"),
                "x_search_velocity": row.get("x_search_velocity"),
                "x_search_engagement": row.get("x_search_engagement"),
                "captured_at": row.get("xai_discovered_at"),
            },
        }, bool(row.get("is_breaking") or row.get("is_major_update")))
        persist_experiment(db_path, experiment)
        experiments.append(experiment)
    return {"count": len(experiments), "experiments": experiments,
            "dry_run": dry_run, "external_publish_attempted": False,
            "shadow_database": shadow}


def status(root: Path) -> dict:
    db_path, shadow = resolve_db_path(root)
    apply_migrations(db_path)
    with closing(connect(db_path)) as conn:
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("post_performance_features",
                                "post_performance_outcomes", "post_experiments",
                                "post_candidate_predictions",
                                "post_feature_correlations",
                                "post_experiment_snapshots",
                                "post_reply_events")}
    return {"phase": PHASE, "enabled": settings()["enabled"],
            "auto_publish": False, "counts": counts,
            "shadow_database": shadow}


def full_cycle(root: Path, limit: int = 300, dry_run: bool = True,
               notify_discord: bool = True) -> dict:
    db_path, shadow = resolve_db_path(root)
    if not apply_migrations(db_path):
        raise RuntimeError("SQLite migration failed")
    source = load_source_data(db_path, limit)
    extracted = feature_extract(root, limit)
    synced = performance_sync(root, limit)
    tested = backtest(root, min(limit, 100), dry_run=True)
    analysis = build_analysis(db_path, measurement_window="24h")
    analysis_72h = build_analysis(db_path, measurement_window="72h")
    persist_correlations(db_path, analysis["correlations"])
    persist_correlations(db_path, analysis_72h["correlations"])
    persisted_analysis = persist_analysis_results(
        db_path, analysis, tested["experiments"])
    quality = write_reports(
        root, source, analysis, tested["experiments"], analysis_72h=analysis_72h)
    result = {"phase": PHASE, "dry_run": dry_run, "audit": {
        key: len(value) for key, value in source.items()},
        "features_saved": extracted["saved"], "outcomes_saved": synced["saved"],
        "analysis_rows_saved": persisted_analysis,
        "backtests": tested["count"], "analyzed_records": len(analysis["records"]),
        "reports": str(root / "outputs/post_experiments/latest"),
        "shadow_database": shadow,
        "external_publish_attempted": False, "quality": quality}
    if notify_discord and not dry_run:
        try:
            from discord_notify import notify
            hook_groups = analysis["groups"].get("hook_type", {})
            angle_groups = analysis["groups"].get("angle_type", {})
            def best(groups: dict, reverse: bool = True) -> str:
                eligible = [(k, v.get("impressions_median")) for k, v in groups.items()
                            if not v.get("impressions_insufficient_sample")
                            and v.get("impressions_median") is not None]
                return sorted(eligible, key=lambda pair: pair[1],
                              reverse=reverse)[0][0] if eligible else "サンプル不足"
            result["discord_notified"] = notify(
                "POST_EXPERIMENT", "投稿改善 Phase A 分析完了",
                "自動的なPhase B移行は行いません。外部投稿なし。",
                fields={
                    "分析期間": f"直近{analysis['window_days']}日",
                    "公開投稿数": str(len(source["published"])),
                    "利用可能メトリクス": str(len(source["x_metrics"])),
                    "control候補数": str(tested["count"]),
                    "variant候補数": str(sum(
                        len(e["variants"]) for e in tested["experiments"])),
                    "伸びた角度": best(angle_groups),
                    "弱かった角度": best(angle_groups, False),
                    "伸びたフック": best(hook_groups),
                    "弱かったフック": best(hook_groups, False),
                    "サンプル不足": str(quality["insufficient_sample"]),
                    "Phase B移行推奨": "不可",
                    "理由": quality["phase_b_reason"],
                })
        except Exception:
            result["discord_notified"] = False
    return result
