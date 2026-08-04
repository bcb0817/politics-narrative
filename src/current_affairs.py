"""Local-only Phase A expansion from politics to current-affairs explainers.

The module classifies already supplied/verified material, scores brand fit,
extends content packets and stores candidates/reports in SQLite.  It does not
import or call any publishing, browser, scraping, profile or task API.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from metrics_db import connect, init_db

ROOT = Path(__file__).resolve().parent.parent
JST = ZoneInfo("Asia/Tokyo")
CATEGORY_FILE = ROOT / "config" / "current_affairs_categories.json"
SOURCE_FILE = ROOT / "config" / "current_affairs_sources.json"
REPORT_DIR = ROOT / "reports" / "current_affairs"

SCHEMA = """
CREATE TABLE IF NOT EXISTS content_categories (
 id INTEGER PRIMARY KEY, category_key TEXT UNIQUE, parent_category_key TEXT,
 display_name TEXT, enabled INTEGER, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS content_category_assignments (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, primary_category TEXT,
 secondary_category TEXT, confidence REAL, source TEXT, created_at TEXT,
 UNIQUE(content_id,primary_category,secondary_category));
CREATE TABLE IF NOT EXISTS content_category_scores (
 id INTEGER PRIMARY KEY, content_id TEXT UNIQUE, social_impact REAL,
 household_impact REAL, economic_impact REAL, policy_relevance REAL,
 systemic_issue REAL, accountability_value REAL, public_safety_value REAL,
 video_potential REAL, reader_utility REAL, source_quality REAL, novelty REAL,
 brand_fit REAL, total_score REAL, created_at TEXT);
CREATE TABLE IF NOT EXISTS content_category_daily_stats (
 id INTEGER PRIMARY KEY, date TEXT, category TEXT, observed_topics INTEGER,
 valid_topics INTEGER, candidates INTEGER, published INTEGER,
 short_candidates INTEGER, article_candidates INTEGER, metrics_json TEXT,
 created_at TEXT, UNIQUE(date,category));
CREATE TABLE IF NOT EXISTS content_category_weekly_stats (
 id INTEGER PRIMARY KEY, week_start TEXT, category TEXT, published INTEGER,
 performance_score REAL, short_promotion_rate REAL, article_promotion_rate REAL,
 follower_growth_association REAL, created_at TEXT, UNIQUE(week_start,category));
CREATE TABLE IF NOT EXISTS content_exclusions (
 id INTEGER PRIMARY KEY, content_id TEXT, topic_key TEXT, reason TEXT,
 category_detected TEXT, brand_fit_score REAL, created_at TEXT,
 UNIQUE(content_id,reason));
CREATE INDEX IF NOT EXISTS idx_category_assignments_topic
 ON content_category_assignments(topic_key,primary_category);
CREATE INDEX IF NOT EXISTS idx_category_daily_date
 ON content_category_daily_stats(date,category);
"""

WEIGHTS = {
    "social_impact": .18, "household_impact": .10, "economic_impact": .10,
    "policy_relevance": .12, "systemic_issue": .12,
    "accountability_value": .10, "public_safety_value": .08,
    "video_potential": .08, "reader_utility": .07, "source_quality": .03,
    "novelty": .01, "brand_fit": .01,
}

CATEGORY_KEYWORDS = {
    "politics_policy": (
        "国会", "政府", "法案", "法律", "税", "予算", "年金", "選挙", "自治体",
        "政策", "規制", "行政", "司法", "移民", "社会保障", "閣議", "大臣",
    ),
    "economy_business": (
        "企業", "決算", "倒産", "破産", "買収", "合併", "賃金", "物価", "雇用",
        "株主", "取引先", "金融", "市場", "景気", "住宅", "経営", "リストラ",
    ),
    "major_incidents": (
        "重大事故", "死亡事故", "列車事故", "航空事故", "産業事故", "大規模火災",
        "建物崩壊", "重大事件", "多数死傷", "公共安全", "運輸安全委員会",
    ),
    "technology_ai": (
        "AI", "人工知能", "生成AI", "ロボット", "自動化", "半導体",
        "自動運転", "デジタル庁", "プラットフォーム", "技術政策", "モデル",
    ),
    "cybersecurity": (
        "サイバー", "ランサムウェア", "情報流出", "不正アクセス", "侵入",
        "アカウント乗っ取り", "マルウェア", "脆弱性", "IPA", "JPCERT",
    ),
    "society_living": (
        "教育", "子育て", "医療", "介護", "食品安全", "観光", "交通",
        "人口", "労働", "消費者", "保育", "家計", "住宅", "生活",
    ),
    "security_defense": (
        "安全保障", "防衛", "外交", "同盟", "台湾", "中国", "経済安保",
        "貿易安全保障", "防衛装備", "抑止", "サプライチェーン",
    ),
    "disaster_infrastructure": (
        "地震", "洪水", "台風", "山火事", "停電", "通信障害", "断水",
        "鉄道運休", "道路寸断", "インフラ", "避難", "気象庁", "復旧",
    ),
}

GOVERNANCE_WORDS = (
    "ガバナンス", "内部統制", "監査", "不正", "責任", "説明責任",
    "行政監督", "規制当局", "公益通報", "調達", "経営陣", "再発防止",
)
EXCLUSION_PATTERNS = {
    "entertainment_gossip": ("熱愛", "不倫", "交際", "離婚", "芸能人の恋愛"),
    "minor_flame": ("軽微な炎上", "SNS炎上だけ", "失言で炎上"),
    "sports_results_only": ("試合結果", "勝利", "敗戦", "スコア"),
    "product_release_only": ("新商品発売", "新製品を発売", "予約開始"),
    "private_person_drama": ("私人同士", "一般人を晒", "個人の私生活"),
}

SUBCATEGORY_KEYWORDS = {
    "national_politics": ("国会", "内閣", "首相"), "legislation": ("法案", "法律", "施行"),
    "tax": ("税", "課税"), "social_security": ("年金", "社会保障"),
    "public_finance": ("予算", "財源", "国債"), "elections": ("選挙", "投票"),
    "local_government": ("自治体", "知事", "市長"), "judiciary": ("裁判", "司法"),
    "corporate_earnings": ("決算", "利益"), "corporate_failure": ("経営破綻", "不祥事"),
    "bankruptcy": ("倒産", "破産"), "merger_acquisition": ("買収", "合併", "M&A"),
    "employment": ("雇用", "リストラ"), "wages_prices": ("賃金", "物価"),
    "generative_ai": ("生成AI", "大規模言語モデル"), "semiconductors": ("半導体",),
    "data_privacy": ("個人情報", "プライバシー"), "ransomware": ("ランサムウェア",),
    "data_breach": ("情報流出", "漏えい"), "service_outage": ("サービス停止", "障害"),
    "healthcare": ("医療",), "education": ("教育",), "childcare": ("子育て", "保育"),
    "labor": ("労働",), "consumer_protection": ("消費者",),
    "defense_policy": ("防衛", "防衛装備"), "foreign_affairs": ("外交",),
    "economic_security": ("経済安保", "経済安全保障"), "earthquake": ("地震",),
    "flood": ("洪水", "浸水"), "power_outage": ("停電",),
    "telecom_outage": ("通信障害",), "rail_disruption": ("鉄道", "運休"),
    "transportation_accident": ("列車事故", "航空事故", "重大事故"),
    "industrial_accident": ("産業事故", "工場事故"), "large_fire": ("大規模火災",),
}

DEFAULT_FIXTURES = (
    {"content_id":"fixture-politics","topic_key":"tax-reform","title":"政府が所得税制度改正法案を公表","summary":"家計負担、財源、国会審議と施行時期を一次資料で確認する。","source_name":"財務省","source_type":"official","verified":True},
    {"content_id":"fixture-politics-budget","topic_key":"public-budget","title":"\u653f\u5e9c\u304c\u4e88\u7b97\u6848\u3068\u8ca1\u6e90\u3092\u516c\u8868","summary":"\u56fd\u4f1a\u5be9\u8b70\u3001\u7d0d\u7a0e\u8005\u8ca0\u62c5\u3001\u884c\u653f\u52b9\u7387\u3068\u653f\u7b56\u52b9\u679c\u3092\u516c\u5f0f\u8cc7\u6599\u3067\u78ba\u8a8d\u3059\u308b\u3002","source_name":"\u8ca1\u52d9\u7701","source_type":"official","verified":True},
    {"content_id":"fixture-politics-local","topic_key":"local-legislation","title":"\u81ea\u6cbb\u4f53\u304c\u6761\u4f8b\u6539\u6b63\u6848\u3092\u516c\u8868","summary":"\u5236\u5ea6\u5229\u7528\u8005\u3001\u516c\u8cbb\u8ca0\u62c5\u3001\u8b70\u4f1a\u306e\u610f\u601d\u6c7a\u5b9a\u3068\u884c\u653f\u8cac\u4efb\u3092\u78ba\u8a8d\u3059\u308b\u3002","source_name":"\u81ea\u6cbb\u4f53\u516c\u5f0f","source_type":"official","verified":True},
    {"content_id":"fixture-corporate","topic_key":"corporate-controls","title":"大手企業の不正会計と内部統制不備","summary":"株主、従業員、取引先への影響と監査・経営責任、行政監督を検証する。","source_name":"企業IR・適時開示","source_type":"official","verified":True},
    {"content_id":"fixture-ai","topic_key":"ai-platform","title":"生成AI基盤モデルの公式発表","summary":"雇用、生産性、プライバシー、規制と国内産業への影響を整理する。","source_name":"企業公式技術文書","source_type":"official","verified":True},
    {"content_id":"fixture-cyber","topic_key":"ransomware-hospital","title":"医療機関がランサムウェア被害を公表","summary":"診療停止、情報流出、バックアップ、認証と経営責任を確認する。","source_name":"医療機関公式・IPA","source_type":"official","verified":True},
    {"content_id":"fixture-society","topic_key":"medical-system","title":"医療費制度変更を政府が公表","summary":"患者の家計負担、利用条件、財源と自治体実務への影響を確認する。","source_name":"厚生労働省","source_type":"official","verified":True},
    {"content_id":"fixture-incident","topic_key":"rail-accident","title":"多数に影響した重大な列車事故","summary":"運輸安全委員会の調査を待ち、確認済み事実、公共安全、原因と再発防止を分離する。","source_name":"事業者公式・運輸安全委員会","source_type":"official","verified":True},
    {"content_id":"fixture-disaster","topic_key":"power-outage","title":"広域で大規模停電が発生","summary":"住民と事業者への影響、復旧、代替手段、設備維持と監督責任を確認する。","source_name":"電力事業者・自治体","source_type":"official","verified":True},
    {"content_id":"fixture-defense","topic_key":"defense-equipment","title":"政府が防衛装備調達方針を公表","summary":"抑止力、法的根拠、国内産業、調達費と同盟への影響を確認する。","source_name":"防衛省","source_type":"official","verified":True},
    {"content_id":"fixture-gossip","topic_key":"celebrity-romance","title":"芸能人の熱愛報道","summary":"私人の恋愛だけで制度・安全・家計への社会的影響はない。","source_name":"まとめサイト","source_type":"news","verified":False},
)


def _now() -> datetime:
    return datetime.now(JST)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def apply_migrations(path: Path | None = None) -> bool:
    init_db(path)
    with closing(connect(path)) as conn:
        conn.executescript(SCHEMA)
        now = _now().isoformat()
        categories = _load_json(CATEGORY_FILE)["categories"]
        for key, row in categories.items():
            enabled = _env_bool(f"CONTENT_CATEGORY_{key.upper()}_ENABLED", True)
            conn.execute(
                """INSERT INTO content_categories
                   (category_key,parent_category_key,display_name,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(category_key) DO UPDATE SET
                    display_name=excluded.display_name,enabled=excluded.enabled,
                    updated_at=excluded.updated_at""",
                (key, None, row["display_name"], int(enabled), now, now),
            )
            for subcategory in row["subcategories"]:
                conn.execute(
                    """INSERT INTO content_categories
                       (category_key,parent_category_key,display_name,enabled,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(category_key) DO UPDATE SET
                        parent_category_key=excluded.parent_category_key,
                        enabled=excluded.enabled,updated_at=excluded.updated_at""",
                    (subcategory, key, subcategory, int(enabled), now, now),
                )
        conn.commit()
    return True


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def category_config() -> dict:
    return _load_json(CATEGORY_FILE)


def categories(path: Path | None = None) -> dict:
    apply_migrations(path)
    cfg = category_config()
    return {
        "bot_definition": cfg["bot_definition"],
        "categories": cfg["categories"],
        "internal_names_may_be_rendered": False,
        "profile_auto_update": False,
    }


def _text(item: dict) -> str:
    return " ".join(str(item.get(k) or "") for k in ("title", "summary", "body", "genre"))


def _matches(text: str, words: Iterable[str]) -> int:
    return sum(1 for word in words if word.lower() in text.lower())


def classify(item: dict) -> dict:
    text = _text(item)
    category_hits = {key: _matches(text, words) for key, words in CATEGORY_KEYWORDS.items()}
    primary = max(category_hits, key=lambda key: (category_hits[key], key == "politics_policy"))
    society_priority_terms = (
        "\u533b\u7642", "\u4ecb\u8b77", "\u6559\u80b2",
        "\u5b50\u80b2\u3066", "\u4fdd\u80b2", "\u6d88\u8cbb\u8005",
    )
    if primary == "politics_policy" and any(term in text for term in society_priority_terms):
        if category_hits["society_living"] >= category_hits[primary] - 1:
            primary = "society_living"
    if category_hits[primary] == 0:
        primary = "politics_policy" if _matches(text, ("制度", "公費", "責任")) else "society_living"
    secondaries = [
        key for key, hits in sorted(category_hits.items(), key=lambda row: -row[1])
        if key != primary and hits > 0
    ][:3]
    if _matches(text, GOVERNANCE_WORDS) and "governance_accountability" not in secondaries:
        secondaries.append("governance_accountability")
    subcategories = [
        key for key, words in SUBCATEGORY_KEYWORDS.items() if _matches(text, words)
    ]
    confidence = min(1.0, .55 + .08 * category_hits[primary])
    critical = primary == "major_incidents"
    return {
        "primary_category": primary,
        "secondary_categories": secondaries,
        "subcategories": subcategories,
        "confidence": round(confidence, 2),
        "route": "existing_major_incident_pipeline" if critical else "current_affairs_phase_a",
        "category_hits": category_hits,
    }


def exclusion_reason(item: dict, classification: dict | None = None) -> str:
    text = _text(item)
    public_interest_override = _matches(
        text, ("ガバナンス", "業界", "規制", "消費者被害", "労働", "公共安全",
               "行政対応", "公費", "内部統制", "プラットフォームの責任")
    )
    if public_interest_override:
        return ""
    for reason, patterns in EXCLUSION_PATTERNS.items():
        if _matches(text, patterns):
            return reason
    if str(item.get("source_name", "")).find("まとめサイト") >= 0:
        return "aggregator_only"
    return ""


def _signal(text: str, words: Iterable[str], base: float = 3.5) -> float:
    return min(10.0, base + 1.5 * _matches(text, words))


def score_item(item: dict, classification: dict | None = None) -> dict:
    classification = classification or classify(item)
    text = _text(item)
    official = str(item.get("source_type", "")).lower() in {"official", "official_api", "official_feed"}
    verified = bool(item.get("verified", official))
    scores = {
        "social_impact": _signal(text, ("社会", "多数", "住民", "患者", "利用者", "従業員", "広域")),
        "household_impact": _signal(text, ("家計", "生活", "物価", "賃金", "医療費", "住宅")),
        "economic_impact": _signal(text, ("企業", "雇用", "経済", "費用", "産業", "株主", "取引先")),
        "policy_relevance": _signal(text, ("政策", "制度", "法律", "規制", "行政", "政府", "自治体")),
        "systemic_issue": _signal(text, ("構造", "制度", "再発防止", "内部統制", "維持管理", "サプライチェーン")),
        "accountability_value": _signal(text, GOVERNANCE_WORDS),
        "public_safety_value": _signal(text, ("安全", "事故", "災害", "攻撃", "停電", "被害", "避難")),
        "video_potential": _signal(text, ("数字", "比較", "仕組み", "なぜ", "影響", "再発防止"), 4.0),
        "reader_utility": _signal(text, ("対応", "利用条件", "代替手段", "家計", "確認", "復旧"), 4.0),
        "source_quality": 9.0 if official else 7.0 if verified else 3.0,
        "novelty": float(item.get("novelty", 7.0)),
        "brand_fit": _signal(text, ("制度", "責任", "お金", "財源", "安全", "ガバナンス",
                                    "家計", "国益", "産業", "雇用", "再発防止"), 5.0),
    }
    if exclusion_reason(item, classification):
        scores["brand_fit"] = min(scores["brand_fit"], 3.0)
    weights = {
        key: _env_float(f"CURRENT_AFFAIRS_WEIGHT_{key.upper()}", value)
        for key, value in WEIGHTS.items()
    }
    category = classification["primary_category"].upper()
    adjustment = _env_float(f"CURRENT_AFFAIRS_CATEGORY_ADJUSTMENT_{category}", 0.0)
    total = sum(scores[key] * weights[key] for key in WEIGHTS) + adjustment
    scores["total_score"] = round(max(0, min(10, total)), 2)
    return {key: round(value, 2) for key, value in scores.items()}


def shelf_life(item: dict, classification: dict | None = None) -> dict:
    classification = classification or classify(item)
    text = _text(item)
    if classification["primary_category"] in {"major_incidents", "disaster_infrastructure"}:
        return {"type": "breaking_hours", "value": 12}
    if "決算" in text:
        return {"type": "short_term_days", "value": 5}
    if classification["primary_category"] == "technology_ai":
        return {"type": "short_term_days", "value": 14}
    if _matches(text, ("制度", "法律", "政策", "ガバナンス", "調査報告")):
        return {"type": "medium_term_weeks", "value": 8}
    return {"type": "evergreen_months", "value": 6}


def explanation_frame(item: dict, classification: dict, scores: dict) -> dict:
    title = str(item.get("title") or "")
    summary = str(item.get("summary") or "")
    primary = classification["primary_category"]
    affected = {
        "economy_business": ["家計", "従業員", "株主", "取引先"],
        "technology_ai": ["利用者", "労働者", "国内産業"],
        "cybersecurity": ["利用者", "顧客", "従業員", "取引先"],
        "society_living": ["制度利用者", "家計", "自治体"],
        "major_incidents": ["被害者・家族", "地域住民", "利用者"],
        "disaster_infrastructure": ["住民", "利用者", "事業者"],
    }.get(primary, ["国民", "納税者", "関係事業者"])
    return {
        "content_id": str(item.get("content_id") or item.get("id") or ""),
        "topic_key": str(item.get("topic_key") or ""),
        "primary_category": primary,
        "secondary_categories": classification["secondary_categories"],
        "main_event": title,
        "affected_groups": affected,
        "impact_scale": summary,
        "social_impact": f"社会影響スコア {scores['social_impact']}",
        "household_impact": f"家計影響スコア {scores['household_impact']}",
        "economic_impact": f"経済影響スコア {scores['economic_impact']}",
        "money_flow": "費用負担、公費、公的支援、利益の帰属を一次資料で確認する",
        "decision_makers": ["制度・組織上の意思決定者を確認"],
        "responsible_entities": ["実施主体", "監督主体", "経営・行政責任者"],
        "current_rules": ["根拠法令、規程、契約、公開基準を確認"],
        "control_mechanisms": ["承認、職務分掌、監査、監督、バックアップを確認"],
        "control_operated": "確認中",
        "individual_factors": [],
        "organizational_factors": ["組織設計と運用を個人要因から分離して検証"],
        "supervisory_factors": ["監督・監査が機能したかを検証"],
        "regulatory_factors": ["規制の有無と実効性を検証"],
        "prevention_measures": ["原因確定後に実行主体・期限・検証方法を伴う対策を確認"],
        "known_facts": [title, summary] if summary else [title],
        "unknowns": ["原因、責任、影響範囲は公式発表で更新する"],
        "reader_questions": ["誰に影響するか", "誰が負担するか", "誰が決めたか", "なぜ防げなかったか"],
        "content_angles": ["制度", "お金", "責任", "安全", "再発防止"],
        "short_video_angles": short_angles(primary),
        "longform_potential": 9 if len(classification["secondary_categories"]) >= 2 else 7,
        "article_potential": 9 if len(classification["secondary_categories"]) >= 2 else 7,
        "brand_fit_score": scores["brand_fit"],
    }


def short_angles(category: str) -> list[str]:
    return {
        "politics_policy": ["制度の仕組み", "財源", "誰が負担するか", "法案の変更点"],
        "economy_business": ["経営判断の背景", "家計・雇用への影響", "ガバナンス", "数字の比較"],
        "major_incidents": ["確認済み概要", "なぜ防げなかったか", "再発防止", "調査報告書"],
        "technology_ai": ["何が新しいか", "誰の仕事が変わるか", "規制と責任", "産業への影響"],
        "cybersecurity": ["何が起きたか", "利用者への影響", "認証・権限・バックアップ", "経営責任"],
        "society_living": ["制度変更", "家計への影響", "利用条件", "よくある誤解"],
        "disaster_infrastructure": ["安全情報", "復旧と代替手段", "維持管理", "再発防止"],
    }.get(category, ["制度", "責任", "安全", "今後の確認点"])


def extend_packet(item: dict) -> dict:
    classification = classify(item)
    scores = score_item(item, classification)
    frame = explanation_frame(item, classification, scores)
    life = shelf_life(item, classification)
    formats = ["x_text", "threads_post"]
    if scores["video_potential"] >= 6.5 or scores["brand_fit"] >= 6.5:
        formats.extend(["x_visual", "short_video"])
    if frame["article_potential"] >= 8:
        formats.extend(["x_thread", "longform_video", "x_article", "note"])
    return {
        **frame,
        "current_affairs_score": scores["total_score"],
        "social_impact_score": scores["social_impact"],
        "household_impact_score": scores["household_impact"],
        "economic_impact_score": scores["economic_impact"],
        "systemic_issue_score": scores["systemic_issue"],
        "accountability_score": scores["accountability_value"],
        "public_safety_score": scores["public_safety_value"],
        "shelf_life": life,
        "audience_segments": frame["affected_groups"],
        "recommended_platforms": ["x", "threads"],
        "recommended_formats": formats or ["skip"],
        "fact_status": "verified" if item.get("verified") else "requires_verification",
        "sns_demand_is_fact": False,
        "major_incident_route": classification["route"],
        "subcategories": classification["subcategories"],
    }


def _save(item: dict, packet: dict, scores: dict, path: Path | None = None) -> None:
    apply_migrations(path)
    now = _now().isoformat()
    content_id, topic_key = packet["content_id"], packet["topic_key"]
    with closing(connect(path)) as conn:
        secondaries = [""] + packet["secondary_categories"]
        for secondary in secondaries:
            conn.execute(
                """INSERT OR REPLACE INTO content_category_assignments
                   (content_id,topic_key,primary_category,secondary_category,
                    confidence,source,created_at) VALUES(?,?,?,?,?,?,?)""",
                (content_id, topic_key, packet["primary_category"], secondary,
                 classify(item)["confidence"], "local_rule_phase_a", now),
            )
        conn.execute(
            """INSERT OR REPLACE INTO content_category_scores
               (content_id,social_impact,household_impact,economic_impact,
                policy_relevance,systemic_issue,accountability_value,
                public_safety_value,video_potential,reader_utility,source_quality,
                novelty,brand_fit,total_score,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (content_id, *[scores[key] for key in (
                "social_impact", "household_impact", "economic_impact",
                "policy_relevance", "systemic_issue", "accountability_value",
                "public_safety_value", "video_potential", "reader_utility",
                "source_quality", "novelty", "brand_fit", "total_score")], now),
        )
        conn.commit()


def classify_items(
    items: Iterable[dict] | None = None, *, content_id: str | None = None,
    dry_run: bool = True, path: Path | None = None,
) -> dict:
    apply_migrations(path)
    source_items = list(items or DEFAULT_FIXTURES)
    if content_id:
        source_items = [row for row in source_items if row.get("content_id") == content_id]
    accepted, excluded = [], []
    minimum = _env_float("CONTENT_MIN_BRAND_FIT_SCORE", 6.5)
    for item in source_items:
        classification = classify(item)
        scores = score_item(item, classification)
        packet = extend_packet(item)
        reason = exclusion_reason(item, classification)
        if not reason and scores["brand_fit"] < minimum:
            reason = "brand_fit_below_threshold"
        if not item.get("verified") and not reason:
            reason = "requires_primary_source_verification"
        if reason:
            row = {**classification, "content_id": item["content_id"],
                   "topic_key": item["topic_key"], "reason": reason,
                   "brand_fit_score": scores["brand_fit"]}
            excluded.append(row)
            with closing(connect(path)) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO content_exclusions
                       (content_id,topic_key,reason,category_detected,
                        brand_fit_score,created_at) VALUES(?,?,?,?,?,?)""",
                    (item["content_id"], item["topic_key"], reason,
                     classification["primary_category"], scores["brand_fit"], _now().isoformat()),
                )
                conn.commit()
            continue
        _save(item, packet, scores, path)
        accepted.append({"classification": classification, "scores": scores, "packet": packet})
    return {
        "dry_run": dry_run, "classified": len(source_items), "accepted": accepted,
        "excluded": excluded, "external_writes": 0, "profiles_changed": 0,
        "production_limits_changed": 0,
    }


def mix_config() -> dict:
    defaults = category_config()["mix"]
    mix = {
        key: _env_float(f"CONTENT_MIX_{key.upper()}", value)
        for key, value in defaults.items()
    }
    return {
        "target_mix": mix,
        "politics_min_weekly_ratio": _env_float("CONTENT_MIX_POLITICS_MIN_WEEKLY_RATIO", .25),
        "max_single_category_ratio": _env_float("CONTENT_MIX_MAX_SINGLE_CATEGORY_RATIO", .45),
        "automatic_application": False,
        "quota": False,
    }


def select_balanced(candidates: list[dict], limit: int | None = None) -> list[dict]:
    """Return a Phase A candidate set with politics floor and category cap."""
    if not candidates:
        return []
    limit = limit or len(candidates)
    cfg = mix_config()
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in candidates:
        category = row.get("primary_category") or row.get("packet", {}).get("primary_category")
        groups[category].append(row)
    for rows in groups.values():
        rows.sort(key=lambda row: row.get("current_affairs_score",
                                          row.get("packet", {}).get("current_affairs_score", 0)),
                  reverse=True)
    selected = []
    politics_needed = min(len(groups["politics_policy"]),
                          int((limit * cfg["politics_min_weekly_ratio"]) + .999))
    selected.extend(groups["politics_policy"][:politics_needed])
    used = Counter({"politics_policy": politics_needed})
    max_single = max(1, int(limit * cfg["max_single_category_ratio"] + .999))
    remaining = sorted(
        (row for category, rows in groups.items() for row in rows
         if row not in selected),
        key=lambda row: row.get("current_affairs_score",
                                row.get("packet", {}).get("current_affairs_score", 0)),
        reverse=True,
    )
    for row in remaining:
        category = row.get("primary_category") or row.get("packet", {}).get("primary_category")
        if used[category] >= max_single:
            continue
        selected.append(row)
        used[category] += 1
        if len(selected) >= limit:
            break
    return selected


def category_candidates(
    category: str | None = None, *, dry_run: bool = True,
    path: Path | None = None,
) -> dict:
    result = classify_items(dry_run=dry_run, path=path)
    rows = [row["packet"] for row in result["accepted"]]
    if category:
        rows = [row for row in rows if row["primary_category"] == category]
    selected = select_balanced(rows, len(rows))
    return {
        "dry_run": dry_run, "category": category or "all",
        "candidates": selected, "counts": dict(Counter(r["primary_category"] for r in selected)),
        "politics_ratio": round(sum(r["primary_category"] == "politics_policy" for r in selected)
                                / max(1, len(selected)), 3),
        "external_posts": 0, "publish_authorized": False,
    }


def short_candidates(*, dry_run: bool = True, path: Path | None = None) -> dict:
    base = category_candidates(dry_run=dry_run, path=path)["candidates"]
    rows = []
    for packet in base:
        if "short_video" not in packet["recommended_formats"]:
            continue
        rows.append({
            "content_id": packet["content_id"], "topic_key": packet["topic_key"],
            "primary_category": packet["primary_category"],
            "angles": packet["short_video_angles"],
            "category_relative_evaluation": True,
            "major_incident_safety_route": packet["major_incident_route"],
            "brand_fit_score": packet["brand_fit_score"],
        })
    return {"dry_run": dry_run, "candidates": rows, "count": len(rows), "external_publishes": 0}


def article_candidates(*, dry_run: bool = True, path: Path | None = None) -> dict:
    base = category_candidates(dry_run=dry_run, path=path)["candidates"]
    rows = []
    for packet in base:
        compound = len(packet["secondary_categories"]) >= 1
        if packet["article_potential"] < 8 and not compound:
            continue
        rows.append({
            "content_id": packet["content_id"], "topic_key": packet["topic_key"],
            "primary_category": packet["primary_category"],
            "secondary_categories": packet["secondary_categories"],
            "compound_theme": compound,
            "recommended_formats": [
                fmt for fmt in packet["recommended_formats"]
                if fmt in {"longform_video", "x_article", "note", "x_thread"}
            ],
            "reasons": ["multiple_primary_sources_required", "timeline",
                        "system_or_structure", "prevention_and_outlook",
                        "cross_category" if compound else "short_text_insufficient"],
        })
    return {"dry_run": dry_run, "candidates": rows, "count": len(rows),
            "external_publishes": 0}


def exclusions(path: Path | None = None) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM content_exclusions ORDER BY id DESC LIMIT 100"
        ).fetchall()]
    return {"exclusions": rows, "count": len(rows)}


def source_health(path: Path | None = None) -> dict:
    apply_migrations(path)
    data = _load_json(SOURCE_FILE)
    return {
        **data["policy"], "sources": data["sources"],
        "enabled_count": sum(bool(row["enabled"]) for row in data["sources"]),
        "network_calls": 0,
    }


def _assignment_counts(path: Path | None = None) -> Counter:
    with closing(connect(path)) as conn:
        return Counter(dict(conn.execute(
            """SELECT primary_category,COUNT(DISTINCT content_id)
               FROM content_category_assignments GROUP BY primary_category"""
        ).fetchall()))


def performance(category: str | None = None, path: Path | None = None) -> dict:
    apply_migrations(path)
    counts = _assignment_counts(path)
    if category:
        counts = Counter({category: counts[category]})
    return {
        "by_category": {
            key: {"candidates": value, "published": 0, "reaction_rate": 0,
                  "follower_growth_association": 0,
                  "category_relative_scoring": True}
            for key, value in counts.items()
        },
        "external_metric_calls": 0,
    }


def _report(*, weekly: bool, dry_run: bool, path: Path | None = None) -> dict:
    apply_migrations(path)
    candidates = category_candidates(dry_run=dry_run, path=path)
    shorts = short_candidates(dry_run=dry_run, path=path)
    articles = article_candidates(dry_run=dry_run, path=path)
    exclusions_result = exclusions(path)
    counts = Counter(row["primary_category"] for row in candidates["candidates"])
    short_counts = Counter(row["primary_category"] for row in shorts["candidates"])
    article_counts = Counter(row["primary_category"] for row in articles["candidates"])
    total = sum(counts.values())
    category_rows = {}
    for category in category_config()["categories"]:
        if category == "governance_accountability":
            continue
        category_rows[category] = {
            "observed_topics": counts[category], "valid_topics": counts[category],
            "candidates": counts[category], "published": 0,
            "short_candidates": short_counts[category],
            "article_candidates": article_counts[category],
            "reaction_rate": 0, "follower_growth_association": 0,
        }
    report = {
        "period": "weekly" if weekly else "daily",
        "generated_at": _now().isoformat(), "dry_run": dry_run,
        "categories": category_rows,
        "politics_ratio": round(counts["politics_policy"] / max(1, total), 3),
        "politics_minimum": mix_config()["politics_min_weekly_ratio"],
        "brand_fit_rate": round(
            sum(row["brand_fit_score"] >= _env_float("CONTENT_MIN_BRAND_FIT_SCORE", 6.5)
                for row in candidates["candidates"]) / max(1, total), 3),
        "excluded_gossip": sum(row["reason"] == "entertainment_gossip"
                               for row in exclusions_result["exclusions"]),
        "compound_topics": sum(bool(row["compound_theme"]) for row in articles["candidates"]),
        "article_candidates": articles["count"], "longform_candidates": articles["count"],
        "automatic_mix_application": False, "external_posts": 0,
    }
    if weekly:
        report.update({
            "category_win_rates": {key: 0 for key in category_rows},
            "short_promotion_rates": {
                key: round(short_counts[key] / max(1, counts[key]), 3) for key in category_rows},
            "article_promotion_rates": {
                key: round(article_counts[key] / max(1, counts[key]), 3) for key in category_rows},
            "growth_contribution": {key: 0 for key in category_rows},
            "brand_drift_candidates": [
                row for row in exclusions_result["exclusions"]
                if row["reason"] == "brand_fit_below_threshold"],
            "recommended_mix": mix_config()["target_mix"],
            "mix_change_proposal_requires_human_approval": True,
        })
    _persist_report(report, weekly, path)
    return report


def _persist_report(report: dict, weekly: bool, path: Path | None) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = _now()
    key = (now.date() - timedelta(days=now.weekday())).isoformat() if weekly else now.date().isoformat()
    kind = "weekly" if weekly else "daily"
    (REPORT_DIR / f"{kind}-{key}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with closing(connect(path)) as conn:
        for category, stats in report["categories"].items():
            if weekly:
                conn.execute(
                    """INSERT OR REPLACE INTO content_category_weekly_stats
                       (week_start,category,published,performance_score,
                        short_promotion_rate,article_promotion_rate,
                        follower_growth_association,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (key, category, 0, 0,
                     report["short_promotion_rates"][category],
                     report["article_promotion_rates"][category], 0, now.isoformat()),
                )
            else:
                conn.execute(
                    """INSERT OR REPLACE INTO content_category_daily_stats
                       (date,category,observed_topics,valid_topics,candidates,published,
                        short_candidates,article_candidates,metrics_json,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (key, category, stats["observed_topics"], stats["valid_topics"],
                     stats["candidates"], 0, stats["short_candidates"],
                     stats["article_candidates"], _json(stats), now.isoformat()),
                )
        conn.commit()


def daily_report(*, dry_run: bool = True, path: Path | None = None) -> dict:
    return _report(weekly=False, dry_run=dry_run, path=path)


def weekly_report(*, dry_run: bool = True, path: Path | None = None) -> dict:
    return _report(weekly=True, dry_run=dry_run, path=path)


def status(path: Path | None = None) -> dict:
    apply_migrations(path)
    counts = _assignment_counts(path)
    return {
        "phase": "A",
        "enabled": _env_bool("CURRENT_AFFAIRS_EXPANSION_ENABLED", True),
        "bot_definition": category_config()["bot_definition"],
        "categories": dict(counts),
        "mix": mix_config(),
        "external_publishing_connected": False,
        "production_posting_limits_changed": False,
        "profile_changed": False,
        "env_modified": False,
    }


def full_cycle(*, dry_run: bool = True, path: Path | None = None) -> dict:
    classified = classify_items(dry_run=dry_run, path=path)
    candidates = category_candidates(dry_run=dry_run, path=path)
    shorts = short_candidates(dry_run=dry_run, path=path)
    articles = article_candidates(dry_run=dry_run, path=path)
    return {
        "information_acquisition": {
            "fixtures_or_existing_verified_data": True, "network_calls": 0,
            "sns_verified_facts_created": 0,
        },
        "classification": {
            "classified": classified["classified"], "accepted": len(classified["accepted"]),
            "excluded": len(classified["excluded"]),
        },
        "theme_evaluation": {"scored": len(classified["accepted"])},
        "content_packets": len(classified["accepted"]),
        "candidates": candidates,
        "shorts": shorts,
        "articles": articles,
        "daily_report": daily_report(dry_run=dry_run, path=path),
        "weekly_report": weekly_report(dry_run=dry_run, path=path),
        "source_health": source_health(path),
        "safety": {
            "external_posts": 0, "x_posts": 0, "threads_posts": 0,
            "video_publishes": 0, "note_publishes": 0, "profile_changes": 0,
            "windows_tasks_registered": 0, "windows_tasks_started": 0,
            "production_limits_changed": 0, "env_modified": False,
            "browser_automation": 0, "html_scraping": 0, "unofficial_api": 0,
        },
    }
