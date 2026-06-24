import urllib.request
import xml.etree.ElementTree as ET
import random
import json
import os

# ニュースソース（RSS）
RSS_FEEDS = [
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

POSTED_FILE = "posted_urls.json"
MAX_HISTORY = 200


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


def fetch_all_items():
    """全ソースからニュースを取得"""
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

            for item in root.findall(".//item"):
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                pub_date = item.findtext("pubDate", "").strip()
                summary = item.findtext("description", "").strip()

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
