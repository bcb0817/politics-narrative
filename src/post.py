#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
politics-narrative — X 自動投稿Bot（政治ニュース・意見図解／テキスト専用）

運用方針（ローカル運用）:
- ローカルPC / ローカルサーバー上で local_bot.py（daemon / once）から起動する
- Python側で JST 現在時刻を取得して投稿可否を判定する
- 投稿スロットは24時間対象。毎時 07分・37分の1日48スロット
- 各スロットには POST_WINDOW_MINUTES（既定20分）の許容幅を持たせる（起動遅延の吸収）
- 時間帯（深夜・早朝）によるスキップは行わない
- 1スロット1投稿まで
- 定期投稿・手動実行ともに diagram モード固定（文章で争点を図解する。画像は使わない）
- linkモード / test投稿 / normal / dry-run / ランダムスケジュール は廃止（復活させない）
- 投稿内容はニュース事実・政策構造・保守寄りの批判的意見を、文章で図解する
- 差別、脅迫、暴力扇動、標的型嫌がらせ、虚偽断定、選挙妨害、個人情報公開は禁止
- POST_ENABLED=false のときは X への実投稿だけを止める（安全弁）

状態・出力・ログ:
- 状態:  STATE_DIR  (既定: リポジトリ直下 data/)   posted_slots.json / posted_urls.json
- ログ:  LOG_DIR    (既定: リポジトリ直下 logs/)    bot.log / post_attempts.jsonl / errors.jsonl

使い方:
    python local_bot.py once    # 推奨（.env読込・ディレクトリ作成込み）
    python post.py diagram      # 直接実行も可（diagram=文章による争点図解）
    FORCE_POST=true python post.py diagram   # 強制投稿（スロット判定なし・diagramのみ）
"""

import os
import sys
import json
import re
import shutil
from pathlib import Path
from datetime import datetime, time, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests

from publishing_policy import (
    CRITIQUE_AXES,
    HOOK_TYPES,
    POST_TYPE_DAILY_LIMITS,
    classify_critique_axis,
    classify_hook_type,
    classify_post_type,
    normalize_topic_key,
    post_type_quota_reached,
    pre_generation_skip_reason,
    stagnation_fallback_active,
    topic_cooldown_skip_reason,
)
from x_attention import final_news_score
from model_router import ModelRouter, is_auth_error
from openai_usage import (
    load_usage_state,
    record_usage,
    today_usage,
)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

JST = ZoneInfo("Asia/Tokyo")
UTC = ZoneInfo("UTC")

SRC_DIR = Path(__file__).resolve().parent
ROOT_DIR = SRC_DIR.parent

# Windowsコンソール(cp932)での日本語ログ文字化け・例外を防ぐ
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _load_env_file(path: Path) -> None:
    """リポジトリ直下の .env を読み込む（標準ライブラリのみの簡易ローダー）。
    - 既に設定済みの環境変数は上書きしない（local_bot.py や手動exportを優先）
    - `KEY=VALUE` 形式。#始まりと空行は無視。前後の引用符は剥がす。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            # 明示的な実行時上書き（安全なdry-run等）を優先する。
            os.environ.setdefault(key, value)


# 環境変数ベースの定数を確定させる前に .env を読む
_load_env_file(ROOT_DIR / ".env")


def _resolve_dir(env_name: str, default: str) -> Path:
    """STATE_DIR / LOG_DIR を解決する。
    相対パスは cwd ではなくリポジトリ直下基準にする（cwd問題の回避）。"""
    raw = os.environ.get(env_name, "").strip() or default
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT_DIR / p
    p.mkdir(parents=True, exist_ok=True)
    return p


STATE_DIR = _resolve_dir("STATE_DIR", "data")
LOG_DIR = _resolve_dir("LOG_DIR", "logs")

POSTED_SLOTS_FILE = STATE_DIR / "posted_slots.json"     # 投稿成功したslot
ATTEMPTED_SLOTS_FILE = STATE_DIR / "attempted_slots.json"  # 投稿トライ済みslot（成功＋低スコアskip等）
POSTED_URLS_FILE = STATE_DIR / "posted_urls.json"       # 投稿履歴（post_history）
OPENAI_USAGE_FILE = STATE_DIR / "openai_usage.json"      # OpenAI推定使用量・予算管理
OPENAI_USAGE_HISTORY_DIR = STATE_DIR / "openai_usage_history"
OPENAI_PRICING_FILE = ROOT_DIR / "config" / "openai_model_pricing.json"
RECENT_TOPICS_FILE = STATE_DIR / "recent_topics.json"    # トピック冷却状態
BOT_LOG_FILE = LOG_DIR / "bot.log"


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes")


# POST_ENABLED 安全弁: true のときだけ X へ実投稿する。
# 未設定・false の場合、候補生成・スコア判定までは行うが投稿はしない。
# これは旧 dry-run モードの復活ではない（環境変数による安全弁）。
POST_ENABLED = _env_bool("POST_ENABLED", "false")


# THREAD_ENABLED: true のときだけ、親投稿に補足の返信（スレッド）をぶら下げる。
# 既定 false = 単発投稿のみ（返信で分割しない）。
THREAD_ENABLED = _env_bool("THREAD_ENABLED", "false")

# MARK_DISABLED_RUN_AS_ATTEMPTED: POST_ENABLED=false の run を attempted に記録するか。
# 既定 false = ローカルテスト/dry-runでslotを消費しない（本番前にslotが処理済みにならない）。
MARK_DISABLED_RUN_AS_ATTEMPTED = _env_bool("MARK_DISABLED_RUN_AS_ATTEMPTED", "false")


def _migrate_legacy_state() -> None:
    """旧配置 src/posted_slots.json / src/posted_urls.json が残っていて、
    新配置 STATE_DIR 側にまだ無い場合、初回だけ自動でコピー移行する。"""
    pairs = [
        (SRC_DIR / "posted_slots.json", POSTED_SLOTS_FILE),
        (SRC_DIR / "attempted_slots.json", ATTEMPTED_SLOTS_FILE),
        (SRC_DIR / "posted_urls.json", POSTED_URLS_FILE),
    ]
    for legacy, new in pairs:
        try:
            if legacy.resolve() == new.resolve():
                continue
            if legacy.exists() and not new.exists():
                shutil.copy2(legacy, new)
                print(f"[情報] 旧形式の状態ファイルを移行しました: {legacy} -> {new}", flush=True)
        except Exception as e:
            print(f"[警告] 旧形式の状態ファイルを移行できませんでした（{legacy}）: {e}", flush=True)


_migrate_legacy_state()

# 投稿スロット（JST）— 24時間対象。
# スロット間隔は SLOT_INTERVAL_MINUTES で可変（既定30分=1日48スロット）。
# 45分なら1日32スロット（45×32=1440で1日に綺麗に割り切れる）。
# 1440 を割り切る値のみ許可（割り切れない値は既定30にフォールバック）。
def _get_slot_interval_minutes() -> int:
    try:
        v = int(os.getenv("SLOT_INTERVAL_MINUTES", "30"))
    except (TypeError, ValueError):
        return 30
    if v < 1 or 1440 % v != 0:
        print(f"[WARN] SLOT_INTERVAL_MINUTES={v} は1440を割り切れないため既定30を使用", flush=True)
        return 30
    return v


SLOT_INTERVAL_MINUTES = _get_slot_interval_minutes()


# 投稿する時間帯（JST）。インプレッションは「誰も見ていない時間の投稿」で大きく下がるため、
# 人がXを見ている時間帯だけに投稿を絞る。書式: "7-9,12-13,18-23"（時のみ・両端含む）。
# 空なら24時間投稿（旧挙動）。
def _parse_active_hours() -> set:
    raw = os.environ.get("ACTIVE_HOURS", "").strip()
    if not raw:
        return set(range(24))
    hours = set()
    try:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                a, b = int(a), int(b)
                for h in range(min(a, b), max(a, b) + 1):
                    hours.add(h % 24)
            else:
                hours.add(int(part) % 24)
    except (TypeError, ValueError):
        print(f"[WARN] ACTIVE_HOURS='{raw}' の解析に失敗。24時間投稿にフォールバック", flush=True)
        return set(range(24))
    return hours or set(range(24))


ACTIVE_HOURS = _parse_active_hours()


def build_post_slots() -> list:
    """1日分の "HH:MM" スロット文字列を SLOT_INTERVAL_MINUTES 間隔で生成する。
    ACTIVE_HOURS が設定されていれば、その時間帯のスロットだけ残す。
    00:00 起点。現在時刻に依存しない（純粋関数）。
    """
    step = SLOT_INTERVAL_MINUTES
    slots = [f"{(m // 60):02d}:{(m % 60):02d}" for m in range(0, 1440, step)]
    return [s for s in slots if int(s[:2]) in ACTIVE_HOURS]


POST_SLOTS = build_post_slots()
assert POST_SLOTS, "POST_SLOTS is empty (ACTIVE_HOURS/SLOT_INTERVAL_MINUTES を確認)"

# スロット開始から +POST_WINDOW_MINUTES 分まで投稿許可（起動遅延の吸収）。
# 次スロットと重複しないよう スロット間隔-1 を上限にクランプ。
def _get_post_window_minutes() -> int:
    try:
        v = int(os.getenv("POST_WINDOW_MINUTES", "20"))
    except (TypeError, ValueError):
        v = 20
    return max(1, min(v, SLOT_INTERVAL_MINUTES - 1))


POST_WINDOW_MINUTES = _get_post_window_minutes()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# catch-up: GitHub Actions の schedule は確実に30分ごとに発火するとは限らないため、
# 過去 CATCH_UP_HOURS 時間以内の「未処理スロット」を後続runで回収する。
# ただし連投・Bot臭を避けるため、1runの投稿トライは MAX_POSTS_PER_RUN 件まで。
CATCH_UP_HOURS = max(1, _env_int("CATCH_UP_HOURS", 24))
MAX_POSTS_PER_RUN = max(0, _env_int("MAX_POSTS_PER_RUN", 1))

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

# 投稿タイプ。Structured Outputs schema と同じ単一情報源を使う。
POST_TYPES = {
    "breaking_news": "速報。変化と今後の確認点を簡潔に示す",
    "issue_diagram": "文章・改行・矢印・対比で争点を図解する",
    "strong_opinion": "政策原則から明確な意見を示す",
    "comparison_factcheck": "複数案・一次資料・改正前後を比較する",
    "morning_evening_digest": "朝刊・夕刊として複数ニュースを整理する",
}
POST_TYPE_KEYS = list(POST_TYPES.keys())

# スコア閾値（section 17）
SCORE_POST_ALWAYS = 9   # 9-10 必ず投稿
SCORE_POST = 7          # 7-8 投稿（参考用。投稿可否の最終判定は MIN_POST_SCORE）
SCORE_SAVE = 5          # 5-6 保存のみ（参考用）
# 4以下は投稿しない
BAN_RISK_BLOCK = 7      # 炎上・BANリスクがこの値以上なら他が高くても投稿しない


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# 投稿可否の最終しきい値: effective_score がこの値以上で投稿可。
MIN_POST_SCORE = _env_float("MIN_POST_SCORE", 6.3)
MAX_DAILY_POSTS = max(0, _env_int("MAX_DAILY_POSTS", 16))
MIN_POST_INTERVAL_MINUTES = max(0, _env_int("MIN_POST_INTERVAL_MINUTES", 45))
TOPIC_COOLDOWN_HOURS = max(0.0, _env_float("TOPIC_COOLDOWN_HOURS", 4.0))
LOW_QUALITY_FALLBACK_HOURS = max(0.0, _env_float("LOW_QUALITY_FALLBACK_HOURS", 3.0))

# 古いニュースの自動投稿を避ける。pubDateを解釈できる記事だけ厳密に判定し、
# 時刻不明の記事は候補として残す。
MAX_NEWS_AGE_HOURS = max(1, _env_int("MAX_NEWS_AGE_HOURS", 12))

# QUALITY_GATE_ENABLED: 品質スコア(MIN_POST_SCORE)による投稿審査を行うか。
# 既定 false = 審査せずどんどん投稿し、実際のインプレッション実績(report)で改善する運用。
# true にすると従来どおり MIN_POST_SCORE 未満をskipする。
# 注意: BANリスク判定(BAN_RISK_BLOCK)はこの設定に関係なく常時有効。
QUALITY_GATE_ENABLED = os.environ.get(
    "QUALITY_GATE_ENABLED", "false").strip().lower() in ("true", "1", "yes")
# overall救済ルールのしきい値（overall>=8 / effective>=6.2 / ban_risk<=2 で投稿可）
RESCUE_OVERALL_MIN = 8
RESCUE_EFFECTIVE_MIN = 6.2
RESCUE_BAN_RISK_MAX = 2


def _score_gate_allows(
    effective_score: float,
    force_bypass: bool,
    rescue_rule_applied: bool,
    stagnation_fallback: bool,
) -> bool:
    """Relax only the performance score; safety checks run before this gate."""
    return bool(
        force_bypass
        or stagnation_fallback
        or effective_score >= MIN_POST_SCORE
        or rescue_rule_applied
    )

# ---------------------------------------------------------------------------
# ログ
# ---------------------------------------------------------------------------
# 標準出力は維持しつつ、ローカル運用向けに logs/bot.log にも追記する。
# 注意: APIキー・Secretはログに出さない。

def log(msg: str) -> None:
    print(msg, flush=True)
    try:
        with open(BOT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass  # ログファイル書き込み失敗で本処理を止めない


def log_jsonl(filename: str, record: dict) -> None:
    """logs/ 配下の JSONL ファイル（post_attempts.jsonl / errors.jsonl）に1行追記する。"""
    try:
        rec = dict(record)
        rec.setdefault("ts_jst", datetime.now(JST).isoformat())
        with open(LOG_DIR / filename, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def log_attempt(record: dict) -> None:
    log_jsonl("post_attempts.jsonl", record)


def log_error(record: dict) -> None:
    log_jsonl("errors.jsonl", record)


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
    """現在JST時刻が、いずれかのスロット開始から +POST_WINDOW_MINUTES 分以内なら、
    そのスロット情報を返す。24時間対象（深夜・早朝のスキップはしない）。
    戻り値: (slot, slot_key, slot_dt, window_end) / 該当なしは (None, None, None, None)
    ※ 該当なし＝「30分スロットの許可ウィンドウ外」であって「時間帯による禁止」ではない。
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


def slot_key_for(slot_dt: datetime, slot: str) -> str:
    """そのスロット自身の日付からslot_keyを作る（過去日のスロットにも対応）。
    例: 2026-06-26 09:07 のスロット -> '2026-06-26_09:07'
    """
    return f"{slot_dt.date().isoformat()}_{slot}"


def slot_datetimes_in_window(now_jst: datetime, hours: int) -> list:
    """now から過去 hours 時間以内に「開始済み」のスロット datetime を
    古い順に返す。各要素は (slot_str, slot_dt)。catch-up探索に使う。
    （POST_WINDOW_MINUTES は使わない＝ウィンドウ外でも未処理なら対象にする）
    """
    window_start = now_jst - timedelta(hours=hours)
    out = []
    d = window_start.date()
    end_date = now_jst.date()
    while d <= end_date:
        for slot in POST_SLOTS:
            hh, mm = map(int, slot.split(":"))
            slot_dt = datetime.combine(d, time(hh, mm), tzinfo=JST)
            # 開始済み(slot_dt <= now) かつ 過去hours以内(window_start < slot_dt)
            if window_start < slot_dt <= now_jst:
                out.append((slot, slot_dt))
        d += timedelta(days=1)
    out.sort(key=lambda x: x[1])  # 古い順
    return out


def find_catch_up_slot(now_jst: datetime, attempted: set, hours: int):
    """catch-up対象のうち最も古い『未トライ(unattempted)』スロットを1件返す。

    以前は posted_slots.json を基準にしていたが、低スコアskip時は posted に
    記録しないため、同じ低スコアslotが何度も選ばれ続ける詰まりが起きていた。
    投稿トライ済み(attempted_slots.json)を基準にすることで、低スコアskipした
    slotも「処理済み」とみなし、次の未トライslotへ進める。

    戻り値: (slot, slot_key, slot_dt, window_slots, unattempted)
            未トライが無ければ slot 系は None。
    """
    window_slots = slot_datetimes_in_window(now_jst, hours)
    unattempted = [(s, dt) for (s, dt) in window_slots
                   if slot_key_for(dt, s) not in attempted]
    if not unattempted:
        return None, None, None, window_slots, unattempted
    slot, slot_dt = unattempted[0]  # 最古
    return slot, slot_key_for(slot_dt, slot), slot_dt, window_slots, unattempted


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
    if not isinstance(posted, list):
        posted = []
    if slot_key not in posted:
        posted.append(slot_key)
    # 古いキーが膨らみすぎないよう直近300件に丸める
    _save_json(POSTED_SLOTS_FILE, posted[-300:])


def is_slot_attempted(slot_key: str) -> bool:
    attempted = _load_json(ATTEMPTED_SLOTS_FILE, [])
    return isinstance(attempted, list) and slot_key in attempted


def mark_slot_attempted(slot_key: str) -> None:
    """投稿トライ済みslotを記録する（投稿成功・低スコアskip等）。
    一時失敗（post_to_x_failed 等）では呼ばないこと。"""
    attempted = _load_json(ATTEMPTED_SLOTS_FILE, [])
    if not isinstance(attempted, list):
        attempted = []
    if slot_key not in attempted:
        attempted.append(slot_key)
    # 肥大化防止のため直近500件に丸める
    _save_json(ATTEMPTED_SLOTS_FILE, attempted[-500:])


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


def load_recent_topics() -> list:
    rows = _load_json(RECENT_TOPICS_FILE, [])
    return rows if isinstance(rows, list) else []


def save_recent_topic(record: dict) -> None:
    rows = load_recent_topics()
    rows.append(record)
    _save_json(RECENT_TOPICS_FILE, rows[-500:])


def recent_genres(history: list, n: int = 3) -> list:
    return [h.get("genre") for h in history[-n:] if h.get("genre")]


def recent_types(history: list, n: int = 5) -> list:
    """直近 n 件の投稿タイプを返す。型の偏り抑制に使う。"""
    return [h.get("post_type") or h.get("type") for h in history[-n:]
            if h.get("post_type") or h.get("type")]


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


def gather_candidate_news(include_x: bool = True) -> list:
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
        raw = fetch_all_items(include_x=include_x)
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
            "discovered_via": list(it.get("discovered_via") or ["rss"]),
            "x_attention_score": float(it.get("x_attention_score", 0) or 0),
            "x_post_count": int(it.get("x_post_count", 0) or 0),
            "x_unique_accounts": int(it.get("x_unique_accounts", 0) or 0),
            "x_velocity_score": float(it.get("x_velocity_score", 0) or 0),
            "x_topic_key": (it.get("x_topic_key") or "").strip(),
        })
    return items


_GENERIC_X_TERMS = {"政治", "政府", "国会", "選挙", "日本", "ニュース"}


def _topic_terms(text: str) -> set:
    """クラスタリング用の政策語・固有の引用語・ハッシュタグを抽出する。"""
    text = text or ""
    known = set(_DEFAULT_IMPORTANT_KEYWORDS)
    known.update(kw for values in GENRE_KEYWORDS.values() for kw in values)
    terms = {kw for kw in known if len(kw) >= 2 and kw in text}
    terms.update(re.findall(r"[#＃]([\w一-龥ぁ-んァ-ヶー]{2,30})", text))
    terms.update(x.strip() for x in re.findall(r"[「『]([^」』]{2,24})[」』]", text))
    return {term for term in terms if term not in _GENERIC_X_TERMS}


def enrich_x_topics(items: list) -> list:
    """X投稿を共通話題で束ね、RSSとの一致と注目理由を付与する。

    Xだけで主張されている内容は事実として扱わない。既定では、RSS記事と
    話題語が一致したXクラスタだけを候補として残す。
    """
    rss_items = [it for it in items if it.get("source_name") != "X検索"]
    x_items = [it for it in items if it.get("source_name") == "X検索"]
    if not x_items:
        return items

    clusters = []
    for item in sorted(x_items, key=lambda x: x.get("x_trend_score", 0), reverse=True):
        terms = _topic_terms(f"{item.get('title','')} {item.get('summary','')}")
        target = next((c for c in clusters if terms and c["terms"] & terms), None)
        if target is None:
            clusters.append({"terms": set(terms), "items": [item]})
        else:
            target["items"].append(item)
            target["terms"].update(terms)

    require_check = _env_bool("X_REQUIRE_EXTERNAL_CORROBORATION", "true")
    enriched = []
    for cluster in clusters:
        members = cluster["items"]
        terms = cluster["terms"]
        representative = dict(members[0])
        frequency = {
            term: sum(term in _topic_terms(m.get("title", "")) for m in members)
            for term in terms
        }
        common = [term for term, count in sorted(frequency.items(), key=lambda x: (-x[1], x[0])) if count >= 2]
        matches = []
        for rss in rss_items:
            rss_terms = _topic_terms(f"{rss.get('title','')} {rss.get('summary','')}")
            overlap = terms & rss_terms
            if overlap:
                matches.append((len(overlap), rss))
        matches.sort(key=lambda x: x[0], reverse=True)
        verified = [m[1] for m in matches[:3]]
        if require_check and not verified:
            continue

        total_engagement = sum(int((m.get("x_metrics") or {}).get("engagement", 0) or 0) for m in members)
        reason = (
            f"関連投稿{len(members)}件で合計反応{total_engagement}。"
            f"短時間の反応速度と複数投稿での共通言及により注目。"
        )
        sources = [
            {"name": v.get("source_name", ""), "title": v.get("title", ""), "url": v.get("url", "")}
            for v in verified
        ]
        verification_text = "、".join(f"{s['name']}「{s['title']}」" for s in sources)
        representative["summary"] = (
            f"{representative.get('summary','')} {reason}"
            f"共通話題: {', '.join(common or sorted(terms)[:5]) or '抽出なし'}。"
            f"外部確認: {verification_text or '一致する外部資料なし'}。"
        ).strip()
        representative["x_cluster_size"] = len(members)
        representative["x_common_topics"] = common or sorted(terms)[:5]
        representative["x_why_trending"] = reason
        representative["verification_sources"] = sources
        representative["externally_corroborated"] = bool(sources)
        enriched.append(representative)

    return rss_items + enriched


# ソース別の軽い信頼度補正（prefilter のスコアに加点）
SOURCE_TRUST_BONUS = {
    "内閣府公式": 3,
    "NHK政治": 2,
    "NHK経済": 2,
    "NHK国際": 1,
    "Yahoo!ニュース政治": 1,
    "Yahoo!ニュース経済": 1,
    "Yahoo!ニュース国際": 0,
    # X上の投稿は話題発見用。一次報道と同等の信頼度加点はしない。
    "X検索": 0,
}

SOURCE_RELIABILITY = {
    "内閣府公式": 10.0,
    "NHK政治": 9.0,
    "NHK経済": 9.0,
    "NHK国際": 9.0,
    "Yahoo!ニュース政治": 7.5,
    "Yahoo!ニュース経済": 7.5,
    "Yahoo!ニュース国際": 7.0,
}

MAJOR_IMPACT_TERMS = (
    "法案", "成立", "採決", "判決", "公式発表", "制度改正", "外交合意",
    "防衛", "安全保障", "関税", "災害", "辞任", "逮捕", "開戦", "停戦",
)


def prefilter_news(items: list, top_n: int = None, allow_low_quality: bool = False) -> list:
    """優先ジャンルのキーワードでニュースを採点し、関連の高い上位だけ残す。
    LLM呼び出し回数を抑え、関連度も上げる。

    追加処理:
    - source_name による軽い信頼度補正
    - title 重複の除去
    - 投稿履歴(posted_urls.json)にある source_url は除外
    - EXCLUDED_TOPICS に該当するものは大きく減点（実質除外）
    - top_n は環境変数 PREFILTER_TOP_N で変更可能（未指定なら1）
    """
    if top_n is None:
        try:
            top_n = int(os.environ.get("PREFILTER_TOP_N", "1"))
        except ValueError:
            top_n = 1
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

    now_utc = datetime.now(UTC)

    def age_hours(it: dict):
        raw = (it.get("pub_date") or "").strip()
        if not raw:
            return None
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return max(0.0, (now_utc - dt.astimezone(UTC)).total_seconds() / 3600.0)
        except Exception:
            return None

    def kw_score(it: dict) -> float:
        text = f"{it.get('title','')} {it.get('summary','')}"
        keyword_hits = sum(1 for kws in GENRE_KEYWORDS.values() for kw in kws if kw in text)
        relevance = min(10.0, keyword_hits * 1.25 + (2.0 if any(t in text for t in MAJOR_IMPACT_TERMS) else 0.0))
        age = age_hours(it)
        if age is not None:
            if age > MAX_NEWS_AGE_HOURS:
                return -999.0
            freshness = max(0.0, 10.0 * (1.0 - age / max(MAX_NEWS_AGE_HOURS, 1)))
        else:
            freshness = 4.0
        source = (it.get("source_name") or "").strip()
        reliability = SOURCE_RELIABILITY.get(source, 6.0)
        x_attention = float(it.get("x_attention_score", 0) or 0)
        x_weight = max(0.0, min(_env_float("X_SEARCH_WEIGHT", 0.25), 0.50))
        s = final_news_score(relevance, freshness, x_attention, reliability, x_weight)
        it["news_relevance_score"] = round(relevance, 3)
        it["freshness_score"] = round(freshness, 3)
        it["source_reliability_score"] = round(reliability, 3)
        it["final_news_score"] = s
        # 除外・低優先テーマは大きく減点（実質除外）
        if any(t in text for t in EXCLUDED_TOPICS):
            s -= 5.0
        return s

    scored = sorted(((kw_score(it), it) for it in deduped),
                    key=lambda x: x[0], reverse=True)

    minimum_final = 0.0 if allow_low_quality else 3.0
    minimum_relevance = 0.5 if allow_low_quality else 2.0
    relevant = [
        it for s, it in scored
        if s >= minimum_final
        and float(it.get("news_relevance_score", 0) or 0) >= minimum_relevance
    ]
    if relevant:
        selected = relevant[:top_n]
        for item in selected:
            log(
                f"[INFO] Candidate score: final={item.get('final_news_score', 0)} "
                f"relevance={item.get('news_relevance_score', 0)} "
                f"freshness={item.get('freshness_score', 0)} "
                f"x_attention={item.get('x_attention_score', 0)} "
                f"reliability={item.get('source_reliability_score', 0)}"
            )
        return selected
    # 関連性の弱いニュースを無理に投稿しない。
    return []


# ---------------------------------------------------------------------------
# 5. 投稿候補の生成・スコアリング（OpenAI）
# ---------------------------------------------------------------------------

GENERATION_SYSTEM = """\
あなたは日本の政治ニュースアカウントの編集長です。
このアカウントは、ニュースの事実を起点に、政策の構造と行政監視の観点からの批判的意見を
「文章の図解」として短く提示します。画像は一切使いません。
狙う反応は、炎上ではなく保存・引用リポスト・返信です。

【編集上の立ち位置】
- 非党派の行政監視アカウント。特定政党の宣伝や、思想・感情の誘導を目的にしない。
- 与党・野党を問わず、同じ原則で政策を評価する。
- 優先する批判軸は次のとおり:
  1. 減税・社会保険料抑制・小さな政府・行政効率
  2. 財政規律・受益と負担の一致・世代間公平
  3. 国益・主権・防衛・経済安全保障
  4. 原発を含む現実的なエネルギー安全保障
  5. 法秩序・国境管理・移民政策の制度設計と社会的コスト
  6. 家族形成・出生率・子育て世帯の可処分所得
  7. 国内産業・技術・食料安全保障・サプライチェーン
  8. 補助金・有識者会議・官僚機構の不透明さと責任所在
  9. 司法の公正・適正手続・冤罪防止・捜査機関の説明責任
- ニュースに根拠がある範囲で、コスト、矛盾、副作用、インセンティブ、国益への影響を批判する。
- 民族・国籍・宗教そのものを攻撃しない。批判対象は政策、制度、組織、意思決定、行為に限定する。

絶対ルール（違反は不可）:
- 事実と意見を混同しない。ニュースに書かれた事実を誇張・創作しない。
- 民族・国籍・宗教・性別・障害などの属性に対する差別や攻撃は禁止。
- 脅迫、暴力の扇動、特定個人への執拗な嫌がらせ、個人情報の公開は禁止。
- 未確認の疑惑、犯罪、数字を事実として断定しない。
- 投票方法・日時・投票所について虚偽を述べたり、投票を妨害したりしない。
- 政策、政党、公人、行政判断への厳しい批判は、確認できる事実に基づき、
  属性攻撃・脅迫・嫌がらせ・虚偽断定を含まない限り許容する。
- 数字は与えられたニュース本文に根拠があるものだけ使う。
  根拠がない数字を使った場合は uses_unverified_number=true にする。
- 選挙について論評する場合も、投票方法・日時・投票所の虚偽や投票妨害はしない。
- 本文にURLは入れない。ハッシュタグは原則なし、最大1個。
- 絵文字は必須。内容に合う異なる絵文字を2〜5個使い、同じ絵文字の連打はしない。
- 見出し、箇条書き、空行を使って視線の流れを作る。絵文字は見出しや転換点の目印として使う。
- 🌷➡️🚨 のように、状況・変化・警告を表す絵文字を意味の通る順序で使ってよい。
- 本文に内部ラベルや制作過程を出さない。「A型」「F型」「post_type」「hook_type」「critique_axis」「decision_reason」「保守寄りの視点からは」「右派の観点からは」「AIとして」「編集部として」は禁止。
- 「表の争点→本当の争点」などの定型句は、実際に二つの異なる争点を具体語で示せる場合だけ使う。
- 中立的な要約だけで終わらせない。必ず監視軸を1つ選び、根拠に基づく厳しい意見を示す。
- ただし、ニュースから批判を正当化できない場合は無理に断定せず、論点・懸念として示す。
- 批判軸はニュースとの因果関係が明確なものだけを選ぶ。右寄りに見せるためだけに財源・移民・安全保障を持ち込まない。
- 司法・刑事手続・再審・裁判・検察・証拠開示のニュースでは、冤罪防止、適正手続、判決の安定性、証拠開示、被害者保護、捜査・検察の説明責任を中心に論じる。
- ニュース本文に税、予算、給付、補助金、費用、保険料などの明示がない限り、「給付→財源→負担者」「国民負担」「財政健全性」「費用増」を使わない。

【投稿の基本構造：ニュース → 文章図解 → 意見】
本文 tweet_lines だけで完結させる。画像も補助資料もない。
1行目: 固有名詞または根拠のある数字を入れた強いフック。
2〜3行目: ニュースで確認できる事実。
次のブロック: ニュースに直接関係する因果・対比を、固有の具体語で文章図解する。定型句を埋めるだけにしない。
最後: 政策・予算・権限・説明責任に向けた批判的結論。必要な場合だけ、線引きや優先順位を問う質問を1つ置ける。
条件: 140〜260字 / 3〜5ブロック / 空行2〜3個まで / 1ブロック1〜2行 / ポエム化しない。
親投稿は必ず260字以内。文の途中や「…」で終わらせない。

【1行目ルール】
次のいずれかを使う:
- 数字ギャップ型
- 常識否定型
- 当事者の負担型
- 建前と実態の矛盾型
禁止: 「〜について」「〜が話題です」「〜が決まりました」、抽象語だけ、疑問文で開始。
hook には1行目をそのまま入れる。

【文章図解の作り方】
- structure_title: 争点を一言で示す対比型タイトル。
- structure_key_message: 政策の構造を短く言い切る一文。
- structure_left / structure_right: 建前と実態、受益者と負担者、短期と長期などの対比。
- structure_points: 因果関係を3〜4点で整理。
- opinion_conclusion: 行政監視の観点からの批判的結論。ニュース事実とは区別する。
これらは画像用ではない。本文や任意のスレッド返信を組み立てるための文章素材である。

post_type、hook_type、critique_axis はユーザー入力で指定された値をそのままJSONへ入れる。
本文にはこれらの内部値や分類名を一切書かない。
問いは最後に1つだけ。単なる「どう思いますか？」は禁止。
税負担、優先順位、制度の線引きなど、具体的な争点を示して問う。
政党・政治家の政策や公的行為への厳しい問いは許容する。ただし、属性攻撃、脅迫、
標的型嫌がらせ、未確認疑惑の断定、投票妨害につながる問いは禁止。

各候補の批判軸はニュースに最も適合するもの1つだけとし、ユーザー入力の指定から変更しない。

出力前に必ず内部確認する:
1. 選んだ批判軸はニュース本文の具体語と直接つながっているか。
2. ニュースにない財源・負担・費用を創作していないか。
3. 型名や制作指示が本文に漏れていないか。
4. 投稿を読んだ人が、そのニュース固有の争点を一つ説明できる内容か。
一つでも満たさなければ書き直してから出力する。

各スコアは0〜10で自己評価する:
- news: ニュース性
- controversy: 健全な論争性
- data_ability: 数字・制度を整理しやすいか
- resonance: 政策関心層への刺さりやすさ
- save_value: 保存価値
- quote_likelihood: 引用リポストされやすさ
- early_reaction_likelihood: 初速反応
- quote_angle_strength: 一言言いたくなる余白
- text_diagram_clarity: 文章図解の分かりやすさ
- policy_structure_value: 政策構造を整理できているか
- conservative_angle_strength: 行政監視の批判軸が明確か（既存スキーマ互換の項目名）
- evergreen_value: 数日後にも読まれる価値
- source_trust: ソース信頼度
- ban_risk: 差別、脅迫・暴力扇動、標的型嫌がらせ、未確認情報の断定、
  選挙妨害、個人情報公開のリスク。高いほど危険。
  事実に基づく政策・政党・公人・行政判断への強い批判だけを理由に高得点にしない。

overall は ban_risk を踏まえた総合点0〜10。
指定されたJSONスキーマに厳密に従い、候補を{n_candidates}案提出する。JSON以外の地の文は書かない。
"""

GENERATION_USER_TMPL = """\
次のニュースから、X投稿候補を必ず{n_candidates}案つくり、指定JSONスキーマで返してください。
事前分類は post_type={post_type} / hook_type={hook_type} / critique_axis={critique_axis} / topic_key={topic_key}。
JSONにはこの指定値をそのまま格納し、本文へ内部ラベルを表示しないでください。

編集人格は「久世ゆい」。元投稿の要約や言い換えで終わらせず、確認済み事実から
制度設計、責任の所在、受益と負担、長期的副作用のいずれか一つを独自論点として加える。
「久世ゆい」という名前や制作過程は投稿本文に書かない。

ニュース:
title: {title}
summary: {summary}
source_name: {source_name}

source_nameが「X検索」の場合、その本文と反応数はX上で話題になっていることを示す
補助情報であり、記載内容が事実だとは限らない。「X上では議論がある」ことと
確認済みの事実を区別し、未確認の主張、人物評価、疑惑を事実として断定しない。
X検索の高反応投稿から参考にしてよいのは、見出し、改行、箇条書き、文章の長さ、
絵文字の個数と配置などの表現形式だけ。政治的主張、怒り・恐怖・対立を煽る語調、
個人攻撃、断定、固有の言い回しは模倣しない。

優先ジャンル: 社会保障 / 税財政 / 少子化 / 安全保障 / エネルギー / 移民政策 / 教育 / 国会法案
本文は「ニュース事実 → そのニュース固有の制度構造 → 意見」の順で構成する。
批判対象は政策・制度・意思決定に限定し、事実にない断定はしない。
司法・再審・裁判に関するニュースへ、財源・給付・国民負担のテンプレを流用しない。
内部の投稿型名や「保守寄り」という説明を本文に書かない。
本文 tweet_lines は1行ずつの配列。空行は "" を入れる。
本文には、内容に合う異なる絵文字を必ず2〜5個入れる。見出しまたは転換点へ配置し、
同じ絵文字を連打しない。見出し・箇条書き・空行を組み合わせ、ひと目で論点が分かる構成にする。
"""

# OpenAI Structured Outputs 用JSON Schema。
GENRE_ENUM = ["社会保障", "税財政", "少子化", "安全保障",
              "エネルギー", "移民政策", "教育", "国会法案"]

_COLUMN_SCHEMA = {
    "type": ["object", "null"],
    "properties": {
        "label": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["label", "items"],
    "additionalProperties": False,
}

_SCORE_KEYS = [
    "news", "controversy", "data_ability", "resonance", "save_value",
    "quote_likelihood", "early_reaction_likelihood", "quote_angle_strength",
    "text_diagram_clarity", "policy_structure_value",
    "conservative_angle_strength", "evergreen_value", "source_trust", "ban_risk",
]

_CANDIDATE_PROPERTIES = {
    "post_type": {"type": "string", "enum": POST_TYPE_KEYS},
    "hook_type": {"type": "string", "enum": list(HOOK_TYPES)},
    "title": {"type": "string"},
    "tweet_lines": {"type": "array", "items": {"type": "string"}},
    "genre": {"type": "string", "enum": GENRE_ENUM},
    "critique_axis": {
        "type": "string",
        "enum": [
            *CRITIQUE_AXES,
        ],
    },
    "hook": {"type": "string"},
    "structure_title": {"type": "string"},
    "structure_key_message": {"type": "string"},
    "structure_points": {"type": "array", "items": {"type": "string"}},
    "structure_left": _COLUMN_SCHEMA,
    "structure_right": _COLUMN_SCHEMA,
    "opinion_conclusion": {"type": "string"},
    "source_name": {"type": "string"},
    "keywords": {"type": "array", "items": {"type": "string"}},
    "uses_unverified_number": {"type": "boolean"},
    "scores": {
        "type": "object",
        "properties": {key: {"type": "integer"} for key in _SCORE_KEYS},
        "required": _SCORE_KEYS,
        "additionalProperties": False,
    },
    "overall": {"type": "integer"},
    "decision_reason": {"type": "string"},
    "importance_score": {"type": "number"},
    "source_reliability_score": {"type": "number"},
    "claim_risk": {"type": "string", "enum": ["low", "medium", "high"]},
    "final_text": {"type": "string"},
    "quality_score": {"type": "number"},
}

CANDIDATE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _CANDIDATE_PROPERTIES,
                "required": list(_CANDIDATE_PROPERTIES.keys()),
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


def _get_candidate_count() -> int:
    """1ニュースあたりの生成候補数（既定1）。1〜3にクランプ。
    少ないほど出力トークンとコストが下がる。"""
    try:
        v = int(os.getenv("CANDIDATES_PER_NEWS", "1"))
    except (TypeError, ValueError):
        v = 1
    return max(1, min(v, 3))


CANDIDATES_PER_NEWS = _get_candidate_count()


def _load_performance_patterns(topic_key: str = "", max_chars: int = 900) -> str:
    """Load at most 3 relevant wins and 5 recent failure/avoid rules."""
    root = ROOT_DIR / "knowledge" / "viral_patterns"
    sections = []
    specs = (
        ("winning_patterns.md", "最近の成功形式", 3, True),
        ("losing_patterns.md", "最近の低成績形式", 5, False),
        ("avoid_patterns.md", "禁止・品質エラー", 5, False),
    )
    for filename, label, limit, prefer_topic in specs:
        path = root / filename
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError):
            continue
        if not text:
            continue
        lines = [line for line in text.splitlines() if line.strip().startswith("-")]
        if prefer_topic and topic_key:
            related = [line for line in lines if f"topic={topic_key}" in line]
            unrelated = [line for line in lines if line not in related]
            selected = related[-limit:]
            if len(selected) < limit:
                selected = selected + unrelated[-(limit - len(selected)):]
        else:
            selected = lines[-limit:]
        if selected:
            sections.append(f"【{label}】\n" + "\n".join(selected))
    out = "\n\n".join(sections).strip()
    return out[:max_chars]


_DEFAULT_IMPORTANT_KEYWORDS = [
    "首相", "内閣", "国会", "選挙", "政権", "法案", "予算", "補正予算",
    "消費税", "所得税", "法人税", "社会保険料", "年金", "社会保障",
    "防衛", "安全保障", "自衛隊", "中国", "台湾", "北朝鮮", "ロシア",
    "原発", "電力", "エネルギー", "移民", "入管", "憲法", "条約",
    "関税", "経済対策", "緊急事態", "領海", "尖閣",
    "再審", "裁判", "司法", "刑事訴訟", "検察", "証拠開示", "冤罪",
]


def _openai_usage_state() -> dict:
    return load_usage_state(OPENAI_USAGE_FILE)


def _today_usage(state: dict) -> dict:
    return today_usage(state)


def _record_openai_usage(response, model: str, tier: str, *, fallback_used: bool = False) -> float:
    event = record_usage(
        response=response,
        model=model,
        task_type="post_generation",
        pricing=ModelRouter(OPENAI_PRICING_FILE).pricing,
        state_path=OPENAI_USAGE_FILE,
        history_dir=OPENAI_USAGE_HISTORY_DIR,
        fallback_used=fallback_used,
    )
    log(
        f"[INFO] OpenAI usage: model={model} tier={tier} input={event['input_tokens']} "
        f"cached={event['cached_input_tokens']} output={event['output_tokens']} "
        f"estimated_usd={event['estimated_cost_usd']:.6f}"
    )
    return float(event["estimated_cost_usd"])


def _call_openai(client, *, model: str, system: str, user: str, max_toks: int,
                 reasoning_effort: str = "none"):
    kwargs = {
        "model": model,
        "instructions": system,
        "input": user,
        "max_output_tokens": max_toks,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "politics_narrative_candidates",
                "strict": True,
                "schema": CANDIDATE_RESPONSE_SCHEMA,
            }
        },
        "store": False,
    }
    if reasoning_effort and reasoning_effort not in ("none", "off", "false", "minimal"):
        kwargs["reasoning"] = {"effort": reasoning_effort}
    return client.responses.create(**kwargs)



_META_LEAK_PATTERNS = [
    r"(?:^|[\s：:])(?:A|B|C|D|E|F|G|H|I|J|K|L)型(?:の問い)?",
    r"\b(?:post_type|hook_type|critique_axis|decision_reason)\b",
    r"(?:breaking_news|issue_diagram|strong_opinion|comparison_factcheck|morning_evening_digest)",
    r"(?:fact_reversal|issue_redefinition|conclusion_first)",
    r"(?:システムプロンプト|内部スコア|JSONキー)",
    r"保守寄りの視点からは",
    r"右派の観点からは",
    r"AIとして",
    r"編集部として",
    r"投稿型",
    r"\bgpt-[\w.\-]+\b",
    r"(?:Model route|Route reason|route_reason|fallback_used)",
]
_FINANCE_TERMS = [
    "税", "予算", "財源", "給付", "補助金", "費用", "負担", "保険料",
    "国債", "歳出", "公費", "支出", "財政", "料金", "賠償",
]
_FINANCE_OUTPUT_TERMS = [
    "給付 → 財源 → 負担者", "給付→財源→負担者", "財源・運用",
    "財政健全性", "無駄な費用増", "国民の負担", "負担者",
]
_JUDICIAL_NEWS_TERMS = [
    "再審", "裁判", "司法", "刑事訴訟", "検察", "判決", "証拠", "冤罪", "法務委",
]
_JUDICIAL_OUTPUT_TERMS = [
    "再審", "裁判", "司法", "適正手続", "証拠", "冤罪", "判決", "検察",
    "被害者", "有罪", "無罪", "捜査", "公正",
]
_SECURITY_NEWS_TERMS = ["外交", "防衛", "同盟", "抑止", "台湾", "中国", "北朝鮮", "領海"]
_SECURITY_OUTPUT_TERMS = ["国益", "防衛", "同盟", "抑止", "安全保障", "主権", "外交"]
_ECONOMIC_NEWS_TERMS = ["経済", "景気", "賃金", "物価", "企業", "産業", "関税", "貿易"]
_UNRELATED_SECURITY_TERMS = ["治安悪化", "犯罪増加", "入管強化"]

_EMOJI_PATTERN = re.compile(
    r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF]"
)


def _candidate_quality_violations(candidate: dict, news_item: dict) -> list[str]:
    """Cheap deterministic checks that block template leakage and off-topic axes."""
    text = (candidate.get("tweet_text") or "").strip()
    source_text = " ".join([
        str(news_item.get("title", "")),
        str(news_item.get("summary", "")),
    ])
    violations = []
    discovered_via = set(news_item.get("discovered_via") or [])
    if "x_search" in discovered_via and not ({"rss", "official"} & discovered_via):
        violations.append("unverified_x_claim")
    for pattern in _META_LEAK_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            violations.append(f"meta_leak:{pattern}")
    source_has_finance = any(term in source_text for term in _FINANCE_TERMS)
    if not source_has_finance and any(term in text for term in _FINANCE_OUTPUT_TERMS):
        violations.append("unsupported_finance_axis")
    judicial_news = any(term in source_text for term in _JUDICIAL_NEWS_TERMS)
    if judicial_news and not any(term in text for term in _JUDICIAL_OUTPUT_TERMS):
        violations.append("judicial_topic_not_addressed")
    if judicial_news and any(term in text for term in _FINANCE_OUTPUT_TERMS):
        violations.append("judicial_with_unrelated_finance")
    security_news = any(term in source_text for term in _SECURITY_NEWS_TERMS)
    if security_news and not any(term in text for term in _SECURITY_OUTPUT_TERMS):
        violations.append("security_topic_not_addressed")
    economic_news = any(term in source_text for term in _ECONOMIC_NEWS_TERMS)
    if economic_news and not any(term in source_text for term in ("治安", "犯罪", "入管")) \
            and any(term in text for term in _UNRELATED_SECURITY_TERMS):
        violations.append("economic_with_unrelated_security")
    expected = {
        "post_type": news_item.get("post_type"),
        "hook_type": news_item.get("hook_type"),
        "critique_axis": news_item.get("critique_axis"),
    }
    for key, value in expected.items():
        if value and candidate.get(key) != value:
            violations.append(f"classification_mismatch:{key}")
    if len(text) < 100:
        violations.append("too_short")
    emojis = _EMOJI_PATTERN.findall(text)
    if len(emojis) < 2:
        violations.append("missing_required_emojis")
    elif len(set(emojis)) < 2:
        violations.append("insufficient_emoji_variety")
    if "\n\n" not in text:
        violations.append("missing_section_break")
    return violations


LAST_GENERATION_FAILURE_REASON = ""


def generate_candidates(news_item: dict, regeneration_attempt: int = 0, retries_used: int = 0) -> list:
    """Generate structured X post candidates with the OpenAI Responses API."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        log("[ERROR] OPENAI_API_KEY is not set")
        return []
    try:
        from openai import OpenAI
    except Exception as e:
        log(f"[ERROR] openai SDK import failed: {e}")
        return []

    n = CANDIDATES_PER_NEWS
    state = _openai_usage_state()
    router = ModelRouter(OPENAI_PRICING_FILE)
    route = router.select_model(
        "post_generation",
        importance_score=float(news_item.get("final_news_score", 0) or 0),
        genre=str(news_item.get("genre", "")),
        source_reliability=float(news_item.get("source_reliability_score", 0) or 0),
        claim_risk=str(news_item.get("claim_risk", "low") or "low"),
        budget_state=state,
        daily_usage=_today_usage(state),
        text=f"{news_item.get('title', '')} {news_item.get('summary', '')}",
    )
    model = route.get("model", "")
    log(f"[INFO] Model route: {model or 'skip'}")
    log(f"[INFO] Route reason: {route.get('route_reason', '')}")
    if not model:
        global LAST_GENERATION_FAILURE_REASON
        LAST_GENERATION_FAILURE_REASON = route.get("skip_reason", "model_route_skip")
        log(f"[WARN] OpenAI generation skipped: {LAST_GENERATION_FAILURE_REASON}")
        return []
    fallback_used = bool(route.get("fallback_used"))
    tier = "important" if route.get("route_reason", "").startswith("important") else "default"
    timeout = max(15.0, _env_float("OPENAI_TIMEOUT_SECONDS", 90.0))
    client = OpenAI(api_key=api_key, timeout=timeout, max_retries=0)
    system = GENERATION_SYSTEM.format(n_candidates=n)
    user = GENERATION_USER_TMPL.format(
        n_candidates=n,
        post_type=news_item.get("post_type", ""),
        hook_type=news_item.get("hook_type", ""),
        critique_axis=news_item.get("critique_axis", ""),
        topic_key=news_item.get("topic_key", ""),
        title=news_item.get("title", ""),
        summary=news_item.get("summary", "")[:1200],
        source_name=news_item.get("source_name", ""),
    )
    perf = _load_performance_patterns(news_item.get("topic_key", ""))
    if perf:
        user += "\n\n実績データ（report コマンドで集計した実際のインプレッション傾向）:\n" + perf
    user += (
        "\n\n出力補足: final_text は tweet_lines を改行で連結した公開本文と完全一致させる。"
        "importance_score・source_reliability_score・claim_risk・quality_scoreも必ず評価する。"
        "モデル名、料金、ルーティング理由など内部情報は公開本文へ書かない。"
    )

    # One candidate normally fits well under this cap. Reasoning tokens are also
    # counted against max_output_tokens, so keep a modest configurable buffer.
    max_toks = int(route["max_output_tokens"])
    log(
        f"[INFO] OpenAI model selected: {model} tier={tier} max_output_tokens={max_toks}"
    )
    try:
        response = _call_openai(
            client, model=model, system=system, user=user, max_toks=max_toks,
            reasoning_effort=route.get("reasoning_effort", "none"),
        )
    except Exception as first_error:
        fallbacks = route.get("fallback_models", [])
        can_retry = retries_used < max(0, min(_env_int("OPENAI_MAX_RETRIES", 1), 1))
        if fallbacks and can_retry and not is_auth_error(first_error):
            model = fallbacks[0]
            tier = "default"
            fallback_used = True
            log(f"[WARN] model unavailable; one fallback to {model}: {type(first_error).__name__}")
            try:
                response = _call_openai(
                    client, model=model, system=system, user=user, max_toks=max_toks,
                    reasoning_effort=os.environ.get("OPENAI_REASONING_EFFORT_DEFAULT", "none"),
                )
            except Exception as second_error:
                log(f"[ERROR] candidate generation failed after fallback: {type(second_error).__name__}")
                return []
        else:
            reason = "authentication_error_no_retry" if is_auth_error(first_error) else "retry_limit"
            log(f"[ERROR] candidate generation failed: {reason} ({type(first_error).__name__})")
            return []

    _record_openai_usage(response, model, tier, fallback_used=fallback_used)
    status = str(getattr(response, "status", "") or "")
    if status == "incomplete":
        details = getattr(response, "incomplete_details", None)
        log(f"[WARN] OpenAI response incomplete: {details}")

    raw = (getattr(response, "output_text", "") or "").strip()
    if not raw:
        log("[ERROR] OpenAI response contained no output_text (possible refusal or incomplete output)")
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        log(f"[ERROR] OpenAI structured output JSON parse failed: {e}")
        return []

    candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
    if not isinstance(candidates, list):
        return []

    LAST_GENERATION_FAILURE_REASON = ""
    cleaned = []
    rejected_violations = []
    for c in candidates[:n]:
        if not isinstance(c, dict):
            continue
        lines = c.get("tweet_lines") or []
        joined_text = "\n".join(str(x) for x in lines).strip()
        c["tweet_text"] = (c.get("final_text") or joined_text).strip()
        if joined_text and c["tweet_text"] != joined_text:
            c["tweet_text"] = joined_text
        if "\n\n" not in c["tweet_text"]:
            nonempty_lines = [line.strip() for line in c["tweet_text"].splitlines() if line.strip()]
            if len(nonempty_lines) >= 3:
                c["tweet_text"] = "\n\n".join(nonempty_lines)
        if not c["tweet_text"]:
            continue
        violations = _candidate_quality_violations(c, news_item)
        if violations:
            rejected_violations.extend(violations)
            log(
                f"[WARN] Candidate rejected by deterministic quality check: "
                f"{','.join(violations)} text={c['tweet_text'][:160]!r}"
            )
            continue
        c.setdefault("source_url", news_item.get("url", ""))
        c["externally_corroborated"] = bool(news_item.get("externally_corroborated"))
        c["verification_sources"] = news_item.get("verification_sources", [])
        c["discovered_via"] = list(news_item.get("discovered_via") or ["rss"])
        c["x_attention_score"] = float(news_item.get("x_attention_score", 0) or 0)
        c["x_post_count"] = int(news_item.get("x_post_count", 0) or 0)
        c["x_unique_accounts"] = int(news_item.get("x_unique_accounts", 0) or 0)
        c["x_velocity_score"] = float(news_item.get("x_velocity_score", 0) or 0)
        c["final_news_score"] = float(news_item.get("final_news_score", 0) or 0)
        c["topic_key"] = news_item.get("topic_key", "")
        c["source_name"] = c.get("source_name") or news_item.get("source_name", "")
        c.setdefault("pub_date", news_item.get("pub_date", ""))
        c["openai_model"] = model
        c["model_route_reason"] = route.get("route_reason", "")
        c["model_fallback_used"] = fallback_used
        if not (c.get("hook") or "").strip():
            first_line = next((str(x).strip() for x in lines if str(x).strip()), "")
            c["hook"] = first_line or (c.get("title") or "").strip()
        cleaned.append(c)
    if not cleaned and rejected_violations:
        LAST_GENERATION_FAILURE_REASON = (
            "unverified_x_claim" if "unverified_x_claim" in rejected_violations
            else "internal_label_leak" if any(v.startswith("meta_leak:") for v in rejected_violations)
            else "relevance_gate_failed"
        )
        max_retry = max(0, min(_env_int("OPENAI_MAX_RETRIES", 1), 1))
        retry_consumed = retries_used + (1 if fallback_used else 0)
        if regeneration_attempt < 1 and retry_consumed < max_retry:
            log("[INFO] Candidate quality rejection; regenerating once")
            retry_item = dict(news_item)
            retry_item["summary"] = (
                f"{news_item.get('summary', '')}\n再生成指示: 前回は品質検査で拒否。"
                "内部ラベルを本文へ出さず、ニュース固有の事実と指定した批判軸だけで書き直す。"
            )
            return generate_candidates(retry_item, regeneration_attempt + 1, retry_consumed + 1)
    return cleaned

# 実効スコアの加重（合計で割って 0〜10 相当に正規化する）
EFFECTIVE_WEIGHTS = {
    "quote_likelihood": 1.5,
    "quote_angle_strength": 1.4,
    "save_value": 1.3,
    "text_diagram_clarity": 1.2,
    "policy_structure_value": 1.2,
    "conservative_angle_strength": 1.3,
    "data_ability": 1.1,
    "early_reaction_likelihood": 1.0,
    "controversy": 0.9,
    "news": 0.8,
    "evergreen_value": 0.8,
    "source_trust": 0.7,
}
_EFFECTIVE_WEIGHT_SUM = sum(EFFECTIVE_WEIGHTS.values())


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
    ttype = c.get("post_type", "")
    ban = int(_num(scores, "ban_risk"))

    # 安全ゲート（維持方針）：BANリスクが閾値以上なら候補から外す。
    # ※ 差別・脅迫・暴力扇動・標的型嫌がらせ・虚偽断定等を防ぐため、
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

    if ttype == "strong_opinion" and quote >= 7 and angle >= 7 and ban <= 4:
        base += 0.4

    return base


# ---------------------------------------------------------------------------
# 7. X への投稿（tweepy / OAuth1.0a）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 7.5 テキスト投稿本文の組み立て（ニュース・文章図解・意見）
# ---------------------------------------------------------------------------

# Xの文字数ルール（config/platform_rules.json と揃える）
X_MAX_CHARS = 280
X_SAFE_CHARS = 260


def _x_len(text: str) -> int:
    """Xの文字数カウント。日本語は1文字1カウントの近似（URLは本文に入れない運用のため未考慮）。"""
    return len(text)


def build_post_text(c: dict) -> str:
    """候補から親投稿用のテキストを組み立てる。
    hook→ニュース事実→文章図解→行政監視の結論/問いを1投稿に収める。
    tweet_lines をそのまま使い、無い場合は hook / image_* から再構成する。
    """
    lines = [str(x) for x in (c.get("tweet_lines") or [])]
    text = "\n".join(lines).strip()
    if text:
        return text
    # フォールバック: tweet_lines が空なら hook と結論から最小構成を作る
    parts = []
    hook = (c.get("hook") or "").strip()
    if hook:
        parts.append(hook)
    concl = (c.get("opinion_conclusion") or "").strip()
    if concl:
        label = "結論："
        parts.append("")
        parts.append(f"{label}{concl}")
    return "\n".join(parts).strip()


def build_reply_texts(c: dict) -> list:
    """親投稿を補足する返信（スレッド）テキストの配列を返す。
    THREAD_ENABLED=false（既定）なら空配列を返し、単発投稿にする。
    背景・争点・見るべきポイントを、余っている素材から最大1件まで作る。
    各要素は X_SAFE_CHARS 以内。無ければ空配列（=単発投稿）。
    """
    if not THREAD_ENABLED:
        return []

    replies = []

    # 対比カラムがあれば「争点の対比」を1返信にまとめる
    left = c.get("structure_left") if isinstance(c.get("structure_left"), dict) else None
    right = c.get("structure_right") if isinstance(c.get("structure_right"), dict) else None
    if left and right:
        li = _normalize_items(left.get("items"))[:3]
        ri = _normalize_items(right.get("items"))[:3]
        if li or ri:
            seg = []
            if left.get("label"):
                seg.append(f"◆{str(left.get('label')).strip()}")
            seg += [f"・{x}" for x in li]
            if right.get("label"):
                seg.append(f"◆{str(right.get('label')).strip()}")
            seg += [f"・{x}" for x in ri]
            replies.append("\n".join(seg).strip())
    else:
        # 対比が無い場合は structure_points を「見るべきポイント」として1返信に
        pts = _normalize_items(c.get("structure_points"))[:4]
        if pts:
            seg = ["見るべきポイント"] + [f"・{x}" for x in pts]
            replies.append("\n".join(seg).strip())

    # 安全化: 各返信を X_SAFE_CHARS 以内に丸め、空要素を除く。最大1件まで。
    out = []
    for r in replies:
        r = (r or "").strip()
        if not r:
            continue
        if _x_len(r) > X_SAFE_CHARS:
            r = r[:X_SAFE_CHARS].rstrip()
        out.append(r)
        if len(out) >= 1:
            break
    return out


def _x_client():
    import tweepy
    api_key = os.environ["API_KEY"]
    api_secret = os.environ["API_KEY_SECRET"]
    access_token = os.environ["ACCESS_TOKEN"]
    access_secret = os.environ["ACCESS_TOKEN_SECRET"]
    return tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret,
    )


def post_to_x(text: str, reply_texts: list = None):
    """Xへテキスト投稿する。画像アップロード経路は存在しない。

    戻り値: (親tweet_id, [各投稿の文字数])。
    reply_texts があれば、親投稿への返信チェーンとして投稿する。
    """
    # URL付き投稿の課金を避けるため、送信直前にも本文からURLを除去する。
    def strip_urls(value: str) -> str:
        value = re.sub(r"(?i)https?://\S+|www\.\S+", "", value or "")
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    text = strip_urls(text)
    if not text:
        raise ValueError("post text became empty after URL removal")

    client = _x_client()
    resp = client.create_tweet(text=text)
    parent_id = str(resp.data.get("id"))
    lengths = [_x_len(text)]

    prev_id = parent_id
    for r in (reply_texts or []):
        r = strip_urls(r)
        if not r:
            continue
        try:
            rr = client.create_tweet(text=r, in_reply_to_tweet_id=prev_id)
            prev_id = str(rr.data.get("id"))
            lengths.append(_x_len(r))
        except Exception as e:
            log(f"[WARN] thread reply failed (parent kept): {e}")
            break

    return parent_id, lengths


# ---------------------------------------------------------------------------
# 8. メイン
# ---------------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "diagram"
    if mode in ("link", "test", "normal"):
        # link / test は完全廃止。normal も今回の運用では使わない。
        log(f"[ERROR] mode '{mode}' is not supported in this deployment. Use 'diagram' (text diagram mode).")
        sys.exit(1)
    mode = "diagram"

    force = os.environ.get("FORCE_POST", "").strip().lower() in ("true", "1", "yes")

    # --- 時刻 ---
    now_jst, time_source = get_jst_now()
    now_utc = now_jst.astimezone(UTC)
    log(f"[INFO] Time source: {time_source}")
    log(f"[INFO] Current JST: {now_jst:%Y-%m-%d %H:%M:%S}")
    log(f"[INFO] Current UTC: {now_utc:%Y-%m-%d %H:%M:%S}")
    log(f"[INFO] Mode: {mode} / FORCE_POST={str(force).lower()}")
    log(f"[INFO] POST_ENABLED: {str(POST_ENABLED).lower()}")
    post_format = "text_only"
    log(f"[INFO] Post format: {post_format}")
    log(f"[INFO] STATE_DIR: {STATE_DIR}")

    # --- スロット判定（24時間対象・catch-up方式・1runで最大1投稿トライ） ---
    # catch-up の未処理判定は attempted_slots.json を基準にする（詰まり防止）。
    posted = set(_load_json(POSTED_SLOTS_FILE, []) or [])
    attempted = set(_load_json(ATTEMPTED_SLOTS_FILE, []) or [])
    log(f"[INFO] Catch-up window hours: {CATCH_UP_HOURS}")
    log(f"[INFO] Max posts per run: {MAX_POSTS_PER_RUN}")
    log(f"[INFO] posted_slots count: {len(posted)}")
    log(f"[INFO] attempted_slots count: {len(attempted)}")

    # 安全弁: 1runの投稿トライ上限が0以下なら何もしない
    if MAX_POSTS_PER_RUN < 1 and not force:
        log("[INFO] Decision: skip")
        log("[INFO] Skip reason: max_posts_per_run_zero")
        return

    if force:
        # FORCE_POST=true は catch-up判定を無視して即時投稿トライ
        slot = f"FORCE_{now_jst:%H:%M}"
        slot_key = f"{now_jst.date().isoformat()}_{slot}"
        slot_dt = now_jst
        log("[INFO] FORCE_POST=true -> catch-up check skipped")
        log(f"[INFO] Selected slot for this run: {now_jst.isoformat()}")
        log(f"[INFO] Slot age minutes: 0")
        log(f"[INFO] Slot key: {slot_key}")
        log(f"[INFO] Slot already attempted: {str(slot_key in attempted).lower()}")
        log(f"[INFO] Slot already posted: {str(slot_key in posted).lower()}")
    else:
        slot, slot_key, slot_dt, window_slots, unattempted = \
            find_catch_up_slot(now_jst, attempted, CATCH_UP_HOURS)
        log(f"[INFO] Post slots in catch-up window: {len(window_slots)}")
        log(f"[INFO] Unattempted slots count: {len(unattempted)}")
        if slot is None:
            # この24時間で開始済みのスロットはすべてトライ済み（=回収すべきものが無い）
            log("[INFO] Selected slot for this run: None")
            log("[INFO] Decision: skip")
            log("[INFO] Skip reason: no_unattempted_slot")
            return
        age_min = int((now_jst - slot_dt).total_seconds() // 60)
        # POST_WINDOW_MINUTES は「現在slotか否か」の表示にのみ使う（catch-up探索には使わない）
        is_current = age_min <= POST_WINDOW_MINUTES
        log(f"[INFO] Selected slot for this run: {slot_dt.isoformat()}")
        log(f"[INFO] Slot age minutes: {age_min}")
        log(f"[INFO] Within current post window: {str(is_current).lower()}")
        log(f"[INFO] Slot key: {slot_key}")
        log(f"[INFO] Slot already attempted: {str(slot_key in attempted).lower()}")
        log(f"[INFO] Slot already posted: {str(slot_key in posted).lower()}")
        # find_catch_up_slot は未トライslotのみ返すため通常ここは false
        if slot_key in attempted:
            log("[INFO] Decision: skip")
            log("[INFO] Skip reason: slot_already_attempted")
            return

    # attempted 記録の共通ヘルパー。
    # skip理由ごとに「attemptedに記録するか」を判定し、ログも統一して出す。
    # 一時失敗（post_to_x_failed / network / rate limit）では
    # mark_attempted=False にして呼ぶこと（slotを失わないため）。
    def finalize_skip(reason: str, *, mark_attempted: bool, extra: dict = None):
        log("[INFO] Decision: skip")
        log(f"[INFO] Skip reason: {reason}")
        recorded = False
        if not POST_ENABLED:
            mark_attempted = False
        if mark_attempted:
            mark_slot_attempted(slot_key)
            recorded = True
            log("[INFO] Slot marked as attempted.")
        else:
            log(f"[INFO] Slot not marked as attempted (reason: {reason}).")
        log("[INFO] Slot not marked as posted because post was not successful.")
        rec = {
            "decision": "skip", "reason": reason,
            "slot_key": slot_key, "selected_slot": slot_key,
            "post_format": post_format,
            "attempted_recorded": recorded,
            "posted_recorded": False,
        }
        if extra:
            rec.update(extra)
        log_attempt(rec)

    # --- 素材収集 ---
    history = load_post_history()
    stagnation_fallback = stagnation_fallback_active(
        history, now_jst, LOW_QUALITY_FALLBACK_HOURS
    )
    log(
        f"[INFO] Low-quality fallback after hours: {LOW_QUALITY_FALLBACK_HOURS:g} "
        f"active={str(stagnation_fallback).lower()}"
    )
    # FORCE_POSTは時刻スロットのソース分割を適用せず、混合候補で安全に検証できるようにする。
    source_split = _env_bool("SOURCE_SCHEDULE_SPLIT", "true") and not force
    slot_minute = int(slot_dt.minute)
    source_lane = "mixed"
    if source_split:
        if slot_minute == 0:
            source_lane = "rss"
        elif slot_minute == 30:
            if stagnation_fallback:
                source_lane = "rss_fallback"
            else:
                x_every_hours = max(1, _env_int("X_SEARCH_EVERY_HOURS", 1))
                first_hour = min(ACTIVE_HOURS) if ACTIVE_HOURS else 0
                if (slot_dt.hour - first_hour) % x_every_hours != 0:
                    log(f"[INFO] X Search interval skip: every {x_every_hours} hours")
                    finalize_skip("x_search_interval", mark_attempted=True)
                    return
                source_lane = "x_search"
        else:
            finalize_skip("unsupported_minute", mark_attempted=True)
            return

    # RSS枠では有料のX Searchを呼ばない。X枠では事実確認用RSSも同時取得する。
    news_items = gather_candidate_news(include_x=(source_lane not in {"rss", "rss_fallback"}))
    log(f"[INFO] News items fetched: {len(news_items)}")

    # 毎時00分はRSS/公式情報、毎時30分はXレーダーでも確認されたRSS話題に分離する。
    # 他者の文章・画像・動画の再アップロードは行わず、どちらも独自テキストを生成する。
    if source_split:
        if source_lane in {"rss", "rss_fallback"}:
            pass
        elif source_lane == "x_search":
            news_items = [
                it for it in news_items
                if "x_search" in set(it.get("discovered_via") or [])
            ]
        else:
            news_items = []
        log(f"[INFO] Source schedule lane: {source_lane} ({len(news_items)} items)")

    if not news_items:
        # no_news は attempted に記録する（要件2-4）
        finalize_skip("no_news", mark_attempted=True)
        return

    digest_hours = {
        int(value) for value in os.environ.get("DIGEST_HOURS", "6,18").split(",")
        if value.strip().isdigit() and 0 <= int(value) <= 23
    }
    digest_window = now_jst.hour in digest_hours and slot_dt.minute == 0
    target_news = prefilter_news(
        news_items,
        top_n=3 if digest_window else None,
        allow_low_quality=stagnation_fallback,
    )
    if digest_window and len(target_news) >= 2:
        digest_items = target_news[:3]
        target_news = [{
            "title": f"{now_jst:%H時}の政治ニュースダイジェスト",
            "summary": " / ".join(item.get("title", "") for item in digest_items),
            "url": digest_items[0].get("url", ""),
            "source_name": "複数ソース",
            "pub_date": digest_items[0].get("pub_date", ""),
            "digest_items": digest_items,
        }]
    log(f"[INFO] News after prefilter: {len(target_news)}")

    if not target_news:
        finalize_skip("no_qualified_news", mark_attempted=True)
        return

    # ニュース監視後、OpenAI生成前に日次上限と投稿間隔を判定する。
    policy_skip = pre_generation_skip_reason(
        history, now_jst, MAX_DAILY_POSTS, MIN_POST_INTERVAL_MINUTES
    )
    if policy_skip:
        finalize_skip(policy_skip, mark_attempted=True, extra={
            "daily_success_count": len([
                h for h in history if str(h.get("posted_at_jst", "")).startswith(now_jst.date().isoformat())
            ]),
            "max_daily_posts": MAX_DAILY_POSTS,
            "min_post_interval_minutes": MIN_POST_INTERVAL_MINUTES,
        })
        return

    recent_topics = load_recent_topics()
    if not recent_topics:
        recent_topics = [
            {
                "topic_key": h.get("topic_key") or normalize_topic_key(h.get("title", ""), h.get("keywords") or []),
                "last_posted_at": h.get("posted_at_jst", ""),
                "tweet_id": h.get("tweet_id", ""),
                "news_title": h.get("title", ""),
            }
            for h in history[-120:] if h.get("posted_at_jst")
        ]
    eligible_news = []
    blocked_for_topic = False
    blocked_for_type_quota = False
    for item in target_news:
        enriched = dict(item)
        enriched["topic_key"] = normalize_topic_key(
            enriched.get("title", ""), enriched.get("keywords") or []
        )
        enriched["post_type"] = classify_post_type(enriched, now_jst)
        enriched["hook_type"] = classify_hook_type(enriched, history)
        enriched["critique_axis"] = classify_critique_axis(enriched)
        if not stagnation_fallback and post_type_quota_reached(
            enriched["post_type"], history, now_jst
        ):
            blocked_for_type_quota = True
            continue
        cooldown_reason = topic_cooldown_skip_reason(
            enriched["topic_key"], enriched.get("title", ""), recent_topics,
            now_jst, TOPIC_COOLDOWN_HOURS,
        )
        if cooldown_reason and not stagnation_fallback:
            blocked_for_topic = True
            continue
        eligible_news.append(enriched)

    if not eligible_news:
        reason = "topic_cooldown" if blocked_for_topic else "post_type_daily_limit"
        finalize_skip(reason, mark_attempted=True)
        return

    # --- 候補生成・採点（複数ニュースからベスト1を選ぶ） ---
    best = None
    best_score = -1.0
    for item in eligible_news:
        for c in generate_candidates(item):
            if is_duplicate(c, history):
                continue
            s = effective_score(c, history)
            if s > best_score:
                best, best_score = c, s

    if best is None:
        reason = LAST_GENERATION_FAILURE_REASON or "candidate_generation_failed"
        finalize_skip(reason, mark_attempted=True)
        return

    scores = best.get("scores") or {}
    overall = int(best.get("overall") or 0)
    ban = int((scores or {}).get("ban_risk", 0) or 0)
    btype = best.get("post_type", "")
    bgenre = best.get("genre", "")
    breason = best.get("decision_reason", "")
    log(f"[INFO] News title: {best.get('title','')}")
    log(f"[INFO] Selected post type: {btype} ({POST_TYPES.get(btype,'')})")
    log(f"[INFO] Hook type: {best.get('hook_type','')}")
    log(f"[INFO] Topic key: {best.get('topic_key','')}")
    log(f"[INFO] Score: overall={overall} effective={best_score:.2f} ban_risk={ban}")
    log(f"[INFO] MIN_POST_SCORE: {MIN_POST_SCORE}")
    log(f"[INFO] Genre: {bgenre}")
    log(f"[INFO] Hook: {best.get('hook','')}")
    log(f"[INFO] Decision reason: {breason}")

    # --- スコア閾値ゲート ---
    # QUALITY_GATE_ENABLED=false（既定）なら品質スコア判定を行わず、どんどん投稿する。
    # 「伸びるかは事前採点では分からない。実際のインプレッションで学習する」運用のため。
    # 注意: BANリスク判定（下の best_score < 0 ブロック）は常に有効のまま。
    #       アカウント凍結を防ぐ安全装置なので、この設定では無効化されない。
    force_bypass_score = (
        force
        and os.environ.get("FORCE_BYPASS_SCORE", "false").strip().lower()
        in ("true", "1", "yes")
    )

    # overall救済ルール: overall>=8 / effective>=6.2 / ban_risk<=2 を満たせば投稿可。
    rescue_rule_applied = (
        overall >= RESCUE_OVERALL_MIN
        and best_score >= RESCUE_EFFECTIVE_MIN
        and ban <= RESCUE_BAN_RISK_MAX
    )

    # --- BANリスク安全弁（常時有効・無効化不可） ---
    # effective_score が負 = BANリスク高 or 未検証数字。差別/煽り/陰謀論などを含むため、
    # QUALITY_GATE_ENABLED=false でも FORCE_BYPASS_SCORE でも絶対に投稿しない。
    # アカウント凍結はインプレッションが永久にゼロになることを意味する。
    if best_score < 0 or ban >= BAN_RISK_BLOCK:
        log(f"[INFO] effective_score={best_score:.2f} overall={overall} ban_risk={ban} "
            f"type={btype} genre={bgenre} decision_reason={breason}")
        log("[INFO] BAN risk gate: blocked (この判定は常時有効で無効化できません)")
        finalize_skip("ban_risk_or_unverified_block", mark_attempted=True, extra={
            "effective_score": round(float(best_score), 2), "overall": overall,
            "ban_risk": ban, "post_type": btype, "genre": bgenre,
            "title": best.get("title", "")})
        return

    # --- 品質スコアゲート（QUALITY_GATE_ENABLED=true のときだけ有効） ---
    if not QUALITY_GATE_ENABLED:
        log("[INFO] QUALITY_GATE_ENABLED=false -> 品質スコア判定をスキップ（実績学習運用）")
        can_post = True
    else:
        can_post = _score_gate_allows(
            best_score,
            force_bypass_score,
            rescue_rule_applied,
            stagnation_fallback,
        )
        log(f"[INFO] Rescue rule applied: {str(rescue_rule_applied).lower()}")
        if stagnation_fallback:
            log(
                "[INFO] Low-quality fallback applied: score threshold and "
                "topic/type quota relaxed; safety gates remain active"
            )
        if force_bypass_score:
            log("[INFO] FORCE_BYPASS_SCORE=true -> score gate bypassed")

    if not can_post:
        log(f"[INFO] effective_score={best_score:.2f} MIN_POST_SCORE={MIN_POST_SCORE} "
            f"overall={overall} ban_risk={ban} type={btype} genre={bgenre} "
            f"decision_reason={breason} rescue_rule_applied={str(rescue_rule_applied).lower()}")
        # effective_score_below_threshold は attempted に記録する（要件2-4）
        finalize_skip("effective_score_below_threshold", mark_attempted=True, extra={
            "effective_score": round(float(best_score), 2), "overall": overall,
            "ban_risk": ban, "post_type": btype, "genre": bgenre,
            "title": best.get("title", "")})
        return

    # --- 本文の組み立て（テキスト投稿がデフォルト） ---
    tweet_text = build_post_text(best)
    reply_texts = build_reply_texts(best)
    # Xの上限を超える親投稿は安全長に丸める（文の途中で切らないよう改行境界を優先）
    if _x_len(tweet_text) > X_MAX_CHARS:
        cut = tweet_text[:X_SAFE_CHARS]
        nl = cut.rfind("\n")
        tweet_text = (cut[:nl] if nl >= X_SAFE_CHARS // 2 else cut).rstrip()
    use_thread = len(reply_texts) > 0
    each_len = [_x_len(tweet_text)] + [_x_len(r) for r in reply_texts]
    log(f"[INFO] final_text_length: {_x_len(tweet_text)}")
    log(f"[INFO] use_thread: {str(use_thread).lower()}")
    log(f"[INFO] thread_reply_count: {len(reply_texts)}")
    log(f"[INFO] each_post_length: {each_len}")

    # 画像生成・画像アップロード機能は廃止。常にテキスト投稿。

    # --- POST_ENABLED 安全弁 ---
    # false の場合、候補生成・スコア判定までは実行済みのまま、
    # X への実投稿だけをここで止める。既定では attempted にも posted にも記録しない
    # （本番投稿前に slot を消費しないため）。
    if not POST_ENABLED:
        log("[INFO] POST_ENABLED=false -> X posting skipped")
        finalize_skip("post_disabled", mark_attempted=MARK_DISABLED_RUN_AS_ATTEMPTED, extra={
            "title": best.get("title", ""), "post_type": btype, "genre": bgenre,
            "effective_score": round(float(best_score), 2), "overall": overall})
        return

    # --- 投稿 ---
    log("[INFO] Decision: post")
    try:
        tweet_id, sent_lengths = post_to_x(tweet_text, reply_texts)
        log(f"[INFO] Posted tweet id: {tweet_id}")
        log(f"[INFO] each_post_length (sent): {sent_lengths}")
    except Exception as e:
        # 投稿失敗は一時失敗扱い: attempted に記録しない（=未処理のまま再挑戦できる）
        log(f"[ERROR] post_to_x failed: {e}")
        log_error({"where": "post_to_x", "error": str(e), "slot_key": slot_key})
        finalize_skip("post_to_x_failed", mark_attempted=False, extra={
            "title": best.get("title", ""), "post_type": btype, "genre": bgenre})
        return

    # --- 投稿成功後にだけ記録（attempted と posted の両方 ＋ post_history） ---
    mark_slot_attempted(slot_key)
    mark_slot_posted(slot_key)
    log("[INFO] Slot marked as attempted.")
    log("[INFO] Slot marked as posted.")
    save_post_record({
        "slot_key": slot_key,
        "posted_at_jst": now_jst.isoformat(),
        "tweet_id": tweet_id,
        "title": best.get("title", ""),
        "post_type": best.get("post_type", ""),
        "hook_type": best.get("hook_type", ""),
        "topic_key": best.get("topic_key", ""),
        "genre": best.get("genre", ""),
        "critique_axis": best.get("critique_axis", ""),
        "source_url": best.get("source_url", ""),
        "keywords": best.get("keywords", []),
        # --- 投稿形態の記録 ---
        "post_format": post_format,
        "use_thread": use_thread,
        "thread_reply_count": len(reply_texts),
        # --- 学習材料（効いた型・問い・スコアの振り返り用） ---
        "tweet_text": tweet_text,
        "hook": best.get("hook", ""),
        "structure_title": best.get("structure_title", ""),
        "structure_key_message": best.get("structure_key_message", ""),
        "opinion_conclusion": best.get("opinion_conclusion", ""),
        "scores": best.get("scores", {}),
        "overall": best.get("overall"),
        "effective_score": round(float(best_score), 2),
        "source_name": best.get("source_name", ""),
        "pub_date": best.get("pub_date", ""),
        "decision_reason": best.get("decision_reason", ""),
        "discovered_via": best.get("discovered_via", ["rss"]),
        "x_attention_score": best.get("x_attention_score", 0.0),
        "x_post_count": best.get("x_post_count", 0),
        "x_unique_accounts": best.get("x_unique_accounts", 0),
        "x_velocity_score": best.get("x_velocity_score", 0.0),
        "final_news_score": best.get("final_news_score", 0.0),
        "low_quality_fallback": stagnation_fallback,
        "openai_model": best.get("openai_model", ""),
    })
    save_recent_topic({
        "topic_key": best.get("topic_key", ""),
        "last_posted_at": now_jst.isoformat(),
        "tweet_id": tweet_id,
        "news_title": best.get("title", ""),
        "major_update_signature": normalize_topic_key(best.get("title", "")),
    })
    log("[INFO] Slot and post history recorded.")
    log_attempt({
        "decision": "post", "reason": "success",
        "slot_key": slot_key, "selected_slot": slot_key,
        "tweet_id": tweet_id,
        "title": best.get("title", ""), "post_type": btype,
        "hook_type": best.get("hook_type", ""), "topic_key": best.get("topic_key", ""),
        "genre": bgenre,
        "critique_axis": best.get("critique_axis", ""),
        "effective_score": round(float(best_score), 2), "overall": overall,
        "ban_risk": ban,
        "post_format": post_format,
        "openai_model": best.get("openai_model", ""),
        "low_quality_fallback": stagnation_fallback,
        "attempted_recorded": True,
        "posted_recorded": True,
    })


if __name__ == "__main__":
    main()
