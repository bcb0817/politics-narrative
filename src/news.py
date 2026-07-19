import urllib.request
import xml.etree.ElementTree as ET
import random
import json
import os
import math
import re
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
        "url": "https://www.cao.go.jp/rss/news.rdf"
    },
    {
        "name": "NHK政治",
        "url": "https://www.nhk.or.jp/rss/news/cat4.xml"
    },
    {
        "name": "NHK経済",
        "url": "https://www.nhk.or.jp/rss/news/cat5.xml"
    },
    {
        "name": "NHK国際",
        "url": "https://www.nhk.or.jp/rss/news/cat6.xml"
    },
    {
        "name": "Yahoo!ニュース政治",
        "url": "https://news.yahoo.co.jp/rss/topics/domestic.xml"
    },
    {
        "name": "Yahoo!ニュース経済",
        "url": "https://news.yahoo.co.jp/rss/topics/business.xml"
    },
    {
        "name": "Yahoo!ニュース国際",
        "url": "https://news.yahoo.co.jp/rss/topics/world.xml"
    },
]

# 注: post.py は fetch_all_items() のみ使うため、このファイルの投稿履歴は
#     通常運用では書き込まれない。混乱防止のため保存先だけ STATE_DIR に揃える。
POSTED_FILE = str(_state_dir() / "posted_urls.json")
MAX_HISTORY = 200


def _env_bool(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes")


def _save_x_search_results(topics: list[dict], queries: list[dict]) -> None:
    state = _state_dir()
    now = datetime.now(timezone.utc)
    payload = {
        "collected_at": now.isoformat(),
        "query_count": len(queries),
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


def fetch_x_search_topics(rss_items: list[dict]) -> list[dict]:
    """Use X Recent Search only as a cross-account attention radar."""
    if not _env_bool("X_SEARCH_ENABLED"):
        return []

    bearer_token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not bearer_token:
        print("X Search unavailable -> continuing with RSS candidates")
        return []

    max_queries = env_int(os.environ.get("X_SEARCH_MAX_QUERIES_PER_RUN"), 5, 1, 5)
    max_results = env_int(os.environ.get("X_SEARCH_MAX_RESULTS_PER_QUERY"), 20, 10, 100)
    lookback_minutes = env_int(os.environ.get("X_SEARCH_LOOKBACK_MINUTES"), 90, 10, 1440)
    min_accounts = env_int(os.environ.get("X_SEARCH_MIN_UNIQUE_ACCOUNTS"), 3, 2, 100)
    min_posts = env_int(os.environ.get("X_SEARCH_MIN_POST_COUNT"), 3, 2, 100)
    max_topics = env_int(os.environ.get("X_SEARCH_MAX_TOPIC_RESULTS"), 10, 1, 50)
    queries = build_search_queries(rss_items, max_queries=max_queries)

    try:
        import tweepy

        client = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=False)
    except Exception as e:
        print(f"X Search unavailable -> continuing with RSS candidates ({type(e).__name__})")
        return []

    all_topics = []
    successful_queries = 0
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
        for tweet in response.data or []:
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
        all_topics.extend(aggregate_attention(
            posts, query, min_unique_accounts=min_accounts, min_post_count=min_posts
        ))

    if successful_queries == 0:
        print("X Search unavailable -> continuing with RSS candidates")
        return []
    merged = {}
    for topic in all_topics:
        key = topic["topic_key"]
        current = merged.get(key)
        if current is None or topic["x_attention_score"] > current["x_attention_score"]:
            merged[key] = topic
    topics = sorted(merged.values(), key=lambda row: row["x_attention_score"], reverse=True)[:max_topics]
    _save_x_search_results(topics, queries)
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

    if include_x and _env_bool("X_SEARCH_ENABLED"):
        try:
            topics = fetch_x_search_topics(all_items)
            all_items = match_topics_to_rss(all_items, topics)
        except Exception as e:
            # X is an optional attention signal. Never expose credentials or stop RSS.
            print(f"X Search unavailable -> continuing with RSS candidates ({type(e).__name__})")

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
