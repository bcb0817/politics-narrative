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
        "name": "産経ニュース",
        "url": "https://www.sankei.com/rss/politics.xml"
    },
    {
        "name": "朝日新聞政治",
        "url": "https://www.asahi.com/rss/politics.rdf"
    },
    {
        "name": "Yahoo!ニュース政治",
        "url": "https://news.yahoo.co.jp/rss/topics/domestic.xml"
    },
    {
        "name": "Yahoo!ニュース経済",
        "url": "https://news.yahoo.co.jp/rss/topics/business.xml"
    },
]

POSTED_FILE = "posted_urls.json"
MAX_HISTORY = 200  # 保存する最大件数

def load_posted_urls():
    """投稿済みURLを読み込む"""
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_posted_url(url):
    """投稿済みURLを保存する"""
    posted = load_posted_urls()
    posted.add(url)
    # 古いものを削除して件数を制限
    posted_list = list(posted)[-MAX_HISTORY:]
    with open(POSTED_FILE, "w") as f:
        json.dump(posted_list, f)

def fetch_all_items():
    """全ソースからニュースを取得"""
    all_items = []
    for feed in RSS_FEEDS:
        try:
            req = urllib.request.Request(
                feed["url"],
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as res:
                xml = res.read().decode("utf-8")
            root = ET.fromstring(xml)
            for item in root.findall(".//item"):
                title = item.findtext("title", "").strip()
                link = item.findtext("link", "").strip()
                if title and "NHK" not in title and link:
                    all_items.append({
                        "title": title,
                        "link": link,
                        "source": feed["name"]
                    })
        except Exception as e:
            print(f"{feed['name']} 取得エラー: {e}")
            continue
    return all_items

def fetch_news(with_link=False):
    """重複なしでニュースを取得"""
    all_items = fetch_all_items()
    posted_urls = load_posted_urls()

    # 未投稿のものだけ抽出
    unposted = [i for i in all_items if i["link"] not in posted_urls]

    if not unposted:
        print("未投稿ニュースなし、履歴をリセット")
        unposted = all_items  # 全部使い切ったらリセット

    item = random.choice(unposted)

    if with_link:
        save_posted_url(item["link"])
        return item
    else:
        save_posted_url(item["link"])
        return {"title": item["title"], "link": None}

def get_recent_titles():
    """AI要約用にタイトルを複数取得"""
    all_items = fetch_all_items()
    titles = [i["title"] for i in all_items[:5]]
    return titles
