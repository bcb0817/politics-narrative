import os
import random
import anthropic
import tweepy
from news import fetch_news, get_recent_titles

# 1日の投稿設定
TOTAL_POSTS = 30
LINK_POSTS = 3

def get_tweepy_client():
    return tweepy.Client(
        consumer_key=os.environ["API_KEY"],
        consumer_secret=os.environ["API_KEY_SECRET"],
        access_token=os.environ["ACCESS_TOKEN"],
        access_token_secret=os.environ["ACCESS_TOKEN_SECRET"],
    )

def get_anthropic_client():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def generate_tweet_with_link(news_item):
    """リンクあり投稿を生成"""
    client = get_anthropic_client()
    titles = get_recent_titles()
    news_text = "\n".join([f"・{t}" for t in titles])

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""以下のニュースを元に、Xに投稿する日本語のツイートを1つ作成してください。

メインニュース：{news_item['title']}

条件：
- 100文字以内（URLを含めるため短めに）
- 客観的な要約
- ハッシュタグを2個つける
- ツイート本文のみ返答すること（URLは含めない）"""
        }]
    )
    text = message.content[0].text.strip()
    # URLを末尾に追加
    full_text = f"{text}\n{news_item['link']}"
    return full_text

def generate_tweet_without_link():
    """リンクなし投稿を生成"""
    client = get_anthropic_client()
    titles = get_recent_titles()
    news_text =
