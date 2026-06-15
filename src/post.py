import os
import anthropic
import tweepy
from news import fetch_news, get_recent_titles

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
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""以下のニュースを元に、Xに投稿する日本語のツイートを1つ作成してください。

メインニュース：{news_item['title']}

条件：
- 100文字から300文字のランダムな文字数で書く
- あなたは日本の保守・右派層に向けた鋭いオピニオンを発信するインフルエンサーです。ターゲット層が強く共感し、思わずリポストしたくなるようなX（旧Twitter）用の投稿を作成してください
- ハッシュタグを2個つける
- ツイート本文のみ返答すること（URLは含めない）"""
        }]
    )
    text = message.content[0].text.strip()
    full_text = f"{text}\n{news_item['link']}"
    return full_text

def generate_tweet_without_link():
    """リンクなし投稿を生成"""
    client = get_anthropic_client()
    titles = get_recent_titles()
    news_text = "\n".join([f"・{t}" for t in titles])
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": f"""以下の最新ニュースを元に、Xに投稿する日本語のツイートを1つ作成してください。

ニュース：
{news_text}

条件：
- 100文字から300文字のランダムな文字数で書く
- あなたは日本の保守・右派層に向けた鋭いオピニオンを発信するインフルエンサーです。ターゲット層が強く共感し、思わずリポストしたくなるようなX（旧Twitter）用の投稿を作成してください
- ハッシュタグを2個つける
- ツイート本文のみ返答すること"""
        }]
    )
    return message.content[0].text.strip()

def generate_tweet_diagram(news_item):
    """図解形式の投稿を生成"""
    client = get_anthropic_client()
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"""以下の政治・経済ニュースを元に、Xに投稿する図解形式のツイートを1つ作成してください。

ニュース：{news_item['title']}
ソース：{news_item['source']}

条件：
- 200文字〜500文字
- 以下のような図解・矢印・箇条書きを使って視覚的にわかりやすく
  例：
  【タイトル】
  原因 → 結果
  　↓
  影響① 〇〇
  影響② 〇〇
  　↓
  結論：〇〇
- 政府・中央銀行の発表に対して批判的・懐疑的な視点を含める
- ハッシュタグを2個つける
- ツイート本文のみ返答すること"""
        }]
    )
    return message.content[0].text.strip()

def post_tweet(text):
    client = get_tweepy_client()
    response = client.create_tweet(text=text)
    print(f"投稿成功: {response.data['id']}")
    print(f"内容: {text}")

if __name__ == "__main__":
    import sys

    mode = sys.argv[1] if len(sys.argv) > 1 else "test"

    if mode == "test":
        print("テストモードで投稿中...")
        post_tweet("世界が平和になりますように🕊️")
    elif mode == "link":
        print("リンクあり投稿を生成中...")
        news_item = fetch_news(with_link=True)
        if news_item:
            tweet = generate_tweet_with_link(news_item)
            post_tweet(tweet)
        else:
            print("ニュース取得失敗")
    elif mode == "diagram":
        print("図解形式の投稿を生成中...")
        news_item = fetch_news(with_link=False)
        if news_item:
            tweet = generate_tweet_diagram(news_item)
            post_tweet(tweet)
        else:
            print("ニュース取得失敗")
    else:
        print("リンクなし投稿を生成中...")
        tweet = generate_tweet_without_link()
        post_tweet(tweet)
