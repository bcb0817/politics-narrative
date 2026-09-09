"""Evidence-based public anger explainers and guarded production integration.

The engine translates verifiable burden, unfairness and accountability gaps
into candidate posts.  Phase A/B remain preview or shadow-only.  Phase C may
attach the concept to low-risk production candidates, while all publishing is
still performed by the existing X/Threads clients and their safety gates.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from metrics_db import connect, init_db


ROOT = Path(__file__).resolve().parent.parent
JST = ZoneInfo("Asia/Tokyo")
REPORT_DIR = ROOT / "reports" / "social_anger"

ANGER_AXES = (
    "unfair_burden",
    "accountability_gap",
    "explanation_failure",
    "double_standard",
    "privilege_protection",
    "tax_waste",
    "administrative_incompetence",
    "corporate_negligence",
    "governance_failure",
    "regulatory_failure",
    "victim_abandonment",
    "cost_shifting",
    "intergenerational_unfairness",
    "rule_evasion",
    "lack_of_transparency",
    "broken_promise",
    "avoidable_harm",
    "responsibility_without_authority",
    "authority_without_responsibility",
    "institutional_self_protection",
)

ANGER_TARGET_TYPES = (
    "policy",
    "decision",
    "organization",
    "management",
    "administrative_process",
    "corporate_process",
    "regulation",
    "budget_allocation",
    "public_statement",
    "failure_to_act",
    "individual_conduct",
    "unknown",
)

POST_TYPES = (
    "public_anger_explainer",
    "accountability_gap",
    "who_pays",
    "who_decided",
    "where_did_the_money_go",
    "avoidable_failure",
    "broken_system",
    "double_standard_check",
    "promise_vs_reality",
    "executive_responsibility",
    "administrative_responsibility",
    "victim_viewpoint",
    "worker_viewpoint",
    "household_impact",
    "anger_to_solution",
    "public_question",
)

POST_TYPE_DISTRIBUTION = {
    "confirmed_breaking": .15,
    "burden_impact": .20,
    "accountability_decision": .20,
    "system_structure": .15,
    "comparison_factcheck": .10,
    "counterargument": .05,
    "improvement": .10,
    "digest": .05,
}

CORE_ANGLES = (
    ("fact", "public_anger_explainer"),
    ("burden", "who_pays"),
    ("responsibility", "accountability_gap"),
    ("structure", "broken_system"),
    ("improvement", "anger_to_solution"),
)

PROTECTED_ATTRIBUTES = (
    "民族",
    "国籍",
    "人種",
    "性別",
    "宗教",
    "障害",
    "年齢",
)

SENSATIONAL_TERMS = (
    "国民がブチ切れ",
    "大炎上",
    "完全終了",
    "ヤバすぎる",
    "狂っている",
    "売国",
    "反日",
    "国賊",
    "絶対に許すな",
    "拡散希望",
    "みんな怒っている",
    "国民全員が怒っている",
    "○○が日本を壊した",
    "地獄",
    "黒幕",
)

EVIDENCE_REQUIRED_TERMS = (
    "血税",
    "利権",
    "中抜き",
    "天下り",
    "隠蔽",
    "癒着",
    "責任放棄",
    "ダブルスタンダード",
    "人災",
    "内部統制崩壊",
)

GENERALIZATION_PATTERNS = (
    r"国民全員",
    r"誰もが",
    r"国民は怒って",
    r"社会は怒って",
    r"怒りが広がって",
    r"みんな怒って",
)

VIOLENCE_OR_HARASSMENT = (
    "襲え",
    "殺せ",
    "死ね",
    "痛い目に",
    "住所を晒",
    "勤務先を晒",
    "追い込め",
)

PHASE_ORDER = {
    "phase_0_signal": 0,
    "phase_1_emergency": 1,
    "phase_2_confirmed_outline": 2,
    "phase_3_cause_investigation": 3,
    "phase_4_accountability": 4,
    "phase_5_reform": 5,
    "phase_6_long_term_review": 6,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS social_anger_assessments (
 id INTEGER PRIMARY KEY, content_id TEXT UNIQUE, topic_key TEXT, claim_key TEXT,
 anger_axis TEXT, anger_target_type TEXT, affected_group_json TEXT,
 decision_maker_json TEXT, beneficiary_json TEXT, cost_bearer_json TEXT,
 responsible_entity_json TEXT, accountability_status TEXT,
 public_explanation_status TEXT, public_harm_score REAL, unfairness_score REAL,
 burden_visibility_score REAL, accountability_gap_score REAL,
 explanation_failure_score REAL, avoidable_harm_score REAL,
 systemic_issue_score REAL, evidence_strength_score REAL,
 constructive_value_score REAL, anger_exploitation_risk REAL,
 mob_targeting_risk REAL, defamation_risk REAL,
 oversimplification_risk REAL, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS social_anger_candidates (
 id INTEGER PRIMARY KEY, content_id TEXT, platform TEXT, post_type TEXT,
 candidate_text TEXT, hook TEXT, unfairness_explanation TEXT,
 accountability_gap TEXT, public_question TEXT, proposed_improvement TEXT,
 effective_score REAL, status TEXT, decision_reason TEXT,
 metadata_json TEXT, created_at TEXT, updated_at TEXT,
 UNIQUE(content_id,platform,post_type));
CREATE TABLE IF NOT EXISTS social_anger_targets (
 id INTEGER PRIMARY KEY, topic_key TEXT, target_type TEXT, target_key TEXT,
 target_name TEXT, publication_count_24h INTEGER,
 publication_count_7d INTEGER, concentration_ratio REAL,
 last_published_at TEXT, created_at TEXT, updated_at TEXT,
 UNIQUE(topic_key,target_type,target_key));
CREATE TABLE IF NOT EXISTS anger_solution_links (
 id INTEGER PRIMARY KEY, topic_key TEXT, anger_content_id TEXT,
 solution_content_id TEXT, status TEXT, created_at TEXT, completed_at TEXT,
 UNIQUE(topic_key,anger_content_id,solution_content_id));
CREATE TABLE IF NOT EXISTS social_anger_weekly_reviews (
 id INTEGER PRIMARY KEY, week_start TEXT UNIQUE, metrics_json TEXT,
 recommendations_json TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_social_anger_candidates_status
 ON social_anger_candidates(status,platform,effective_score);
CREATE INDEX IF NOT EXISTS idx_social_anger_assessments_axis
 ON social_anger_assessments(anger_axis,evidence_strength_score);
CREATE INDEX IF NOT EXISTS idx_social_anger_targets_key
 ON social_anger_targets(target_key,last_published_at);
"""

FIXTURES = (
    {
        "content_id": "anger-tax",
        "topic_key": "social-insurance-burden",
        "title": "社会保険料の負担拡大案を政府が公表",
        "summary": "現役世代の保険料負担を増やす一方、総費用と終了条件は資料に明記されていない。",
        "source_name": "政府公式資料",
        "source_type": "official",
        "verified": True,
        "affected_group": ["現役世代", "保険料負担者"],
        "decision_maker": ["政府", "国会"],
        "beneficiary": ["制度利用者"],
        "cost_bearer": ["現役世代", "事業主"],
        "responsible_entity": ["所管省庁"],
    },
    {
        "content_id": "anger-admin",
        "topic_key": "administrative-explanation",
        "title": "行政が制度変更を公表",
        "summary": "対象者と申請期限を変更したが、影響人数と移行措置の説明が不足している。",
        "source_name": "行政公式発表",
        "source_type": "official",
        "verified": True,
        "affected_group": ["制度利用者"],
        "decision_maker": ["所管省庁"],
        "beneficiary": ["行政手続の実施主体"],
        "cost_bearer": ["制度利用者", "自治体窓口"],
        "responsible_entity": ["所管省庁"],
    },
    {
        "content_id": "anger-governance",
        "topic_key": "corporate-governance",
        "title": "企業が不正送金と内部統制不備を公表",
        "summary": "一人のアカウントで申請と承認が完結し、監査通知も機能していなかった。",
        "source_name": "企業適時開示",
        "source_type": "official",
        "verified": True,
        "affected_group": ["顧客", "従業員", "株主", "取引先"],
        "decision_maker": ["経営陣"],
        "beneficiary": [],
        "cost_bearer": ["会社", "株主", "取引先"],
        "responsible_entity": ["経営陣", "監査部門"],
    },
    {
        "content_id": "anger-victim",
        "topic_key": "victim-relief",
        "title": "事業者が被害者救済策を公表",
        "summary": "被害の受付は始まったが、補償対象と審査期限が示されていない。",
        "source_name": "事業者公式発表",
        "source_type": "official",
        "verified": True,
        "affected_group": ["被害者", "家族"],
        "decision_maker": ["事業者経営陣"],
        "beneficiary": [],
        "cost_bearer": ["被害者"],
        "responsible_entity": ["事業者"],
    },
    {
        "content_id": "anger-incident",
        "topic_key": "major-accident-report",
        "title": "重大事故の調査報告書が公表",
        "summary": "原因調査により、点検手順と監督体制の欠陥が確認された。再発防止策の実行期限は未定。",
        "source_name": "事故調査機関",
        "source_type": "official",
        "verified": True,
        "major_incident": True,
        "incident_phase": "phase_3_cause_investigation",
        "affected_group": ["被害者", "家族", "利用者"],
        "decision_maker": ["事業者経営陣"],
        "beneficiary": [],
        "cost_bearer": ["被害者", "利用者"],
        "responsible_entity": ["事業者", "監督部門"],
    },
    {
        "content_id": "anger-policy",
        "topic_key": "ordinary-policy-change",
        "title": "自治体が利用制度の変更を公表",
        "summary": "申請方法と対象者が変わる。費用負担と見直し時期は公式資料で確認できる。",
        "source_name": "自治体公式",
        "source_type": "official",
        "verified": True,
        "affected_group": ["制度利用者"],
        "decision_maker": ["自治体"],
        "beneficiary": ["制度利用者"],
        "cost_bearer": ["自治体", "納税者"],
        "responsible_entity": ["自治体"],
    },
    {
        "content_id": "anger-social-only",
        "topic_key": "unverified-flame",
        "title": "SNSで大炎上とされる未確認テーマ",
        "summary": "一次資料はなく、投稿数だけが話題になっている。",
        "source_name": "SNS",
        "source_type": "social",
        "verified": False,
    },
    {
        "content_id": "anger-attribute",
        "topic_key": "attribute-attack",
        "title": "特定の国籍を一括して責める投稿",
        "summary": "国籍への怒りを含むが、制度上の行為や確認可能な損害は示されていない。",
        "source_name": "SNS",
        "source_type": "social",
        "verified": False,
    },
)


def _now() -> datetime:
    return datetime.now(JST)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def apply_migrations(path: Path | None = None) -> bool:
    """Apply additive, idempotent tables without rewriting existing schema."""
    if not init_db(path):
        return False
    with closing(connect(path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()
    return True


def _text(item: dict) -> str:
    return " ".join(str(item.get(key) or "") for key in ("title", "summary", "body"))


def _contains(text: str, words: Iterable[str]) -> int:
    lowered = text.lower()
    return sum(1 for word in words if word.lower() in lowered)


def classify_anger_axis(item: dict) -> str:
    text = _text(item)
    rules = (
        ("victim_abandonment", ("被害者救済", "補償対象", "被害者", "救済不足")),
        ("accountability_gap", ("責任を取らない", "責任所在", "経営責任", "監督責任")),
        ("unfair_burden", ("負担", "保険料", "増税", "費用", "現役世代")),
        ("explanation_failure", (
            "説明不足", "説明が不足", "明記されていない", "示されていない", "未定"
        )),
        ("double_standard", ("二重基準", "基準の不一致", "同様の事案", "扱いが異なる")),
        ("governance_failure", ("内部統制", "監査", "職務分掌", "申請と承認")),
        ("broken_promise", ("公約", "約束", "計画と実績")),
        ("tax_waste", ("税金", "公費", "予算", "補助金", "使途")),
        ("administrative_incompetence", ("行政", "手続", "申請期限", "移行措置")),
        ("corporate_negligence", ("企業", "事業者", "経営陣")),
        ("avoidable_harm", ("防げた", "点検手順", "再発防止", "基本的統制")),
        ("lack_of_transparency", ("非公開", "透明性", "開示されていない")),
    )
    for axis, words in rules:
        if _contains(text, words):
            return axis
    return "unfair_burden" if item.get("cost_bearer") else "explanation_failure"


def classify_target_type(item: dict, axis: str | None = None) -> str:
    text = _text(item)
    if any(attribute in text for attribute in PROTECTED_ATTRIBUTES):
        return "unknown"
    rules = (
        ("budget_allocation", (
            "予算", "公費", "税金", "消費税", "減税", "税率",
            "補助金", "保険料",
        )),
        ("regulation", ("規制", "法令", "基準")),
        ("administrative_process", ("行政", "申請", "省庁", "自治体")),
        ("corporate_process", ("内部統制", "監査", "送金", "承認")),
        ("management", ("経営陣", "経営責任", "取締役")),
        ("public_statement", ("発言", "公約", "説明")),
        ("failure_to_act", ("放置", "対応しなかった", "未実施")),
        ("policy", ("政策", "制度", "法案", "社会保険")),
        ("decision", ("決定", "承認")),
        ("organization", ("政府", "企業", "事業者")),
    )
    for target_type, words in rules:
        if _contains(text, words):
            return target_type
    return "unknown"


def extract_roles(item: dict) -> dict:
    """Keep affected, deciding, benefiting, paying and responsible roles separate."""
    text = _text(item)
    affected = list(item.get("affected_group") or [])
    decision = list(item.get("decision_maker") or [])
    beneficiary = list(item.get("beneficiary") or [])
    cost = list(item.get("cost_bearer") or [])
    responsible = list(item.get("responsible_entity") or [])
    if not affected:
        if "保険料" in text or "増税" in text:
            affected = ["保険料負担者", "納税者"]
        elif "被害" in text or "事故" in text:
            affected = ["被害者", "利用者"]
        elif "企業" in text:
            affected = ["顧客", "従業員", "株主", "取引先"]
        else:
            affected = ["制度利用者"]
    if not decision:
        decision = ["経営陣"] if _contains(text, ("企業", "経営", "内部統制")) else ["行政・政策決定者"]
    if not cost:
        cost = ["納税者"] if _contains(text, ("税", "公費", "予算")) else affected[:1]
    if not responsible:
        responsible = ["経営陣・監督部門"] if _contains(text, ("企業", "経営", "内部統制")) else ["所管・監督主体"]
    return {
        "affected_group": affected,
        "decision_maker": decision,
        "beneficiary": beneficiary,
        "cost_bearer": cost,
        "responsible_entity": responsible,
    }


def _signal(text: str, words: Iterable[str], base: float = 4.0, step: float = 1.3) -> float:
    return round(min(10.0, base + step * _contains(text, words)), 2)


def score_assessment(item: dict, roles: dict | None = None) -> dict:
    roles = roles or extract_roles(item)
    text = _text(item)
    verified = bool(item.get("verified"))
    official = str(item.get("source_type") or "").lower() in {
        "official", "official_api", "official_feed", "government_official"
    }
    social_only = str(item.get("source_type") or "").lower() == "social"
    protected = any(attribute in text for attribute in PROTECTED_ATTRIBUTES)
    sensational = _contains(text, SENSATIONAL_TERMS)
    generalization = any(re.search(pattern, text) for pattern in GENERALIZATION_PATTERNS)
    unfairness = _signal(text, ("不公平", "非対称", "負担", "二重基準", "責任"))
    if roles["cost_bearer"] and roles["decision_maker"]:
        unfairness = min(10.0, unfairness + 1.5)
    accountability_gap = _signal(text, ("責任", "決定", "承認", "監督", "経営陣"))
    if roles["decision_maker"] and roles["responsible_entity"]:
        accountability_gap = min(10.0, accountability_gap + 1.5)
    public_harm = _signal(
        text, (
            "負担", "被害", "損害", "利用者", "家計", "現役世代",
            "患者", "消費税", "減税", "税率",
        )
    )
    public_harm = min(10.0, public_harm + min(2.5, len(roles["affected_group"]) * .65))
    constructive = _signal(
        text, ("見直し", "改善", "期限", "条件", "再発防止", "制度")
    )
    if verified and roles["responsible_entity"]:
        constructive = min(10.0, constructive + 2.5)
    scores = {
        "public_harm_score": public_harm,
        "unfairness_score": unfairness,
        "burden_visibility_score": min(10.0, 4.0 + len(roles["cost_bearer"]) * 1.5),
        "accountability_gap_score": accountability_gap,
        "explanation_failure_score": _signal(text, ("説明不足", "示されていない", "未定", "明記されていない")),
        "avoidable_harm_score": _signal(text, ("防げた", "内部統制", "監査", "点検", "再発防止")),
        "systemic_issue_score": _signal(text, ("制度", "構造", "内部統制", "手順", "監督体制")),
        "evidence_strength_score": 9.0 if verified and official else 8.0 if verified else 2.0,
        "constructive_value_score": constructive,
        "anger_exploitation_risk": min(10.0, (6.0 if social_only else 1.0) + sensational * 2.0),
        "mob_targeting_risk": min(10.0, (8.0 if protected else 1.0) + sensational * 1.5),
        "defamation_risk": 7.0 if not verified and _contains(text, ("犯人", "犯罪", "隠蔽", "黒幕")) else (4.0 if not verified else 1.0),
        "oversimplification_risk": min(
            10.0, 1.0 + (3.0 if social_only else 0.0) + (3.0 if generalization else 0.0)
        ),
    }
    return {key: round(value, 2) for key, value in scores.items()}


def anger_relevance_score(scores: dict, penalties: dict | None = None) -> float:
    value = (
        scores["public_harm_score"] * .15
        + scores["unfairness_score"] * .15
        + scores["accountability_gap_score"] * .15
        + scores["burden_visibility_score"] * .10
        + scores["explanation_failure_score"] * .10
        + scores["systemic_issue_score"] * .10
        + scores["evidence_strength_score"] * .15
        + scores["constructive_value_score"] * .10
    )
    penalties = penalties or {}
    risk_penalty = (
        scores["anger_exploitation_risk"]
        + scores["mob_targeting_risk"]
        + scores["defamation_risk"]
        + scores["oversimplification_risk"]
    ) * .08
    local_penalty = sum(float(penalties.get(key, 0)) for key in (
        "unsupported_generalization",
        "unsupported_intent_claim",
        "sensational_language",
        "repeated_targeting",
    )) * .25
    return round(max(0.0, min(10.0, value - risk_penalty - local_penalty)), 2)


def _thresholds() -> dict:
    return {
        "evidence_strength_score": _env_float("SOCIAL_ANGER_MIN_EVIDENCE_SCORE", 8.0),
        "public_harm_score": _env_float("SOCIAL_ANGER_MIN_PUBLIC_HARM_SCORE", 6.5),
        "constructive_value_score": _env_float("SOCIAL_ANGER_MIN_CONSTRUCTIVE_VALUE_SCORE", 6.5),
        "anger_exploitation_risk": _env_float("SOCIAL_ANGER_MAX_EXPLOITATION_RISK", 3.0),
        "mob_targeting_risk": _env_float("SOCIAL_ANGER_MAX_MOB_TARGETING_RISK", 2.0),
        "defamation_risk": _env_float("SOCIAL_ANGER_MAX_DEFAMATION_RISK", 2.0),
        "oversimplification_risk": _env_float("SOCIAL_ANGER_MAX_OVERSIMPLIFICATION_RISK", 3.0),
    }


def assessment_allows_candidate(assessment: dict) -> tuple[bool, list[str]]:
    scores = assessment["scores"]
    limits = _thresholds()
    reasons: list[str] = []
    for key in ("evidence_strength_score", "public_harm_score", "constructive_value_score"):
        if scores[key] < limits[key]:
            reasons.append(f"{key}_below_threshold")
    for key in (
        "anger_exploitation_risk",
        "mob_targeting_risk",
        "defamation_risk",
        "oversimplification_risk",
    ):
        if scores[key] > limits[key]:
            reasons.append(f"{key}_above_threshold")
    if _env_bool("SOCIAL_ANGER_REQUIRE_AFFECTED_GROUP", True) and not assessment["affected_group"]:
        reasons.append("affected_group_missing")
    if _env_bool("SOCIAL_ANGER_REQUIRE_RESPONSIBLE_ENTITY", True) and not assessment["responsible_entity"]:
        reasons.append("responsible_entity_missing")
    return not reasons, reasons


def assess(item: dict) -> dict:
    roles = extract_roles(item)
    axis = classify_anger_axis(item)
    target_type = classify_target_type(item, axis)
    scores = score_assessment(item, roles)
    assessment = {
        "content_id": str(item.get("content_id") or item.get("id") or ""),
        "topic_key": str(item.get("topic_key") or ""),
        "claim_key": f"{item.get('topic_key') or 'topic'}:{axis}",
        "anger_axis": axis,
        "anger_target_type": target_type,
        **roles,
        "accountability_status": "identified" if roles["responsible_entity"] else "unknown",
        "public_explanation_status": (
            "insufficient" if scores["explanation_failure_score"] >= 6.5 else "available"
        ),
        "scores": scores,
        "sns_demand_is_verified_fact": False,
        "fact_opinion_boundary": "事実は出典で確認し、不公平・責任の評価は評価として分離する。",
        "major_incident": bool(item.get("major_incident")),
        "incident_phase": item.get("incident_phase") or "",
        "source_name": item.get("source_name") or "",
        "source_type": item.get("source_type") or "",
        "verified_facts": [value for value in (item.get("title"), item.get("summary")) if value],
    }
    allowed, reasons = assessment_allows_candidate(assessment)
    assessment["eligible"] = allowed
    assessment["decision_reasons"] = reasons
    assessment["anger_relevance_score"] = anger_relevance_score(scores)
    return assessment


def _save_assessment(assessment: dict, path: Path | None = None) -> None:
    apply_migrations(path)
    now = _now().isoformat()
    scores = assessment["scores"]
    with closing(connect(path)) as conn:
        conn.execute(
            """INSERT INTO social_anger_assessments
               (content_id,topic_key,claim_key,anger_axis,anger_target_type,
                affected_group_json,decision_maker_json,beneficiary_json,
                cost_bearer_json,responsible_entity_json,accountability_status,
                public_explanation_status,public_harm_score,unfairness_score,
                burden_visibility_score,accountability_gap_score,
                explanation_failure_score,avoidable_harm_score,
                systemic_issue_score,evidence_strength_score,
                constructive_value_score,anger_exploitation_risk,
                mob_targeting_risk,defamation_risk,oversimplification_risk,
                created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(content_id) DO UPDATE SET
                anger_axis=excluded.anger_axis,
                anger_target_type=excluded.anger_target_type,
                affected_group_json=excluded.affected_group_json,
                decision_maker_json=excluded.decision_maker_json,
                beneficiary_json=excluded.beneficiary_json,
                cost_bearer_json=excluded.cost_bearer_json,
                responsible_entity_json=excluded.responsible_entity_json,
                public_harm_score=excluded.public_harm_score,
                unfairness_score=excluded.unfairness_score,
                burden_visibility_score=excluded.burden_visibility_score,
                accountability_gap_score=excluded.accountability_gap_score,
                explanation_failure_score=excluded.explanation_failure_score,
                avoidable_harm_score=excluded.avoidable_harm_score,
                systemic_issue_score=excluded.systemic_issue_score,
                evidence_strength_score=excluded.evidence_strength_score,
                constructive_value_score=excluded.constructive_value_score,
                anger_exploitation_risk=excluded.anger_exploitation_risk,
                mob_targeting_risk=excluded.mob_targeting_risk,
                defamation_risk=excluded.defamation_risk,
                oversimplification_risk=excluded.oversimplification_risk,
                updated_at=excluded.updated_at""",
            (
                assessment["content_id"], assessment["topic_key"], assessment["claim_key"],
                assessment["anger_axis"], assessment["anger_target_type"],
                _json(assessment["affected_group"]), _json(assessment["decision_maker"]),
                _json(assessment["beneficiary"]), _json(assessment["cost_bearer"]),
                _json(assessment["responsible_entity"]), assessment["accountability_status"],
                assessment["public_explanation_status"],
                *[scores[key] for key in (
                    "public_harm_score", "unfairness_score", "burden_visibility_score",
                    "accountability_gap_score", "explanation_failure_score",
                    "avoidable_harm_score", "systemic_issue_score",
                    "evidence_strength_score", "constructive_value_score",
                    "anger_exploitation_risk", "mob_targeting_risk",
                    "defamation_risk", "oversimplification_risk",
                )],
                now, now,
            ),
        )
        conn.commit()


def safety_violations(text: str, metadata: dict | None = None) -> list[str]:
    metadata = metadata or {}
    violations: list[str] = []
    if any(re.search(pattern, text) for pattern in GENERALIZATION_PATTERNS):
        violations.append("unsupported_generalization")
    if any(term in text for term in SENSATIONAL_TERMS):
        violations.append("sensational_language")
    if any(term in text for term in VIOLENCE_OR_HARASSMENT):
        violations.append("violence_or_harassment")
    if any(attribute in text and ("責め" in text or "排除" in text) for attribute in PROTECTED_ATTRIBUTES):
        violations.append("protected_attribute_targeting")
    if re.search(r"(逮捕|容疑).{0,12}(犯人|犯罪者)", text):
        violations.append("presumption_of_guilt")
    if re.search(r"(わざと|故意に|裏取引|悪意で|黒幕)", text):
        violations.append("unsupported_intent_claim")
    if re.search(r"(社員|職員|現場).{0,12}(すべての責任|だけが悪い)", text):
        violations.append("worker_blame_shifting")
    if re.search(r"(大臣|社長|知事|市長|議員|氏|さん).{0,16}(無能|クズ|人間失格)", text):
        violations.append("personal_attack")
    evidence = metadata.get("term_evidence") or {}
    for term in EVIDENCE_REQUIRED_TERMS:
        if term in text and not evidence.get(term):
            violations.append(f"unsupported_term:{term}")
    if metadata.get("requires_improvement", True) and not metadata.get("proposed_improvement"):
        violations.append("improvement_missing")
    if metadata.get("requires_responsible_entity", True) and not metadata.get("responsible_entity"):
        violations.append("criticism_target_ambiguous")
    if metadata.get("fact_opinion_boundary") in (None, ""):
        violations.append("fact_opinion_boundary_missing")
    return sorted(set(violations))


def _incident_allows(item: dict, angle: str) -> tuple[bool, str]:
    if not item.get("major_incident"):
        return True, ""
    phase = str(item.get("incident_phase") or "phase_0_signal")
    order = PHASE_ORDER.get(phase, 0)
    if order == 0:
        return False, "major_incident_phase_0_no_post"
    if order == 1:
        return False, "major_incident_phase_1_public_safety_only"
    if order == 2 and angle != "fact":
        return False, "major_incident_phase_2_facts_only"
    minimum = os.environ.get(
        "SOCIAL_ANGER_MAJOR_INCIDENT_MIN_PHASE", "phase_3_cause_investigation"
    )
    if angle != "fact" and order < PHASE_ORDER.get(minimum, 3):
        return False, "major_incident_phase_below_accountability_threshold"
    return True, ""


def _hook(item: dict, assessment: dict, angle: str) -> str:
    cost = "、".join(assessment["cost_bearer"]) or "影響を受ける人"
    decision = "、".join(assessment["decision_maker"]) or "意思決定側"
    responsible = "、".join(assessment["responsible_entity"]) or "監督主体"
    if angle == "fact":
        return f"確認できる事実はここまでです。{item.get('title', '')}"
    if angle == "burden":
        return f"負担するのは{cost}。決定したのは{decision}です。"
    if angle == "responsibility":
        return f"決める権限と説明する責任は、{responsible}にあります。"
    if angle == "structure":
        return "個人の失敗だけでなく、権限設計と監督の仕組みを確認すべきです。"
    return "怒りの原因を減らすには、責任主体・期限・検証方法が必要です。"


def _improvement(item: dict, assessment: dict) -> str:
    text = _text(item)
    if _contains(text, ("税", "保険料", "公費", "予算")):
        return "総費用、負担者別の金額、終了条件、見直し時期を一次資料で公開する。"
    if _contains(text, ("内部統制", "監査", "企業", "送金")):
        return "申請と承認の職務分掌、権限上限、異常検知、経営陣の実行期限を公開する。"
    if item.get("major_incident"):
        return "調査報告に基づき、再発防止策の実行主体・期限・第三者検証を明示する。"
    return "決定根拠、対象者、費用、実施主体、期限、見直し条件を公開する。"


def _question(assessment: dict) -> str:
    responsible = "、".join(assessment["responsible_entity"]) or "責任主体"
    return f"{responsible}は、改善策の実行期限と検証結果をいつ公開しますか。"


def _candidate_body(item: dict, assessment: dict, angle: str, platform: str) -> str:
    fact = str(item.get("summary") or item.get("title") or "").strip()
    affected = "、".join(assessment["affected_group"])
    decision = "、".join(assessment["decision_maker"])
    responsible = "、".join(assessment["responsible_entity"])
    cost = "、".join(assessment["cost_bearer"])
    improvement = _improvement(item, assessment)
    hook = _hook(item, assessment, angle)
    blocks = {
        "fact": [
            hook,
            f"確認済み：{fact}",
            f"影響を受けるのは{affected}です。",
            "評価は未確認情報と分け、公式資料の範囲で行います。",
        ],
        "burden": [
            hook,
            f"確認済み：{fact}",
            f"費用や不利益を負うのは{cost}です。",
            f"一方、決定・承認側は{decision}です。",
            f"改善には、{improvement}",
        ],
        "responsibility": [
            hook,
            f"確認済み：{fact}",
            f"決定・承認は{decision}、説明と是正の責任主体は{responsible}です。",
            "権限を持つ主体と責任を取る主体がずれていないか、事実で確認する必要があります。",
            f"改善には、{improvement}",
        ],
        "structure": [
            hook,
            f"確認済み：{fact}",
            f"影響は{affected}、負担は{cost}に生じます。",
            "問題は個人属性ではなく、制度・権限・承認・監督の設計です。",
            f"改善には、{improvement}",
        ],
        "improvement": [
            hook,
            f"確認済み：{fact}",
            f"責任主体は{responsible}です。",
            f"必要な改善：{improvement}",
            _question(assessment),
        ],
    }[angle]
    if platform == "threads":
        blocks.insert(
            2,
            f"背景として、負担者・決定者・実施者・監督者を分けて見る必要があります。",
        )
        if not item.get("major_incident") and angle != "improvement":
            blocks.append(_question(assessment))
    return "\n\n".join(blocks)


def build_candidate(
    item: dict,
    assessment: dict,
    angle: str,
    *,
    platform: str = "x",
    target_concentration: float = 0.0,
) -> dict:
    post_type = dict(CORE_ANGLES)[angle]
    text = _candidate_body(item, assessment, angle, platform)
    improvement = _improvement(item, assessment)
    metadata = {
        "verified_facts": assessment["verified_facts"],
        "affected_group": assessment["affected_group"],
        "decision_maker": assessment["decision_maker"],
        "beneficiary": assessment["beneficiary"],
        "cost_bearer": assessment["cost_bearer"],
        "responsible_entity": assessment["responsible_entity"],
        "anger_axis": assessment["anger_axis"],
        "anger_target_type": assessment["anger_target_type"],
        "fact_opinion_boundary": assessment["fact_opinion_boundary"],
        "proposed_improvement": improvement,
        "requires_improvement": angle != "fact",
        "requires_responsible_entity": angle != "fact",
        "term_evidence": item.get("term_evidence") or {},
    }
    violations = safety_violations(text, metadata)
    scores = assessment["scores"]
    penalties = {
        "unsupported_generalization": int("unsupported_generalization" in violations),
        "unsupported_intent_claim": int("unsupported_intent_claim" in violations),
        "sensational_language": int("sensational_language" in violations),
        "repeated_targeting": max(0.0, target_concentration - _env_float(
            "SOCIAL_ANGER_MAX_TARGET_CONCENTRATION_RATIO", .30
        )) * 10,
    }
    anger_score = anger_relevance_score(scores, penalties)
    conventional_value = (
        scores["evidence_strength_score"] * .4
        + scores["constructive_value_score"] * .35
        + scores["systemic_issue_score"] * .25
    )
    source_reliability_bonus = (
        .5 if scores["evidence_strength_score"] >= 9.0 else 0.0
    )
    effective = round(
        anger_score * .65 + conventional_value * .35
        + source_reliability_bonus,
        2,
    )
    incident_allowed, incident_reason = _incident_allows(item, angle)
    eligible, assessment_reasons = assessment_allows_candidate(assessment)
    status = "candidate"
    reasons = list(assessment_reasons)
    if not incident_allowed:
        reasons.append(incident_reason)
    if violations:
        reasons.extend(violations)
    if effective < 7.0:
        reasons.append("effective_score_below_7")
    if reasons or not eligible:
        status = "rejected"
    return {
        "post_text": text,
        "title": str(item.get("title") or ""),
        "hook": _hook(item, assessment, angle),
        "verified_facts": assessment["verified_facts"],
        "affected_group": assessment["affected_group"],
        "decision_maker": assessment["decision_maker"],
        "beneficiary": assessment["beneficiary"],
        "cost_bearer": assessment["cost_bearer"],
        "responsible_entity": assessment["responsible_entity"],
        "anger_axis": assessment["anger_axis"],
        "anger_target_type": assessment["anger_target_type"],
        "unfairness_explanation": (
            "負担を負う主体と、決定・説明する主体が一致しているかを確認する。"
        ),
        "accountability_gap": (
            f"決定者：{'、'.join(assessment['decision_maker'])}／"
            f"責任主体：{'、'.join(assessment['responsible_entity'])}"
        ),
        "public_question": _question(assessment),
        "proposed_improvement": improvement,
        "fact_opinion_boundary": assessment["fact_opinion_boundary"],
        **{key: scores[key] for key in (
            "evidence_strength_score", "public_harm_score", "unfairness_score",
            "accountability_gap_score", "constructive_value_score",
            "anger_exploitation_risk", "mob_targeting_risk",
            "defamation_risk", "oversimplification_risk",
        )},
        "ban_risk": max(
            scores["mob_targeting_risk"],
            scores["defamation_risk"],
            scores["anger_exploitation_risk"],
        ),
        "decision_reason": ",".join(sorted(set(reasons))) or "phase_a_candidate_only",
        "content_id": assessment["content_id"],
        "topic_key": assessment["topic_key"],
        "platform": platform,
        "angle": angle,
        "post_type": post_type,
        "effective_score": effective,
        "status": status,
        "safety_violations": violations,
        "target_concentration": round(target_concentration, 3),
        "production_publish_connected": False,
        "model_route": {
            "classification": "nano_or_local",
            "candidate_generation": "mini_or_local",
            "important_final_review": "luna_optional",
            "high_price_model_for_all_candidates": False,
        },
    }


def _deduplicate(candidates: list[dict]) -> list[dict]:
    output: list[dict] = []
    signatures: set[tuple[str, str]] = set()
    for row in candidates:
        normalized = re.sub(r"\s+", "", row["post_text"])
        signature = (row["platform"], normalized)
        if signature in signatures:
            continue
        signatures.add(signature)
        if any(
            row["platform"] == existing["platform"]
            and row["post_type"] != existing["post_type"]
            and normalized == re.sub(r"\s+", "", existing["post_text"])
            for existing in output
        ):
            continue
        output.append(row)
    return output


def _save_candidate(candidate: dict, path: Path | None = None) -> None:
    apply_migrations(path)
    now = _now().isoformat()
    metadata = {
        key: candidate[key]
        for key in (
            "verified_facts", "affected_group", "decision_maker", "beneficiary",
            "cost_bearer", "responsible_entity", "anger_axis",
            "anger_target_type", "fact_opinion_boundary", "safety_violations",
            "target_concentration", "model_route",
        )
    }
    with closing(connect(path)) as conn:
        conn.execute(
            """INSERT INTO social_anger_candidates
               (content_id,platform,post_type,candidate_text,hook,
                unfairness_explanation,accountability_gap,public_question,
                proposed_improvement,effective_score,status,decision_reason,
                metadata_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(content_id,platform,post_type) DO UPDATE SET
                candidate_text=excluded.candidate_text,hook=excluded.hook,
                effective_score=excluded.effective_score,status=excluded.status,
                decision_reason=excluded.decision_reason,
                metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
            (
                candidate["content_id"], candidate["platform"], candidate["post_type"],
                candidate["post_text"], candidate["hook"],
                candidate["unfairness_explanation"], candidate["accountability_gap"],
                candidate["public_question"], candidate["proposed_improvement"],
                candidate["effective_score"], candidate["status"],
                candidate["decision_reason"], _json(metadata), now, now,
            ),
        )
        conn.commit()


def _item_importance(item: dict, assessment: dict) -> bool:
    return bool(
        item.get("important")
        or item.get("major_incident")
        or assessment["anger_axis"] in {
            "accountability_gap", "governance_failure", "victim_abandonment",
            "tax_waste", "avoidable_harm",
        }
        or assessment["scores"]["public_harm_score"] >= 8
        or assessment["scores"]["systemic_issue_score"] >= 8
    )


def generate_candidates(
    item: dict,
    *,
    platform: str = "x",
    path: Path | None = None,
    persist: bool = True,
) -> list[dict]:
    assessment = assess(item)
    if persist:
        _save_assessment(assessment, path)
    important = _item_importance(item, assessment)
    limit = _env_int(
        "SOCIAL_ANGER_CANDIDATES_PER_IMPORTANT_TOPIC" if important
        else "SOCIAL_ANGER_CANDIDATES_PER_NORMAL_TOPIC",
        5 if important else 3,
    )
    angles = list(
        CORE_ANGLES if important
        else (CORE_ANGLES[0], CORE_ANGLES[1], CORE_ANGLES[4])
    )
    concentration = target_concentration(
        assessment["responsible_entity"][0] if assessment["responsible_entity"] else "",
        path=path,
    )
    rows = [
        build_candidate(
            item, assessment, angle, platform=platform,
            target_concentration=concentration,
        )
        for angle, _ in angles[:limit]
    ]
    rows = _deduplicate(rows)
    if persist:
        for row in rows:
            _save_candidate(row, path)
        _save_solution_link(assessment["topic_key"], rows, path)
        _extend_existing_packet(assessment, rows, path)
    return rows


def _save_solution_link(topic_key: str, candidates: list[dict], path: Path | None) -> None:
    anger = next((row for row in candidates if row["angle"] != "improvement"), None)
    solution = next((row for row in candidates if row["angle"] == "improvement"), None)
    if not anger:
        return
    now = _now().isoformat()
    solution_id = solution["content_id"] + ":solution" if solution else ""
    with closing(connect(path)) as conn:
        conn.execute(
            """INSERT INTO anger_solution_links
               (topic_key,anger_content_id,solution_content_id,status,created_at,completed_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(topic_key,anger_content_id,solution_content_id)
               DO UPDATE SET status=excluded.status,
                             completed_at=excluded.completed_at""",
            (
                topic_key, anger["content_id"], solution_id,
                "paired" if solution else "solution_missing", now,
                now if solution else None,
            ),
        )
        conn.commit()


def _extend_existing_packet(
    assessment: dict, candidates: list[dict], path: Path | None
) -> None:
    """Add Phase A fields only when a matching factory packet already exists."""
    try:
        with closing(connect(path)) as conn:
            row = conn.execute(
                "SELECT packet_json FROM content_packets WHERE content_id=?",
                (assessment["content_id"],),
            ).fetchone()
            if not row:
                return
            packet = _loads(row[0], {})
            by_angle = {candidate["angle"]: candidate for candidate in candidates}
            packet.update({
                "anger_post_candidate": by_angle.get("responsibility"),
                "solution_post_candidate": by_angle.get("improvement"),
                "factcheck_post_candidate": by_angle.get("fact"),
                "counterargument_post_candidate": {
                    "content_id": assessment["content_id"],
                    "topic_key": assessment["topic_key"],
                    "text": (
                        "反対論や制度目的を最も強い形で整理し、"
                        "確認済み事実と分けて比較する。"
                    ),
                    "status": "phase_a_outline",
                },
                "short_video_candidate": short_candidate(assessment, by_angle),
                "article_candidate": article_candidate(assessment, by_angle),
            })
            conn.execute(
                "UPDATE content_packets SET packet_json=?,updated_at=? WHERE content_id=?",
                (_json(packet), _now().isoformat(), assessment["content_id"]),
            )
            conn.commit()
    except Exception:
        return


def short_candidate(assessment: dict, by_angle: dict[str, dict] | None = None) -> dict:
    by_angle = by_angle or {}
    return {
        "content_id": assessment["content_id"],
        "topic_key": assessment["topic_key"],
        "enabled": _env_bool("SOCIAL_ANGER_SHORT_ENABLED", True),
        "outline": [
            f"0-3秒: 負担・責任・矛盾 — {', '.join(assessment['cost_bearer'])}",
            "3-15秒: 確認済みの出来事",
            "15-35秒: 誰が決め、誰が負担するか",
            "35-50秒: 制度・権限・監督の問題",
            "50-60秒: 改善策と長尺への導線",
        ],
        "forbidden_visuals": [
            "群衆", "炎や暴動", "実在人物を悪役化", "国民激怒", "黒幕",
        ],
        "source_candidate": (by_angle.get("structure") or {}).get("post_text", ""),
        "external_publish": False,
    }


def article_candidate(assessment: dict, by_angle: dict[str, dict] | None = None) -> dict:
    by_angle = by_angle or {}
    return {
        "content_id": assessment["content_id"],
        "topic_key": assessment["topic_key"],
        "enabled": _env_bool("SOCIAL_ANGER_ARTICLE_ENABLED", True),
        "note_enabled": _env_bool("SOCIAL_ANGER_NOTE_ENABLED", True),
        "outline": [
            "確認済み事実", "影響・負担", "意思決定と監督",
            "不公平・責任の不一致", "反対論・留保", "改善策",
        ],
        "source_candidate": (by_angle.get("fact") or {}).get("post_text", ""),
        "external_publish": False,
    }


def target_concentration(target_key: str, *, path: Path | None = None) -> float:
    if not target_key:
        return 0.0
    apply_migrations(path)
    since = (_now() - timedelta(days=7)).isoformat()
    with closing(connect(path)) as conn:
        total = conn.execute(
            """SELECT COALESCE(SUM(publication_count_7d),0)
               FROM social_anger_targets WHERE last_published_at>=?""",
            (since,),
        ).fetchone()[0]
        target = conn.execute(
            """SELECT COALESCE(SUM(publication_count_7d),0)
               FROM social_anger_targets
               WHERE target_key=? AND last_published_at>=?""",
            (target_key, since),
        ).fetchone()[0]
    return round(float(target or 0) / max(1, int(total or 0)), 3)


def record_target(
    topic_key: str,
    target_type: str,
    target_key: str,
    target_name: str,
    *,
    published_at: datetime | None = None,
    path: Path | None = None,
) -> None:
    apply_migrations(path)
    when = (published_at or _now()).isoformat()
    now = _now().isoformat()
    with closing(connect(path)) as conn:
        conn.execute(
            """INSERT INTO social_anger_targets
               (topic_key,target_type,target_key,target_name,
                publication_count_24h,publication_count_7d,
                concentration_ratio,last_published_at,created_at,updated_at)
               VALUES(?,?,?,?,1,1,0,?,?,?)
               ON CONFLICT(topic_key,target_type,target_key) DO UPDATE SET
                publication_count_24h=publication_count_24h+1,
                publication_count_7d=publication_count_7d+1,
                last_published_at=excluded.last_published_at,
                updated_at=excluded.updated_at""",
            (topic_key, target_type, target_key, target_name, when, now, now),
        )
        conn.commit()


def learning_metrics(events: Iterable[dict], links: Iterable[dict] = ()) -> dict:
    rows = list(events)
    valid_replies = sum(
        int(row.get("specific_question", 0)) + int(row.get("constructive_reply", 0))
        for row in rows
        if not row.get("hostile_or_abusive")
    )
    hostile = sum(int(bool(row.get("hostile_or_abusive"))) for row in rows)
    saves = sum(int(row.get("bookmarks", 0)) for row in rows)
    quotes = sum(int(row.get("quotes", 0)) for row in rows)
    linked = list(links)
    paired = sum(row.get("status") in {"paired", "completed"} for row in linked)
    targets = Counter(row.get("target_key") for row in rows if row.get("target_key"))
    total_targets = sum(targets.values())
    return {
        "anger_explanation_performance": 0,
        "accountability_post_performance": 0,
        "who_pays_post_performance": 0,
        "solution_post_performance": 0,
        "factcheck_post_performance": 0,
        "constructive_reply_rate": round(valid_replies / max(1, len(rows)), 3),
        "specific_question_rate": round(
            sum(bool(row.get("specific_question")) for row in rows) / max(1, len(rows)), 3
        ),
        "anger_to_solution_completion_rate": round(paired / max(1, len(linked)), 3),
        "target_concentration": round(max(targets.values(), default=0) / max(1, total_targets), 3),
        "negative_sentiment_dependency": round(hostile / max(1, len(rows)), 3),
        "follower_growth_association": 0,
        "short_promotion_rate": 0,
        "quality_signals": {"bookmarks": saves, "quotes": quotes, "constructive_replies": valid_replies},
        "hostile_replies_counted_as_success": False,
        "automatic_intensity_increase": False,
    }


def _news_items(path: Path | None = None, content_id: str | None = None) -> list[dict]:
    fixtures = [dict(row) for row in FIXTURES]
    if content_id:
        matched = [row for row in fixtures if row["content_id"] == content_id]
        if matched:
            return matched
    try:
        with closing(connect(path)) as conn:
            query = (
                """SELECT id,source_type,source_name,source_url,title,summary,
                          topic_key,genre,verified,metadata_json
                   FROM news_candidates WHERE verified=1"""
            )
            params: tuple[Any, ...] = ()
            if content_id:
                query += " AND CAST(id AS TEXT)=?"
                params = (content_id,)
            query += " ORDER BY id DESC LIMIT 15"
            rows = [dict(row) for row in conn.execute(query, params).fetchall()]
        for row in rows:
            metadata = _loads(row.pop("metadata_json", None), {})
            row["content_id"] = str(row.pop("id"))
            row.update({
                key: metadata[key] for key in (
                    "affected_group", "decision_maker", "beneficiary",
                    "cost_bearer", "responsible_entity", "major_incident",
                    "incident_phase", "term_evidence",
                ) if key in metadata
            })
        if rows:
            return rows
    except Exception:
        pass
    return [row for row in fixtures if not content_id or row["content_id"] == content_id]


def assess_items(
    items: Iterable[dict] | None = None,
    *,
    content_id: str | None = None,
    dry_run: bool = True,
    path: Path | None = None,
) -> dict:
    source = list(items) if items is not None else _news_items(path, content_id)
    if content_id:
        source = [row for row in source if str(row.get("content_id")) == str(content_id)]
    assessments = []
    for item in source:
        row = assess(item)
        _save_assessment(row, path)
        assessments.append(row)
    return {
        "phase": "A", "dry_run": dry_run, "assessments": assessments,
        "assessed": len(assessments), "eligible": sum(row["eligible"] for row in assessments),
        "external_posts": 0,
    }


def candidate_cycle(
    items: Iterable[dict] | None = None,
    *,
    content_id: str | None = None,
    dry_run: bool = True,
    path: Path | None = None,
) -> dict:
    source = list(items) if items is not None else _news_items(path, content_id)
    if content_id:
        source = [row for row in source if str(row.get("content_id")) == str(content_id)]
    candidates: list[dict] = []
    for item in source:
        candidates.extend(generate_candidates(item, platform="x", path=path))
        candidates.extend(generate_candidates(item, platform="threads", path=path))
    return {
        "phase": "A", "dry_run": dry_run, "candidates": candidates,
        "candidate_count": len(candidates),
        "accepted_count": sum(row["status"] == "candidate" for row in candidates),
        "rejected_count": sum(row["status"] == "rejected" for row in candidates),
        "external_posts": 0, "publish_authorized": False,
    }


def targets_report(path: Path | None = None) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM social_anger_targets ORDER BY publication_count_7d DESC"
        ).fetchall()]
    maximum = _env_float("SOCIAL_ANGER_MAX_TARGET_CONCENTRATION_RATIO", .30)
    return {
        "targets": rows, "maximum_ratio": maximum,
        "over_concentrated": [row for row in rows if row["concentration_ratio"] > maximum],
    }


def risk_report(path: Path | None = None) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        assessments = [dict(row) for row in conn.execute(
            """SELECT content_id,topic_key,anger_axis,evidence_strength_score,
                      anger_exploitation_risk,mob_targeting_risk,
                      defamation_risk,oversimplification_risk
               FROM social_anger_assessments ORDER BY updated_at DESC"""
        ).fetchall()]
        rejected = [dict(row) for row in conn.execute(
            """SELECT content_id,platform,post_type,decision_reason
               FROM social_anger_candidates WHERE status='rejected'
               ORDER BY updated_at DESC"""
        ).fetchall()]
    return {
        "assessments": assessments, "rejected_candidates": rejected,
        "thresholds": _thresholds(), "external_posts": 0,
    }


def solution_gaps(path: Path | None = None) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        rows = [dict(row) for row in conn.execute(
            """SELECT * FROM anger_solution_links
               WHERE status NOT IN ('paired','completed') ORDER BY created_at DESC"""
        ).fetchall()]
        total = conn.execute("SELECT COUNT(*) FROM anger_solution_links").fetchone()[0]
        completed = conn.execute(
            "SELECT COUNT(*) FROM anger_solution_links WHERE status IN ('paired','completed')"
        ).fetchone()[0]
    return {
        "gaps": rows,
        "anger_to_solution_completion_rate": round(completed / max(1, total), 3),
        "external_posts": 0,
    }


def short_candidates(path: Path | None = None) -> dict:
    assessments = assess_items(path=path)["assessments"]
    rows = [short_candidate(row) for row in assessments if row["eligible"]]
    return {"candidates": rows, "count": len(rows), "external_publishes": 0}


def article_candidates(path: Path | None = None) -> dict:
    assessments = assess_items(path=path)["assessments"]
    rows = [article_candidate(row) for row in assessments if row["eligible"]]
    return {
        "article_candidates": rows,
        "note_candidates": [row for row in rows if row["note_enabled"]],
        "count": len(rows),
        "external_publishes": 0,
    }


def _report(weekly: bool, *, dry_run: bool = True, path: Path | None = None) -> dict:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        assessments = [dict(row) for row in conn.execute(
            "SELECT * FROM social_anger_assessments ORDER BY updated_at DESC"
        ).fetchall()]
        candidates = [dict(row) for row in conn.execute(
            "SELECT * FROM social_anger_candidates ORDER BY updated_at DESC"
        ).fetchall()]
        links = [dict(row) for row in conn.execute(
            "SELECT * FROM anger_solution_links"
        ).fetchall()]
    axes = Counter(row["anger_axis"] for row in assessments)
    types = Counter(row["post_type"] for row in candidates if row["status"] == "candidate")
    target_rows = targets_report(path)
    metrics = learning_metrics([], links)
    report = {
        "period": "weekly" if weekly else "daily",
        "generated_at": _now().isoformat(),
        "dry_run": dry_run,
        "anger_axes": dict(axes),
        "candidate_types": dict(types),
        "rejected": sum(row["status"] == "rejected" for row in candidates),
        "solution_gaps": solution_gaps(path),
        "target_concentration": target_rows,
        "learning_metrics": metrics,
        "review_questions": [
            "どの不公平構造が反応を得たか",
            "どの負担者視点が理解されたか",
            "どの責任主体への具体的質問が多かったか",
            "感情語なしでも伸びた投稿は何か",
            "煽情表現に依存した候補は何か",
            "批判だけで改善につながらなかったテーマは何か",
            "同じ対象への批判集中がないか",
            "反対意見を不当に単純化していないか",
            "事実と評価が混同されていないか",
            "Short・記事化すべき説明不足は何か",
        ],
        "recommendations": [
            "提案のみ保存し、プロンプトや批判強度を自動変更しない",
            "保存・引用・具体的質問・改善策への継続反応を重視する",
            "敵対的な返信や罵倒を成功指標に含めない",
        ],
        "automatic_prompt_change": False,
        "automatic_intensity_change": False,
        "external_posts": 0,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = _now()
    key = (
        (now.date() - timedelta(days=now.weekday())).isoformat()
        if weekly else now.date().isoformat()
    )
    (REPORT_DIR / f"{report['period']}-{key}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if weekly:
        with closing(connect(path)) as conn:
            conn.execute(
                """INSERT INTO social_anger_weekly_reviews
                   (week_start,metrics_json,recommendations_json,created_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(week_start) DO UPDATE SET
                    metrics_json=excluded.metrics_json,
                    recommendations_json=excluded.recommendations_json,
                    created_at=excluded.created_at""",
                (key, _json(metrics), _json(report["recommendations"]), now.isoformat()),
            )
            conn.commit()
    return report


def daily_report(*, dry_run: bool = True, path: Path | None = None) -> dict:
    return _report(False, dry_run=dry_run, path=path)


def weekly_report(*, dry_run: bool = True, path: Path | None = None) -> dict:
    return _report(True, dry_run=dry_run, path=path)


def production_settings() -> dict:
    """Return the guarded rollout settings used by X and Threads."""
    phase = os.environ.get("SOCIAL_ANGER_PRODUCTION_PHASE", "B").strip().upper()
    if phase not in {"A", "B", "C", "D"}:
        phase = "B"
    allowed = {
        value.strip() for value in os.environ.get(
            "SOCIAL_ANGER_ALLOWED_PRODUCTION_TARGETS",
            "policy,decision,organization,management,administrative_process,"
            "corporate_process,regulation,budget_allocation,public_statement,"
            "failure_to_act",
        ).split(",") if value.strip()
    }
    return {
        "enabled": _env_bool("SOCIAL_ANGER_CONCEPT_ENABLED", True),
        "production_enabled": _env_bool(
            "SOCIAL_ANGER_PRODUCTION_ENABLED", False),
        "phase": phase,
        "shadow_compare_enabled": _env_bool(
            "SOCIAL_ANGER_SHADOW_COMPARE_ENABLED", True),
        "allowed_targets": allowed,
        "minimum_effective_score": _env_float(
            "SOCIAL_ANGER_MIN_PRODUCTION_EFFECTIVE_SCORE", 7.0),
    }


def production_prompt_context(
    item: dict, *, path: Path | None = None, persist: bool = True
) -> dict:
    """Build fact-grounded context without authorizing a publication."""
    assessment = assess(item)
    if persist:
        _save_assessment(assessment, path)
    improvement = _improvement(item, assessment)
    return {
        "assessment": assessment,
        "prompt_context": {
            "affected_group": assessment["affected_group"],
            "decision_maker": assessment["decision_maker"],
            "cost_bearer": assessment["cost_bearer"],
            "responsible_entity": assessment["responsible_entity"],
            "anger_axis": assessment["anger_axis"],
            "anger_target_type": assessment["anger_target_type"],
            "fact_opinion_boundary": assessment["fact_opinion_boundary"],
            "proposed_improvement": improvement,
            "public_question": _question(assessment),
        },
        "settings": production_settings(),
    }


def evaluate_production_candidate(
    item: dict,
    text: str,
    *,
    platform: str,
    path: Path | None = None,
    persist: bool = True,
) -> dict:
    """Evaluate whether a candidate may use the production concept.

    A candidate that is not eligible for Phase C remains available to the
    legacy neutral pipeline.  Severe safety violations are always exposed to
    the caller so the existing publication gate can reject them.
    """
    context = production_prompt_context(item, path=path, persist=persist)
    assessment = context["assessment"]
    cfg = context["settings"]
    metadata = {
        **context["prompt_context"],
        "term_evidence": item.get("term_evidence") or {},
        "requires_improvement": True,
        "requires_responsible_entity": True,
    }
    violations = safety_violations(text, metadata)
    allowed_incident, incident_reason = _incident_allows(item, "responsibility")
    if not allowed_incident:
        violations.append(incident_reason)

    phase_allows = cfg["phase"] in {"C", "D"}
    target_allows = assessment["anger_target_type"] in cfg["allowed_targets"]
    if assessment["anger_target_type"] == "individual_conduct":
        target_allows = cfg["phase"] == "D"
    connected = bool(
        cfg["enabled"]
        and cfg["production_enabled"]
        and phase_allows
        and target_allows
        and assessment["eligible"]
        and allowed_incident
        and not violations
        and assessment["anger_relevance_score"]
        >= cfg["minimum_effective_score"]
    )
    reasons = list(assessment["decision_reasons"])
    if not cfg["production_enabled"]:
        reasons.append("production_disabled")
    elif not phase_allows:
        reasons.append("phase_shadow_only")
    if not target_allows:
        reasons.append("target_not_in_guarded_rollout")
    reasons.extend(violations)
    return {
        "production_publish_connected": connected,
        "phase": cfg["phase"],
        "platform": platform,
        "assessment": assessment,
        "prompt_context": context["prompt_context"],
        "safety_violations": sorted(set(violations)),
        "decision_reasons": sorted(set(reasons)),
        "effective_score": assessment["anger_relevance_score"],
    }


def status(path: Path | None = None) -> dict:
    apply_migrations(path)
    names = (
        "social_anger_assessments",
        "social_anger_candidates",
        "social_anger_targets",
        "anger_solution_links",
        "social_anger_weekly_reviews",
    )
    with closing(connect(path)) as conn:
        counts = {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in names
        }
    production = production_settings()
    return {
        "phase": production["phase"],
        "enabled": _env_bool("SOCIAL_ANGER_CONCEPT_ENABLED", True),
        "account_definition": (
            "政治・行政・企業・重大事件・社会問題を、誰が決め、誰が利益を得て、"
            "誰が負担し、誰が責任を取るのかという視点で整理し、生活者の正当な"
            "怒りを事実と制度の言葉に翻訳する時事解説AI。"
        ),
        "counts": counts,
        "anger_axes": list(ANGER_AXES),
        "post_types": list(POST_TYPES),
        "target_post_type_distribution": POST_TYPE_DISTRIBUTION,
        "minimum_constructive_share": .25,
        "profile_changed": False,
        "production_prompt_changed": production["phase"] in {"B", "C", "D"},
        "production_posting_limits_changed": False,
        "production_publish_connected": bool(
            production["production_enabled"]
            and production["phase"] in {"C", "D"}
        ),
        "automatic_profile_update": False,
        "automatic_intensity_increase": False,
        "external_posts": 0,
    }


def full_cycle(*, dry_run: bool = True, path: Path | None = None) -> dict:
    assessments = assess_items(dry_run=dry_run, path=path)
    candidates = candidate_cycle(dry_run=dry_run, path=path)
    shorts = short_candidates(path)
    articles = article_candidates(path)
    return {
        "phase": "A",
        "dry_run": dry_run,
        "input": {
            "existing_verified_news_or_fixtures": True,
            "network_calls": 0,
            "sns_demand_used_as_fact": False,
        },
        "assessments": {
            "count": assessments["assessed"],
            "eligible": assessments["eligible"],
        },
        "candidates": candidates,
        "shorts": shorts,
        "articles_and_note": articles,
        "risk_report": risk_report(path),
        "solution_gaps": solution_gaps(path),
        "daily_report": daily_report(dry_run=dry_run, path=path),
        "weekly_report": weekly_report(dry_run=dry_run, path=path),
        "safety": {
            "external_posts": 0,
            "x_posts": 0,
            "threads_posts": 0,
            "video_publishes": 0,
            "note_publishes": 0,
            "profile_changes": 0,
            "production_prompt_changes": 0,
            "production_posting_limit_changes": 0,
            "windows_tasks_registered": 0,
            "windows_tasks_started": 0,
        },
    }
