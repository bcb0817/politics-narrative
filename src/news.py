import urllib.request
import xml.etree.ElementTree as ET
import random
import json
import os
import math
from pathlib import Path
from email.utils import format_datetime
from datetime import datetime, timezone

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
            os.environ[key] = value


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


def fetch_x_search_items():
    """X API v2 Recent Searchから投稿候補を取得する。

    X_SEARCH_ENABLED=true の場合だけ実行する。X上の投稿は報道機関の記事とは
    限らないため、事実の確定情報ではなく話題・論点を見つける補助ソースとして扱う。
    """
    if not _env_bool("X_SEARCH_ENABLED"):
        return []

    bearer_token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not bearer_token:
        print("X検索エラー: X_BEARER_TOKEN が設定されていません")
        return []

    query = os.environ.get(
        "X_SEARCH_QUERY",
        "(政治 OR 国会 OR 政府 OR 法案 OR 選挙) lang:ja -is:retweet -is:reply",
    ).strip()
    if not query:
        print("X検索エラー: X_SEARCH_QUERY が空です")
        return []

    try:
        max_results = int(os.environ.get("X_SEARCH_MAX_RESULTS", "20"))
    except ValueError:
        max_results = 20
    max_results = max(10, min(max_results, 100))

    try:
        min_likes = max(0, int(os.environ.get("X_SEARCH_MIN_LIKES", "0")))
    except ValueError:
        min_likes = 0
    try:
        min_engagement = max(0, int(os.environ.get("X_SEARCH_MIN_ENGAGEMENT", "0")))
    except ValueError:
        min_engagement = 0

    try:
        import tweepy

        client = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=False)
        response = client.search_recent_tweets(
            query=query,
            max_results=max_results,
            tweet_fields=["author_id", "conversation_id", "created_at", "lang", "public_metrics"],
        )
    except Exception as e:
        print(f"X検索取得エラー: {e}")
        return []

    items = []
    for tweet in response.data or []:
        raw_text = (tweet.text or "").strip()
        text = " ".join(raw_text.split())
        metrics = tweet.public_metrics or {}
        likes = int(metrics.get("like_count", 0) or 0)
        retweets = int(metrics.get("retweet_count", 0) or 0)
        replies = int(metrics.get("reply_count", 0) or 0)
        quotes = int(metrics.get("quote_count", 0) or 0)
        engagement = likes + (retweets * 2) + replies + (quotes * 2)
        if not text or likes < min_likes or engagement < min_engagement:
            continue

        created_at = tweet.created_at
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            pub_date = format_datetime(created_at)
        else:
            pub_date = ""

        # 古い累積値だけが勝たないよう、経過時間で補正した反応速度を使う。
        if created_at is not None:
            age_hours = max(
                0.5,
                (datetime.now(timezone.utc) - created_at.astimezone(timezone.utc)).total_seconds() / 3600.0,
            )
        else:
            age_hours = 24.0
        engagement_per_hour = engagement / age_hours
        # 極端なバズ1件で全候補が固定されないよう対数化し、0〜10へ制限する。
        trend_score = min(10.0, math.log1p(engagement_per_hour) * 2.0)

        tweet_id = str(tweet.id)
        emoji_samples = []
        for symbol in re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", raw_text):
            if symbol not in emoji_samples:
                emoji_samples.append(symbol)
        nonempty_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        bullet_lines = sum(
            line.startswith(("-", "・", "●", "○", "▶", "➡")) for line in nonempty_lines
        )
        style_hint = (
            f"表現形式: {len(nonempty_lines) or 1}行、箇条書き{bullet_lines}行、"
            f"絵文字{len(emoji_samples)}種"
            + (f"（{' '.join(emoji_samples[:5])}）" if emoji_samples else "")
            + "。主張や煽り口調ではなく、この構成情報だけを参考にする。"
        )
        summary = (
            f"X上の投稿。いいね {likes}、リポスト "
            f"{retweets}、返信 {replies}、引用 {quotes}。"
            f"反応速度 {engagement_per_hour:.1f}/時、注目度 {trend_score:.2f}/10。{style_hint}"
        )
        items.append({
            "title": text,
            "link": f"https://x.com/i/web/status/{tweet_id}",
            "source": "X検索",
            "pub_date": pub_date,
            "summary": summary,
            "x_metrics": {
                "likes": likes,
                "retweets": retweets,
                "replies": replies,
                "quotes": quotes,
                "engagement": engagement,
                "engagement_per_hour": round(engagement_per_hour, 2),
            },
            "x_trend_score": round(trend_score, 3),
        })
    items.sort(key=lambda item: item.get("x_trend_score", 0), reverse=True)
    return items


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
                })

        except Exception as e:
            print(f"{feed['name']} 取得エラー: {e}")
            continue

    if include_x:
        for item in fetch_x_search_items():
            title = (item.get("title") or "").strip()
            link = (item.get("link") or "").strip()
            if not title or not link or link in seen_links or title in seen_titles:
                continue
            seen_links.add(link)
            seen_titles.add(title)
            all_items.append(item)

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
