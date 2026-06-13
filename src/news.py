import urllib.request
import xml.etree.ElementTree as ET
import random

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
        "name": "日経RSS",
        "url": "https://www.nikkei.com/rss/index.rss"
    },
]

def fetch_news(with_link=False):
    """ニュースを取得する。with_link=TrueならURLも返す"""
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
                if title and "NHK" not in title:
                    all_items.append({
                        "title": title,
                        "link": link,
                        "source": feed["name"]
                    })
        except Exception as e:
            print(f"{feed['name']} 取得エラー: {e}")
            continue
    
    if not all_items:
        return None
    
    # ランダムに1件選ぶ
    item = random.choice(all_items)
    
    if with_link:
        return item  # title + link
    else:
        return {"title": item["title"], "link": None}

def get_recent_titles():
    """AI要約用にタイトルを複数取得"""
    all_titles = []
    
    for feed in RSS_FEEDS:
        try:
            req = urllib.request.Request(
                feed["url"],
                headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as res:
                xml = res.read().decode("utf-8")
            
            root = ET.fromstring(xml)
            for item in root.findall(".//item")[:3]:
                title = item.findtext("title", "").strip()
                if title and "NHK" not in title:
                    all_titles.append(title)
        except Exception as e:
            print(f"{feed['name']} 取得エラー: {e}")
            continue
    
    return all_titles[:5]
