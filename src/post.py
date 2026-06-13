import os
import tweepy
from datetime import datetime

def post_tweet():
    client = tweepy.Client(
        consumer_key=os.environ["API_KEY"],
        consumer_secret=os.environ["API_KEY_SECRET"],
        access_token=os.environ["ACCESS_TOKEN"],
        access_token_secret=os.environ["ACCESS_TOKEN_SECRET"],
    )

    text = f"自動投稿テスト 🤖\n{datetime.now().strftime('%Y-%m-%d %H:%M')}"
    response = client.create_tweet(text=text)
    print(f"投稿成功: {response.data['id']}")

if __name__ == "__main__":
    post_tweet()
