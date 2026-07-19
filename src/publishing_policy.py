"""Deterministic publishing policy for the politics narrative bot.

This module is deliberately independent from OpenAI and X clients so that all
rate, cooldown, taxonomy, and review calculations can be unit-tested offline.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")

POST_TYPES = (
    "breaking_news",
    "issue_diagram",
    "strong_opinion",
    "comparison_factcheck",
    "morning_evening_digest",
)
POST_TYPE_DAILY_LIMITS = {
    "breaking_news": 5,
    "issue_diagram": 4,
    "strong_opinion": 3,
    "comparison_factcheck": 2,
    "morning_evening_digest": 2,
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
    "撤回", "公式発表", "判決", "被害拡大", "死者", "避難指示", "施行",
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
    text = re.sub(r"https?://\S+", "", title or "")
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
    if any(term in text for term in SIGNIFICANT_UPDATE_TERMS + ("速報", "緊急")):
        return "breaking_news"
    if any(term in text for term in ("比較", "改正前", "改正後", "与党案", "野党案", "一次資料")):
        return "comparison_factcheck"
    if any(term in text for term in ("制度", "仕組み", "法案", "予算", "再審", "税制")):
        return "issue_diagram"
    return "strong_opinion"


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
