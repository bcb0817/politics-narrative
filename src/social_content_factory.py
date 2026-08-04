"""Phase A social content demand-discovery and inventory engine.

This module is deliberately local-first.  It reads already verified material,
creates reusable content packets and candidates, and stores them in SQLite.
It contains no X, Threads, note, video, Discord, or browser publishing client.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from metrics_db import connect, db_path, init_db


ROOT = Path(__file__).resolve().parent.parent
JST = ZoneInfo("Asia/Tokyo")
WINDOWS = ("15m", "1h", "6h", "24h", "72h")

SCHEMA = """
CREATE TABLE IF NOT EXISTS content_topics (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT UNIQUE, title TEXT,
 category TEXT, status TEXT, source_count INTEGER DEFAULT 0,
 theme_value_score REAL, first_seen_at TEXT, last_seen_at TEXT, metadata_json TEXT);
CREATE TABLE IF NOT EXISTS content_claims (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 claim_text TEXT, fact_status TEXT, risk_level TEXT, primary_sources_json TEXT,
 created_at TEXT, updated_at TEXT, UNIQUE(topic_key,claim_key));
CREATE TABLE IF NOT EXISTS content_angles (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 content_angle TEXT, angle_type TEXT, question_answered TEXT, status TEXT,
 created_at TEXT, UNIQUE(content_id,claim_key,content_angle));
CREATE TABLE IF NOT EXISTS content_packets (
 id INTEGER PRIMARY KEY, content_id TEXT UNIQUE, topic_key TEXT, packet_json TEXT,
 source_hash TEXT, generated_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS content_inventory (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 content_angle TEXT, priority REAL, freshness REAL, expires_at TEXT,
 source_count INTEGER, fact_status TEXT, risk_level TEXT, quality_score REAL,
 theme_value_score REAL, platform TEXT, format TEXT, status TEXT,
 payload_json TEXT, created_at TEXT, updated_at TEXT,
 UNIQUE(content_id,claim_key,content_angle,platform,format));
CREATE TABLE IF NOT EXISTS content_hypotheses (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 hypothesis TEXT, success_metric TEXT, status TEXT, created_at TEXT,
 UNIQUE(content_id,claim_key,hypothesis));
CREATE TABLE IF NOT EXISTS content_variants (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 content_angle TEXT, platform TEXT, format TEXT, hook TEXT, body TEXT,
 conclusion TEXT, source_type TEXT, theme_value_score REAL,
 quality_score REAL, threshold REAL, status TEXT, created_at TEXT,
 UNIQUE(content_id,platform,claim_key,content_angle,hook));
CREATE TABLE IF NOT EXISTS content_experiments (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 content_angle TEXT, platform TEXT, format TEXT, hypothesis_id INTEGER,
 status TEXT, scheduled_at TEXT, published_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS content_performance_windows (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 platform TEXT, platform_post_id TEXT, measurement_window TEXT,
 due_at TEXT, measured_at TEXT, metrics_json TEXT, relative_score REAL,
 anomaly_flags_json TEXT, status TEXT,
 UNIQUE(platform,platform_post_id,measurement_window));
CREATE TABLE IF NOT EXISTS content_demand_signals (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 platform TEXT, signal_type TEXT, signal_value REAL, sample_size INTEGER,
 verified_fact INTEGER DEFAULT 0, payload_json TEXT, observed_at TEXT);
CREATE TABLE IF NOT EXISTS content_visual_candidates (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 visual_type TEXT, aspect_ratio TEXT, brief_json TEXT, source_json TEXT,
 fact_status TEXT, status TEXT, created_at TEXT,
 UNIQUE(content_id,claim_key,visual_type,aspect_ratio));
CREATE TABLE IF NOT EXISTS content_thread_candidates (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 posts_json TEXT, source_json TEXT, status TEXT, created_at TEXT,
 UNIQUE(content_id,claim_key));
CREATE TABLE IF NOT EXISTS content_short_candidates (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 winning_hook TEXT, winning_platform TEXT, audience_question TEXT,
 core_claim TEXT, counterpoint TEXT, visual_metaphor TEXT,
 source_posts_json TEXT, short_script_outline_json TEXT,
 longform_potential REAL, confidence REAL, criteria_json TEXT,
 status TEXT, created_at TEXT, UNIQUE(content_id,claim_key));
CREATE TABLE IF NOT EXISTS content_longform_candidates (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 outline_json TEXT, potential REAL, status TEXT, created_at TEXT,
 UNIQUE(content_id,claim_key));
CREATE TABLE IF NOT EXISTS content_article_candidates (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 recommended_format TEXT, x_article INTEGER, note INTEGER, longform INTEGER,
 thread INTEGER, skip INTEGER, reason TEXT, source_count INTEGER,
 reader_questions_json TEXT, issue_count INTEGER, status TEXT, created_at TEXT,
 UNIQUE(content_id,claim_key,recommended_format));
CREATE TABLE IF NOT EXISTS content_reuse_links (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 source_platform TEXT, source_id TEXT, destination_format TEXT,
 destination_id TEXT, created_at TEXT,
 UNIQUE(source_platform,source_id,destination_format));
CREATE TABLE IF NOT EXISTS platform_actions (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 platform TEXT, action_type TEXT, risk_level TEXT, fact_status TEXT,
 approval_required INTEGER, auto_enabled INTEGER, status TEXT,
 payload_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS reply_candidates (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 platform TEXT, target_id TEXT, reply_type TEXT, body TEXT,
 faq_match TEXT, risk_level TEXT, fact_status TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS quote_candidates (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, claim_key TEXT,
 platform TEXT, target_id TEXT, source_type TEXT, body TEXT,
 risk_level TEXT, fact_status TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS source_health (
 id INTEGER PRIMARY KEY, source_name TEXT UNIQUE, source_url TEXT,
 source_type TEXT, authority_level INTEGER, update_frequency TEXT,
 category TEXT, language TEXT, enabled INTEGER, last_success_at TEXT,
 failure_count INTEGER DEFAULT 0, cost REAL DEFAULT 0, metadata_json TEXT);
CREATE TABLE IF NOT EXISTS config_audit_results (
 id INTEGER PRIMARY KEY, audited_at TEXT, config_json TEXT,
 mismatch_count INTEGER, env_modified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS growth_daily_reports (
 id INTEGER PRIMARY KEY, report_date TEXT UNIQUE, generated_at TEXT,
 report_json TEXT, dry_run INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS growth_weekly_reports (
 id INTEGER PRIMARY KEY, week_start TEXT UNIQUE, generated_at TEXT,
 report_json TEXT, dry_run INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_factory_inventory_status
 ON content_inventory(status,platform,expires_at);
CREATE INDEX IF NOT EXISTS idx_factory_variants_platform
 ON content_variants(platform,status,created_at);
CREATE INDEX IF NOT EXISTS idx_factory_windows_due
 ON content_performance_windows(status,due_at);
CREATE INDEX IF NOT EXISTS idx_factory_signals_topic
 ON content_demand_signals(topic_key,platform,observed_at);
CREATE INDEX IF NOT EXISTS idx_factory_claim
 ON content_claims(topic_key,claim_key);
"""


EVERGREEN_SEEDS = (
    ("tax-income", "所得税の仕組み", "税", "https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/"),
    ("tax-consumption", "消費税の仕組み", "税", "https://www.nta.go.jp/taxes/shiraberu/taxanswer/shohi/"),
    ("social-insurance-health", "健康保険料はどう決まるか", "社会保険", "https://www.kyoukaikenpo.or.jp/g3/"),
    ("social-insurance-pension", "公的年金の財源", "社会保険", "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/nenkin/nenkin/index.html"),
    ("budget-general", "一般会計予算の読み方", "予算", "https://www.mof.go.jp/policy/budget/"),
    ("budget-supplementary", "補正予算と当初予算", "予算", "https://www.mof.go.jp/policy/budget/"),
    ("bond-issuance", "国債発行の仕組み", "国債", "https://www.mof.go.jp/jgbs/"),
    ("bond-interest", "国債費と金利", "国債", "https://www.mof.go.jp/policy/budget/"),
    ("election-house", "衆議院選挙の仕組み", "選挙制度", "https://www.soumu.go.jp/senkyo/senkyo_s/naruhodo/"),
    ("election-council", "参議院選挙の仕組み", "選挙制度", "https://www.soumu.go.jp/senkyo/senkyo_s/naruhodo/"),
    ("diet-committee", "国会委員会の役割", "国会", "https://www.shugiin.go.jp/internet/itdb_annai.nsf/html/statics/ugoki/"),
    ("diet-question", "国会質問と答弁の記録", "国会", "https://kokkai.ndl.go.jp/"),
    ("law-process", "法案成立までの流れ", "法案成立", "https://www.sangiin.go.jp/japanese/aramashi/houritu.html"),
    ("law-cabinet", "閣法と議員立法", "法案成立", "https://www.clb.go.jp/recent-laws/process/"),
    ("cabinet-decision", "閣議決定と法律の違い", "内閣", "https://www.kantei.go.jp/jp/kakugi/"),
    ("cabinet-confidence", "内閣不信任決議", "内閣", "https://www.shugiin.go.jp/"),
    ("local-assembly", "地方議会の役割", "地方自治", "https://www.soumu.go.jp/main_sosiki/jichi_gyousei/"),
    ("local-chief", "知事・市町村長の権限", "地方自治", "https://www.soumu.go.jp/main_sosiki/jichi_gyousei/"),
    ("local-allocation", "地方交付税の仕組み", "地方交付税", "https://www.soumu.go.jp/main_sosiki/c-zaisei/kouhu.html"),
    ("local-tax", "地方税と国税の違い", "地方交付税", "https://www.soumu.go.jp/main_sosiki/jichi_zeisei/"),
    ("security-defense", "防衛費の構成", "安全保障", "https://www.mod.go.jp/j/budget/"),
    ("security-treaty", "日米安全保障条約", "安全保障", "https://www.mofa.go.jp/mofaj/area/usa/hosho/"),
    ("energy-mix", "エネルギーミックス", "エネルギー", "https://www.enecho.meti.go.jp/category/others/basic_plan/"),
    ("energy-grid", "電力系統と安定供給", "エネルギー", "https://www.enecho.meti.go.jp/category/electricity_and_gas/electric/"),
    ("foreign-residence", "在留資格の仕組み", "外国人政策", "https://www.moj.go.jp/isa/"),
    ("foreign-worker", "外国人雇用制度", "外国人政策", "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/koyou_roudou/koyou/gaikokujin/"),
    ("justice-retrial", "再審制度", "司法制度", "https://www.courts.go.jp/"),
    ("justice-procedure", "刑事裁判と適正手続", "司法制度", "https://www.courts.go.jp/"),
    ("admin-public-comment", "パブリックコメント", "行政監視", "https://public-comment.e-gov.go.jp/"),
    ("admin-information", "情報公開制度", "行政監視", "https://www.soumu.go.jp/main_sosiki/gyoukan/kanri/jyohokokai/"),
    ("corporate-edinet", "EDINETで分かること", "企業ガバナンス", "https://disclosure.edinet-fsa.go.jp/"),
    ("corporate-tdnet", "適時開示制度", "企業ガバナンス", "https://www.jpx.co.jp/equities/listing/disclosure/"),
)


ANGLE_DEFINITIONS = (
    ("breaking", "速報角度", "いま何が決まったのか"),
    ("change", "何が変わるか", "生活・制度は何が変わるか"),
    ("beneficiary", "誰が得るか", "直接・間接の受益者は誰か"),
    ("burden", "誰が負担するか", "費用と実務を誰が負担するか"),
    ("fiscal_source", "財源", "財源と持続可能性は何か"),
    ("institutional_problem", "制度上の問題", "権限・責任・検証方法は明確か"),
    ("historical_comparison", "過去との比較", "従来制度から何が変わったか"),
    ("international_comparison", "海外との比較", "制度前提の違いは何か"),
    ("support", "賛成側の主張", "最も強い賛成論は何か"),
    ("opposition", "反対側の主張", "最も強い反対論は何か"),
    ("editorial", "久世ゆいの評価", "行政監視の原則でどう評価するか"),
    ("misunderstanding", "よくある誤解", "見出しだけで誤解しやすい点は何か"),
    ("short_video", "Short動画案", "60秒で説明すべき一点は何か"),
    ("longform", "長尺動画案", "背景から検証する論点は何か"),
    ("x_article", "X記事案", "短文では不足する説明は何か"),
    ("note", "note案", "一次資料を読み解く記事にできるか"),
)

CLAIM_BY_ANGLE = {
    "breaking": "main-event",
    "change": "policy-impact",
    "beneficiary": "beneficiary-impact",
    "burden": "burden-impact",
    "fiscal_source": "fiscal-source",
    "institutional_problem": "accountability",
    "historical_comparison": "historical-change",
    "international_comparison": "international-context",
    "support": "supporting-case",
    "opposition": "opposing-case",
    "editorial": "editorial-assessment",
    "misunderstanding": "misunderstanding",
    "short_video": "short-potential",
    "longform": "longform-potential",
    "x_article": "article-potential",
    "note": "note-potential",
}

THRESHOLDS = {
    "breaking": 7.5,
    "news_explainer": 7.0,
    "conversation": 6.5,
    "evergreen": 7.0,
    "visual": 7.0,
    "thread": 7.5,
    "short_candidate": 8.0,
    "longform_candidate": 8.5,
}


def _now() -> datetime:
    return datetime.now(JST)


def _slug(text: str, fallback: str = "topic") -> str:
    value = re.sub(r"[^a-z0-9一-龥ぁ-んァ-ン]+", "-", text.lower()).strip("-")
    if not value:
        value = fallback
    return value[:72]


def _hash(*parts: Any, length: int = 12) -> str:
    raw = "|".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def apply_migrations(path: Path | None = None) -> bool:
    init_db(path)
    try:
        with closing(connect(path)) as conn:
            conn.executescript(SCHEMA)
            conn.commit()
        return True
    except sqlite3.Error:
        return False


def theme_value_score(features: dict[str, float]) -> float:
    weights = {
        "demand": .25,
        "public_value": .20,
        "novelty": .15,
        "video_potential": .15,
        "discussion": .10,
        "primary_sources": .10,
        "persona_fit": .05,
    }
    return round(sum(max(0, min(10, float(features.get(k, 0)))) * w for k, w in weights.items()), 2)


def post_quality_score(features: dict[str, float]) -> float:
    weights = {
        "accuracy": .30,
        "hook": .20,
        "clarity": .20,
        "originality": .15,
        "conversation": .10,
        "safety": .05,
    }
    return round(sum(max(0, min(10, float(features.get(k, 0)))) * w for k, w in weights.items()), 2)


def threshold_for(post_type: str) -> float:
    env_name = f"CONTENT_THRESHOLD_{post_type.upper()}"
    try:
        return float(os.environ.get(env_name, THRESHOLDS.get(post_type, 7.0)))
    except ValueError:
        return THRESHOLDS.get(post_type, 7.0)


def _latest_verified_news(limit: int = 15, path: Path | None = None) -> list[dict]:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        rows = conn.execute(
            """SELECT id,title,summary,source_name,source_url,topic_key,genre,
                      source_reliability_score,final_news_score,published_at,
                      metadata_json,discovered_via_json
               FROM news_candidates
               WHERE verified=1
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _packet_from_source(source: dict) -> dict:
    title = str(source.get("title") or "政治制度の基礎")
    topic_key = str(source.get("topic_key") or _slug(title))
    source_id = str(source.get("id") or _hash(title, source.get("source_url")))
    content_id = f"content-{_hash(topic_key, source_id)}"
    source_url = str(source.get("source_url") or "")
    source_name = str(source.get("source_name") or "公式資料")
    primary = [{"name": source_name, "url": source_url}] if source_url else []
    summary = str(source.get("summary") or "")
    event = title if not summary else f"{title}。{summary[:240]}"
    angles = [
        {
            "claim_key": CLAIM_BY_ANGLE[key],
            "content_angle": key,
            "label": label,
            "question": question,
        }
        for key, label, question in ANGLE_DEFINITIONS
    ]
    packet = {
        "content_id": content_id,
        "topic_key": topic_key,
        "source_news_ids": [source_id],
        "primary_sources": primary,
        "verified_facts": [title, summary[:300]] if summary else [title],
        "main_event": event,
        "stakeholders": ["国民", "政府・行政", "国会", "関係事業者"],
        "financial_impact": "財源、直接負担、実施コストを一次資料で確認する",
        "legal_or_policy_impact": "根拠法令、実施主体、施行時期、検証方法を確認する",
        "supporting_arguments": ["政策目的を達成するための合理性と緊急性"],
        "opposing_arguments": ["負担配分、透明性、副作用、代替案の不足"],
        "common_misunderstandings": ["発表・閣議決定・法成立・施行は同じではない"],
        "reader_questions": [
            "誰が対象ですか",
            "誰が負担しますか",
            "いつから変わりますか",
            "検証と見直しは誰が行いますか",
        ],
        "hook_variants": [
            f"{title}。見出しより重要なのは制度の条件です。",
            f"誰が得て、誰が負担するのか。{title}を分解します。",
            f"{title}で、次に確認すべき数字は何か。",
        ],
        "x_text_variants": [
            "事実→制度→負担→評価",
            "結論→賛成論→反対論→確認点",
        ],
        "threads_text_variants": [
            "背景→制度→読者への問い",
            "よくある誤解→一次資料→補足",
        ],
        "content_angles": angles,
        "visual_angles": ["3つの論点", "誰が負担するか", "制度フロー"],
        "short_video_hooks": [
            f"60秒で分かる：{title}",
            f"{title}、負担するのは誰か",
        ],
        "short_video_angles": ["60秒で変化を説明", "受益と負担を1枚で比較"],
        "short_video_suitability": 8,
        "longform_potential": 8,
        "note_potential": 8,
        "x_article_potential": 8,
        "fact_status": "verified",
        "risk_level": "low",
        "source_type": "official_or_verified_news",
    }
    return packet


def generate_packet(
    topic_key: str | None = None,
    content_id: str | None = None,
    *,
    persist: bool = True,
    path: Path | None = None,
) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        if content_id:
            row = conn.execute(
                "SELECT packet_json FROM content_packets WHERE content_id=?",
                (content_id,),
            ).fetchone()
            if row:
                return _loads(row[0], {})
        if topic_key:
            row = conn.execute(
                """SELECT id,title,summary,source_name,source_url,topic_key,genre,
                          source_reliability_score,final_news_score,published_at
                   FROM news_candidates WHERE verified=1 AND topic_key=?
                   ORDER BY id DESC LIMIT 1""",
                (topic_key,),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT id,title,summary,source_name,source_url,topic_key,genre,
                          source_reliability_score,final_news_score,published_at
                   FROM news_candidates WHERE verified=1 ORDER BY id DESC LIMIT 1"""
            ).fetchone()
    if row:
        packet = _packet_from_source(dict(row))
    else:
        seed = EVERGREEN_SEEDS[0]
        packet = _packet_from_source(
            {
                "id": f"evergreen-{seed[0]}",
                "title": seed[1],
                "summary": "公式資料を基に制度、負担、責任の所在を整理する。",
                "source_name": "公式資料",
                "source_url": seed[3],
                "topic_key": seed[0],
                "genre": seed[2],
            }
        )
    if persist:
        _save_packet(packet, path)
    return packet


def _save_packet(packet: dict, path: Path | None = None) -> None:
    now = _now().isoformat()
    features = {
        "demand": 6.5,
        "public_value": 8.5,
        "novelty": 7,
        "video_potential": 8,
        "discussion": 7,
        "primary_sources": 9 if packet["primary_sources"] else 5,
        "persona_fit": 8,
    }
    theme_score = theme_value_score(features)
    with closing(connect(path)) as conn:
        conn.execute(
            """INSERT INTO content_topics
               (content_id,topic_key,title,category,status,source_count,
                theme_value_score,first_seen_at,last_seen_at,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(topic_key) DO UPDATE SET
                 last_seen_at=excluded.last_seen_at,
                 source_count=MAX(content_topics.source_count,excluded.source_count),
                 theme_value_score=MAX(content_topics.theme_value_score,excluded.theme_value_score)""",
            (
                packet["content_id"], packet["topic_key"], packet["main_event"][:180],
                "", "active", len(packet["primary_sources"]), theme_score,
                now, now, _json({"source_type": packet["source_type"]}),
            ),
        )
        conn.execute(
            """INSERT INTO content_packets
               (content_id,topic_key,packet_json,source_hash,generated_at,updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(content_id) DO UPDATE SET
                 packet_json=excluded.packet_json,updated_at=excluded.updated_at""",
            (
                packet["content_id"], packet["topic_key"], _json(packet),
                _hash(packet["primary_sources"], packet["verified_facts"]), now, now,
            ),
        )
        for angle in packet["content_angles"]:
            claim_key = angle["claim_key"]
            conn.execute(
                """INSERT INTO content_claims
                   (content_id,topic_key,claim_key,claim_text,fact_status,risk_level,
                    primary_sources_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(topic_key,claim_key) DO UPDATE SET updated_at=excluded.updated_at""",
                (
                    packet["content_id"], packet["topic_key"], claim_key,
                    angle["question"], packet["fact_status"], packet["risk_level"],
                    _json(packet["primary_sources"]), now, now,
                ),
            )
            conn.execute(
                """INSERT OR IGNORE INTO content_angles
                   (content_id,topic_key,claim_key,content_angle,angle_type,
                    question_answered,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    packet["content_id"], packet["topic_key"], claim_key,
                    angle["content_angle"], angle["label"], angle["question"],
                    "candidate", now,
                ),
            )
        conn.commit()


def seed_evergreen_inventory(path: Path | None = None) -> dict:
    apply_migrations(path)
    created = 0
    now = _now()
    for key, title, category, url in EVERGREEN_SEEDS:
        packet = _packet_from_source(
            {
                "id": f"evergreen-{key}",
                "title": title,
                "summary": f"{category}について公式資料を基に制度と負担を整理する。",
                "source_name": "公式資料",
                "source_url": url,
                "topic_key": key,
                "genre": category,
            }
        )
        if not any(
            item.get("url") == "https://elaws.e-gov.go.jp/"
            for item in packet["primary_sources"]
        ):
            packet["primary_sources"].append(
                {"name": "e-Gov法令検索", "url": "https://elaws.e-gov.go.jp/"}
            )
        _save_packet(packet, path)
        content_angle = "evergreen_explainer"
        claim_key = "institutional-basics"
        payload = {
            "title": title,
            "category": category,
            "source": {"name": "公式資料", "url": url},
            "question": "制度の目的、対象、負担、見直し方法は何か",
        }
        with closing(connect(path)) as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO content_inventory
                   (content_id,topic_key,claim_key,content_angle,priority,freshness,
                    expires_at,source_count,fact_status,risk_level,quality_score,
                    theme_value_score,platform,format,status,payload_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    packet["content_id"], key, claim_key, content_angle, 6.5, 10,
                    (now + timedelta(days=365)).isoformat(), 1, "verified", "low",
                    7.5, 7.5, "x", "text", "evergreen_ready", _json(payload),
                    now.isoformat(), now.isoformat(),
                ),
            )
            conn.commit()
            created += conn.total_changes - before
    return {"evergreen_available": len(EVERGREEN_SEEDS), "created": created}


def build_inventory(
    *, dry_run: bool = True, limit: int = 15, path: Path | None = None
) -> dict:
    apply_migrations(path)
    sources = _latest_verified_news(limit, path)
    packets = []
    for source in sources:
        packet = _packet_from_source(source)
        _save_packet(packet, path)
        packets.append(packet)
    if not packets:
        packets.append(generate_packet(persist=True, path=path))
    now = _now()
    added = 0
    status_by_angle = {
        "breaking": "breaking_ready",
        "change": "news_explainer_ready",
        "beneficiary": "news_explainer_ready",
        "burden": "news_explainer_ready",
        "fiscal_source": "news_explainer_ready",
        "institutional_problem": "news_explainer_ready",
        "historical_comparison": "visual_ready",
        "international_comparison": "visual_ready",
        "support": "question_ready",
        "opposition": "question_ready",
        "editorial": "news_explainer_ready",
        "misunderstanding": "evergreen_ready",
        "short_video": "short_ready",
        "longform": "longform_ready",
        "x_article": "x_article_ready",
        "note": "note_ready",
    }
    for packet in packets:
        for angle in packet["content_angles"]:
            theme_score = 7.8
            quality = 7.6 if angle["content_angle"] != "short_video" else 8.1
            fmt = (
                "visual" if "comparison" in angle["content_angle"]
                else "video" if angle["content_angle"] in {"short_video", "longform"}
                else "text"
            )
            payload = {
                "main_event": packet["main_event"],
                "question": angle["question"],
                "primary_sources": packet["primary_sources"],
            }
            with closing(connect(path)) as conn:
                before = conn.total_changes
                conn.execute(
                    """INSERT OR IGNORE INTO content_inventory
                       (content_id,topic_key,claim_key,content_angle,priority,freshness,
                        expires_at,source_count,fact_status,risk_level,quality_score,
                        theme_value_score,platform,format,status,payload_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        packet["content_id"], packet["topic_key"], angle["claim_key"],
                        angle["content_angle"], theme_score, 9,
                        (now + timedelta(hours=24)).isoformat(),
                        len(packet["primary_sources"]), "verified", "low", quality,
                        theme_score, "multi", fmt, status_by_angle[angle["content_angle"]],
                        _json(payload), now.isoformat(), now.isoformat(),
                    ),
                )
                conn.commit()
                added += conn.total_changes - before
    evergreen = seed_evergreen_inventory(path)
    return {
        "dry_run": dry_run,
        "external_writes": 0,
        "packets": len(packets),
        "angles": sum(len(p["content_angles"]) for p in packets),
        "inventory_added": added,
        **evergreen,
    }


def inventory_status(path: Path | None = None) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        statuses = [
            dict(row)
            for row in conn.execute(
                """SELECT status,platform,format,COUNT(*) AS count
                   FROM content_inventory GROUP BY status,platform,format
                   ORDER BY status,platform"""
            )
        ]
        totals = {
            "topics": conn.execute("SELECT COUNT(*) FROM content_topics").fetchone()[0],
            "packets": conn.execute("SELECT COUNT(*) FROM content_packets").fetchone()[0],
            "claims": conn.execute("SELECT COUNT(*) FROM content_claims").fetchone()[0],
            "angles": conn.execute("SELECT COUNT(*) FROM content_angles").fetchone()[0],
            "inventory": conn.execute("SELECT COUNT(*) FROM content_inventory").fetchone()[0],
            "expired": conn.execute(
                "SELECT COUNT(*) FROM content_inventory WHERE expires_at < ?",
                (_now().isoformat(),),
            ).fetchone()[0],
        }
    return {**totals, "statuses": statuses}


def select_inventory(
    platform: str,
    *,
    now: datetime | None = None,
    limit: int = 2,
    path: Path | None = None,
) -> list[dict]:
    """Select safe local candidates; this function never publishes."""
    apply_migrations(path)
    now = now or _now()
    maximum = max(1, min(2, int(limit)))
    with closing(connect(path)) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM content_inventory
                   WHERE platform IN (?, 'multi')
                     AND fact_status='verified'
                     AND risk_level='low'
                     AND expires_at>=?
                     AND status LIKE '%_ready'
                   ORDER BY priority DESC,theme_value_score DESC,
                            quality_score DESC,freshness DESC,id ASC""",
                (platform, now.isoformat()),
            )
        ]
    selected: list[dict] = []
    seen_claims: set[tuple[str, str]] = set()
    criticized_people: set[str] = set()
    for row in rows:
        required = threshold_for(
            {
                "breaking_ready": "breaking",
                "question_ready": "conversation",
                "evergreen_ready": "evergreen",
                "visual_ready": "visual",
                "thread_ready": "thread",
                "short_ready": "short_candidate",
                "longform_ready": "longform_candidate",
            }.get(str(row["status"]), "news_explainer")
        )
        if float(row.get("quality_score") or 0) < required:
            continue
        claim = (str(row["topic_key"]), str(row["claim_key"]))
        if claim in seen_claims:
            continue
        payload = _loads(row.get("payload_json"), {})
        people = {
            str(person).strip()
            for person in payload.get("criticized_people", [])
            if str(person).strip()
        }
        if people & criticized_people:
            continue
        row["publish_authorized"] = False
        selected.append(row)
        seen_claims.add(claim)
        criticized_people.update(people)
        if len(selected) >= maximum:
            break
    return selected


def _variant_text(packet: dict, angle: dict, platform: str, index: int) -> tuple[str, str, str]:
    hooks = (
        f"{angle['label']}。見出しだけでは判断できません。",
        f"この政策、見るべきは「{angle['question']}」です。",
        f"決定より重要なのは、実施後に誰が責任を負うかです。",
    )
    hook = hooks[index % len(hooks)]
    body = (
        f"{packet['main_event'][:120]}\n\n"
        f"確認点\n・{angle['question']}\n・一次資料と実施時期\n・受益と負担の対応"
    )
    if platform == "threads":
        body += "\n\n結論を急がず、制度の条件を一つずつ確認する必要があります。"
    conclusion = "政策目的だけでなく、負担・権限・検証方法まで示すべきです。"
    return hook, body, conclusion


def generate_variants(
    *, dry_run: bool = True, path: Path | None = None
) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        packets = [
            _loads(row[0], {})
            for row in conn.execute(
                "SELECT packet_json FROM content_packets ORDER BY id DESC LIMIT 15"
            )
        ]
    if not packets:
        build_inventory(dry_run=dry_run, path=path)
        return generate_variants(dry_run=dry_run, path=path)

    x_target = max(10, min(14, int(os.environ.get("X_DAILY_ORIGINAL_TARGET_MAX", "14"))))
    threads_target = max(4, min(7, int(os.environ.get("THREADS_DAILY_POST_TARGET_MAX", "6"))))
    created = {"x": 0, "threads": 0}
    now = _now().isoformat()
    seen_signatures: set[str] = set()
    x_formats = (
        "text", "text", "visual", "text", "video", "text", "visual",
        "thread", "text", "reply_quote", "video", "text", "visual", "reply_quote",
    )
    threads_sources = (
        "x_reuse", "threads_native", "reply_driven",
        "evergreen", "video_promotion", "threads_native",
    )
    for platform, target in (("x", x_target), ("threads", threads_target)):
        with closing(connect(path)) as conn:
            existing = int(
                conn.execute(
                    """SELECT COUNT(*) FROM content_variants
                       WHERE platform=? AND status='candidate'""",
                    (platform,),
                ).fetchone()[0]
            )
        required_new = max(0, target - existing)
        search_space = target * max(3, len(packets)) * 2
        for packet_index in range(search_space):
            if created[platform] >= required_new:
                break
            packet = packets[packet_index % len(packets)]
            angles = packet.get("content_angles") or []
            angle = angles[(packet_index // max(1, len(packets))) % len(angles)]
            if angle["content_angle"] in {"longform", "note", "x_article"}:
                continue
            hook, body, conclusion = _variant_text(
                packet, angle, platform, packet_index
            )
            signature = _hash(
                packet["topic_key"], angle["claim_key"], angle["content_angle"], hook
            )
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            post_type = (
                "conversation" if angle["content_angle"] in {"support", "opposition"}
                else "breaking" if angle["content_angle"] == "breaking"
                else "news_explainer"
            )
            theme_score = 7.8
            quality = post_quality_score(
                {
                    "accuracy": 9,
                    "hook": 7.5,
                    "clarity": 8,
                    "originality": 7.5,
                    "conversation": 7.5,
                    "safety": 9,
                }
            )
            variant_format = (
                x_formats[created[platform] % len(x_formats)]
                if platform == "x" else "text"
            )
            source_type = (
                "official_or_verified_news"
                if platform == "x"
                else threads_sources[created[platform] % len(threads_sources)]
            )
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO content_variants
                       (content_id,topic_key,claim_key,content_angle,platform,format,
                        hook,body,conclusion,source_type,theme_value_score,
                        quality_score,threshold,status,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        packet["content_id"], packet["topic_key"], angle["claim_key"],
                        angle["content_angle"], platform, variant_format, hook, body,
                        conclusion, source_type, theme_score,
                        quality, threshold_for(post_type), "candidate", now,
                    ),
                )
                if conn.total_changes:
                    created[platform] += 1
                conn.commit()
    with closing(connect(path)) as conn:
        available = {
            platform: min(
                target,
                int(
                    conn.execute(
                        """SELECT COUNT(*) FROM content_variants
                           WHERE platform=? AND status='candidate'""",
                        (platform,),
                    ).fetchone()[0]
                ),
            )
            for platform, target in (("x", x_target), ("threads", threads_target))
        }
    return {
        "dry_run": dry_run,
        "external_writes": 0,
        "x_candidates": available["x"],
        "threads_candidates": available["threads"],
        "new_candidates": created,
        "same_fact_paraphrases_filtered": True,
        "topic_claim_angle_separated": True,
    }


def plan_publication_batch(
    candidates: list[dict],
    *,
    now: datetime | None = None,
    max_candidates: int | None = None,
    delay_minutes: int | None = None,
) -> list[dict]:
    """Plan at most two non-simultaneous candidates without publishing them."""
    now = now or _now()
    max_candidates = max_candidates or int(
        os.environ.get("MAX_CONTENT_CANDIDATES_PER_RUN", "2")
    )
    delay_minutes = delay_minutes or int(
        os.environ.get("CONTENT_CANDIDATE_DELAY_MINUTES", "30")
    )
    maximum = max(1, min(2, max_candidates))
    selected = []
    seen_claims = set()
    for candidate in candidates:
        claim = (
            str(candidate.get("topic_key") or ""),
            str(candidate.get("claim_key") or ""),
        )
        if claim in seen_claims:
            continue
        planned = dict(candidate)
        planned["planned_at"] = (
            now + timedelta(minutes=delay_minutes * len(selected))
        ).isoformat()
        planned["publish_authorized"] = False
        selected.append(planned)
        seen_claims.add(claim)
        if len(selected) >= maximum:
            break
    return selected


def hypotheses(*, dry_run: bool = True, path: Path | None = None) -> dict:
    apply_migrations(path)
    created = 0
    with closing(connect(path)) as conn:
        angles = conn.execute(
            """SELECT content_id,topic_key,claim_key,content_angle
               FROM content_angles ORDER BY id DESC LIMIT 60"""
        ).fetchall()
        for row in angles:
            hypothesis = (
                f"{row['content_angle']}の疑問へ答える投稿は、単純なニュース要約より"
                "質問・保存・動画昇格シグナルを得やすい"
            )
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO content_hypotheses
                   (content_id,topic_key,claim_key,hypothesis,success_metric,status,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    row["content_id"], row["topic_key"], row["claim_key"],
                    hypothesis, "relative_performance_and_questions",
                    "untested", _now().isoformat(),
                ),
            )
            created += conn.total_changes - before
        conn.commit()
    return {"dry_run": dry_run, "hypotheses_created": created, "external_writes": 0}


def visual_candidates(*, dry_run: bool = True, path: Path | None = None) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        packets = [
            _loads(row[0], {})
            for row in conn.execute(
                "SELECT packet_json FROM content_packets ORDER BY id DESC LIMIT 2"
            )
        ]
    created = 0
    output = []
    visual_types = ("3つの論点", "誰が負担するか")
    for index, packet in enumerate(packets):
        visual_type = visual_types[index % len(visual_types)]
        brief = {
            "renderer": "html_css_svg_or_pillow",
            "visual_type": visual_type,
            "headline": packet["main_event"][:70],
            "panels": [
                "確認済み事実",
                "制度上の争点",
                "今後確認すべき数字",
            ],
            "outputs": {"x": "16:9", "threads": "4:5", "video": "9:16"},
            "numbers_require_primary_source": True,
        }
        with closing(connect(path)) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO content_visual_candidates
                   (content_id,topic_key,claim_key,visual_type,aspect_ratio,
                    brief_json,source_json,fact_status,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    packet["content_id"], packet["topic_key"], "accountability",
                    visual_type, "multi", _json(brief),
                    _json(packet["primary_sources"]), "verified",
                    "visual_ready", _now().isoformat(),
                ),
            )
            created += conn.total_changes
            conn.commit()
        output.append(brief)
    return {"dry_run": dry_run, "created": created, "candidates": output, "external_writes": 0}


def thread_candidates(*, dry_run: bool = True, path: Path | None = None) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        packets = [
            _loads(row[0], {})
            for row in conn.execute(
                "SELECT packet_json FROM content_packets ORDER BY id DESC LIMIT 3"
            )
        ]
    created = 0
    for packet in packets:
        posts = [
            f"結論：{packet['main_event'][:100]}",
            f"何が起きたか：{packet['verified_facts'][0][:120]}",
            "本当の争点：目的だけでなく、対象・負担・実施主体を分けて見ることです。",
            "賛成論には政策目的、反対論には負担と副作用があります。両方を一次資料で確認します。",
            "今後見るべき点：施行時期、予算、検証指標、見直し期限です。",
        ]
        with closing(connect(path)) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO content_thread_candidates
                   (content_id,topic_key,claim_key,posts_json,source_json,status,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    packet["content_id"], packet["topic_key"], "accountability",
                    _json(posts), _json(packet["primary_sources"]), "thread_ready",
                    _now().isoformat(),
                ),
            )
            created += conn.total_changes
            conn.commit()
    return {
        "dry_run": dry_run,
        "created": created,
        "thread_length": "3-5",
        "auto_publish": False,
        "external_writes": 0,
    }


def reply_candidate_list(*, dry_run: bool = True, path: Path | None = None) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        packets = [
            _loads(row[0], {})
            for row in conn.execute(
                "SELECT packet_json FROM content_packets ORDER BY id DESC LIMIT 5"
            )
        ]
    created = 0
    allowed = ("terminology_question", "source_request", "faq", "thanks", "content_location")
    for index, packet in enumerate(packets[:5]):
        reply_type = allowed[index % len(allowed)]
        source = packet["primary_sources"][0] if packet["primary_sources"] else {}
        body = (
            f"確認に使った一次資料は「{source.get('name', '公式資料')}」です。"
            "制度の対象・実施主体・時期を分けて確認できます。"
        )
        with closing(connect(path)) as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT INTO reply_candidates
                   (content_id,topic_key,claim_key,platform,target_id,reply_type,
                    body,faq_match,risk_level,fact_status,status,created_at)
                   SELECT ?,?,?,?,?,?,?,?,?,?,?,?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM reply_candidates
                       WHERE content_id=? AND claim_key=? AND platform=?
                         AND target_id=? AND reply_type=?
                   )""",
                (
                    packet["content_id"], packet["topic_key"], "source-guidance",
                    "multi", "", reply_type, body, reply_type, "low", "verified",
                    "approval_required", _now().isoformat(),
                    packet["content_id"], "source-guidance", "multi", "", reply_type,
                ),
            )
            created += conn.total_changes - before
            conn.commit()
    return {
        "dry_run": dry_run,
        "created": created,
        "allowed_types": allowed,
        "auto_reply_enabled": False,
        "external_writes": 0,
    }


def quote_candidate_list(*, dry_run: bool = True, path: Path | None = None) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        packets = [
            _loads(row[0], {})
            for row in conn.execute(
                "SELECT packet_json FROM content_packets ORDER BY id DESC LIMIT 5"
            )
        ]
    created = 0
    for packet in packets:
        source = packet["primary_sources"][0] if packet["primary_sources"] else {}
        if not source.get("url"):
            continue
        body = "一次資料で確認したいのは、対象、負担、実施時期、見直し条件の4点です。"
        with closing(connect(path)) as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT INTO quote_candidates
                   (content_id,topic_key,claim_key,platform,target_id,source_type,
                    body,risk_level,fact_status,status,created_at)
                   SELECT ?,?,?,?,?,?,?,?,?,?,?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM quote_candidates
                       WHERE content_id=? AND claim_key=? AND platform=? AND target_id=?
                   )""",
                (
                    packet["content_id"], packet["topic_key"], "source-guidance",
                    "x", source["url"], "official_primary_source", body,
                    "low", "verified", "approval_required", _now().isoformat(),
                    packet["content_id"], "source-guidance", "x", source["url"],
                ),
            )
            created += conn.total_changes - before
            conn.commit()
    return {
        "dry_run": dry_run,
        "created": created,
        "people_opinion_quotes_automated": False,
        "auto_quote_enabled": False,
        "external_writes": 0,
    }


def schedule_performance_windows(path: Path | None = None) -> dict:
    apply_migrations(path)
    inserted = 0
    now = _now()
    source_rows: list[tuple[str, str, str, str]] = []
    with closing(connect(path)) as conn:
        for row in conn.execute(
            """SELECT COALESCE(gp.id,pp.id),pp.topic_key,pp.tweet_id,pp.posted_at
               FROM published_posts pp
               LEFT JOIN generated_posts gp ON gp.id=pp.generated_post_id
               WHERE pp.tweet_id IS NOT NULL ORDER BY pp.id DESC LIMIT 100"""
        ):
            content_id = f"x-{row[0]}"
            source_rows.append((content_id, str(row[1] or ""), str(row[2]), str(row[3] or now.isoformat())))
        for row in conn.execute(
            """SELECT COALESCE(source_content_id,'threads-'||id),topic_key,
                      threads_post_id,published_at
               FROM threads_posts WHERE status='published' AND threads_post_id IS NOT NULL
               ORDER BY id DESC LIMIT 100"""
        ):
            source_rows.append((str(row[0]), str(row[1] or ""), str(row[2]), str(row[3] or now.isoformat())))
    offsets = {"15m": 15, "1h": 60, "6h": 360, "24h": 1440, "72h": 4320}
    for content_id, topic_key, post_id, published_at in source_rows:
        try:
            published = datetime.fromisoformat(published_at).astimezone(JST)
        except ValueError:
            published = now
        platform = "threads" if content_id.startswith("threads-") else "x"
        with closing(connect(path)) as conn:
            for window in WINDOWS:
                before = conn.total_changes
                conn.execute(
                    """INSERT OR IGNORE INTO content_performance_windows
                       (content_id,topic_key,claim_key,platform,platform_post_id,
                        measurement_window,due_at,status)
                       VALUES(?,?,?,?,?,?,?,'scheduled')""",
                    (
                        content_id, topic_key, "", platform, post_id, window,
                        (published + timedelta(minutes=offsets[window])).isoformat(),
                    ),
                )
                inserted += conn.total_changes - before
            conn.commit()
    return {"scheduled": inserted, "windows": list(WINDOWS)}


def relative_performance(value: float, peer_values: Iterable[float]) -> float:
    peers = sorted(float(item) for item in peer_values if item is not None)
    if not peers:
        return 1.0
    middle = len(peers) // 2
    median = (
        peers[middle]
        if len(peers) % 2
        else (peers[middle - 1] + peers[middle]) / 2
    )
    return round(float(value) / max(median, 1.0), 3)


def collect_demand_signals(
    *, dry_run: bool = True, path: Path | None = None
) -> dict:
    """Aggregate local SNS observations; never promote them to verified facts."""
    apply_migrations(path)
    signals = []
    now = _now().isoformat()
    try:
        with closing(connect(path)) as conn:
            for row in conn.execute(
                """SELECT entity_key,post_count,unique_authors,velocity,
                          cross_source_count,trend_score
                   FROM threads_trend_entities ORDER BY id DESC LIMIT 100"""
            ):
                signals.append(
                    {
                        "topic_key": str(row["entity_key"] or ""),
                        "platform": "threads",
                        "signal_type": "trend_sample",
                        "value": float(row["trend_score"] or 0),
                        "sample_size": int(row["post_count"] or 0),
                        "payload": {
                            "unique_authors": row["unique_authors"],
                            "velocity": row["velocity"],
                            "cross_source_count": row["cross_source_count"],
                        },
                    }
                )
            for row in conn.execute(
                """SELECT r.root_post_id,a.intent,a.sentiment,a.misunderstanding_score
                   FROM threads_reply_analyses a
                   JOIN threads_replies r ON r.reply_id=a.reply_id
                   ORDER BY a.id DESC LIMIT 100"""
            ):
                signals.append(
                    {
                        "topic_key": str(row["root_post_id"] or ""),
                        "platform": "threads",
                        "signal_type": (
                            "question" if row["intent"] == "question"
                            else "reply_observation"
                        ),
                        "value": float(row["misunderstanding_score"] or 0),
                        "sample_size": 1,
                        "payload": {"intent": row["intent"], "sentiment": row["sentiment"]},
                    }
                )
    except sqlite3.Error:
        pass

    xai_path = ROOT / "data" / "xai_search_latest.json"
    try:
        payload = json.loads(xai_path.read_text(encoding="utf-8"))
        topics = payload.get("topics") or payload.get("items") or []
        for row in topics:
            signals.append(
                {
                    "topic_key": str(row.get("topic_key") or row.get("entity_key") or ""),
                    "platform": "x",
                    "signal_type": "xai_demand_sample",
                    "value": float(
                        row.get("attention_score") or row.get("trend_score") or 0
                    ),
                    "sample_size": int(
                        row.get("post_count") or len(row.get("representative_posts") or [])
                    ),
                    "payload": {
                        "unique_authors": row.get("unique_accounts"),
                        "velocity": row.get("velocity_score"),
                    },
                }
            )
    except (OSError, ValueError, TypeError):
        pass

    inserted = 0
    with closing(connect(path)) as conn:
        for signal in signals:
            if not signal["topic_key"]:
                continue
            before = conn.total_changes
            conn.execute(
                """INSERT INTO content_demand_signals
                   (content_id,topic_key,claim_key,platform,signal_type,
                    signal_value,sample_size,verified_fact,payload_json,observed_at)
                   VALUES(?,?,'',?,?,?,?,0,?,?)""",
                (
                    "", signal["topic_key"], signal["platform"], signal["signal_type"],
                    signal["value"], signal["sample_size"], _json(signal["payload"]), now,
                ),
            )
            inserted += conn.total_changes - before
        conn.commit()
    return {
        "dry_run": dry_run,
        "observed": len(signals),
        "stored": inserted,
        "verified_facts_created": 0,
        "external_writes": 0,
    }


def update_performance_windows(path: Path | None = None) -> dict:
    """Copy already-collected official platform metrics into factory windows."""
    apply_migrations(path)
    updated = 0
    with closing(connect(path)) as conn:
        metric_rows = [
            {
                "platform": "x",
                "post_id": str(row["tweet_id"]),
                "window": str(row["measurement_window"]),
                "value": float(row["impressions"] or 0),
                "payload": dict(row),
            }
            for row in conn.execute(
                """SELECT tweet_id,measurement_window,measured_at,impressions,
                          likes,reposts,replies,quotes,bookmarks,profile_clicks
                   FROM post_metrics"""
            )
        ]
        metric_rows.extend(
            {
                "platform": "threads",
                "post_id": str(row["threads_post_id"]),
                "window": str(row["measurement_window"]),
                "value": float(row["views"] or 0),
                "payload": dict(row),
            }
            for row in conn.execute(
                """SELECT threads_post_id,measurement_window,measured_at,views,
                          likes,replies,reposts,quotes,shares
                   FROM threads_metrics"""
            )
        )
        peer_map: dict[tuple[str, str], list[float]] = {}
        for row in metric_rows:
            peer_map.setdefault((row["platform"], row["window"]), []).append(row["value"])
        for row in metric_rows:
            before = conn.total_changes
            conn.execute(
                """UPDATE content_performance_windows
                   SET measured_at=?,metrics_json=?,relative_score=?,status='measured'
                   WHERE platform=? AND platform_post_id=? AND measurement_window=?""",
                (
                    row["payload"].get("measured_at"), _json(row["payload"]),
                    relative_performance(
                        row["value"], peer_map[(row["platform"], row["window"])]
                    ),
                    row["platform"], row["post_id"], row["window"],
                ),
            )
            updated += conn.total_changes - before
        conn.commit()
    return {"updated": updated, "windows": list(WINDOWS)}


def _short_criteria(packet: dict, path: Path | None = None) -> dict[str, bool]:
    topic = packet.get("topic_key", "")
    relative_scores = []
    platforms = set()
    question_count = 0
    try:
        with closing(connect(path)) as conn:
            relative_scores = [
                float(row[0] or 0)
                for row in conn.execute(
                    """SELECT relative_score FROM content_performance_windows
                       WHERE topic_key=? AND status='measured'""",
                    (topic,),
                )
            ]
            rows = conn.execute(
                """SELECT platform,signal_type,SUM(sample_size)
                   FROM content_demand_signals WHERE topic_key=?
                   GROUP BY platform,signal_type""",
                (topic,),
            ).fetchall()
            platforms = {str(row[0]) for row in rows}
            question_count = sum(
                int(row[2] or 0) for row in rows if row[1] == "question"
            )
    except sqlite3.Error:
        pass
    return {
        "relative_views_1_5x": any(score >= 1.5 for score in relative_scores),
        "reply_rate_top_quartile": False,
        "share_signal_top_quartile": False,
        "three_specific_questions": (
            question_count >= 3 or len(packet.get("reader_questions") or []) >= 3
        ),
        "cross_platform_response": {"x", "threads"} <= platforms,
        "visual_ready": bool(packet.get("visual_angles")),
        "clear_number": any(re.search(r"\d", fact) for fact in packet.get("verified_facts") or []),
        "one_point_in_60_seconds": bool(packet.get("short_video_angles")),
        "longform_room": float(packet.get("longform_potential") or 0) >= 7,
        "primary_sources_sufficient": len(packet.get("primary_sources") or []) >= 1,
    }


def promote_short_candidates(
    *, dry_run: bool = True, path: Path | None = None
) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        packets = [
            _loads(row[0], {})
            for row in conn.execute(
                "SELECT packet_json FROM content_packets ORDER BY id DESC LIMIT 8"
            )
        ]
    target = max(3, min(5, int(os.environ.get("SHORT_CANDIDATES_DAILY_TARGET_MAX", "5"))))
    promoted = []
    for packet in packets:
        criteria = _short_criteria(packet, path)
        matched = sum(criteria.values())
        if matched < 3:
            continue
        candidate = {
            "content_id": packet["content_id"],
            "topic_key": packet["topic_key"],
            "winning_hook": f"60秒で分かる：{packet['main_event'][:70]}",
            "winning_platform": "inventory",
            "audience_question": packet["reader_questions"][0],
            "core_claim": packet["content_angles"][1]["question"],
            "counterpoint": packet["opposing_arguments"][0],
            "visual_metaphor": "受益・負担・責任の三角形",
            "source_posts": [],
            "short_script_outline": [
                "0-3秒: 結論",
                "3-15秒: 確認済み事実",
                "15-40秒: 制度と負担",
                "40-55秒: 反対論と注意点",
                "55-60秒: 次に確認する数字",
            ],
            "longform_potential": packet["longform_potential"],
            "confidence": round(min(1, matched / 8), 2),
            "criteria": criteria,
        }
        with closing(connect(path)) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO content_short_candidates
                   (content_id,topic_key,claim_key,winning_hook,winning_platform,
                    audience_question,core_claim,counterpoint,visual_metaphor,
                    source_posts_json,short_script_outline_json,longform_potential,
                    confidence,criteria_json,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    packet["content_id"], packet["topic_key"], "short-potential",
                    candidate["winning_hook"], candidate["winning_platform"],
                    candidate["audience_question"], candidate["core_claim"],
                    candidate["counterpoint"], candidate["visual_metaphor"],
                    _json(candidate["source_posts"]), _json(candidate["short_script_outline"]),
                    candidate["longform_potential"], candidate["confidence"],
                    _json(criteria), "short_ready", _now().isoformat(),
                ),
            )
            conn.commit()
        promoted.append(candidate)
        if len(promoted) >= target:
            break
    return {
        "dry_run": dry_run,
        "promoted": len(promoted),
        "minimum_criteria": 3,
        "candidates": promoted,
        "external_writes": 0,
    }


def promote_article_candidates(
    *, dry_run: bool = True, path: Path | None = None
) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        packets = [
            _loads(row[0], {})
            for row in conn.execute(
                "SELECT packet_json FROM content_packets ORDER BY id DESC LIMIT 8"
            )
        ]
    output = []
    for packet in packets:
        questions = packet.get("reader_questions") or []
        source_count = len(packet.get("primary_sources") or [])
        issue_count = len(packet.get("content_angles") or [])
        qualifies = len(questions) >= 3 and issue_count >= 6 and source_count >= 2
        recommended = "note" if packet.get("note_potential", 0) >= 8 else "x_article"
        record = {
            "content_id": packet["content_id"],
            "topic_key": packet["topic_key"],
            "recommended_format": recommended if qualifies else "skip",
            "x_article": qualifies,
            "note": qualifies,
            "longform": qualifies and packet.get("longform_potential", 0) >= 8,
            "thread": qualifies,
            "skip": not qualifies,
            "reason": "multiple_questions_and_issue_depth" if qualifies else "insufficient_depth",
            "source_count": source_count,
            "reader_questions": questions,
            "issue_count": issue_count,
        }
        with closing(connect(path)) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO content_article_candidates
                   (content_id,topic_key,claim_key,recommended_format,x_article,
                    note,longform,thread,skip,reason,source_count,
                    reader_questions_json,issue_count,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    packet["content_id"], packet["topic_key"], "article-potential",
                    record["recommended_format"], int(record["x_article"]),
                    int(record["note"]), int(record["longform"]), int(record["thread"]),
                    int(record["skip"]), record["reason"], source_count,
                    _json(questions), issue_count,
                    "article_ready" if qualifies else "skipped", _now().isoformat(),
                ),
            )
            if qualifies:
                conn.execute(
                    """INSERT OR REPLACE INTO content_longform_candidates
                       (content_id,topic_key,claim_key,outline_json,potential,status,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        packet["content_id"], packet["topic_key"], "longform-potential",
                        _json(["背景", "制度", "受益と負担", "賛否", "検証ポイント"]),
                        packet.get("longform_potential", 0), "longform_ready", _now().isoformat(),
                    ),
                )
            conn.commit()
        if qualifies:
            output.append(record)
        if len(output) >= 2:
            break
    return {"dry_run": dry_run, "promoted": len(output), "candidates": output, "external_writes": 0}


def source_health(path: Path | None = None) -> dict:
    apply_migrations(path)
    registry_path = ROOT / "config" / "social_content_sources.json"
    sources = json.loads(registry_path.read_text(encoding="utf-8"))
    with closing(connect(path)) as conn:
        for source in sources:
            conn.execute(
                """INSERT INTO source_health
                   (source_name,source_url,source_type,authority_level,
                    update_frequency,category,language,enabled,failure_count,cost,
                    metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,0,?,?)
                   ON CONFLICT(source_name) DO UPDATE SET
                     source_url=excluded.source_url,
                     enabled=excluded.enabled,metadata_json=excluded.metadata_json""",
                (
                    source["name"], source["url"], source["source_type"],
                    source["authority_level"], source["update_frequency"],
                    source["category"], source["language"], int(source["enabled"]),
                    source["cost"], _json({"network_check": "not_run_by_phase_a_dry_run"}),
                ),
            )
        conn.commit()
        rows = [dict(row) for row in conn.execute("SELECT * FROM source_health ORDER BY authority_level DESC")]
    return {"sources": rows, "network_calls": 0}


def budget_simulation(
    x_posts: int = 14,
    threads_posts: int = 6,
    visuals: int = 2,
    short_candidates: int = 5,
    days: int = 30,
) -> dict:
    x_posts = max(0, x_posts)
    threads_posts = max(0, threads_posts)
    visuals = max(0, visuals)
    short_candidates = max(0, short_candidates)
    days = max(1, days)
    rates = {
        "openai_x_candidate": .0040,
        "openai_threads_candidate": .0030,
        "openai_visual_brief": .0015,
        "openai_short_candidate": .0060,
        "xai_daily": .06,
        "x_create": .0025,
        "image_local": 0.0,
        "tts_preview": .0030,
    }
    openai_daily = (
        x_posts * rates["openai_x_candidate"]
        + threads_posts * rates["openai_threads_candidate"]
        + visuals * rates["openai_visual_brief"]
        + short_candidates * rates["openai_short_candidate"]
    )
    xai_daily = rates["xai_daily"]
    x_daily = x_posts * rates["x_create"]
    media_daily = visuals * rates["image_local"] + short_candidates * rates["tts_preview"]
    total = (openai_daily + xai_daily + x_daily + media_daily) * days
    current_budget = float(os.environ.get("TOTAL_MONTHLY_API_BUDGET_USD", "36"))
    daily = total / days
    over_day = math.floor(current_budget / daily) + 1 if daily > 0 and total > current_budget else None
    return {
        "inputs": {
            "x_posts": x_posts,
            "threads_posts": threads_posts,
            "visuals": visuals,
            "short_candidates": short_candidates,
            "days": days,
        },
        "openai_forecast_usd": round(openai_daily * days, 2),
        "xai_forecast_usd": round(xai_daily * days, 2),
        "x_api_forecast_usd": round(x_daily * days, 2),
        "image_tts_forecast_usd": round(media_daily * days, 2),
        "total_forecast_usd": round(total, 2),
        "candidate_generation_unit_cost_usd": round(openai_daily / max(1, x_posts + threads_posts), 4),
        "published_post_unit_cost_usd": round((openai_daily + x_daily) / max(1, x_posts + threads_posts), 4),
        "short_candidate_unit_cost_usd": rates["openai_short_candidate"] + rates["tts_preview"],
        "current_budget_usd": current_budget,
        "budget_overrun_day": over_day,
        "production_budget_changed": False,
    }


def growth_status(path: Path | None = None) -> dict:
    apply_migrations(path)
    names = (
        "content_topics", "content_claims", "content_angles", "content_packets",
        "content_inventory", "content_hypotheses", "content_variants",
        "content_visual_candidates", "content_thread_candidates",
        "reply_candidates", "quote_candidates", "content_short_candidates",
        "content_longform_candidates", "content_article_candidates",
        "content_performance_windows",
    )
    with closing(connect(path)) as conn:
        counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in names
        }
    return {
        "phase": os.environ.get("SOCIAL_CONTENT_FACTORY_PHASE", "A"),
        "external_auto_publish_increased": False,
        "production_posting_limits_changed": False,
        "counts": counts,
    }


def _report_payload(path: Path | None = None) -> dict:
    status = growth_status(path)
    counts = status["counts"]
    with closing(connect(path)) as conn:
        demand_signals = conn.execute(
            "SELECT COUNT(*) FROM content_demand_signals"
        ).fetchone()[0]
    return {
        "observed_topics": counts["content_topics"],
        "valid_topic_clusters": min(15, counts["content_packets"]),
        "effective_hypotheses": counts["content_hypotheses"],
        "x_original_candidates": min(14, _variant_count("x", path)),
        "x_reply_quote_candidates": min(
            5, counts["reply_candidates"] + counts["quote_candidates"]
        ),
        "threads_candidates": min(7, _variant_count("threads", path)),
        "visual_candidates": min(2, counts["content_visual_candidates"]),
        "short_candidates": min(5, counts["content_short_candidates"]),
        "longform_candidates": min(1, counts["content_longform_candidates"]),
        "article_candidates": min(2, counts["content_article_candidates"]),
        "demand_signals": demand_signals,
        "reuse_contents_per_topic": round(
            counts["content_variants"] / max(1, counts["content_topics"]), 2
        ),
        "measurement_windows": list(WINDOWS),
        "kpi_priority": [
            "effective_content_hypotheses",
            "video_ready_topics",
            "demand_signals_from_questions_and_quotes",
            "short_conversion_rate",
            "reuse_contents_per_topic",
            "cross_platform_win_rate",
        ],
    }


def _variant_count(platform: str, path: Path | None = None) -> int:
    with closing(connect(path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM content_variants WHERE platform=?", (platform,)
        ).fetchone()[0]


def daily_report(*, dry_run: bool = True, path: Path | None = None) -> dict:
    apply_migrations(path)
    report = _report_payload(path)
    report["report_date"] = _now().date().isoformat()
    report["dry_run"] = dry_run
    with closing(connect(path)) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO growth_daily_reports
               (report_date,generated_at,report_json,dry_run) VALUES(?,?,?,?)""",
            (report["report_date"], _now().isoformat(), _json(report), int(dry_run)),
        )
        conn.commit()
    return report


def weekly_report(*, dry_run: bool = True, path: Path | None = None) -> dict:
    apply_migrations(path)
    now = _now()
    week_start = (now.date() - timedelta(days=now.weekday())).isoformat()
    report = _report_payload(path)
    report["week_start"] = week_start
    report["dry_run"] = dry_run
    with closing(connect(path)) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO growth_weekly_reports
               (week_start,generated_at,report_json,dry_run) VALUES(?,?,?,?)""",
            (week_start, now.isoformat(), _json(report), int(dry_run)),
        )
        conn.commit()
    return report


def full_cycle(*, dry_run: bool = True, path: Path | None = None) -> dict:
    """Run the local Phase A cycle.  No external publishing client is imported."""
    results = {
        "inventory": build_inventory(dry_run=dry_run, path=path),
        "variants": generate_variants(dry_run=dry_run, path=path),
        "hypotheses": hypotheses(dry_run=dry_run, path=path),
        "visuals": visual_candidates(dry_run=dry_run, path=path),
        "threads": thread_candidates(dry_run=dry_run, path=path),
        "replies": reply_candidate_list(dry_run=dry_run, path=path),
        "quotes": quote_candidate_list(dry_run=dry_run, path=path),
        "demand_signals": collect_demand_signals(dry_run=dry_run, path=path),
        "performance_windows": schedule_performance_windows(path),
        "measured_windows": update_performance_windows(path),
        "shorts": promote_short_candidates(dry_run=dry_run, path=path),
        "articles": promote_article_candidates(dry_run=dry_run, path=path),
        "sources": source_health(path),
    }
    results["daily_report"] = daily_report(dry_run=dry_run, path=path)
    results["safety"] = {
        "external_posts": 0,
        "x_posts": 0,
        "threads_posts": 0,
        "video_publishes": 0,
        "note_publishes": 0,
        "windows_tasks_registered": 0,
        "env_modified": False,
    }
    return results
