import urllib.request
import xml.etree.ElementTree as ET
import random
import json
import os
import math
import re
from contextlib import closing
from pathlib import Path
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from x_attention import (
    aggregate_attention,
    build_search_queries,
    env_int,
    match_topics_to_rss,
)
from api_budget import (estimate_x, finalize as finalize_budget, forecast as cost_forecast,
                        reserve as reserve_budget, usage_totals)
from metrics_db import connect, db_path, init_db
from xai_radar import apply_verified_attention, search as fetch_xai_radar

# リポジトリ直下（cwdに依存しない）
_ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path: Path):
    """単体実行時にもリポジトリ直下の.envを読み込む。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file(_ROOT_DIR / ".env")


def _state_dir() -> Path:
    """STATE_DIR 環境変数（既定: リポジトリ直下 data/）。相対パスはリポジトリ直下基準。"""
    raw = os.environ.get("STATE_DIR", "").strip() or "data"
    p = Path(raw)
    if not p.is_absolute():
        p = _ROOT_DIR / p
    p.mkdir(parents=True, exist_ok=True)
    return p

# ニュースソース（RSS）
RSS_FEEDS = [
    {
        "name": "内閣府公式",
        "url": "https://www.cao.go.jp/rss/news.rdf",
        "source_type": "government_official"
    },
    {
        "name": "首相官邸公式",
        "url": "https://www.kantei.go.jp/jp/rss/index.rdf",
        "source_type": "government_official"
    },
    {
        "name": "外務省公式",
        "url": "https://www.mofa.go.jp/mofaj/rss/whatsnew.rdf",
        "source_type": "ministry_official"
    },
    {
        "name": "NHK政治",
        "url": "https://www.nhk.or.jp/rss/news/cat4.xml", "source_type": "trusted_media"
    },
    {
        "name": "NHK経済",
        "url": "https://www.nhk.or.jp/rss/news/cat5.xml", "source_type": "trusted_media"
    },
    {
        "name": "NHK国際",
        "url": "https://www.nhk.or.jp/rss/news/cat6.xml", "source_type": "trusted_media"
    },
    {
        "name": "Yahoo!ニュース政治",
        "url": "https://news.yahoo.co.jp/rss/topics/domestic.xml", "source_type": "news_aggregator"
    },
    {
        "name": "Yahoo!ニュース経済",
        "url": "https://news.yahoo.co.jp/rss/topics/business.xml", "source_type": "news_aggregator"
    },
    {
        "name": "Yahoo!ニュース国際",
        "url": "https://news.yahoo.co.jp/rss/topics/world.xml", "source_type": "news_aggregator"
    },
]

# 注: post.py は fetch_all_items() のみ使うため、このファイルの投稿履歴は
#     通常運用では書き込まれない。混乱防止のため保存先だけ STATE_DIR に揃える。
POSTED_FILE = str(_state_dir() / "posted_urls.json")
MAX_HISTORY = 200


def _env_bool(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes")


def _save_x_search_results(
    topics: list[dict],
    queries: list[dict],
    resources: int = 0,
    estimated_cost: float = 0.0,
    *,
    representative_posts: list[dict] | None = None,
    notify_discord: bool = False,
) -> None:
    state = _state_dir()
    now = datetime.now(timezone.utc)
    payload = {
        "provider": "native_x",
        "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=env_int(os.environ.get("X_SEARCH_CACHE_TTL_MINUTES"), 360, 1, 1440))).isoformat(),
        "query_count": len(queries),
        "post_resources_read": resources,
        "estimated_cost_usd": estimated_cost,
        "queries": [{"label": query.get("label", ""), "dynamic": query.get("dynamic", False)}
                    for query in queries],
        "topic_count": len(topics),
        "topics": topics,
    }
    latest = state / "x_search_latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history_dir = state / "x_search_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    history_file = history_dir / f"{now.astimezone(ZoneInfo('Asia/Tokyo')).date().isoformat()}.jsonl"
    with open(history_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    if notify_discord and _env_bool("X_DISCORD_RESEARCH_ENABLED", "false"):
        try:
            from discord_notify import notify_x_research
            notify_x_research({
                "provider": "X API Recent Search",
                "lookback_minutes": env_int(
                    os.environ.get("X_SEARCH_LOOKBACK_MINUTES"), 240, 10, 1440),
                "query_count": len(queries),
                "resource_count": resources,
                "topic_count": len(topics),
                "queries": [
                    query.get("label", "") for query in queries
                    if query.get("label")
                ],
                "representative_posts": representative_posts or [],
                "topics": [{
                    "topic_key": topic.get("topic_key"),
                    "attention_score": topic.get("x_attention_score"),
                    "velocity_score": topic.get("velocity_score"),
                    "post_count": topic.get("x_post_count"),
                    "unique_accounts": topic.get("unique_accounts"),
                    "externally_corroborated": bool(
                        topic.get("externally_corroborated")),
                } for topic in topics[:5]],
                "corroborated_topic_count": sum(
                    bool(topic.get("externally_corroborated"))
                    for topic in topics),
            })
        except Exception:
            pass


def load_x_search_cache(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    try:
        payload = json.loads((_state_dir() / "x_search_latest.json").read_text(encoding="utf-8"))
        expires = datetime.fromisoformat(payload.get("expires_at", ""))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now <= expires and _env_bool("X_SEARCH_REUSE_RESULTS", "true"):
            return payload.get("topics", []) if isinstance(payload.get("topics"), list) else []
    except Exception:
        pass
    return []


def should_run_x_search(now_jst: datetime | None = None) -> bool:
    now_jst = now_jst or datetime.now(ZoneInfo("Asia/Tokyo"))
    schedule = {value.strip() for value in os.environ.get("X_SEARCH_SCHEDULE", "06:00,12:00,18:00").split(",")}
    return now_jst.strftime("%H:%M") in schedule


def _already_ran_current_schedule(now_jst: datetime) -> bool:
    try:
        payload = json.loads((_state_dir() / "x_search_latest.json").read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(payload.get("generated_at", "")).astimezone(ZoneInfo("Asia/Tokyo"))
        return generated.strftime("%Y-%m-%d %H:%M") == now_jst.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return False


def _load_seen_x_post_ids() -> set[str]:
    try:
        values = json.loads((_state_dir() / "x_search_seen_post_ids.json").read_text(encoding="utf-8"))
        return {str(value) for value in values if value}
    except Exception:
        return set()


def _save_seen_x_post_ids(values: set[str]) -> None:
    # Snowflake IDs are time-sortable; retaining the newest 10,000 bounds local state.
    newest = sorted(values, key=lambda value: int(value) if value.isdigit() else 0)[-10000:]
    (_state_dir() / "x_search_seen_post_ids.json").write_text(
        json.dumps(newest, ensure_ascii=False, indent=2), encoding="utf-8")


def _daily_x_search_reads(path: Path | None = None) -> int:
    init_db(path)
    day = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    try:
        with closing(connect(path)) as conn:
            return int(conn.execute("""SELECT COALESCE(SUM(resource_count),0) FROM api_usage_events
                WHERE provider='x' AND operation='x_search' AND timestamp LIKE ?""", (day + "%",)).fetchone()[0])
    except Exception:
        return 0


def fetch_x_search_topics(rss_items: list[dict]) -> list[dict]:
    """Use X Recent Search only as a cross-account attention radar."""
    if not _env_bool("X_SEARCH_ENABLED"):
        return []

    now_jst = datetime.now(ZoneInfo("Asia/Tokyo"))
    if not should_run_x_search(now_jst):
        return load_x_search_cache()
    from audit_tools import guard_provider_execution
    if not guard_provider_execution("native_x", now_jst):
        print("Discovery provider conflict: xai already executed in this slot")
        return load_x_search_cache()
    if _already_ran_current_schedule(now_jst):
        return load_x_search_cache()
    if cost_forecast(db_path()).get("pause_x_search"):
        print("X Search paused: projected monthly budget reached the 93% restriction stage")
        return load_x_search_cache()

    bearer_token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not bearer_token:
        print("X Search unavailable -> continuing with RSS candidates")
        return []

    max_queries = env_int(os.environ.get("X_SEARCH_MAX_QUERIES_PER_RUN"), 3, 1, 3)
    max_results = env_int(os.environ.get("X_SEARCH_MAX_RESULTS_PER_QUERY"), 6, 1, 100)
    lookback_minutes = env_int(os.environ.get("X_SEARCH_LOOKBACK_MINUTES"), 240, 10, 1440)
    min_accounts = env_int(os.environ.get("X_SEARCH_MIN_UNIQUE_ACCOUNTS"), 3, 2, 100)
    min_posts = env_int(os.environ.get("X_SEARCH_MIN_POST_COUNT"), 3, 2, 100)
    max_topics = env_int(os.environ.get("X_SEARCH_MAX_TOPIC_RESULTS"), 10, 1, 50)
    queries = build_search_queries(rss_items, max_queries=max_queries)
    per_run_cap = env_int(os.environ.get("X_SEARCH_MAX_POST_READS_PER_RUN"), 18, 1, 10000)
    daily_cap = env_int(os.environ.get("X_SEARCH_MAX_POST_READS_PER_DAY"), 54, 1, 10000)
    monthly_cap = env_int(os.environ.get("X_SEARCH_MAX_POST_READS_PER_MONTH"), 1620, 1, 100000)
    if queries:
        max_results = min(max_results, max(1, per_run_cap // len(queries)))
    totals = usage_totals(db_path())
    planned_reads = len(queries) * max_results
    if _daily_x_search_reads() + planned_reads > daily_cap:
        print("X Search daily read cap reached")
        print("Continuing with RSS and cached X attention data")
        return load_x_search_cache()
    if totals.get("x_search_reads", 0) + planned_reads > monthly_cap:
        print("X Search monthly read cap reached")
        print("Continuing with RSS and cached X attention data")
        return load_x_search_cache()
    reservation, budget_reason = reserve_budget(
        "x", "x_search", "recent_search", estimate_x("post_read_per_resource", planned_reads),
        planned_reads, {"query_count": len(queries)})
    if not reservation:
        print(f"X Search skipped: {budget_reason}")
        return load_x_search_cache()

    try:
        import tweepy

        client = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=False)
    except Exception as e:
        finalize_budget(reservation, 0, success=False, error_type=type(e).__name__, resource_count=0)
        print(f"X Search unavailable -> continuing with RSS candidates ({type(e).__name__})")
        return []

    all_topics = []
    successful_queries = 0
    resources_read = 0
    seen_tweet_ids = _load_seen_x_post_ids()
    newly_seen_tweet_ids = set()
    representative_candidates = []
    start_time = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    for query in queries:
        try:
            response = client.search_recent_tweets(
                query=query["query"],
                max_results=max_results,
                start_time=start_time,
                expansions=["author_id"],
                tweet_fields=["author_id", "conversation_id", "created_at", "lang", "public_metrics"],
                user_fields=["name", "description", "verified", "created_at", "public_metrics"],
            )
            successful_queries += 1
        except Exception as e:
            print(f"X Search query failed: {query.get('label','')} ({type(e).__name__})")
            continue
        includes = getattr(response, "includes", None) or {}
        users = {str(user.id): user for user in includes.get("users", [])}
        posts = []
        response_posts = list(response.data or [])[:max_results]
        resources_read += len(response_posts)
        for tweet in response_posts:
            if len(posts) >= max_results:
                break
            if str(tweet.id) in seen_tweet_ids:
                continue
            seen_tweet_ids.add(str(tweet.id))
            newly_seen_tweet_ids.add(str(tweet.id))
            metrics = tweet.public_metrics or {}
            author = users.get(str(tweet.author_id))
            posts.append({
                "tweet_id": str(tweet.id),
                "text": (tweet.text or "").strip(),
                "author_id": str(tweet.author_id or ""),
                "author_name": str(getattr(author, "name", "") or ""),
                "author_description": str(getattr(author, "description", "") or ""),
                "author_verified": bool(getattr(author, "verified", False)),
                "author_created_at": getattr(author, "created_at", None),
                "author_followers": int((getattr(author, "public_metrics", None) or {}).get("followers_count", 0) or 0),
                "created_at": tweet.created_at,
                "is_reply": bool(getattr(tweet, "in_reply_to_user_id", None)),
                "likes": int(metrics.get("like_count", 0) or 0),
                "reposts": int(metrics.get("retweet_count", 0) or 0),
                "replies": int(metrics.get("reply_count", 0) or 0),
                "quotes": int(metrics.get("quote_count", 0) or 0),
            })
        representative_candidates.extend({
            "post_id": post.get("tweet_id"),
            "text": post.get("text"),
            "engagement": (
                int(post.get("likes") or 0)
                + int(post.get("reposts") or 0) * 2
                + int(post.get("replies") or 0)
                + int(post.get("quotes") or 0) * 2
            ),
        } for post in posts)
        all_topics.extend(aggregate_attention(
            posts, query, min_unique_accounts=min_accounts, min_post_count=min_posts
        ))

    if successful_queries == 0:
        finalize_budget(reservation, 0, success=False, error_type="x_search_failed",
                        resource_count=resources_read)
        print("X Search unavailable -> continuing with RSS candidates")
        return []
    merged = {}
    for topic in all_topics:
        key = topic["topic_key"]
        current = merged.get(key)
        if current is None or topic["x_attention_score"] > current["x_attention_score"]:
            merged[key] = topic
    topics = sorted(merged.values(), key=lambda row: row["x_attention_score"], reverse=True)[:max_topics]
    corroborated_rows = match_topics_to_rss(rss_items, topics)
    corroborated_keys = {
        str(row.get("x_topic_key") or "")
        for row in corroborated_rows
        if "x_search" in set(row.get("discovered_via") or [])
    }
    topics = [{
        **topic,
        "externally_corroborated": (
            str(topic.get("topic_key") or "") in corroborated_keys),
    } for topic in topics]
    representative_posts = sorted(
        representative_candidates,
        key=lambda row: int(row.get("engagement") or 0),
        reverse=True,
    )[:3]
    actual_cost = estimate_x("post_read_per_resource", resources_read) or 0
    finalize_budget(reservation, actual_cost, success=True, resource_count=resources_read)
    if newly_seen_tweet_ids:
        _save_seen_x_post_ids(seen_tweet_ids)
    _save_x_search_results(
        topics, queries, resources_read, actual_cost,
        representative_posts=representative_posts,
        notify_discord=True,
    )
    print(f"X Search topics found: {len(all_topics)}")
    print(f"X Search qualified topics: {len(topics)}")
    for topic in topics[:5]:
        print(f"X attention score applied: {topic['topic_key']} = {topic['x_attention_score']}")
    return topics


def load_posted_urls():
    """投稿済みURLをリストで読み込む"""
    if not os.path.exists(POSTED_FILE):
        return []
    try:
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        print(f"投稿履歴の読み込みエラー: {e}")
        return []


def save_posted_url(url):
    """投稿済みURLを保存する"""
    posted = load_posted_urls()
    if url in posted:
        return
    posted.append(url)
    posted = posted[-MAX_HISTORY:]
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(posted, f, ensure_ascii=False, indent=2)


def fetch_all_items(include_x=True):
    """RSSと、指定された場合だけX検索から候補を取得する。"""
    all_items = []
    seen_links = set()
    seen_titles = set()

    for feed in RSS_FEEDS:
        try:
            req = urllib.request.Request(
                feed["url"],
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as res:
                xml = res.read().decode("utf-8", errors="ignore")

            root = ET.fromstring(xml)

            # RSS 2.0と、名前空間付きRSS 1.0/RDFの両方に対応する。
            rss_items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "item"]
            for item in rss_items:
                fields = {
                    child.tag.rsplit("}", 1)[-1]: (child.text or "").strip()
                    for child in list(item)
                }
                title = fields.get("title", "")
                link = fields.get("link", "")
                pub_date = fields.get("pubDate", "") or fields.get("date", "")
                summary = fields.get("description", "")

                if not title or not link:
                    continue

                if link in seen_links or title in seen_titles:
                    continue

                seen_links.add(link)
                seen_titles.add(title)

                all_items.append({
                    "title": title,
                    "link": link,
                    "source": feed["name"],
                    "source_type": feed.get("source_type", "rss"),
                    "pub_date": pub_date,
                    "summary": summary,
                    "discovered_via": ["rss"],
                    "x_attention_score": 0.0,
                    "x_post_count": 0,
                    "x_unique_accounts": 0,
                    "x_velocity_score": 0.0,
                })

        except Exception as e:
            print(f"{feed['name']} 取得エラー: {e}")
            continue

    if include_x:
        provider = os.environ.get("X_TOPIC_DISCOVERY_PROVIDER", "xai").strip().lower()
        try:
            if provider == "xai" and _env_bool("XAI_ENABLED", "true"):
                all_items = apply_verified_attention(
                    all_items, fetch_xai_radar(
                        candidates=all_items, notify_discord=True))
            elif provider == "native_x" and _env_bool("X_NATIVE_SEARCH_ENABLED", "false") \
                    and _env_bool("X_SEARCH_ENABLED"):
                all_items = match_topics_to_rss(all_items, fetch_x_search_topics(all_items))
            elif provider not in {"xai", "native_x", "none"}:
                print(f"Unknown X_TOPIC_DISCOVERY_PROVIDER={provider}; using RSS only")
        except Exception as e:
            # Never switch to another paid discovery provider automatically.
            print(f"Topic discovery unavailable -> continuing with RSS candidates ({type(e).__name__})")

    return all_items


def fetch_news(with_link=False):
    """重複なしでニュースを1件取得"""
    all_items = fetch_all_items()

    if not all_items:
        print("取得できたニュースがありません")
        return None

    posted_urls = set(load_posted_urls())

    unposted = [
        item for item in all_items
        if item["link"] not in posted_urls
    ]

    if not unposted:
        print("未投稿ニュースなし。全ニュースから再選択します")
        unposted = all_items

    item = random.choice(unposted)
    save_posted_url(item["link"])

    if with_link:
        return item

    return {
        "title": item["title"],
        "link": None,
        "source": item["source"],
    }


def get_recent_titles(limit=5):
    """AI要約用にタイトルを複数取得"""
    all_items = fetch_all_items()

    if not all_items:
        return []

    random.shuffle(all_items)

    return [
        item["title"]
        for item in all_items[:limit]
    ]
