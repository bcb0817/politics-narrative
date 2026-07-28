"""Deterministic publishing policy for the politics narrative bot.

This module is deliberately independent from OpenAI and X clients so that all
rate, cooldown, taxonomy, and review calculations can be unit-tested offline.
"""

from __future__ import annotations

import re
import hashlib
import os
import unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")
ROOT_DIR = Path(__file__).resolve().parent.parent

POST_TYPES = (
    "breaking_news",
    "issue_diagram",
    "strong_opinion",
    "comparison_factcheck",
    "steelman_counterargument",
    "evergreen_explainer",
    "morning_evening_digest",
)
POST_TYPE_DAILY_LIMITS = {
    "breaking_news": 2,
    "issue_diagram": 4, "strong_opinion": 4, "comparison_factcheck": 3,
    "steelman_counterargument": 1, "evergreen_explainer": 1,
    "morning_evening_digest": 2,
}
POST_STYLE_TARGETS = {
    "breaking_news": 0.25, "issue_diagram": 0.20, "strong_opinion": 0.20,
    "comparison_factcheck": 0.15, "steelman_counterargument": 0.10,
    "morning_evening_digest": 0.10,
}
HOOK_TYPES = (
    "fact_reversal",
    "issue_redefinition",
    "number",
    "contrast",
    "question",
    "conclusion_first",
)
CRITIQUE_AXES = (
    "fiscal_discipline",
    "small_government",
    "rule_of_law",
    "due_process",
    "national_security",
    "energy_security",
    "domestic_industry",
    "family_policy",
    "intergenerational_fairness",
    "administrative_transparency",
    "regulatory_cost",
    "local_autonomy",
)

SIGNIFICANT_UPDATE_TERMS = (
    "成立", "可決", "否決", "採決", "辞任", "逮捕", "起訴", "開戦", "停戦",
    "撤回", "政策撤回", "外交合意", "公式発表", "公式数値", "重要判決", "判決",
    "大規模災害", "被害拡大", "死者", "避難指示", "施行",
)

BREAKING_NEWS_TERMS = (
    "法案成立", "成立", "重要採決", "採決", "可決", "否決", "辞任", "逮捕",
    "重要判決", "開戦", "停戦", "大規模災害", "外交合意", "政策撤回",
)

_TOPIC_PATTERNS = (
    (re.compile(r"再審|証拠開示|刑事訴訟"), "再審制度改正"),
    (re.compile(r"消費税.*(?:減税|廃止)|(?:減税|廃止).*消費税"), "消費税減税"),
    (re.compile(r"日米.*関税|関税.*日米"), "日米関税交渉"),
    (re.compile(r"ホルムズ.*(?:封鎖|海峡)|(?:封鎖|海峡).*ホルムズ"), "ホルムズ海峡封鎖"),
    (re.compile(r"原発|原子力発電"), "原子力政策"),
    (re.compile(r"社会保険料"), "社会保険料"),
    (re.compile(r"少子化|出生率|出生数"), "少子化政策"),
    (re.compile(r"防衛費|防衛予算"), "防衛予算"),
    (re.compile(r"入管|技能実習|外国人労働|移民"), "入管・外国人労働制度"),
)

_GENERIC_TITLE_WORDS = {
    "速報", "独自", "解説", "詳報", "ニュース", "政府", "国会", "日本", "きょう",
    "今日", "明らか", "方針", "検討", "発表", "めぐり", "について", "見通し",
}


def parse_jst(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value or "").strip())
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


def successful_posts_today(history: list[dict], now_jst: datetime) -> list[dict]:
    today = now_jst.astimezone(JST).date()
    out = []
    for row in history:
        dt = parse_jst(row.get("posted_at_jst") or row.get("posted_at"))
        if dt and dt.date() == today and row.get("tweet_id"):
            out.append(row)
    return out


def stagnation_fallback_active(
    history: list[dict], now_jst: datetime, fallback_hours: float
) -> bool:
    """Enable score relaxation after a long gap since the last successful post."""
    successful_times = []
    for row in history:
        if not row.get("tweet_id"):
            continue
        dt = parse_jst(row.get("posted_at_jst") or row.get("posted_at"))
        if dt and dt <= now_jst:
            successful_times.append(dt)
    if not successful_times:
        return False
    elapsed = now_jst - max(successful_times)
    return elapsed >= timedelta(hours=max(0.0, fallback_hours))


def pre_generation_skip_reason(
    history: list[dict], now_jst: datetime, max_daily_posts: int, min_interval_minutes: int
) -> str | None:
    today_posts = successful_posts_today(history, now_jst)
    if len(today_posts) >= max(0, max_daily_posts):
        return "daily_post_limit"
    times = [parse_jst(row.get("posted_at_jst") or row.get("posted_at")) for row in history]
    times = [dt for dt in times if dt and dt <= now_jst]
    if times and now_jst - max(times) < timedelta(minutes=max(0, min_interval_minutes)):
        return "minimum_post_interval"
    return None


def normalize_topic_key(title: str, keywords: list[str] | None = None) -> str:
    text = unicodedata.normalize("NFKC", title or "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[【】\[\]（）()「」『』〈〉《》!?！？…・:：|｜]", " ", text)
    text = re.sub(r"\b(?:20\d{2}|\d{1,2})[年/月日時分]\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for pattern, key in _TOPIC_PATTERNS:
        if pattern.search(text):
            return key
    tokens = list(keywords or [])
    tokens.extend(re.findall(r"[一-龥ぁ-んァ-ヶーA-Za-z0-9]{2,20}", text))
    cleaned = []
    for token in tokens:
        token = token.strip()
        if not token or token in _GENERIC_TITLE_WORDS or token.isdigit() or token in cleaned:
            continue
        cleaned.append(token)
    return "・".join(cleaned[:3]) or text[:40] or "未分類"


def is_significant_update(new_title: str, previous_title: str = "") -> bool:
    if any(term in (new_title or "") and term not in (previous_title or "") for term in SIGNIFICANT_UPDATE_TERMS):
        return True
    new_numbers = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", new_title or "")]
    old_numbers = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", previous_title or "")]
    if new_numbers and old_numbers:
        for new, old in zip(new_numbers, old_numbers):
            if old and abs(new - old) / abs(old) >= 0.20:
                return True
    return False


def topic_cooldown_skip_reason(
    topic_key: str,
    news_title: str,
    recent_topics: list[dict],
    now_jst: datetime,
    cooldown_hours: float,
) -> str | None:
    for row in reversed(recent_topics):
        old_key = str(row.get("topic_key") or "")
        similarity = SequenceMatcher(None, topic_key, old_key).ratio() if old_key else 0.0
        if old_key != topic_key and similarity < 0.82:
            continue
        posted_at = parse_jst(row.get("last_posted_at"))
        if not posted_at or now_jst - posted_at >= timedelta(hours=max(0.0, cooldown_hours)):
            continue
        if is_significant_update(news_title, str(row.get("news_title") or "")):
            return None
        return "topic_cooldown"
    return None


def classify_post_type(news: dict, now_jst: datetime) -> str:
    text = f"{news.get('title', '')} {news.get('summary', '')}"
    if now_jst.hour in (5, 6, 17, 18) and news.get("digest_items"):
        return "morning_evening_digest"
    if news.get("is_evergreen"):
        return "evergreen_explainer"
    # 「速報」「緊急」という見出しだけでは追加2件の重大速報枠を使わない。
    official_number_change = bool(news.get("is_major_update")) and bool(re.search(r"\d", text)) and any(
        marker in text for marker in ("公式", "政府発表", "省発表", "統計")
    )
    if any(term in text for term in BREAKING_NEWS_TERMS) or official_number_change:
        return "breaking_news"
    if any(term in text for term in ("比較", "改正前", "改正後", "与党案", "野党案", "一次資料")):
        return "comparison_factcheck"
    if any(term in text for term in ("制度", "仕組み", "法案", "予算", "再審", "税制")):
        return "issue_diagram"
    return "strong_opinion"


def choose_post_style(news: dict, history: list[dict], now_jst: datetime) -> tuple[str, bool]:
    """Apply soft ratio balancing and deterministic 80/20 exploration."""
    base = classify_post_type(news, now_jst)
    if base in {"breaking_news", "morning_evening_digest", "evergreen_explainer"}:
        return base, False
    recent = [row.get("post_type") or row.get("type") for row in history if row.get("post_type") or row.get("type")]
    last24 = []
    for row in history:
        dt = parse_jst(row.get("posted_at_jst") or row.get("posted_at"))
        if dt and now_jst - dt <= timedelta(hours=24):
            last24.append(row.get("post_type") or row.get("type"))
    key = f"{news.get('topic_key','')}|{now_jst.date().isoformat()}"
    bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % 100
    exploration_ratio = float(os.environ.get("STYLE_EXPLORATION_RATIO", "0.20"))
    exploration = bucket < int(max(0, min(1, exploration_ratio)) * 100)
    exploration_count = sum(
        1 for row in history
        if bool(row.get("is_exploration"))
        and (parse_jst(row.get("posted_at_jst") or row.get("posted_at")) or now_jst).date()
        == now_jst.date()
    )
    if exploration_count >= int(os.environ.get("STYLE_EXPLORATION_MAX_PER_DAY", "2")):
        exploration = False
    candidates = [base, "issue_diagram", "strong_opinion", "comparison_factcheck"]
    targets = {
        "breaking_news": float(os.environ.get("POST_STYLE_BREAKING_RATIO", ".25")),
        "issue_diagram": float(os.environ.get("POST_STYLE_DIAGRAM_RATIO", ".20")),
        "strong_opinion": float(os.environ.get("POST_STYLE_OPINION_RATIO", ".20")),
        "comparison_factcheck": float(os.environ.get("POST_STYLE_COMPARISON_RATIO", ".15")),
        "steelman_counterargument": float(os.environ.get("POST_STYLE_STEELMAN_RATIO", ".10")),
        "morning_evening_digest": float(os.environ.get("POST_STYLE_DIGEST_RATIO", ".10")),
    }
    week_start = now_jst - timedelta(days=7)
    steelman_count = sum(1 for row in history if (row.get("post_type") or row.get("type")) == "steelman_counterargument"
                         and (parse_jst(row.get("posted_at_jst") or row.get("posted_at")) or now_jst) >= week_start)
    if steelman_count < 3 and (exploration or news.get("has_counter_claims")):
        candidates.append("steelman_counterargument")
    total = max(1, len(last24))
    try:
        from review_strategy import load_active_strategy
        active_strategy = load_active_strategy(ROOT_DIR, now=now_jst)
    except Exception:
        active_strategy = {}
    priorities = active_strategy.get("post_type_priority") or []
    priority_bonus = {
        value: max(0.02, 0.10 - index * 0.02)
        for index, value in enumerate(priorities)
    }
    selected = min(
        candidates,
        key=lambda style: (
            last24.count(style) / total
            - targets.get(style, .15)
            - priority_bonus.get(style, 0.0)
        ),
    )
    if len(recent) >= 2 and recent[-1] == recent[-2] == selected:
        selected = next(style for style in candidates if style != selected)
    return selected, exploration


def phase_daily_limit_reached(history: list[dict], now_jst: datetime, is_breaking: bool,
                              normal_limit: int = 8, breaking_limit: int = 2,
                              total_limit: int = 10) -> bool:
    today = successful_posts_today(history, now_jst)
    if len(today) >= total_limit:
        return True
    breaking = sum((row.get("post_type") or row.get("type")) == "breaking_news" for row in today)
    normal = len(today) - breaking
    return breaking >= breaking_limit if is_breaking else normal >= normal_limit
def classify_critique_axis(news: dict) -> str:
    text = f"{news.get('title', '')} {news.get('summary', '')}"
    rules = (
        (("再審", "証拠", "裁判", "検察", "冤罪"), "due_process"),
        (("司法", "憲法", "法の支配"), "rule_of_law"),
        (("防衛", "外交", "同盟", "台湾", "中国", "北朝鮮"), "national_security"),
        (("原発", "電力", "エネルギー", "再エネ"), "energy_security"),
        (("少子化", "出生", "子育て", "家族"), "family_policy"),
        (("世代", "年金"), "intergenerational_fairness"),
        (("税", "予算", "国債", "財政"), "fiscal_discipline"),
        (("規制", "許認可", "手数料"), "regulatory_cost"),
        (("自治体", "知事", "市町村", "地方"), "local_autonomy"),
        (("産業", "半導体", "食料", "供給網"), "domestic_industry"),
        (("行政", "官僚", "補助金", "有識者", "情報公開"), "administrative_transparency"),
    )
    for terms, axis in rules:
        if any(term in text for term in terms):
            return axis
    return "small_government"


def classify_hook_type(news: dict, history: list[dict]) -> str:
    text = f"{news.get('title', '')} {news.get('summary', '')}"
    if re.search(r"\d", text):
        preferred = "number"
    elif any(term in text for term in ("一方", "対し", "改正前", "改正後", "vs", "ＶＳ")):
        preferred = "contrast"
    elif any(term in text for term in ("実は", "誤解", "事実")):
        preferred = "fact_reversal"
    elif any(term in text for term in ("なぜ", "問われ", "焦点")):
        preferred = "question"
    else:
        preferred = "conclusion_first"
    try:
        from review_strategy import load_active_strategy
        priorities = load_active_strategy(ROOT_DIR).get(
            "hook_type_priority") or []
    except Exception:
        priorities = []
    compatible = {
        "number": bool(re.search(r"\d", text)),
        "contrast": any(
            term in text for term in (
                "一方", "対し", "改正前", "改正後", "vs", "ＶＳ")),
        "fact_reversal": any(
            term in text for term in (
                "実は", "誤解", "事実", "公式", "発表")),
        "issue_redefinition": True,
        "question": True,
        "conclusion_first": True,
    }
    preferred = next(
        (value for value in priorities if compatible.get(value)), preferred)
    recent = [row.get("hook_type") for row in history if row.get("hook_type")][-2:]
    if len(recent) == 2 and recent[0] == recent[1] == preferred:
        return next(hook for hook in HOOK_TYPES if hook != preferred and hook not in recent)
    return preferred


def post_type_quota_reached(post_type: str, history: list[dict], now_jst: datetime) -> bool:
    count = sum(1 for row in successful_posts_today(history, now_jst) if row.get("post_type") == post_type)
    return count >= POST_TYPE_DAILY_LIMITS.get(post_type, 0)


def budget_reached(spent: float, limit: float) -> bool:
    return limit > 0 and spent >= limit


def calculate_growth_score(metrics: dict, weights: dict) -> float:
    impressions_per_hour = float(metrics.get("impressions_per_hour") or 0)
    engagement_rate = float(metrics.get("engagement_rate") or metrics.get("engagement_rate_pct") or 0)
    if engagement_rate > 1:
        engagement_rate /= 100.0
    profile_clicks = float(metrics.get("profile_clicks") or 0)
    quotes_bookmarks = float(metrics.get("quotes") or 0) + float(metrics.get("bookmarks") or 0)
    follow_conversion = float(metrics.get("follow_conversion") or 0)
    normalized = {
        "impressions_per_hour": min(impressions_per_hour / 1000.0, 1.0),
        "engagement_rate": min(engagement_rate / 0.10, 1.0),
        "profile_clicks": min(profile_clicks / 50.0, 1.0),
        "quotes_bookmarks": min(quotes_bookmarks / 50.0, 1.0),
        "follow_conversion": min(follow_conversion / 0.05, 1.0),
    }
    total_weight = sum(max(0.0, float(value)) for value in weights.values()) or 1.0
    score = sum(normalized.get(key, 0.0) * max(0.0, float(weight)) for key, weight in weights.items())
    return round(score / total_weight * 100.0, 3)
