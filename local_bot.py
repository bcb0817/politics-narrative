#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_bot.py — politics-narrative のローカル運用エントリポイント

GitHub Actions に依存せず、ローカルPC / ローカルサーバーで Bot を動かす。
実際の投稿ロジックは src/post.py（文章による意見図解・テキスト専用）を使う。

コマンド:
    python local_bot.py init-state        # 初回だけ: 過去スロットを処理済み化（バックログ暴発防止）
    python local_bot.py once              # 1回だけ通常実行（スロット判定あり）
    python local_bot.py force             # 強制投稿（スロット判定なし）
    python local_bot.py force --bypass-score  # 強制投稿＋スコアゲート無視（effective<0は投稿しない）
    python local_bot.py daemon            # 常駐。JST毎時07分・37分に実行
    python local_bot.py status            # 状態確認

安全設計（維持）:
- mode は diagram 固定（link / test / normal / dry-run は復活させない）
- POST_ENABLED=true にしない限り X への実投稿はしない
- effective_score < 0 は強制でも投稿しない（post.py側で担保）
- 投稿成功後にだけ posted_slots.json に記録（post.py側で担保）
"""

import os
import sys
import json
import re
import time as time_mod
import signal
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
ENV_FILE = ROOT_DIR / ".env"
JST = ZoneInfo("Asia/Tokyo")

# スロット間隔（分）。post.py の SLOT_INTERVAL_MINUTES と揃える（既定30分）。
# .env の SLOT_INTERVAL_MINUTES で変更（例: 45）。1440を割り切る値のみ有効。
def _slot_interval_minutes() -> int:
    try:
        v = int(os.environ.get("SLOT_INTERVAL_MINUTES", "30"))
    except (TypeError, ValueError):
        return 30
    if v < 1 or 1440 % v != 0:
        return 30
    return v

# 実行が重ならないようにするロックファイル
LOCK_STALE_SECONDS = 30 * 60  # 30分以上残っている lock は stale とみなす

# Windowsコンソール(cp932)対策
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------------------
# .env / ディレクトリ / ログ
# ---------------------------------------------------------------------------

def load_env(require: bool = True) -> None:
    """リポジトリ直下の .env を読み込む（標準ライブラリのみの簡易ローダー）。
    既に設定済みの環境変数は上書きしない。"""
    if not ENV_FILE.exists():
        if require:
            print("[エラー] .env が見つかりません。")
            print(f"        期待する場所: {ENV_FILE}")
            print("        セットアップ: cp .env.example .env して各値を設定してください。")
            sys.exit(1)
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            # 明示的な実行時上書き（安全なdry-run等）を優先する。
            os.environ.setdefault(key, value)


def resolve_dir(env_name: str, default: str) -> Path:
    raw = os.environ.get(env_name, "").strip() or default
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT_DIR / p
    return p


def ensure_dirs() -> dict:
    dirs = {
        "state": resolve_dir("STATE_DIR", "data"),
        "log": resolve_dir("LOG_DIR", "logs"),
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def log(msg: str) -> None:
    line = f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        log_dir = resolve_dir("LOG_DIR", "logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "bot.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def env_flag(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("true", "1", "yes")


def check_api_keys() -> None:
    """不足しているAPIキー系環境変数を分かりやすく知らせる。
    - OPENAI_API_KEY が無いと候補生成できないためエラー
    - X系キーは POST_ENABLED=true のときだけ必須（falseなら警告のみ）
    """
    missing_x = [k for k in ("API_KEY", "API_KEY_SECRET",
                             "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET")
                 if not os.environ.get(k, "").strip()]
    missing_openai = not os.environ.get("OPENAI_API_KEY", "").strip()

    if missing_openai:
        print("[エラー] 環境変数 OPENAI_API_KEY が設定されていません（候補生成に必須）。")
        print("        .env に OPENAI_API_KEY=... を設定してください。")
        sys.exit(1)

    if missing_x:
        if env_flag("POST_ENABLED"):
            print(f"[エラー] POST_ENABLED=true ですが X APIキーが不足しています: {', '.join(missing_x)}")
            print("        .env に設定するか、POST_ENABLED=false にしてください。")
            sys.exit(1)
        log(f"[WARN] X APIキー未設定: {', '.join(missing_x)} "
            f"(POST_ENABLED=false のため実投稿はしないので続行)")


# ---------------------------------------------------------------------------
# スロット時刻計算
# ---------------------------------------------------------------------------

def _active_hours() -> set:
    """ACTIVE_HOURS（例 "7-9,12-13,18-23"）を時のsetに解析。空なら24時間。"""
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
                for h in range(min(int(a), int(b)), max(int(a), int(b)) + 1):
                    hours.add(h % 24)
            else:
                hours.add(int(part) % 24)
    except (TypeError, ValueError):
        return set(range(24))
    return hours or set(range(24))


def next_slot_dt(now: datetime) -> datetime:
    """now(JST) より後の、直近の有効スロット時刻を返す。
    スロットは 00:00 起点の SLOT_INTERVAL_MINUTES 間隔のうち、
    ACTIVE_HOURS の時間帯に入るものだけ（最大2日先まで探索）。"""
    step = _slot_interval_minutes()
    active = _active_hours()
    base = now.replace(second=0, microsecond=0)
    midnight = base.replace(hour=0, minute=0)
    minutes_since = int((base - midnight).total_seconds() // 60)
    idx = (minutes_since // step) + 1
    cand = midnight + timedelta(minutes=idx * step)
    # 最大2日分探索すれば必ず有効スロットに当たる
    for _ in range((1440 // step) * 2 + 2):
        if cand > now and cand.hour in active:
            return cand
        cand += timedelta(minutes=step)
    return cand


def _daily_review_time() -> time:
    """DAILY_REVIEW_AT（HH:MM、既定04:45）をJST時刻として返す。"""
    raw = os.environ.get("DAILY_REVIEW_AT", "04:45").strip()
    try:
        hour, minute = (int(x) for x in raw.split(":", 1))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    except (TypeError, ValueError):
        pass
    return time(4, 45)


def next_review_dt(now: datetime) -> datetime:
    """nowより後の次回日次レビュー時刻を返す。"""
    review_at = _daily_review_time()
    candidate = now.replace(
        hour=review_at.hour, minute=review_at.minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _run_due_review_after_start(now: datetime) -> None:
    """レビュー時刻後にデーモンを起動した場合、その日のレビューを補完する。"""
    if now.time() < _daily_review_time():
        return
    log("[INFO] daemon: checking today's integrated daily review")
    try:
        rc = cmd_report()
        log(f"[INFO] daemon: integrated daily review check end (exit={rc})")
    except Exception as e:
        log(f"[ERROR] daemon: integrated daily review failed: {e}")


# ---------------------------------------------------------------------------
# ロックファイル
# ---------------------------------------------------------------------------

def lock_path() -> Path:
    return resolve_dir("STATE_DIR", "data") / "bot.lock"


def acquire_lock() -> bool:
    """ロック取得。取得できたら True。既存ロックが stale なら奪って取得する。"""
    lp = lock_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    if lp.exists():
        try:
            age = time_mod.time() - lp.stat().st_mtime
        except OSError:
            age = 0
        if age < LOCK_STALE_SECONDS:
            return False
        log(f"[WARN] Stale lock detected (age {int(age)}s) -> removing: {lp}")
        try:
            lp.unlink()
        except OSError:
            return False
    try:
        # 排他的作成で競合を防ぐ
        with open(lp, "x", encoding="utf-8") as f:
            f.write(json.dumps({
                "pid": os.getpid(),
                "started_at_jst": datetime.now(JST).isoformat(),
            }))
        return True
    except FileExistsError:
        return False


def release_lock() -> None:
    try:
        lock_path().unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# post.py 実行
# ---------------------------------------------------------------------------

def run_post(extra_env: dict = None) -> int:
    """src/post.py diagram を別プロセスで1回実行する。
    cwd を src/ にして従来の実行形態（cd src && python post.py diagram）を再現しつつ、
    STATE_DIR / LOG_DIR は post.py 側がリポジトリ直下基準で解決するため
    cwd に依存せずファイル位置は壊れない。"""
    env = os.environ.copy()
    env.setdefault("TZ", "Asia/Tokyo")
    env.setdefault("DISABLE_TIME_API", "true")
    if extra_env:
        env.update(extra_env)

    if not acquire_lock():
        log("[INFO] Skip run: another run is in progress (lock file exists)")
        log(f"[INFO] Lock file: {lock_path()}")
        return 0

    try:
        proc = subprocess.run(
            [sys.executable, "post.py", "diagram"],
            cwd=str(SRC_DIR),
            env=env,
        )
        return proc.returncode
    finally:
        release_lock()


# ---------------------------------------------------------------------------
# コマンド実装
# ---------------------------------------------------------------------------

def cmd_once() -> int:
    load_env()
    ensure_dirs()
    check_api_keys()
    log("[INFO] local_bot: once run start")
    rc = run_post()
    log(f"[INFO] local_bot: once run end (exit={rc})")
    return rc


def cmd_force(bypass_score: bool) -> int:
    load_env()
    ensure_dirs()
    check_api_keys()
    extra = {"FORCE_POST": "true"}
    if bypass_score:
        extra["FORCE_BYPASS_SCORE"] = "true"
    log(f"[INFO] local_bot: force run start (bypass_score={str(bypass_score).lower()})")
    rc = run_post(extra)
    log(f"[INFO] local_bot: force run end (exit={rc})")
    return rc


def cmd_daemon() -> int:
    load_env()
    ensure_dirs()
    check_api_keys()

    stop = {"flag": False}

    def _handle_sigint(signum, frame):
        stop["flag"] = True
        log("[INFO] daemon: stop signal received. Exiting after current wait/run...")

    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        signal.signal(signal.SIGTERM, _handle_sigint)
    except (AttributeError, ValueError):
        pass  # Windows等でSIGTERM未対応でも続行

    log("[INFO] daemon: started")
    log(f"[INFO] daemon: POST_ENABLED={str(env_flag('POST_ENABLED')).lower()}")
    log(f"[INFO] daemon: integrated daily review at {_daily_review_time():%H:%M} JST")
    _run_due_review_after_start(datetime.now(JST))

    while not stop["flag"]:
        now = datetime.now(JST)
        post_nxt = next_slot_dt(now)
        review_nxt = next_review_dt(now)
        is_review = review_nxt < post_nxt
        nxt = review_nxt if is_review else post_nxt
        wait_sec = (nxt - now).total_seconds()
        event_name = "daily review" if is_review else "post"
        log(f"[INFO] daemon: next {event_name} at {nxt:%Y-%m-%d %H:%M} JST "
            f"(in {int(wait_sec)}s)")

        # 1分ごとポーリングではなく、次スロットまで sleep（Ctrl+C 応答用に分割sleep）
        end = time_mod.monotonic() + wait_sec
        while not stop["flag"]:
            remain = end - time_mod.monotonic()
            if remain <= 0:
                break
            time_mod.sleep(min(remain, 5.0))

        if stop["flag"]:
            break

        if is_review:
            log(f"[INFO] daemon: integrated daily review start ({nxt:%H:%M} JST)")
            try:
                rc = cmd_report()
                log(f"[INFO] daemon: integrated daily review end (exit={rc})")
            except Exception as e:
                log(f"[ERROR] daemon: integrated daily review failed: {e}")
        else:
            log(f"[INFO] daemon: run start (slot {nxt:%H:%M} JST)")
            try:
                rc = run_post()
                log(f"[INFO] daemon: run end (exit={rc})")
            except Exception as e:
                log(f"[ERROR] daemon: run failed: {e}")
        # 同一スロット内での再実行を防ぐため、スロット時刻+65秒までは必ず進める
        while datetime.now(JST) <= nxt + timedelta(seconds=65) and not stop["flag"]:
            time_mod.sleep(1.0)

    log("[INFO] daemon: stopped")
    return 0


def cmd_init_state() -> int:
    """ローカル移行初回用: 過去 CATCH_UP_HOURS 時間以内に開始済みのスロットを
    「トライ済み」として attempted_slots.json に登録する（実投稿はしない）。

    catch-up の未処理判定は attempted_slots.json を基準にするため、
    これをやらないと初回起動時に過去24時間の未トライスロットを古い順に
    回収しようとして、意図しないバックログ投稿になる。
    """
    load_env()
    ensure_dirs()

    # post.py の実装（スロット列挙・保存形式）をそのまま使い、二重実装によるズレを防ぐ
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    import post as post_mod  # noqa: E402

    now_jst = datetime.now(JST)
    hours = post_mod.CATCH_UP_HOURS
    window_slots = post_mod.slot_datetimes_in_window(now_jst, hours)

    attempted = post_mod._load_json(post_mod.ATTEMPTED_SLOTS_FILE, [])
    if not isinstance(attempted, list):
        attempted = []
    attempted_set = set(attempted)

    added = 0
    for slot, slot_dt in window_slots:
        key = post_mod.slot_key_for(slot_dt, slot)
        if key not in attempted_set:
            attempted.append(key)
            attempted_set.add(key)
            added += 1

    post_mod._save_json(post_mod.ATTEMPTED_SLOTS_FILE, attempted[-500:])

    log(f"[INFO] init-state: now JST = {now_jst:%Y-%m-%d %H:%M:%S}")
    log(f"[INFO] init-state: CATCH_UP_HOURS = {hours}")
    log(f"[INFO] init-state: slots in window = {len(window_slots)}")
    log(f"[INFO] init-state: newly marked as attempted = {added}")
    log(f"[INFO] init-state: attempted_slots total = {len(attempted[-500:])}")
    log(f"[INFO] init-state: file = {post_mod.ATTEMPTED_SLOTS_FILE}")
    log("[INFO] init-state: done. 以後は未来の設定済み投稿スロットから通常運用になります。")
    return 0


def cmd_report() -> int:
    """Review all bot posts from the latest 24 hours and learn from the top 3 by impressions.

    - Uses the authenticated user's own timeline (Owned Read when eligible).
    - Intersects X results with local posted_urls.json so manual posts are excluded.
    - Saves full review data under data/daily_reviews/.
    - Appends concise top-3 patterns to knowledge/viral_patterns/patterns.md.
    - Runs at most once per JST date unless FORCE_REPORT=true.
    """
    load_env()
    dirs = ensure_dirs()

    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    import post as post_mod  # noqa: E402
    from publishing_policy import calculate_growth_score  # noqa: E402

    now_jst = datetime.now(JST)
    window_hours = 24
    try:
        window_hours = max(1, min(int(os.environ.get("DAILY_REVIEW_WINDOW_HOURS", "24")), 168))
    except (TypeError, ValueError):
        window_hours = 24
    start_jst = now_jst - timedelta(hours=window_hours)
    review_date = now_jst.date().isoformat()
    force_report = env_flag("FORCE_REPORT", "false")

    state_file = dirs["state"] / "daily_review_state.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}

    if state.get("last_review_date_jst") == review_date and not force_report:
        log(f"[INFO] report: already completed for {review_date}; skip (set FORCE_REPORT=true to rerun)")
        return 0

    history = post_mod._load_json(post_mod.POSTED_URLS_FILE, [])
    if not isinstance(history, list):
        history = []

    def parse_posted_at(value: str):
        try:
            dt = datetime.fromisoformat((value or "").strip())
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=JST)
            return dt.astimezone(JST)
        except Exception:
            return None

    recent = []
    seen_ids = set()
    for h in history:
        if not isinstance(h, dict) or not h.get("tweet_id"):
            continue
        dt = parse_posted_at(h.get("posted_at_jst", ""))
        if dt is None or not (start_jst <= dt <= now_jst):
            continue
        tid = str(h.get("tweet_id"))
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        row = dict(h)
        row["_posted_dt"] = dt
        recent.append(row)

    if not recent:
        log(f"[INFO] report: no bot posts in latest {window_hours} hours")
        state.update({
            "last_review_date_jst": review_date,
            "last_reviewed_at_jst": now_jst.isoformat(),
            "reviewed_count": 0,
        })
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    local_by_id = {str(h["tweet_id"]): h for h in recent}
    log(f"[INFO] report: reviewing {len(local_by_id)} bot posts from latest {window_hours} hours")

    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=os.environ["API_KEY"],
            consumer_secret=os.environ["API_KEY_SECRET"],
            access_token=os.environ["ACCESS_TOKEN"],
            access_token_secret=os.environ["ACCESS_TOKEN_SECRET"],
        )

        user_cache = dirs["state"] / "x_user.json"
        user_id = ""
        try:
            cached = json.loads(user_cache.read_text(encoding="utf-8"))
            user_id = str(cached.get("id", "")).strip() if isinstance(cached, dict) else ""
        except Exception:
            pass
        if not user_id:
            me = client.get_me(user_auth=True)
            if not me.data:
                raise RuntimeError("X API get_me returned no user")
            user_id = str(me.data.id)
            user_cache.write_text(
                json.dumps({"id": user_id, "cached_at_jst": now_jst.isoformat()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        resp = client.get_users_tweets(
            id=user_id,
            start_time=start_jst.astimezone(ZoneInfo("UTC")),
            end_time=now_jst.astimezone(ZoneInfo("UTC")),
            max_results=100,
            tweet_fields=["public_metrics", "created_at"],
            user_auth=True,
        )
    except KeyError as e:
        log(f"[ERROR] report: missing X API key: {e}")
        return 1
    except Exception as e:
        log(f"[ERROR] report: failed to retrieve metrics: {e}")
        log("[INFO] report: confirm X API read credits and user authentication")
        return 1

    metrics = {}
    for t in (resp.data or []):
        tid = str(t.id)
        if tid not in local_by_id:
            continue
        pm = t.public_metrics or {}
        metrics[tid] = {
            "impressions": int(pm.get("impression_count", 0) or 0),
            "likes": int(pm.get("like_count", 0) or 0),
            "reposts": int(pm.get("retweet_count", 0) or 0),
            "replies": int(pm.get("reply_count", 0) or 0),
            "quotes": int(pm.get("quote_count", 0) or 0),
            "bookmarks": int(pm.get("bookmark_count", 0) or 0),
            "profile_clicks": int(pm.get("user_profile_clicks", 0) or 0),
            "url_clicks": int(pm.get("url_link_clicks", 0) or 0),
        }

    missing_tweet_errors = [
        {"reason": "missing_from_x_timeline", "tweet_id": tid,
         "topic_key": local_by_id[tid].get("topic_key", ""),
         "post_type": local_by_id[tid].get("post_type") or local_by_id[tid].get("type", "")}
        for tid in local_by_id if tid not in metrics
    ]

    if not metrics:
        log("[WARN] report: no matching metrics returned for local bot posts")
        return 1

    growth_weights = {
        "impressions_per_hour": float(os.environ.get("SCORE_WEIGHT_IMPRESSIONS_PER_HOUR", "0.25")),
        "engagement_rate": float(os.environ.get("SCORE_WEIGHT_ENGAGEMENT_RATE", "0.20")),
        "profile_clicks": float(os.environ.get("SCORE_WEIGHT_PROFILE_CLICKS", "0.25")),
        "quotes_bookmarks": float(os.environ.get("SCORE_WEIGHT_QUOTES_BOOKMARKS", "0.15")),
        "follow_conversion": float(os.environ.get("SCORE_WEIGHT_FOLLOW_CONVERSION", "0.15")),
    }
    rows = []
    for tid, h in local_by_id.items():
        if tid not in metrics:
            continue
        m = metrics[tid]
        posted_dt = h["_posted_dt"]
        age_hours = max((now_jst - posted_dt).total_seconds() / 3600.0, 0.25)
        engagement_total = m["likes"] + m["reposts"] + m["replies"] + m["quotes"] + m["bookmarks"]
        impressions = m["impressions"]
        impressions_per_hour = round(impressions / age_hours, 2)
        engagement_rate = round((engagement_total / impressions), 6) if impressions else 0.0
        row = {
            "tweet_id": tid,
            "text": h.get("tweet_text", ""),
            "topic": h.get("topic_key", ""),
            "post_type": h.get("post_type") or h.get("type", ""),
            "hook_type": h.get("hook_type", ""),
            "critique_axis": h.get("critique_axis", ""),
            "model": h.get("openai_model", ""),
            "posted_at": h.get("posted_at_jst", ""),
            "posted_hour_jst": posted_dt.hour,
            "text_length": len(h.get("tweet_text", "") or ""),
            **m,
            "engagement_rate": engagement_rate,
            "impressions_per_hour": impressions_per_hour,
            "growth_score": 0.0,
        }
        row["growth_score"] = calculate_growth_score(row, growth_weights)
        rows.append(row)

    by_impressions = sorted(rows, key=lambda r: (r["impressions"], r["impressions_per_hour"]), reverse=True)
    by_growth = sorted(rows, key=lambda r: r["growth_score"], reverse=True)
    top3 = by_impressions[:3]
    growth_top3 = by_growth[:3]
    bottom3 = by_growth[-3:] if rows else []

    quality_errors = list(missing_tweet_errors)
    attempts_file = dirs["log"] / "post_attempts.jsonl"
    try:
        for line in attempts_file.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            try:
                rec_dt = datetime.fromisoformat(str(rec.get("ts_jst") or ""))
                if rec_dt.tzinfo is None:
                    rec_dt = rec_dt.replace(tzinfo=JST)
                if rec_dt.astimezone(JST) < start_jst:
                    continue
            except (TypeError, ValueError):
                continue
            if rec.get("reason") in {
                "relevance_gate_failed", "internal_label_leak", "ban_risk_or_unverified_block",
                "manual_delete",
            }:
                quality_errors.append(rec)
    except Exception:
        quality_errors = list(missing_tweet_errors)

    reviews_dir = dirs["state"] / "daily_reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    review_payload = {
        "reviewed_at_jst": now_jst.isoformat(),
        "window_start_jst": start_jst.isoformat(),
        "window_end_jst": now_jst.isoformat(),
        "ranking": "impressions_and_growth",
        "reviewed_count": len(rows),
        "top_impressions_3": top3,
        "top_growth_3": growth_top3,
        "bottom_3": bottom3,
        "quality_errors": quality_errors[-20:],
        "growth_score_weights": growth_weights,
        "all_posts": rows,
    }
    dated_file = reviews_dir / f"{now_jst:%Y-%m-%d}.json"
    latest_file = dirs["state"] / "daily_review_latest.json"
    payload_text = json.dumps(review_payload, ensure_ascii=False, indent=2)
    dated_file.write_text(payload_text, encoding="utf-8")
    latest_file.write_text(payload_text, encoding="utf-8")

    log("[INFO] report: top 3 by impressions")
    for idx, r in enumerate(top3, 1):
        log(
            f"  #{idx} imp={r['impressions']} imp/h={r['impressions_per_hour']} "
            f"growth={r['growth_score']} post_type={r['post_type']} hook_type={r['hook_type']}"
        )

    patterns_dir = ROOT_DIR / "knowledge" / "viral_patterns"
    patterns_dir.mkdir(parents=True, exist_ok=True)

    def style_signature(value: str) -> str:
        """Learn layout signals only; never persist political claims or outrage wording."""
        value = value or ""
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        bullets = sum(line.startswith(("-", "・", "●", "○", "▶", "➡")) for line in lines)
        emojis = []
        for symbol in re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", value):
            if symbol not in emojis:
                emojis.append(symbol)
        return (
            f"blocks={len([b for b in re.split(r'\n\s*\n', value) if b.strip()])} "
            f"lines={len(lines)} bullets={bullets} emoji_types={len(emojis)} "
            f"emoji_samples={' '.join(emojis[:5]) or '-'}"
        )

    heading = f"## {now_jst:%Y-%m-%d %H:%M} 24h review"
    winning_lines = [heading]
    for rank, r in enumerate(growth_top3, 1):
        winning_lines.append(
            f"- #{rank} topic={r['topic']} growth={r['growth_score']} imp={r['impressions']} "
            f"post_type={r['post_type']} hook_type={r['hook_type']} axis={r['critique_axis']} "
            f"style=({style_signature(r['text'])})"
        )
    losing_lines = [heading]
    for rank, r in enumerate(bottom3, 1):
        losing_lines.append(
            f"- #{rank} topic={r['topic']} growth={r['growth_score']} imp={r['impressions']} "
            f"post_type={r['post_type']} hook_type={r['hook_type']} axis={r['critique_axis']} "
            f"style=({style_signature(r['text'])})"
        )
    avoid_lines = [heading] + [
        f"- reason={rec.get('reason','')} post_type={rec.get('post_type','')} topic={rec.get('topic_key','')}"
        for rec in quality_errors[-5:]
    ]
    for filename, lines in (
        ("winning_patterns.md", winning_lines),
        ("losing_patterns.md", losing_lines),
        ("avoid_patterns.md", avoid_lines),
    ):
        with open(patterns_dir / filename, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(lines) + "\n")

    state.update({
        "last_review_date_jst": review_date,
        "last_reviewed_at_jst": now_jst.isoformat(),
        "reviewed_count": len(rows),
        "top3_tweet_ids": [r["tweet_id"] for r in top3],
        "latest_file": str(latest_file),
    })
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"[INFO] report: learning data appended to {patterns_dir}")
    log(f"[INFO] report: full review saved to {dated_file}")
    log("[INFO] report: next post generation will use the latest winning patterns")
    return 0

def cmd_status() -> int:
    load_env(require=False)
    dirs = ensure_dirs()
    state_dir = dirs["state"]

    slots_file = state_dir / "posted_slots.json"
    attempted_file = state_dir / "attempted_slots.json"
    urls_file = state_dir / "posted_urls.json"

    def load_list(p: Path) -> list:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    slots = load_list(slots_file)
    attempted = load_list(attempted_file)
    history = load_list(urls_file)

    now = datetime.now(JST)
    nxt = next_slot_dt(now)

    print("=== politics-narrative ローカルBot 状態 ===")
    print(f"現在JST時刻          : {now:%Y-%m-%d %H:%M:%S}")
    print(f"次回実行予定時刻      : {nxt:%Y-%m-%d %H:%M} JST")
    print(f".env                 : {'あり' if ENV_FILE.exists() else 'なし (cp .env.example .env)'}")
    print(f"STATE_DIR            : {state_dir}")
    print(f"LOG_DIR              : {dirs['log']}")
    print(f"posted_slots.json    : {len(slots)} 件（投稿成功slot）")
    print(f"attempted_slots.json : {len(attempted)} 件（トライ済みslot・catch-up基準）")
    print(f"posted_urls.json     : {len(history)} 件（投稿履歴）")
    print(f"POST_ENABLED         : {os.environ.get('POST_ENABLED', '(未設定→false扱い)')}")
    print(f"X_SEARCH_ENABLED     : {os.environ.get('X_SEARCH_ENABLED', '(未設定→false扱い)')}")
    print(f"X_SEARCH_QUERY       : {os.environ.get('X_SEARCH_QUERY', '(未設定)')}")
    print(f"SOURCE_SCHEDULE_SPLIT: {os.environ.get('SOURCE_SCHEDULE_SPLIT', '(未設定→true扱い)')}")
    print(f"MIN_POST_SCORE       : {os.environ.get('MIN_POST_SCORE', '(未設定→6.3)')}")
    print(f"MAX_DAILY_POSTS      : {os.environ.get('MAX_DAILY_POSTS', '(未設定→16)')}")
    print(f"MIN_POST_INTERVAL_MINUTES: {os.environ.get('MIN_POST_INTERVAL_MINUTES', '(未設定→45)')}")
    print(f"TOPIC_COOLDOWN_HOURS : {os.environ.get('TOPIC_COOLDOWN_HOURS', '(未設定→4)')}")
    print(f"CATCH_UP_HOURS       : {os.environ.get('CATCH_UP_HOURS', '(未設定→24)')}")
    print(f"MAX_POSTS_PER_RUN    : {os.environ.get('MAX_POSTS_PER_RUN', '(未設定→1)')}")
    print(f"OPENAI_MODEL_DEFAULT : {os.environ.get('OPENAI_MODEL_DEFAULT', '(未設定→gpt-5-nano)')}")
    print(f"OPENAI_MODEL_IMPORTANT: {os.environ.get('OPENAI_MODEL_IMPORTANT', '(未設定→gpt-5-mini)')}")
    print(f"OPENAI_MONTHLY_BUDGET_USD: {os.environ.get('OPENAI_MONTHLY_BUDGET_USD', '(未設定→8.0)')}")
    usage_file = state_dir / "openai_usage.json"
    try:
        with open(usage_file, "r", encoding="utf-8") as f:
            usage = json.load(f)
        print(f"OpenAI今月推定額USD   : {float(usage.get('estimated_cost_usd', 0.0)):.4f}")
        print(f"OpenAI今月API calls   : {int(usage.get('calls', 0) or 0)}")
    except Exception:
        print("OpenAI今月使用量      : まだ記録なし")

    last = None
    for h in reversed(history):
        if isinstance(h, dict) and h.get("tweet_id"):
            last = h
            break
    if last:
        print("--- 直近投稿 ---")
        print(f"  posted_at_jst : {last.get('posted_at_jst', '')}")
        print(f"  tweet_id      : {last.get('tweet_id', '')}")
        print(f"  type/genre    : {last.get('type', '')} / {last.get('genre', '')}")
        print(f"  title         : {last.get('title', '')}")
    else:
        print("直近投稿履歴       : なし")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="politics-narrative ローカル運用Bot (Xテキスト専用 / 意見図解)",
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="ヘルプを表示して終了")
    parser._optionals.title = "オプション"
    sub = parser.add_subparsers(title="コマンド", dest="command", required=True)

    sub.add_parser("once", help="1回だけ通常実行（スロット判定あり）")

    p_force = sub.add_parser("force", help="強制投稿（スロット判定なし）")
    p_force.add_argument("--bypass-score", action="store_true",
                         help="スコアゲートも無視する（effective_score<0 は投稿しない）")

    sub.add_parser("daemon", help="常駐実行（スロット間隔・時間帯は.envで設定）")
    sub.add_parser("init-state", help="初回用: 過去スロットを処理済み化（バックログ暴発防止）")
    sub.add_parser("status", help="状態確認")
    sub.add_parser("report", help="投稿実績（インプレッション等）を取得しknowledge/へ学習パターンを書き出す")

    args = parser.parse_args()

    if args.command == "once":
        return cmd_once()
    if args.command == "force":
        return cmd_force(bypass_score=args.bypass_score)
    if args.command == "daemon":
        return cmd_daemon()
    if args.command == "init-state":
        return cmd_init_state()
    if args.command == "status":
        return cmd_status()
    if args.command == "report":
        return cmd_report()
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
