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
# POST_TYPES のキー順がそのまま CANDIDATE_TOOL の type enum になる（ズレ防止）。
POST_TYPES = {
    "A": "対比型（政府の説明 vs 国民の実感／表の争点 vs 本当の争点）",
    "B": "数字インパクト型（規模・比較で驚かせる）",
    "C": "誤解訂正型（『〜は本当か？』で前提を問い直す）",
    "D": "争点整理型（本当に見るべき論点を整理する）",
    "E": "未来警告型（このままだとどうなるかを示す）",
    "F": "争点問いかけ型（争点・優先順位・負担配分を提示し、最後に1つだけ問う）",
    "G": "誰が得するか型（その決定で誰が利益を得るのかを整理する）",
    "H": "負担の見える化型（コスト・負担が誰にどう乗るのかを可視化する）",
    "I": "海外比較型（他国の制度・水準と比べて日本の位置を示す）",
    "J": "世代間構造型（現役・将来世代・高齢層の負担と受益の構造を示す）",
    "K": "政策の副作用型（狙いと、その裏で起きる想定外の影響を整理する）",
    "L": "ニュースの裏にある制度型（報道の背後にある制度・仕組みを解説する）",
}
# CANDIDATE_TOOL の type enum と必ず一致させるための単一情報源。
POST_TYPE_KEYS = list(POST_TYPES.keys())  # ["A","B","C","D","E","F","G","H","I","J","K","L"]

# スコア閾値（section 17）
SCORE_POST_ALWAYS = 9   # 9-10 必ず投稿
SCORE_POST = 7          # 7-8 投稿
SCORE_SAVE = 5          # 5-6 保存のみ（投稿見送り）
# 4以下は投稿しない
BAN_RISK_BLOCK = 7      # 炎上・BANリスクがこの値以上なら他が高くても投稿しない

ANTHROPIC_MODEL = "claude-sonnet-4-6"  # コスト重視。必要なら opus 等に変更可

# 図解画像サイズ。将来の A/B テスト用に DIAGRAM_PRESET で切り替え可能にする。
#   landscape: 1200x675（X タイムライン向け 16:9・既定）
#   vertical : 1080x1350（保存・縦長シェア向け 4:5）
# 未指定時は landscape のまま。
DIAGRAM_PRESETS = {
    "landscape": (1200, 675),
    "vertical": (1080, 1350),
}


def get_diagram_size():
    """環境変数 DIAGRAM_PRESET に応じた (幅, 高さ) を返す。未指定は landscape。"""
    preset = os.environ.get("DIAGRAM_PRESET", "").strip().lower()
    return DIAGRAM_PRESETS.get(preset, DIAGRAM_PRESETS["landscape"])


# モジュール定数（後方互換のため名前は維持）。プロセス起動時の環境変数で確定する。
IMG_W, IMG_H = get_diagram_size()


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


def recent_types(history: list, n: int = 5) -> list:
    """直近 n 件の投稿型（type）を返す。型の偏り抑制に使う。"""
    return [h.get("type") for h in history[-n:] if h.get("type")]


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

def _import_fetch_all_items():
    """src/news.py の fetch_all_items を読み込む。
    workflow は `cd src && python post.py` で動くが、別の作業ディレクトリから
    呼ばれても動くよう SRC_DIR を sys.path に通してから import する。
    """
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from news import fetch_all_items  # noqa: E402
    return fetch_all_items


def gather_candidate_news() -> list:
    """投稿の素材になるニュースを集める。

    src/news.py の fetch_all_items() を使う（NHK 政治/経済/国際・Yahoo! 政治/経済/国際）。
    ※ fetch_news() は取得時に save_posted_url() してしまい、実際に投稿していない
      ニュースまで投稿済み扱いになるため、ここでは使わない。

    戻り値: [{"title","summary","url","source_name","pub_date"}, ...]
    """
    try:
        fetch_all_items = _import_fetch_all_items()
    except Exception as e:
        log(f"[ERROR] failed to import news.fetch_all_items: {e}")
        return []

    try:
        raw = fetch_all_items()
    except Exception as e:
        log(f"[WARN] fetch_all_items failed: {e}")
        return []

    items = []
    for it in raw or []:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        link = (it.get("link") or "").strip()
        if not title or not link:
            continue
        items.append({
            "title": title,
            "summary": (it.get("summary") or "").strip(),
            "url": link,
            "source_name": (it.get("source") or "").strip(),
            "pub_date": (it.get("pub_date") or "").strip(),
        })
    return items


# ソース別の軽い信頼度補正（prefilter のスコアに加点）
SOURCE_TRUST_BONUS = {
    "NHK政治": 2,
    "NHK経済": 2,
    "NHK国際": 1,
    "Yahoo!ニュース政治": 1,
    "Yahoo!ニュース経済": 1,
    "Yahoo!ニュース国際": 0,
}


def prefilter_news(items: list, top_n: int = None) -> list:
    """優先ジャンルのキーワードでニュースを採点し、関連の高い上位だけ残す。
    LLM呼び出し回数を抑え、関連度も上げる。

    追加処理:
    - source_name による軽い信頼度補正
    - title 重複の除去
    - 投稿履歴(posted_urls.json)にある source_url は除外
    - EXCLUDED_TOPICS に該当するものは大きく減点（実質除外）
    - top_n は環境変数 PREFILTER_TOP_N で変更可能（未指定なら4）
    """
    if top_n is None:
        try:
            top_n = int(os.environ.get("PREFILTER_TOP_N", "4"))
        except ValueError:
            top_n = 4
        if top_n <= 0:
            top_n = 4

    history = load_post_history()
    posted_urls = {
        (h.get("source_url") or "").strip()
        for h in history if (h.get("source_url") or "").strip()
    }

    # title 重複の除去 ＋ 投稿済みURLの除外
    seen_titles = set()
    deduped = []
    for it in items:
        title = (it.get("title") or "").strip()
        url = (it.get("url") or "").strip()
        if not title:
            continue
        if url and url in posted_urls:
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)
        deduped.append(it)

    def kw_score(it: dict) -> float:
        text = f"{it.get('title','')} {it.get('summary','')}"
        s = float(sum(1 for kws in GENRE_KEYWORDS.values() for kw in kws if kw in text))
        # ソース信頼度の軽い補正
        s += SOURCE_TRUST_BONUS.get((it.get("source_name") or "").strip(), 0)
        # 除外・低優先テーマは大きく減点（実質除外）
        if any(t in text for t in EXCLUDED_TOPICS):
            s -= 5.0
        return s

    scored = sorted(((kw_score(it), it) for it in deduped),
                    key=lambda x: x[0], reverse=True)

    relevant = [it for s, it in scored if s > 0]
    if relevant:
        return relevant[:top_n]
    # 関連語ゼロでも投稿が完全に止まらないよう、除外テーマだけは外して先頭を残す
    fallback = [it for s, it in scored if s > -5.0]
    return fallback[:top_n]


# ---------------------------------------------------------------------------
# 5. 投稿候補の生成・スコアリング（Anthropic）
# ---------------------------------------------------------------------------

GENERATION_SYSTEM = """\
あなたは日本の政治・政策アカウントの編集長です。
このアカウントは「煽動Bot」ではありません。政治・政策ニュースを
『争点の構造』として図解で見せ、保存・引用リポスト・返信を取りにいくアカウントです。
狙うインプレッションは炎上ではなく、保存・引用・返信です。

絶対ルール（違反は不可）:
- 過度な煽り、陰謀論、差別的・攻撃的表現、個人攻撃、政党罵倒は禁止。
- 「目覚めよ」「終わってる」「売国」「反日」「ヤバい」のような低品質な煽りは禁止。
- 民族・国籍・宗教への攻撃、特定個人への攻撃は禁止。
- 数字は、与えられたニュース本文に根拠があるものだけ使う。
  根拠が無い数字は使わない（使ったら uses_unverified_number=true にする）。
- 選挙の投票先・投票方法・投票日時・投票所・選挙手続きに関する断定や誘導はしない。
- 本文に URL は入れない。ハッシュタグは原則なし（使うなら最大1）。絵文字なし。
- 無難な要約で終わらせない。「違和感」「争点」「対比」「数字」「構造」を使う。

投稿文の狙い:
政治投稿で狙うのは「読者を怒らせること」ではなく、
「読者が争点を一言で言い直したくなる状態」を作ること。
説明しすぎず、引用リポストされる“余白”を残す。

投稿文の構成（良い本文）:
1行目: 違和感のあるフック
2〜4行目: 政策構造・対比
5〜7行目: 数字・負担・制度の見え方
最後: 短い結論。ただし F=争点問いかけ型なら、最後は1つの問いにする。
条件: 120〜240字 / 3〜5ブロック / 1ブロック1〜2行 / 空行2〜3個まで / ポエム化しない。
本文は1行ずつの配列(tweet_lines)で返す。空行は空文字列 "" を要素として入れる。

投稿型は必ず1つ選ぶ:
A=対比型, B=数字インパクト型, C=誤解訂正型, D=争点整理型, E=未来警告型,
F=争点問いかけ型, G=誰が得するか型, H=負担の見える化型, I=海外比較型,
J=世代間構造型, K=政策の副作用型, L=ニュースの裏にある制度型。

F=争点問いかけ型のルール:
- 目的は返信・引用リポストを増やすこと。最後に読者が意見を言いたくなる問いを1つだけ置く。
- 単なる「あなたはどう思いますか？」は禁止。必ず争点（線引き・優先順位・負担配分）を提示してから聞く。
- 良い問いの例:
  ・これは「支援」なのか、「将来世代への負担移転」なのか。あなたはどう見ますか？
  ・税金と社会保険料、どちらの負担をより重く感じますか？
  ・限られた財源なら、給付拡大と負担抑制のどちらを優先すべきだと思いますか？
  ・表の争点は「誰に配るか」。本当の争点は「誰が払うか」。ここをもっと議論すべきだと思いますか？
- 悪い問いの例（禁止）:
  ・これヤバくないですか？ / 政府おかしくないですか？ / この政党終わってませんか？
  ・誰に投票すべきですか？ / 日本人はいつまで黙ってるんですか？
- F型の制約:
  ・問いは最後に1つだけ。煽りではなく争点整理として聞く。
  ・政党・政治家・個人への攻撃に誘導しない。
  ・投票先・投票行動・選挙手続きに関する問いは禁止。
  ・事件事故、災害、民族・国籍・宗教対立を招きやすいテーマでは F 型を使わない。

図解タイトル(image_title)は説明ではなく対比型・争点型にする:
- 良い例: 防衛費より大きい本丸 / 増税ではなく社会保険料 / 表の争点 vs 本当の争点
- 悪い例: 社会保障費について / 防衛費の推移

各スコアは0〜10で自己評価する:
- news: ニュース性
- controversy: 論争性（健全な議論を生む度合い。煽りの強さではない）
- data_ability: データ化・図解化しやすさ
- resonance: 政策関心層への刺さりやすさ
- save_value: 保存価値
- quote_likelihood: 引用リポストされやすさ
- early_reaction_likelihood: 投稿直後にいいね/返信/引用が付きやすいか
- quote_angle_strength: 引用リポストで一言言いたくなる“余白”があるか
- visual_clarity: 画像だけで意味が伝わるか
- policy_structure_value: 感想ではなく政策構造を整理できているか
- evergreen_value: 数日後にも読まれる価値があるか
- source_trust: ソースの信頼度
- ban_risk: 炎上・BANリスク（高いほど危険）
overall は ban_risk を踏まえた総合点(0〜10)。

必ず submit_candidates ツールを呼び、候補を3案提出する。地の文・説明は書かない。
"""

GENERATION_USER_TMPL = """\
次のニュースから、X投稿候補を必ず3案つくり、submit_candidates ツールで提出してください。
3案は投稿型を散らす（同じ型ばかりにしない）。

【F型の割り当て】
原則として、3案のうち1案は F=争点問いかけ型 にしてください。
ただし、このニュースが次のいずれかに当てはまる場合は F 型を使わず、
A〜E・G〜L の中から3案を作ってください。
- 選挙手続き（投票先・投票方法・投票日時・投票所など）に関わる
- 災害
- 事件事故
- 外国人・民族・宗教など、差別的な対立を招きやすい話題
- 事実確認が弱い（裏取りしにくい）ニュース
- 個人攻撃になりやすいニュース

ニュース:
title: {title}
summary: {summary}
source_name: {source_name}

優先ジャンル(高い順): 社会保障 / 税財政 / 少子化 / 安全保障 / エネルギー / 移民政策 / 教育 / 国会法案
このニュースが上記と無関係、または芸能・陰謀論・民族攻撃・宗教対立煽り等なら、
各案の ban_risk を高く、overall を低く、source_trust を低くしてください。
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
                        "type": {"type": "string", "enum": POST_TYPE_KEYS},
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
                                "early_reaction_likelihood": {"type": "integer"},
                                "quote_angle_strength": {"type": "integer"},
                                "visual_clarity": {"type": "integer"},
                                "policy_structure_value": {"type": "integer"},
                                "evergreen_value": {"type": "integer"},
                                "source_trust": {"type": "integer"},
                                "ban_risk": {"type": "integer"},
                            },
                            "required": ["news", "controversy", "data_ability",
                                         "resonance", "save_value",
                                         "quote_likelihood",
                                         "early_reaction_likelihood",
                                         "quote_angle_strength", "visual_clarity",
                                         "policy_structure_value", "evergreen_value",
                                         "source_trust", "ban_risk"],
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
        c.setdefault("pub_date", news_item.get("pub_date", ""))
        cleaned.append(c)
    return cleaned


# 実効スコアの加重（合計で割って 0〜10 相当に正規化する）
EFFECTIVE_WEIGHTS = {
    "quote_likelihood": 1.5,
    "quote_angle_strength": 1.4,
    "save_value": 1.3,
    "visual_clarity": 1.2,
    "policy_structure_value": 1.2,
    "data_ability": 1.1,
    "early_reaction_likelihood": 1.0,
    "controversy": 0.9,
    "news": 0.8,
    "evergreen_value": 0.8,
    "source_trust": 0.7,
}
_EFFECTIVE_WEIGHT_SUM = sum(EFFECTIVE_WEIGHTS.values())  # = 11.9（正規化係数）


def _num(scores: dict, key: str) -> float:
    try:
        return float(scores.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def effective_score(c: dict, history: list) -> float:
    """投稿可否に使う実効スコア。
    反応の取りやすさ（引用・保存・初速）を重く見た加重平均を 0〜10 に正規化し、
    BANリスク・未検証数字・ジャンル/型の連続・低信頼ソースで減点する。
    """
    scores = c.get("scores") or {}
    ttype = c.get("type", "")
    ban = int(_num(scores, "ban_risk"))

    # 安全ゲート（維持方針）：BANリスクが閾値以上なら候補から外す。
    # ※ 運用方針として「過度な煽り・差別・個人攻撃は禁止」を守るため、
    #   ここは加点減点ではなく失格扱いにしている。
    if ban >= BAN_RISK_BLOCK:
        return -10.0

    # --- 加重ベース（0〜10 相当に正規化） ---
    weighted = sum(w * _num(scores, k) for k, w in EFFECTIVE_WEIGHTS.items())
    base = weighted / _EFFECTIVE_WEIGHT_SUM

    src_trust = _num(scores, "source_trust")
    quote = _num(scores, "quote_likelihood")
    angle = _num(scores, "quote_angle_strength")

    # --- ペナルティ ---
    # 未検証数字（誤情報の自動投稿を防ぐ）
    if c.get("uses_unverified_number"):
        base -= 4.0
    # ban_risk が閾値未満でも、やや高ければ軽く減点
    if ban >= 5:
        base -= 1.0
    # 直近3件と同ジャンルが続くなら減点（ジャンルローテーション）
    if c.get("genre") in recent_genres(history, 3):
        base -= 1.5
    # 直近5件に同じ type が2回以上あれば減点（型の偏り防止）
    if ttype and recent_types(history, 5).count(ttype) >= 2:
        base -= 1.0
    # 低信頼ソースは減点
    if src_trust <= 4:
        base -= 1.0

    # --- F型（争点問いかけ型）の追加処理 ---
    if ttype == "F":
        # 反応が取れそうな良問のみ加点
        if quote >= 7 and angle >= 7 and ban <= 4:
            base += 0.7
        # F型でBANリスクがやや高いものは敬遠（重ねて減点）
        if ban >= 5:
            base -= 1.0
        # F型が直近10投稿で3回以上なら、増えすぎ防止で減点
        if recent_types(history, 10).count("F") >= 3:
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


def _wrap_text(draw, text, font, max_px: int, max_lines: int) -> list:
    """ピクセル幅に基づいて CJK 文字を折り返す（textwrap の固定文字数より溢れにくい）。
    max_lines を超える分は最後の行末に「…」を付けて打ち切る。
    """
    text = " ".join(str(text).split())
    if not text or max_px <= 0:
        return []
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_px:
            cur += ch
            continue
        if cur:
            lines.append(cur)
            cur = ch
        else:  # 1文字でも幅を超える稀なケースは強制配置
            lines.append(ch)
            cur = ""
        if len(lines) >= max_lines:
            cur = ""
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)

    # 文字が残っている＝打ち切ったので、最後の行に「…」を付ける
    consumed = sum(len(l) for l in lines)
    if consumed < len(text) and lines:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_px:
            last = last[:-1]
        lines[-1] = (last + "…") if last else "…"
    return lines[:max_lines]


def render_diagram(c: dict, out_path: Path) -> Path:
    """候補から図解PNGを生成する。
    - 文字はピクセル幅で折り返し、はみ出しを防ぐ
    - 対比カラムの item も折り返す（1項目=最大2行・各カラム最大4項目）
    - image_title / image_conclusion は最大2行
    - F型の結論ラベルは「つまり：」ではなく「問い：」にする
    - DIAGRAM_PRESET=landscape / vertical でサイズを切り替え（既定 landscape）
    """
    from PIL import Image, ImageDraw

    # プロセス起動後に環境変数が変わっても追従できるよう、ここで再取得する。
    W, H = get_diagram_size()

    bg = (17, 22, 30)
    fg = (236, 240, 244)
    sub = (150, 160, 172)
    accent = (96, 165, 250)
    warn = (248, 180, 90)
    ask = (134, 222, 170)  # F型「問い：」用のアクセント色

    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    f_title = _load_font(54)
    f_label = _load_font(30)
    f_item = _load_font(28)
    f_concl = _load_font(32)
    f_src = _load_font(20)

    margin = 56
    content_w = W - margin * 2
    ttype = c.get("type", "")

    # 結論（問い）ボックス用に下部を確保
    concl_reserved = 168
    body_bottom = H - concl_reserved

    y = margin

    # --- タイトル（最大2行） ---
    title = c.get("image_title") or c.get("title") or ""
    for line in _wrap_text(d, title, f_title, content_w, max_lines=2):
        d.text((margin, y), line, font=f_title, fill=fg)
        y += 64
    y += 10
    d.line([(margin, y), (W - margin, y)], fill=accent, width=3)
    y += 28

    left = c.get("image_left")
    right = c.get("image_right")

    if isinstance(left, dict) and isinstance(right, dict):
        # --- 対比レイアウト（左右カラム） ---
        gap = 40
        col_w = (content_w - gap) // 2
        x_left = margin
        x_right = margin + col_w + gap
        for x, col in ((x_left, left), (x_right, right)):
            yy = y
            # ラベル（最大1行）
            for lab in _wrap_text(d, col.get("label", ""), f_label, col_w, max_lines=1):
                d.text((x, yy), lab, font=f_label, fill=accent)
            yy += 48
            items = [str(it) for it in (col.get("items") or []) if str(it).strip()][:4]
            for it in items:
                wrapped = _wrap_text(d, it, f_item, col_w - 24, max_lines=2)
                for i, wl in enumerate(wrapped):
                    prefix = "・" if i == 0 else "　"
                    d.text((x + 8, yy), f"{prefix}{wl}", font=f_item, fill=fg)
                    yy += 40
                yy += 6
                if yy > body_bottom:
                    break
    else:
        # --- 箇条書きレイアウト（最大4項目・各2行） ---
        yy = y
        points = [str(it) for it in (c.get("image_points") or []) if str(it).strip()][:4]
        for it in points:
            wrapped = _wrap_text(d, it, f_item, content_w - 24, max_lines=2)
            for i, wl in enumerate(wrapped):
                prefix = "・" if i == 0 else "　"
                d.text((margin + 8, yy), f"{prefix}{wl}", font=f_item, fill=fg)
                yy += 42
            yy += 8
            if yy > body_bottom:
                break

    # --- 結論 / 問い（最大2行） ---
    concl = (c.get("image_conclusion") or "").strip()
    if concl:
        box_y = H - 150
        d.line([(margin, box_y - 14), (W - margin, box_y - 14)], fill=sub, width=1)
        if ttype == "F":
            label_text, label_color = "問い：", ask
        else:
            label_text, label_color = "つまり：", warn
        d.text((margin, box_y), label_text, font=f_label, fill=label_color)
        cy = box_y + 44
        for line in _wrap_text(d, concl, f_concl, content_w, max_lines=2):
            d.text((margin, cy), line, font=f_concl, fill=fg)
            cy += 40

    # --- 出典（維持） ---
    src = f"出典: {c.get('source_name','')}"
    bbox = d.textbbox((0, 0), src, font=f_src)
    d.text((W - margin - (bbox[2] - bbox[0]), H - 36), src, font=f_src, fill=sub)

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

    target_news = prefilter_news(news_items)
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
        # --- 学習材料（効いた型・問い・スコアの振り返り用） ---
        "tweet_text": best.get("tweet_text", ""),
        "hook": best.get("hook", ""),
        "image_title": best.get("image_title", ""),
        "image_conclusion": best.get("image_conclusion", ""),
        "scores": best.get("scores", {}),
        "overall": best.get("overall"),
        "effective_score": round(float(best_score), 2),
        "source_name": best.get("source_name", ""),
        "pub_date": best.get("pub_date", ""),
        "decision_reason": best.get("decision_reason", ""),
    })
    log("[INFO] Slot and post history recorded.")


if __name__ == "__main__":
    main()
