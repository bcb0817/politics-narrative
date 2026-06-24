#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
politics-narrative — X 自動投稿Bot（政治・政策系 図解投稿）

運用方針:
- GitHub Actions は10分おきに起動する
- Python側で JST 現在時刻を取得して投稿可否を判定する
- 投稿許可スロットは JST 05:07〜23:17 の1日40回
- 各スロットには12分の許容幅を持たせる
- 1スロット1投稿まで
- 定期投稿・手動実行ともに diagram モード固定
- linkモード / test投稿 / ランダムスケジュール / ランダム投稿数 / post.yml自動書き換え は廃止
- 投稿内容は政策・データ・図解中心
- 過度な煽り、陰謀論、差別表現、個人攻撃は禁止

使い方:
    python post.py diagram      # 通常（時刻判定あり）
    FORCE_POST=true python post.py diagram   # 強制投稿（時刻判定なし・diagramのみ）
"""

import os
import sys
import json
import textwrap
from pathlib import Path
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

JST = ZoneInfo("Asia/Tokyo")
UTC = ZoneInfo("UTC")

SRC_DIR = Path(__file__).resolve().parent
POSTED_SLOTS_FILE = SRC_DIR / "posted_slots.json"
POSTED_URLS_FILE = SRC_DIR / "posted_urls.json"

# 投稿許可スロット（JST）— SNSピーク時間帯に寄せた1日40本
POST_SLOTS = [
    "05:07", "05:37",
    "06:07", "06:37",
    "07:07", "07:27", "07:47",
    "08:07", "08:27", "08:47",
    "09:07", "09:27",
    "10:07", "10:37",
    "11:07", "11:37", "11:57",
    "12:17", "12:37", "12:57",
    "13:17",
    "14:07",
    "15:07",
    "16:07",
    "17:07", "17:27", "17:47",
    "18:07", "18:27", "18:47",
    "19:07",
    "20:07", "20:27", "20:47",
    "21:07", "21:27", "21:47",
    "22:07", "22:27",
    "23:17",
]
assert len(POST_SLOTS) == 40, f"POST_SLOTS must be 40, got {len(POST_SLOTS)}"

# スロット開始から +12分まで投稿許可
POST_WINDOW_MINUTES = 12

# 優先ジャンル（高いほど優先）
PRIORITY_GENRES = [
    "社会保障",   # 年金・医療・介護
    "税財政",     # 税金・財政・社会保険料
    "少子化",     # 人口動態
    "安全保障",   # 防衛費
    "エネルギー", # 原発・電気代
    "移民政策",   # 外国人労働者
    "教育",       # 子育て政策
    "国会法案",   # 法案・選挙制度
]

# 除外・低優先テーマ（投稿しない / スコアを大きく下げる）
EXCLUDED_TOPICS = [
    "芸能人の政治発言", "陰謀論", "民族・国籍への攻撃",
    "ワクチン陰謀", "宗教対立煽り", "皇室への過激言及",
    "個人攻撃", "政党罵倒",
]

# ニュース事前フィルタ用のジャンル別キーワード（コスト削減＋関連度向上）
GENRE_KEYWORDS = {
    "社会保障": ["社会保障", "年金", "医療", "介護", "健康保険", "後期高齢"],
    "税財政": ["税", "増税", "減税", "財政", "予算", "国債", "社会保険料", "消費税"],
    "少子化": ["少子化", "出生", "人口", "子育て", "児童手当"],
    "安全保障": ["防衛", "安全保障", "自衛隊", "ミサイル", "有事"],
    "エネルギー": ["原発", "電気代", "エネルギー", "再エネ", "電力", "ガソリン"],
    "移民政策": ["外国人", "移民", "技能実習", "入管", "在留"],
    "教育": ["教育", "大学", "奨学金", "給食", "教員"],
    "国会法案": ["国会", "法案", "選挙", "解散", "委員会", "可決", "閣議"],
}

# 投稿型
POST_TYPES = {
    "A": "対比型（政府の説明 vs 国民の実感／表の争点 vs 本当の争点）",
    "B": "数字インパクト型（規模・比較で驚かせる）",
    "C": "誤解訂正型（『〜は本当か？』で前提を問い直す）",
    "D": "争点整理型（本当に見るべき論点を整理する）",
    "E": "未来警告型（このままだとどうなるかを示す）",
}

# スコア閾値（section 17）
SCORE_POST_ALWAYS = 9   # 9-10 必ず投稿
SCORE_POST = 7          # 7-8 投稿
SCORE_SAVE = 5          # 5-6 保存のみ（投稿見送り）
# 4以下は投稿しない
BAN_RISK_BLOCK = 7      # 炎上・BANリスクがこの値以上なら他が高くても投稿しない

ANTHROPIC_MODEL = "claude-sonnet-4-6"  # コスト重視。必要なら opus 等に変更可

# 図解画像サイズ（X 向け 16:9）
IMG_W, IMG_H = 1200, 675


# ---------------------------------------------------------------------------
# ログ
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# 1. JST 現在時刻の取得（外部API → ローカル zoneinfo フォールバック）
# ---------------------------------------------------------------------------

def get_jst_now():
    """戻り値: (jst_datetime, source_str)"""
    # worldtimeapi が不安定な環境向けに、外部API取得を環境変数で無効化できる。
    # GitHub runner の時計は NTP 同期済みなので local_zoneinfo でも正確。
    if os.environ.get("DISABLE_TIME_API", "").strip().lower() in ("true", "1", "yes"):
        return datetime.now(JST), "local_zoneinfo (api disabled)"
    try:
        r = requests.get(
            "https://worldtimeapi.org/api/timezone/Asia/Tokyo",
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        dt = datetime.fromisoformat(data["datetime"])
        return dt.astimezone(JST), "worldtimeapi"
    except Exception as e:
        log(f"[WARN] Failed to fetch JST time from API: {e}")
        return datetime.now(JST), "local_zoneinfo"


# ---------------------------------------------------------------------------
# 2. 投稿許可スロット判定
# ---------------------------------------------------------------------------

def find_current_post_slot(now_jst: datetime):
    """現在JST時刻が許可スロットの 0〜+12分 以内なら、その情報を返す。
    戻り値: (slot, slot_key, slot_dt, window_end) / 該当なしは (None, None, None, None)
    """
    today = now_jst.date()
    for slot in POST_SLOTS:
        hour, minute = map(int, slot.split(":"))
        slot_dt = datetime.combine(today, time(hour, minute), tzinfo=JST)
        window_end = slot_dt + timedelta(minutes=POST_WINDOW_MINUTES)
        if slot_dt <= now_jst <= window_end:
            slot_key = f"{today.isoformat()}_{slot}"
            return slot, slot_key, slot_dt, window_end
    return None, None, None, None


# ---------------------------------------------------------------------------
# 3. 重複投稿防止（スロット記録 / URL・テーマ記録）
# ---------------------------------------------------------------------------

def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def is_slot_posted(slot_key: str) -> bool:
    posted = _load_json(POSTED_SLOTS_FILE, [])
    return slot_key in posted


def mark_slot_posted(slot_key: str) -> None:
    posted = _load_json(POSTED_SLOTS_FILE, [])
    if slot_key not in posted:
        posted.append(slot_key)
    # 古いキーが膨らみすぎないよう直近300件に丸める
    _save_json(POSTED_SLOTS_FILE, posted[-300:])


def load_post_history() -> list:
    """過去投稿の記録（重複・類似回避とジャンルローテーション用）。
    旧Botが残した『URL文字列の配列』形式も受け入れ、dictに正規化する。
    """
    raw = _load_json(POSTED_URLS_FILE, [])
    if not isinstance(raw, list):
        return []
    norm = []
    for h in raw:
        if isinstance(h, dict):
            norm.append(h)
        elif isinstance(h, str):
            norm.append({"source_url": h})
        # それ以外の型は無視
    return norm


def save_post_record(record: dict) -> None:
    history = load_post_history()
    history.append(record)
    _save_json(POSTED_URLS_FILE, history[-500:])


def recent_genres(history: list, n: int = 3) -> list:
    return [h.get("genre") for h in history[-n:] if h.get("genre")]


def is_duplicate(candidate: dict, history: list) -> bool:
    """URL一致 or タイトル一致 or 主要キーワードの強い重なりで重複と判断"""
    url = (candidate.get("source_url") or "").strip()
    title = (candidate.get("title") or "").strip()
    kw = set(candidate.get("keywords") or [])
    for h in history[-120:]:
        if url and url == (h.get("source_url") or "").strip():
            return True
        if title and title == (h.get("title") or "").strip():
            return True
        hkw = set(h.get("keywords") or [])
        if kw and hkw and len(kw & hkw) >= max(2, min(len(kw), len(hkw))):
            return True
    return False


# ---------------------------------------------------------------------------
# 4. ニュース取得（※既存のRSS取り込みがあればここを差し替え）
# ---------------------------------------------------------------------------

def gather_candidate_news() -> list:
    """投稿の素材になるニュースを集める。
    戻り値: [{"title","summary","url","source_name"} , ...]

    ★ 既存リポジトリに RSS ingestion がある場合は、その出力をここに渡すこと。
      下は feedparser を使った最小フォールバック。フィードは適宜差し替える。
    """
    feeds = [
        # 政策系の最小フォールバック（NHK 政治 / 経済）。
        # ★ 既存の RSS ingestion がある場合は、その出力をこの関数に渡して下さい。
        # ★ 下のURLが解決しない場合は、実在するフィードに差し替えて下さい。
        "https://www3.nhk.or.jp/rss/news/cat4.xml",  # 政治
        "https://www3.nhk.or.jp/rss/news/cat5.xml",  # 経済
    ]
    items = []
    try:
        import feedparser
        for url in feeds:
            d = feedparser.parse(url)
            src = d.feed.get("title", "RSS")
            for e in d.entries[:15]:
                items.append({
                    "title": e.get("title", "").strip(),
                    "summary": e.get("summary", "").strip(),
                    "url": e.get("link", "").strip(),
                    "source_name": src,
                })
    except Exception as e:
        log(f"[WARN] RSS fetch failed: {e}")
    return items


def prefilter_news(items: list, top_n: int = 4) -> list:
    """優先ジャンルのキーワードでニュースを採点し、関連の高い上位だけ残す。
    LLM呼び出し回数を抑え、関連度も上げる。
    """
    def kw_score(it: dict) -> int:
        text = f"{it.get('title','')} {it.get('summary','')}"
        return sum(1 for kws in GENRE_KEYWORDS.values() for kw in kws if kw in text)

    scored = [(kw_score(it), it) for it in items]
    relevant = [it for s, it in sorted(scored, key=lambda x: x[0], reverse=True) if s > 0]
    if relevant:
        return relevant[:top_n]
    # 関連語ゼロしか無いときは先頭を少しだけ（完全に投稿が止まらないように）
    return items[:top_n]


# ---------------------------------------------------------------------------
# 5. 投稿候補の生成・スコアリング（Anthropic）
# ---------------------------------------------------------------------------

GENERATION_SYSTEM = """\
あなたは日本の政治・政策アカウントの編集長です。
保守・政策層に届く「図解・データ」投稿を作ります。

絶対ルール:
- 過度な煽り、陰謀論、差別的・攻撃的表現、個人攻撃、政党罵倒は禁止。
- 「これはヤバい」「売国」「反日」「目覚めよ」のような安っぽい煽りは禁止。
- 数字は、与えられたニュース本文に根拠があるものだけ使う。
  根拠が無い数字は使わない（使ったら uses_unverified_number=true にする）。
- 無難すぎるニュース要約にはしない。「違和感」「争点」「対比」「数字」「構造」を使う。

投稿文の構成:
1行目: 強いフック
2〜4行目: 論点の説明
5〜7行目: 数字・比較・構造
最後: 引用されやすい短い結論
条件: 120〜240字 / 3〜5ブロック / 1ブロック1〜2行 / 空行2〜3個まで /
      ハッシュタグ原則なし(使うなら最大1) / URLなし / 絵文字なし / ポエム化しない。
本文は1行ずつの配列(tweet_lines)で返す。空行は空文字列 "" を要素として入れる。

投稿型は必ず1つ選ぶ:
A=対比型, B=数字インパクト型, C=誤解訂正型, D=争点整理型, E=未来警告型。

図解タイトルは説明ではなく対比型・争点型にする。
良い例: 防衛費より大きい本丸 / 増税ではなく社会保険料 / 表の争点 vs 本当の争点
悪い例: 社会保障費について / 防衛費の推移

各スコアは0〜10で自己評価する:
news(ニュース性), controversy(論争性), data_ability(データ化しやすさ),
resonance(保守層への刺さりやすさ), save_value(保存価値),
quote_likelihood(引用リポストされやすさ), ban_risk(炎上・BANリスク=高いほど危険)。
overall は ban_risk を踏まえた総合点(0〜10)。

必ず submit_candidates ツールを呼び、候補を3案提出する。地の文・説明は書かない。
"""

GENERATION_USER_TMPL = """\
次のニュースから、X投稿候補を必ず3案つくり、submit_candidates ツールで提出してください。
フックは混ぜる（対比型/数字型/誤解訂正型 など）。

ニュース:
title: {title}
summary: {summary}
source_name: {source_name}

優先ジャンル(高い順): 社会保障 / 税財政 / 少子化 / 安全保障 / エネルギー / 移民政策 / 教育 / 国会法案
このニュースが上記と無関係、または芸能・陰謀論・民族攻撃・宗教対立煽り等なら、各案の ban_risk を高く、overall を低くしてください。
本文(tweet_lines)は1行ずつの配列。空行は "" を入れる。図解タイトルは対比型/争点型にする。
"""

# Anthropic ツール（構造化出力）。SDKがJSON妥当性を保証するので手書きパース不要。
GENRE_ENUM = ["社会保障", "税財政", "少子化", "安全保障",
              "エネルギー", "移民政策", "教育", "国会法案"]

_COLUMN_SCHEMA = {
    "type": ["object", "null"],
    "properties": {
        "label": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
    },
}

CANDIDATE_TOOL = {
    "name": "submit_candidates",
    "description": "X投稿候補を3案提出する。",
    "input_schema": {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["A", "B", "C", "D", "E"]},
                        "title": {"type": "string"},
                        "tweet_lines": {"type": "array", "items": {"type": "string"}},
                        "genre": {"type": "string", "enum": GENRE_ENUM},
                        "hook": {"type": "string"},
                        "image_title": {"type": "string"},
                        "image_points": {"type": "array", "items": {"type": "string"}},
                        "image_left": _COLUMN_SCHEMA,
                        "image_right": _COLUMN_SCHEMA,
                        "image_conclusion": {"type": "string"},
                        "source_name": {"type": "string"},
                        "keywords": {"type": "array", "items": {"type": "string"}},
                        "uses_unverified_number": {"type": "boolean"},
                        "scores": {
                            "type": "object",
                            "properties": {
                                "news": {"type": "integer"},
                                "controversy": {"type": "integer"},
                                "data_ability": {"type": "integer"},
                                "resonance": {"type": "integer"},
                                "save_value": {"type": "integer"},
                                "quote_likelihood": {"type": "integer"},
                                "ban_risk": {"type": "integer"},
                            },
                            "required": ["news", "controversy", "data_ability",
                                         "resonance", "save_value",
                                         "quote_likelihood", "ban_risk"],
                        },
                        "overall": {"type": "integer"},
                        "decision_reason": {"type": "string"},
                    },
                    "required": ["type", "title", "tweet_lines", "genre",
                                 "image_title", "image_points", "image_conclusion",
                                 "keywords", "uses_unverified_number", "scores",
                                 "overall"],
                },
            },
        },
        "required": ["candidates"],
    },
}


def generate_candidates(news_item: dict) -> list:
    """1ニュースから3案を生成して返す。失敗時は空配列。
    Anthropic のツール使用で構造化出力を強制し、JSON妥当性をSDKに保証させる。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log("[ERROR] ANTHROPIC_API_KEY is not set")
        return []
    try:
        import anthropic
    except Exception as e:
        log(f"[ERROR] anthropic SDK import failed: {e}")
        return []

    client = anthropic.Anthropic(api_key=api_key)
    user = GENERATION_USER_TMPL.format(
        title=news_item.get("title", ""),
        summary=news_item.get("summary", "")[:1200],
        source_name=news_item.get("source_name", ""),
    )
    try:
        resp = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=6000,
            system=GENERATION_SYSTEM,
            tools=[CANDIDATE_TOOL],
            tool_choice={"type": "tool", "name": "submit_candidates"},
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        log(f"[ERROR] candidate generation failed: {e}")
        return []

    if getattr(resp, "stop_reason", None) == "max_tokens":
        log("[WARN] generation hit max_tokens (output may be truncated)")

    candidates = []
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use" and block.name == "submit_candidates":
            candidates = (block.input or {}).get("candidates", []) or []
            break

    if not isinstance(candidates, list):
        return []

    # 後処理: 本文を組み立て、欠損を補完
    cleaned = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        lines = c.get("tweet_lines") or []
        c["tweet_text"] = "\n".join(str(x) for x in lines).strip()
        if not c["tweet_text"]:
            continue
        c.setdefault("source_url", news_item.get("url", ""))
        c.setdefault("source_name", news_item.get("source_name", ""))
        cleaned.append(c)
    return cleaned


def effective_score(c: dict, history: list) -> float:
    """投稿可否に使う実効スコア。BANリスク・未検証数字・ジャンル連続でペナルティ。"""
    base = float(c.get("overall", 0) or 0)
    ban = int((c.get("scores") or {}).get("ban_risk", 0) or 0)

    # BANリスクが高ければ実質失格
    if ban >= BAN_RISK_BLOCK:
        return -1.0
    # 未検証数字を使っているなら大きく減点（誤情報の自動投稿を防ぐ）
    if c.get("uses_unverified_number"):
        base -= 4.0
    # 直近3件と同じジャンルが続くなら軽く減点（ローテーション）
    if c.get("genre") in recent_genres(history, 3):
        base -= 1.5
    return base


# ---------------------------------------------------------------------------
# 6. 図解画像の生成（Pillow）
# ---------------------------------------------------------------------------

def _load_font(size: int):
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Bold.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def render_diagram(c: dict, out_path: Path) -> Path:
    """候補から図解PNGを生成する。"""
    from PIL import Image, ImageDraw

    bg = (17, 22, 30)
    fg = (236, 240, 244)
    sub = (150, 160, 172)
    accent = (96, 165, 250)
    warn = (248, 180, 90)

    img = Image.new("RGB", (IMG_W, IMG_H), bg)
    d = ImageDraw.Draw(img)

    f_title = _load_font(54)
    f_label = _load_font(30)
    f_item = _load_font(28)
    f_concl = _load_font(32)
    f_src = _load_font(20)

    margin = 56
    y = margin

    # タイトル
    title = c.get("image_title") or c.get("title") or ""
    for line in textwrap.wrap(title, width=18)[:2]:
        d.text((margin, y), line, font=f_title, fill=fg)
        y += 64
    y += 10
    d.line([(margin, y), (IMG_W - margin, y)], fill=accent, width=3)
    y += 28

    left = c.get("image_left")
    right = c.get("image_right")

    if left and right:
        # 対比レイアウト
        col_w = (IMG_W - margin * 2 - 40) // 2
        x_left = margin
        x_right = margin + col_w + 40
        for x, col in ((x_left, left), (x_right, right)):
            yy = y
            d.text((x, yy), str(col.get("label", "")), font=f_label, fill=accent)
            yy += 48
            for it in (col.get("items") or [])[:5]:
                d.text((x + 8, yy), f"・{it}", font=f_item, fill=fg)
                yy += 44
    else:
        # 箇条書きレイアウト
        yy = y
        for it in (c.get("image_points") or [])[:5]:
            for line in textwrap.wrap(str(it), width=34)[:2]:
                d.text((margin + 8, yy), f"・{line}", font=f_item, fill=fg)
                yy += 44

    # つまり
    concl = c.get("image_conclusion") or ""
    if concl:
        box_y = IMG_H - 160
        d.line([(margin, box_y - 14), (IMG_W - margin, box_y - 14)], fill=sub, width=1)
        d.text((margin, box_y), "つまり：", font=f_label, fill=warn)
        cy = box_y + 44
        for line in textwrap.wrap(concl, width=30)[:2]:
            d.text((margin, cy), line, font=f_concl, fill=fg)
            cy += 40

    # 出典
    src = f"出典: {c.get('source_name','')}"
    bbox = d.textbbox((0, 0), src, font=f_src)
    d.text((IMG_W - margin - (bbox[2] - bbox[0]), IMG_H - 36), src, font=f_src, fill=sub)

    img.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------------------
# 7. X への投稿（tweepy / OAuth1.0a）
# ---------------------------------------------------------------------------

def post_to_x(text: str, image_path: Path):
    """画像付きツイートを投稿。戻り値: tweet_id 文字列 / 失敗時に例外。"""
    import tweepy

    api_key = os.environ["API_KEY"]
    api_secret = os.environ["API_KEY_SECRET"]
    access_token = os.environ["ACCESS_TOKEN"]
    access_secret = os.environ["ACCESS_TOKEN_SECRET"]

    # メディアアップロードは v1.1
    auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_secret)
    api_v1 = tweepy.API(auth)
    media = api_v1.media_upload(filename=str(image_path))

    # ツイート作成は v2
    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )
    resp = client.create_tweet(text=text, media_ids=[media.media_id])
    return str(resp.data.get("id"))


# ---------------------------------------------------------------------------
# 8. メイン
# ---------------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "diagram"
    if mode in ("link", "test", "normal"):
        # link / test は完全廃止。normal も今回の運用では使わない。
        log(f"[ERROR] mode '{mode}' is not supported in this deployment. Use 'diagram'.")
        sys.exit(1)
    mode = "diagram"

    force = os.environ.get("FORCE_POST", "").strip().lower() in ("true", "1", "yes")

    # --- 時刻 ---
    now_jst, time_source = get_jst_now()
    now_utc = now_jst.astimezone(UTC)
    log(f"[INFO] Time source: {time_source}")
    log(f"[INFO] Current JST: {now_jst:%Y-%m-%d %H:%M:%S}")
    log(f"[INFO] Current UTC: {now_utc:%Y-%m-%d %H:%M:%S}")

    # --- スロット判定 ---
    if force:
        slot = f"FORCE_{now_jst:%H:%M}"
        slot_key = f"{now_jst.date().isoformat()}_{slot}"
        log("[INFO] FORCE_POST=true -> time window check skipped")
        log(f"[INFO] Matched slot: {slot}")
        log(f"[INFO] Slot key: {slot_key}")
    else:
        slot, slot_key, _, _ = find_current_post_slot(now_jst)
        log(f"[INFO] Matched slot: {slot if slot else 'None'}")
        log(f"[INFO] Slot key: {slot_key if slot_key else 'None'}")
        if slot is None:
            log("[INFO] Decision: skip")
            log("[INFO] Skip reason: outside_post_window")
            return
        if is_slot_posted(slot_key):
            log("[INFO] Decision: skip")
            log("[INFO] Skip reason: already_posted_slot")
            return

    # --- 素材収集 ---
    history = load_post_history()
    news_items = gather_candidate_news()
    log(f"[INFO] News items fetched: {len(news_items)}")
    if not news_items:
        log("[INFO] Decision: skip")
        log("[INFO] Skip reason: no_news")
        return

    target_news = prefilter_news(news_items, top_n=4)
    log(f"[INFO] News after prefilter: {len(target_news)}")

    # --- 候補生成・採点（複数ニュース×3案からベスト1を選ぶ） ---
    best = None
    best_score = -1.0
    for item in target_news:
        for c in generate_candidates(item):
            if is_duplicate(c, history):
                continue
            s = effective_score(c, history)
            if s > best_score:
                best, best_score = c, s

    if best is None:
        log("[INFO] Decision: skip")
        log("[INFO] Skip reason: no_valid_candidate")
        return

    scores = best.get("scores") or {}
    log(f"[INFO] News title: {best.get('title','')}")
    log(f"[INFO] Selected post type: {best.get('type','')} ({POST_TYPES.get(best.get('type',''),'')})")
    log(f"[INFO] Score: overall={best.get('overall')} effective={best_score:.1f} ban_risk={scores.get('ban_risk')}")
    log(f"[INFO] Genre: {best.get('genre','')}")
    log(f"[INFO] Hook: {best.get('hook','')}")
    log(f"[INFO] Decision reason: {best.get('decision_reason','')}")

    # --- スコア閾値ゲート ---
    if best_score < SCORE_POST:
        log("[INFO] Decision: skip")
        if best_score < 0:
            log("[INFO] Skip reason: ban_risk_or_unverified_block")
        elif best_score < SCORE_SAVE:
            log("[INFO] Skip reason: low_score_do_not_post")
        else:
            log("[INFO] Skip reason: save_only_score_5_6")
        return

    # --- 画像生成 ---
    img_path = SRC_DIR / "diagram.png"
    try:
        render_diagram(best, img_path)
    except Exception as e:
        log(f"[ERROR] render_diagram failed: {e}")
        log("[INFO] Decision: skip")
        log("[INFO] Skip reason: render_error")
        return

    # --- 投稿 ---
    tweet_text = (best.get("tweet_text") or "").strip()
    log("[INFO] Decision: post")
    try:
        tweet_id = post_to_x(tweet_text, img_path)
        log(f"[INFO] Posted tweet id: {tweet_id}")
    except Exception as e:
        # 投稿失敗時はスロット記録しない（=未投稿扱い）
        log(f"[ERROR] post_to_x failed: {e}")
        log("[INFO] Skip reason: post_api_failed (slot left unposted)")
        return

    # --- 投稿成功後にだけ記録 ---
    mark_slot_posted(slot_key)
    save_post_record({
        "slot_key": slot_key,
        "posted_at_jst": now_jst.isoformat(),
        "tweet_id": tweet_id,
        "title": best.get("title", ""),
        "type": best.get("type", ""),
        "genre": best.get("genre", ""),
        "source_url": best.get("source_url", ""),
        "keywords": best.get("keywords", []),
    })
    log("[INFO] Slot and post history recorded.")


if __name__ == "__main__":
    main()
