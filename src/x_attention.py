"""X Search attention radar.

X posts are never treated as factual sources. This module only aggregates
cross-account attention signals and applies conservative anti-spam penalties.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from publishing_policy import normalize_topic_key


FIXED_QUERY_GROUPS = (
    ("国会・政府", ("国会", "政府", "首相", "法案", "採決", "政党", "選挙")),
    ("税・社会保障", ("税制", "消費税", "社会保障", "年金", "規制")),
    ("外交・安全保障", ("外交", "防衛", "安全保障", "治安")),
    ("エネルギー", ("原発", "エネルギー", "電力")),
    ("人口・地方", ("少子化", "子育て", "移民", "地方自治")),
)

ENGAGEMENT_BAIT = ("拡散希望", "リポストお願いします", "いいねお願いします", "フォローお願いします")
OFFICIAL_TERMS = ("公式", "省", "庁", "自治体", "国会", "新聞", "放送", "報道", "議員", "大臣")


def env_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def extract_dynamic_terms(rss_items: list[dict], limit: int = 8) -> list[str]:
    """Extract a bounded set of named entities/phrases from trusted RSS titles."""
    candidates = []
    known_countries = (
        "日本", "米国", "アメリカ", "中国", "台湾", "韓国", "北朝鮮", "ロシア",
        "ウクライナ", "イラン", "イスラエル", "EU", "NATO",
    )
    for item in rss_items[:40]:
        title = str(item.get("title") or "")
        candidates.extend(re.findall(r"[「『]([^」』]{2,24})[」』]", title))
        candidates.extend(country for country in known_countries if country in title)
        candidates.extend(re.findall(r"[一-龥ァ-ヶー]{2,12}(?:法案|法|会議|委員会|省|庁|党|政策|制度|協定)", title))
        candidates.extend(re.findall(r"[一-龥]{2,6}(?:首相|大臣|知事|市長|議員|代表)", title))
    counts = Counter(term.strip() for term in candidates if 2 <= len(term.strip()) <= 24)
    return [term for term, _ in counts.most_common(max(0, limit))]


def build_search_queries(rss_items: list[dict], max_queries: int = 5) -> list[dict]:
    max_queries = max(1, min(max_queries, 5))
    dynamic = extract_dynamic_terms(rss_items, limit=max_queries * 2)
    queries = []
    for term in dynamic[: min(3, max_queries)]:
        queries.append({
            "label": normalize_topic_key(term),
            "query": f'"{term}" lang:ja -is:retweet -is:reply',
            "terms": [term],
            "dynamic": True,
        })
    for label, terms in FIXED_QUERY_GROUPS:
        if len(queries) >= max_queries:
            break
        joined = " OR ".join(terms)
        queries.append({
            "label": label,
            "query": f"({joined}) lang:ja -is:retweet -is:reply",
            "terms": list(terms),
            "dynamic": False,
        })
    return queries[:max_queries]


def normalized_post_text(text: str) -> str:
    value = re.sub(r"https?://\S+", "", text or "")
    value = re.sub(r"[#＃][\w一-龥ぁ-んァ-ヶー]+", "", value)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def post_spam_penalty(post: dict, duplicate_ratio: float = 0.0) -> float:
    """Return a soft multiplier; never claim that an account is a bot."""
    text = str(post.get("text") or "")
    penalty = 1.0
    if post.get("is_reply"):
        penalty *= 0.45
    if any(term in text for term in ENGAGEMENT_BAIT):
        penalty *= 0.35
    words_without_tags = re.sub(r"[#＃][\w一-龥ぁ-んァ-ヶー]+", "", text).strip()
    if not words_without_tags:
        penalty *= 0.20
    if len(re.findall(r"https?://\S+", text)) >= 2:
        penalty *= 0.55
    if duplicate_ratio >= 0.40:
        penalty *= max(0.20, 1.0 - duplicate_ratio)
    if int(post.get("author_post_count") or 1) >= 5:
        penalty *= 0.65
    created = post.get("author_created_at")
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created.astimezone(timezone.utc) < timedelta(days=30):
            penalty *= 0.70
    if int(post.get("author_followers") or 0) < 5:
        penalty *= 0.80
    return max(0.05, min(penalty, 1.0))


def _derive_topic(text: str, query: dict) -> str:
    matched = [term for term in query.get("terms", []) if term and term in text]
    if matched:
        return normalize_topic_key(" ".join(matched[:2]))
    return normalize_topic_key(text)


def aggregate_attention(
    posts: list[dict],
    query: dict,
    min_unique_accounts: int = 3,
    min_post_count: int = 3,
    now_utc: datetime | None = None,
) -> list[dict]:
    now_utc = now_utc or datetime.now(timezone.utc)
    groups = defaultdict(list)
    author_counts = Counter(str(post.get("author_id") or "") for post in posts)
    normalized_counts = Counter(normalized_post_text(str(post.get("text") or "")) for post in posts)
    for post in posts:
        post = dict(post)
        normalized = normalized_post_text(str(post.get("text") or ""))
        post["author_post_count"] = author_counts[str(post.get("author_id") or "")]
        post["duplicate_ratio"] = normalized_counts[normalized] / max(1, len(posts)) if normalized else 1.0
        groups[_derive_topic(str(post.get("text") or ""), query)].append(post)

    topics = []
    for topic_key, members in groups.items():
        accounts = {str(post.get("author_id") or "") for post in members if post.get("author_id")}
        if len(members) < min_post_count or len(accounts) < min_unique_accounts:
            continue
        totals = {key: 0 for key in ("likes", "reposts", "replies", "quotes")}
        velocity_total = 0.0
        penalties = []
        official_present = False
        for post in members:
            for key in totals:
                totals[key] += int(post.get(key) or 0)
            raw = (
                int(post.get("likes") or 0)
                + int(post.get("reposts") or 0) * 2
                + int(post.get("replies") or 0)
                + int(post.get("quotes") or 0) * 2
            )
            created_at = post.get("created_at")
            if not isinstance(created_at, datetime):
                created_at = now_utc
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            hours = max((now_utc - created_at.astimezone(timezone.utc)).total_seconds() / 3600.0, 0.5)
            penalty = post_spam_penalty(post, float(post.get("duplicate_ratio") or 0))
            penalties.append(penalty)
            velocity_total += raw / hours * penalty
            description = f"{post.get('author_name','')} {post.get('author_description','')}"
            official_present = official_present or bool(post.get("author_verified")) or any(
                term in description for term in OFFICIAL_TERMS
            )
        unique_factor = min(1.0, len(accounts) / max(min_unique_accounts, 1))
        diversity_factor = sum(penalties) / max(len(penalties), 1)
        velocity_score = min(10.0, math.log1p(velocity_total) * 1.65)
        attention = min(10.0, velocity_score * unique_factor * diversity_factor + (0.4 if official_present else 0.0))
        topics.append({
            "topic_key": topic_key,
            "x_post_count": len(members),
            "unique_accounts": len(accounts),
            "total_likes": totals["likes"],
            "total_reposts": totals["reposts"],
            "total_replies": totals["replies"],
            "total_quotes": totals["quotes"],
            "velocity_score": round(velocity_score, 3),
            "x_attention_score": round(attention, 3),
            "official_or_media_present": official_present,
            "duplicate_ratio": round(1.0 - diversity_factor, 3),
            "query_label": query.get("label", ""),
        })
    return sorted(topics, key=lambda row: row["x_attention_score"], reverse=True)


def topic_similarity(topic_key: str, text: str) -> float:
    normalized = normalize_topic_key(text)
    if topic_key and topic_key in text:
        return 1.0
    return SequenceMatcher(None, topic_key, normalized).ratio()


def match_topics_to_rss(rss_items: list[dict], topics: list[dict]) -> list[dict]:
    """Attach attention only to externally corroborated RSS/official candidates."""
    enriched = []
    for item in rss_items:
        row = dict(item)
        haystack = f"{row.get('title', '')} {row.get('summary', '')}"
        matches = [
            (topic_similarity(str(topic.get("topic_key") or ""), haystack), topic)
            for topic in topics
        ]
        matches = [pair for pair in matches if pair[0] >= 0.72]
        if matches:
            _, topic = max(matches, key=lambda pair: (pair[0], pair[1].get("x_attention_score", 0)))
            row.update({
                "discovered_via": ["rss", "x_search"],
                "x_attention_score": float(topic.get("x_attention_score") or 0),
                "x_post_count": int(topic.get("x_post_count") or 0),
                "x_unique_accounts": int(topic.get("unique_accounts") or 0),
                "x_velocity_score": float(topic.get("velocity_score") or 0),
                "x_topic_key": topic.get("topic_key", ""),
            })
        else:
            row.setdefault("discovered_via", ["rss"])
            row.setdefault("x_attention_score", 0.0)
            row.setdefault("x_post_count", 0)
            row.setdefault("x_unique_accounts", 0)
            row.setdefault("x_velocity_score", 0.0)
        enriched.append(row)
    return enriched


def final_news_score(
    relevance: float, freshness: float, x_attention: float, reliability: float, x_weight: float = 0.25
) -> float:
    """Normalize components to 0..10 and preserve total weight at 1.0."""
    values = [max(0.0, min(float(value), 10.0)) for value in (relevance, freshness, x_attention, reliability)]
    x_weight = max(0.0, min(float(x_weight), 0.50))
    remaining = 1.0 - x_weight
    base_total = 0.35 + 0.25 + 0.15
    weights = (remaining * 0.35 / base_total, remaining * 0.25 / base_total, x_weight,
               remaining * 0.15 / base_total)
    return round(sum(value * weight for value, weight in zip(values, weights)), 4)
