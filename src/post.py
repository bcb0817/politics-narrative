import os
import sys
import json
import anthropic
import tweepy
from news import fetch_news

# ============================================================
# 運用方針：
# - 定期投稿はすべて diagram モード（論点化された図解風テキスト）
# - 手動実行も diagram モード
# - link / normal / test 投稿は使わない
# - 1ニュースにつき3案生成し、最も伸びそうな1案だけ投稿
# - 投稿基準を満たさないニュースはスキップ（無理に投稿しない）
# ============================================================

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 2000

# 投稿可否のしきい値（総合スコア 1-10）
POST_THRESHOLD = 7.0   # 7以上は投稿 / 5-6台は素材保存・見送り / 5未満は不採用
SAVE_THRESHOLD = 5.0

TWEET_CHAR_LIMIT = 280  # Xの上限

# ジャンル・ローテーション順（連続を避ける優先順）
GENRE_ROTATION = [
    "社会保障", "税金", "少子化", "安全保障",
    "エネルギー", "移民政策", "教育", "国会・法案",
]

POST_TYPES = {
    "A": "対比型（政府の説明 vs 国民の実感 / 表の争点 vs 本当の争点）",
    "B": "数字インパクト型（防衛費より大きい社会保障費 など）",
    "C": "誤解訂正型（「○○だけが問題」は本当か？）",
    "D": "争点整理型（本当に見るべき数字 / なぜ話が噛み合わないか）",
    "E": "未来警告型（このままだと現役世代の負担は / 出生数減少の財政影響）",
}

# 直近投稿の記録（news.py の posted_urls.json とは別管理）
LOG_PATH = os.path.join(os.path.dirname(__file__), "posted_log.json")


def get_tweepy_client():
    return tweepy.Client(
        consumer_key=os.environ["API_KEY"],
        consumer_secret=os.environ["API_KEY_SECRET"],
        access_token=os.environ["ACCESS_TOKEN"],
        access_token_secret=os.environ["ACCESS_TOKEN_SECRET"],
    )


def get_anthropic_client():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ---------- 直近ログの読み書き ----------
def load_recent_log(n=12):
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)[-n:]
    except Exception:
        return []


def record_log(entry):
    existing = []
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    existing.append(entry)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(existing[-200:], f, ensure_ascii=False, indent=2)


def _parse_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e != -1:
        t = t[s:e + 1]
    return json.loads(t)


# ---------- diagram 生成（3案→スコア→最良1案） ----------
SYSTEM_PROMPT = """あなたは日本向けの政治・政策「図解」アカウントの編集長です。
目的は X でのインプレッション最大化。保存・引用リポスト・リポストされる投稿だけを作ります。
無難なニュース要約は禁止。必ず「違和感・争点・対比・数字・構造」で論点化します。
特定政党への単なる罵倒・人格攻撃・デマは作りません（伸びても不採用）。

# 本文の構成（必須）
1行目：強いフック（短く・引っかかる）
2〜4行目：論点の説明
5〜7行目：数字・比較・構造
最後の1行：単独で引用されても刺さる短い結論

# 本文の条件
- 120〜240字程度（Xの上限280字を絶対に超えない）
- 改行を多めに
- ハッシュタグは原則なし（使うなら最大1つ）
- URLは入れない
- 問題意識は明確に

# タイトル（投稿の主題）
必ず対比型・問いかけ型・意外性のどれか。
良い例：防衛費より大きい本丸 / 増税ではなく社会保険料 / 政府の説明 vs 国民の実感 / 税収最高でも生活が苦しい理由
悪い例：社会保障費について / 防衛費の推移 / ○○の解説 / ○○とは

# 投稿型（必ず1つ選ぶ）
A 対比型 / B 数字インパクト型 / C 誤解訂正型 / D 争点整理型 / E 未来警告型

# 優先ジャンル
社会保障・年金・医療・介護 / 税金・財政・社会保険料 / 少子化・人口動態 /
安全保障・防衛費 / エネルギー・原発・電気代 / 外国人労働者・移民政策 /
教育・子育て政策 / 国会・法案・選挙制度。単なる政党罵倒は除外。

# スコアリング（各軸 1-10）
news=ニュース性 / controversy=論争性 / dataizable=データ化しやすさ /
conservative=保守層への刺さりやすさ / saveability=保存価値 / quotability=引用されやすさ /
total=総合（単純平均でなく伸びる確信度を1-10で）

# 重複回避
直近投稿（recent）とテーマ・タイトル・主要キーワードが似た案は作らない。
直近で続いたジャンルは避け、別ジャンルに寄せる。

# 候補生成
このニュースに対し、フックの異なる候補を必ず3案作る。各案をスコアリングし、totalが最も高い案を選ぶ。

# 出力（JSONのみ。前置き・コードフェンス禁止）
{
  "news_title": "対象ニュースの要約タイトル",
  "genre": "優先ジャンルのどれか",
  "keywords": ["主要キーワード", "..."],
  "candidates": [
    {"type":"A〜E","hook":"1行目","tweet":"本文(改行込み120-240字)",
     "score":{"news":0,"controversy":0,"dataizable":0,"conservative":0,"saveability":0,"quotability":0,"total":0.0}}
  ],
  "selected_index": 0,
  "decision_reason": "なぜその案か（短く）"
}
"""


def generate_diagram_candidates(news_item, recent):
    client = get_anthropic_client()
    recent_view = [
        {"title": p.get("title", ""), "genre": p.get("genre", ""), "keywords": p.get("keywords", [])}
        for p in recent
    ]
    user_msg = (
        "# 対象ニュース\n"
        f"見出し: {news_item.get('title','')}\n"
        f"概要: {news_item.get('summary','')}\n"
        f"出典: {news_item.get('source','')}\n\n"
        "# 直近の投稿（重複・連続ジャンルを避ける材料）\n"
        f"{json.dumps(recent_view, ensure_ascii=False)}\n\n"
        "フック違いの候補を3案作り、最も伸びる1案を選んでJSONで返してください。"
    )
    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    return _parse_json(text)


def select_and_decide(data, recent):
    """しきい値・文字数・ジャンル連続を考慮して最終決定。
    返り値: (should_post: bool, tweet: str, selected: dict, reason: str)"""
    cands = data.get("candidates", [])
    if not cands:
        return False, "", {}, "候補が生成されなかった"

    # totalの高い順、かつ280字以内のものを優先
    ordered = sorted(cands, key=lambda c: c.get("score", {}).get("total", 0), reverse=True)

    last_genre = recent[-1]["genre"] if recent else None
    recent_titles = [p.get("title", "") for p in recent]

    for c in ordered:
        tweet = c.get("tweet", "").strip()
        total = float(c.get("score", {}).get("total", 0))

        # 同ジャンル連続はソフトに減点（投稿は止めないが優先度を下げる）
        if last_genre and data.get("genre") == last_genre:
            total -= 1.5

        if len(tweet) > TWEET_CHAR_LIMIT:
            continue  # 文字数オーバーは次案へ
        if _too_similar(data.get("news_title", ""), recent_titles):
            return False, "", c, "直近投稿とテーマが重複"

        if total >= POST_THRESHOLD:
            return True, tweet, c, data.get("decision_reason", "")
        elif total >= SAVE_THRESHOLD:
            return False, "", c, f"総合スコア{total:.1f}（基準{POST_THRESHOLD}未満・素材保存）"
        else:
            return False, "", c, f"総合スコア{total:.1f}（基準未満・不採用）"

    return False, "", ordered[0], "全候補が文字数超過"


def _too_similar(title, recent_titles):
    """簡易な重複チェック（完全一致または主要語の高重複）。"""
    if not title:
        return False
    t = set(title)
    for rt in recent_titles:
        if not rt:
            continue
        if title == rt:
            return True
        overlap = len(t & set(rt)) / max(1, len(t | set(rt)))
        if overlap > 0.8:
            return True
    return False


def log_decision(news_item, data, selected, should_post, reason):
    sc = selected.get("score", {})
    print("---- diagram post decision ----")
    print(f"news_title : {data.get('news_title', news_item.get('title',''))}")
    print(f"genre      : {data.get('genre','')}")
    print(f"type       : {selected.get('type','')} ({POST_TYPES.get(selected.get('type',''),'')})")
    print(f"hook       : {selected.get('hook','')}")
    print(f"score      : total={sc.get('total','')} | news={sc.get('news','')} "
          f"contro={sc.get('controversy','')} data={sc.get('dataizable','')} "
          f"cons={sc.get('conservative','')} save={sc.get('saveability','')} "
          f"quote={sc.get('quotability','')}")
    print(f"decision   : {'POST' if should_post else 'SKIP'}")
    print(f"reason     : {reason}")
    print("--------------------------------")


def post_tweet(text):
    client = get_tweepy_client()
    response = client.create_tweet(text=text)
    print(f"投稿成功: {response.data['id']}")
    print(f"内容: {text}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "diagram"

    if mode != "diagram":
        # 運用は diagram のみ。それ以外は何もしない。
        print(f"mode='{mode}' は無効です。diagram のみ実行されます。")
        sys.exit(0)

    print("図解形式の投稿を生成中...")
    news_item = fetch_news(with_link=False)
    if not news_item:
        print("ニュース取得失敗")
        sys.exit(0)

    recent = load_recent_log()
    try:
        data = generate_diagram_candidates(news_item, recent)
    except Exception as e:
        print(f"生成/解析エラー: {e}")
        sys.exit(0)

    should_post, tweet, selected, reason = select_and_decide(data, recent)
    log_decision(news_item, data, selected, should_post, reason)

    if not should_post:
        print("今回は投稿を見送りました。")
        sys.exit(0)

    post_tweet(tweet)
    record_log({
        "title": data.get("news_title", news_item.get("title", "")),
        "genre": data.get("genre", ""),
        "type": selected.get("type", ""),
        "hook": selected.get("hook", ""),
        "keywords": data.get("keywords", []),
        "url": news_item.get("link") or news_item.get("url", ""),
    })
