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

# 監視間隔（分）。MONITOR_INTERVAL_MINUTESを優先し、既定60分。
# 互換用にSLOT_INTERVAL_MINUTESも読み込む。1440を割り切る値のみ有効。
def _slot_interval_minutes() -> int:
    try:
        v = int(os.environ.get("MONITOR_INTERVAL_MINUTES",
                               os.environ.get("SLOT_INTERVAL_MINUTES", "60")))
    except (TypeError, ValueError):
        return 60
    if v < 1 or 1440 % v != 0:
        return 60
    return v


def _monitor_schedule_minute() -> int:
    try:
        return max(0, min(59, int(os.environ.get("MONITOR_SCHEDULE_MINUTE", "0"))))
    except (TypeError, ValueError):
        return 0

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
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    try:
        from metrics_db import init_db, migrate_json_state  # noqa: E402
        db_file = dirs["state"] / "bot_metrics.db"
        init_db(db_file)
        migrate_json_state(dirs["state"], db_file)
    except Exception:
        pass
    return dirs


def log(msg: str) -> None:
    line = f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)


def atomic_write_text(path: Path, text: str) -> None:
    """Replace a state file atomically, including files created by an elevated task."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        os.replace(temporary, path)
    except PermissionError:
        # Files created by an elevated scheduled task can be read-only to the
        # interactive sandbox user. Preserve them and write an explicit local
        # fallback instead of weakening directory ACLs.
        fallback = path.with_name(f"{path.stem}.local{path.suffix}")
        os.replace(temporary, fallback)
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
    origin = midnight + timedelta(minutes=_monitor_schedule_minute())
    elapsed = (base - origin).total_seconds() / 60
    idx = max(0, int(elapsed // step) + 1)
    cand = origin + timedelta(minutes=idx * step)
    # 最大2日分探索すれば必ず有効スロットに当たる
    for _ in range((1440 // step) * 2 + 2):
        if cand > now and cand.hour in active:
            return cand
        cand += timedelta(minutes=step)
    return cand


def _daily_review_time() -> time:
    """Return the synchronous daily-review start time in JST."""
    raw = os.environ.get(
        "DAILY_REVIEW_TIME", os.environ.get("DAILY_REVIEW_AT", "04:40")
    ).strip()
    try:
        hour, minute = (int(x) for x in raw.split(":", 1))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    except (TypeError, ValueError):
        pass
    return time(4, 40)


def next_review_dt(now: datetime) -> datetime:
    """nowより後の次回日次レビュー時刻を返す。"""
    review_at = _daily_review_time()
    candidate = now.replace(
        hour=review_at.hour, minute=review_at.minute, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _parse_clock_list(raw: str) -> list[time]:
    out = []
    for value in raw.split(","):
        try:
            hour, minute = (int(part) for part in value.strip().split(":", 1))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                out.append(time(hour, minute))
        except (TypeError, ValueError):
            continue
    return out


def next_auxiliary_event(now: datetime) -> tuple[datetime, str]:
    """Return the next non-posting maintenance event."""
    candidates = []
    for clock in _parse_clock_list(os.environ.get(
        "ENGAGEMENT_QUEUE_SCHEDULE", "12:20,20:20")):
        for day_offset in (0, 1):
            value = (now + timedelta(days=day_offset)).replace(
                hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
            if value > now:
                candidates.append((value, "engagement_queue"))
                break
    for clock in _parse_clock_list(os.environ.get(
        "FOLLOWER_SNAPSHOT_SCHEDULE", "00:05,23:55")):
        for day_offset in (0, 1):
            value = (now + timedelta(days=day_offset)).replace(
                hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
            if value > now:
                candidates.append((value, "follower_snapshot"))
                break
    weekday = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3,
               "FRI": 4, "SAT": 5, "SUN": 6}
    weekly_specs = (
        ("WEEKLY_BATCH_SUBMIT_DAY", "SUN", "WEEKLY_BATCH_SUBMIT_TIME", "22:30",
         "weekly_batch_submit"),
        ("WEEKLY_BATCH_COLLECT_DAY", "MON", "WEEKLY_BATCH_COLLECT_TIME", "04:15",
         "weekly_batch_collect"),
    )
    for day_key, day_default, time_key, time_default, event_name in weekly_specs:
        target_day = weekday.get(os.environ.get(day_key, day_default).upper(), weekday[day_default])
        clocks = _parse_clock_list(os.environ.get(time_key, time_default))
        clock = clocks[0] if clocks else _parse_clock_list(time_default)[0]
        for day_offset in range(8):
            value = (now + timedelta(days=day_offset)).replace(
                hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
            if value > now and value.weekday() == target_day:
                candidates.append((value, event_name))
                break
    return min(candidates, key=lambda item: item[0])


def _run_auxiliary_event(event_name: str, scheduled_at: datetime) -> None:
    """Run a local/owned-read maintenance event at most once per slot."""
    state_path = resolve_dir("STATE_DIR", "data") / "aux_schedule_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    event_key = f"{event_name}:{scheduled_at:%Y-%m-%dT%H:%M}"
    if state.get(event_key):
        return
    try:
        if event_name == "engagement_queue":
            cmd_engagement_queue()
            cmd_engagement_brief()
        elif event_name == "follower_snapshot":
            cmd_follower_status(capture=True)
        elif event_name == "weekly_batch_submit":
            cmd_weekly_report()
        elif event_name == "weekly_batch_collect":
            cmd_batch_collect()
    finally:
        state[event_key] = datetime.now(JST).isoformat()
        state = dict(list(state.items())[-100:])
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
    try:
        from api_budget import startup_budget_lines  # noqa: E402
        for budget_line in startup_budget_lines():
            log(f"[INFO] budget: {budget_line}")
    except Exception as e:
        log(f"[WARN] daemon: budget configuration log skipped ({type(e).__name__})")
    log(f"[INFO] daemon: integrated daily review at {_daily_review_time():%H:%M} JST")
    try:
        from discord_notify import notify_startup  # noqa: E402
        if notify_startup():
            log("[INFO] daemon: Discord startup notification sent")
        else:
            log("[WARN] daemon: Discord startup notification not sent")
    except Exception as e:
        log(f"[WARN] daemon: Discord startup notification skipped ({type(e).__name__})")
    try:
        cmd_batch_collect()
    except Exception as e:
        log(f"[WARN] daemon: Batch API collection skipped ({type(e).__name__})")
    _run_due_review_after_start(datetime.now(JST))

    while not stop["flag"]:
        now = datetime.now(JST)
        post_nxt = next_slot_dt(now)
        review_nxt = next_review_dt(now)
        aux_nxt, aux_name = next_auxiliary_event(now)
        nxt, event_name = min(
            ((post_nxt, "post"), (review_nxt, "daily_review"), (aux_nxt, aux_name)),
            key=lambda item: item[0],
        )
        wait_sec = (nxt - now).total_seconds()
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

        if event_name == "daily_review":
            log(f"[INFO] daemon: integrated daily review start ({nxt:%H:%M} JST)")
            try:
                rc = cmd_report()
                log(f"[INFO] daemon: integrated daily review end (exit={rc})")
            except Exception as e:
                log(f"[ERROR] daemon: integrated daily review failed: {e}")
        elif event_name == "post":
            log(f"[INFO] daemon: run start (slot {nxt:%H:%M} JST)")
            attempts_path = resolve_dir("LOG_DIR", "logs") / "post_attempts.jsonl"
            try:
                attempts_size = attempts_path.stat().st_size
            except OSError:
                attempts_size = 0
            try:
                rc = run_post()
                log(f"[INFO] daemon: run end (exit={rc})")
            except Exception as e:
                rc = 1
                log(f"[ERROR] daemon: run failed: {e}")
                try:
                    from discord_notify import notify_error  # noqa: E402
                    notify_error("daemon_post_run", f"{type(e).__name__}: {e}",
                                 slot=f"{nxt:%Y-%m-%d %H:%M} JST")
                except Exception:
                    pass
            try:
                from discord_notify import notify_attempt_since  # noqa: E402
                notify_attempt_since(attempts_path, attempts_size, exit_code=rc)
            except Exception as e:
                log(f"[WARN] daemon: Discord run log skipped ({type(e).__name__})")
            if env_flag("PHASE3_ENABLED", "true") and env_flag("POST_METRICS_ENABLED", "true"):
                try:
                    cmd_collect_metrics()
                except Exception as e:
                    log(f"[WARN] daemon: metrics collection skipped ({type(e).__name__})")
        else:
            log(f"[INFO] daemon: auxiliary event start ({event_name})")
            try:
                _run_auxiliary_event(event_name, nxt)
                log(f"[INFO] daemon: auxiliary event end ({event_name})")
            except Exception as e:
                log(f"[WARN] daemon: auxiliary event failed ({event_name}, {type(e).__name__})")
        try:
            cmd_batch_collect()
        except Exception as e:
            log(f"[WARN] daemon: Batch API collection skipped ({type(e).__name__})")
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
    from review_scoring import (calculate_four_axes, eligible_winning_example,
                                safety_dimensions, winner_types)  # noqa: E402

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
    local_state_file = state_file.with_name(
        f"{state_file.stem}.local{state_file.suffix}")
    state_read_file = (
        local_state_file
        if local_state_file.exists()
        and (not state_file.exists() or local_state_file.stat().st_mtime > state_file.stat().st_mtime)
        else state_file
    )
    try:
        state = json.loads(state_read_file.read_text(encoding="utf-8"))
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
        atomic_write_text(state_file, json.dumps(state, ensure_ascii=False, indent=2))
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
        log("[WARN] report: using stored metrics without stopping the posting daemon")
        resp = type("StoredMetricsFallback", (), {"data": []})()
    except Exception as e:
        log(f"[ERROR] report: failed to retrieve metrics: {e}")
        log("[INFO] report: using stored 1h/24h/72h metrics")
        resp = type("StoredMetricsFallback", (), {"data": []})()

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
        try:
            from metrics_db import connect as review_connect, db_path as review_db_path  # noqa: E402
            with review_connect(review_db_path()) as conn:
                for tid in local_by_id:
                    row = conn.execute("""SELECT * FROM post_metrics WHERE tweet_id=?
                      ORDER BY measured_at DESC LIMIT 1""", (tid,)).fetchone()
                    if row:
                        metrics[tid] = {
                            "impressions": int(row["impressions"] or 0),
                            "likes": int(row["likes"] or 0),
                            "reposts": int(row["reposts"] or 0),
                            "replies": int(row["replies"] or 0),
                            "quotes": int(row["quotes"] or 0),
                            "bookmarks": int(row["bookmarks"] or 0),
                            "profile_clicks": (
                                int(row["profile_clicks"]) if row["profile_clicks"] is not None else None
                            ),
                            "url_clicks": (
                                int(row["url_clicks"]) if row["url_clicks"] is not None else None
                            ),
                        }
        except Exception:
            pass
    if not metrics:
        log("[WARN] report: no current or stored metrics; local empty review retained")
        state.update({
            "last_review_date_jst": review_date,
            "last_reviewed_at_jst": now_jst.isoformat(),
            "reviewed_count": 0,
            "local_only": True,
        })
        atomic_write_text(state_file, json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    growth_weights = {
        "impressions_per_hour": float(os.environ.get("SCORE_WEIGHT_IMPRESSIONS_PER_HOUR", "0.25")),
        "engagement_rate": float(os.environ.get("SCORE_WEIGHT_ENGAGEMENT_RATE", "0.20")),
        "profile_clicks": float(os.environ.get("SCORE_WEIGHT_PROFILE_CLICKS", "0.25")),
        "quotes_bookmarks": float(os.environ.get("SCORE_WEIGHT_QUOTES_BOOKMARKS", "0.15")),
        "follow_conversion": float(os.environ.get("SCORE_WEIGHT_FOLLOW_CONVERSION", "0.15")),
    }
    follower_change = None
    external_conversions = None
    try:
        from metrics_db import connect as metrics_connect, db_path as current_db_path  # noqa: E402
        with metrics_connect(current_db_path()) as conn:
            snapshots = conn.execute("""SELECT followers_count FROM follower_snapshots
              ORDER BY captured_at DESC LIMIT 2""").fetchall()
            if len(snapshots) == 2:
                follower_change = int(snapshots[0][0] or 0) - int(snapshots[1][0] or 0)
            external_conversions = int(conn.execute("""SELECT COUNT(*) FROM conversion_events
              WHERE occurred_at>=? AND occurred_at<=?""",
              (start_jst.isoformat(), now_jst.isoformat())).fetchone()[0])
    except Exception:
        pass
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
            "prompt_version": h.get("prompt_version", "v1"),
            "review_strategy_active": bool(
                h.get("review_strategy_active")),
            "review_strategy_experiment": h.get(
                "review_strategy_experiment", ""),
            "review_strategy_id": h.get("review_strategy_id", ""),
            "review_strategy_variant": h.get(
                "review_strategy_variant", "inactive"),
            "posted_at": h.get("posted_at_jst", ""),
            "posted_hour_jst": posted_dt.hour,
            "text_length": len(h.get("tweet_text", "") or ""),
            **m,
            "engagement_rate": engagement_rate,
            "impressions_per_hour": impressions_per_hour,
            "growth_score": 0.0,
            "x_attention_score_at_post": float(h.get("x_attention_score", 0) or 0),
            "x_unique_accounts_at_post": int(h.get("x_unique_accounts", 0) or 0),
            "follow_gain_estimate": (
                round(follower_change / max(1, len(local_by_id)), 3)
                if follower_change is not None else None
            ),
            "follow_conversion_estimate": (
                round(follower_change / max(1, len(local_by_id)), 3)
                if follower_change is not None else None
            ),
            "external_conversions": (
                round(external_conversions / max(1, len(local_by_id)), 3)
                if external_conversions is not None else None
            ),
        }
        row["growth_score"] = calculate_growth_score(row, growth_weights)
        row["four_axes"] = calculate_four_axes(row)
        row.update(safety_dimensions(row))
        row["trust_score"] = row["four_axes"]["trust_score"]
        row["eligible_winning_example"] = eligible_winning_example(row)
        row["winner_types"] = winner_types(row)
        rows.append(row)

    by_impressions = sorted(rows, key=lambda r: (r["impressions"], r["impressions_per_hour"]), reverse=True)
    by_growth = sorted(rows, key=lambda r: r["growth_score"], reverse=True)
    top3 = by_impressions[:3]
    eligible_rows = [row for row in rows if row["eligible_winning_example"]]
    growth_top3 = sorted(eligible_rows, key=lambda r: (
        r["four_axes"]["balanced_score"] or 0, r["growth_score"]), reverse=True)[:3]
    trust_top3 = sorted(eligible_rows, key=lambda r: (
        r["four_axes"]["trust_score"] or 0, r["growth_score"]), reverse=True)[:3]
    conversation_top3 = sorted(eligible_rows, key=lambda r: (
        r["four_axes"]["conversation_score"] or 0, r["growth_score"]), reverse=True)[:3]
    bottom3 = by_growth[-3:] if rows else []

    def performance_breakdown(field: str) -> dict:
        grouped = {}
        for row in rows:
            key = str(row.get(field, "") or "unknown")
            grouped.setdefault(key, []).append(row)
        return {
            key: {
                "count": len(values),
                "avg_growth_score": round(sum(v["growth_score"] for v in values) / len(values), 4),
                "avg_impressions": round(sum(v["impressions"] for v in values) / len(values), 2),
                "avg_profile_clicks": round(sum(v["profile_clicks"] for v in values) / len(values), 2),
            }
            for key, values in sorted(grouped.items())
        }

    growth_median = 0.0
    if by_growth:
        middle = len(by_growth) // 2
        growth_median = (
            by_growth[middle]["growth_score"] if len(by_growth) % 2
            else (by_growth[middle - 1]["growth_score"] + by_growth[middle]["growth_score"]) / 2
        )
    timing_rows = []
    for row in rows:
        attention = row["x_attention_score_at_post"]
        if attention < 3.0:
            timing = "before_attention_or_low_attention"
        elif attention >= 7.0:
            timing = "high_attention_or_late_followup"
        else:
            timing = "attention_growing"
        timing_rows.append({
            "tweet_id": row["tweet_id"],
            "timing_estimate": timing,
            "x_attention_score_at_post": attention,
            "growth_score": row["growth_score"],
        })
    x_timing_analysis = {
        "method_note": "投稿時点の集計値による推定。X注目度の時系列比較がないため断定しない。",
        "posts": timing_rows,
        "high_x_low_growth": [
            row["tweet_id"] for row in rows
            if row["x_attention_score_at_post"] >= 7.0 and row["growth_score"] < growth_median
        ],
        "low_x_high_growth": [
            row["tweet_id"] for row in rows
            if row["x_attention_score_at_post"] < 3.0 and row["growth_score"] > growth_median
        ],
    }

    opening_counts = {}
    for row in rows:
        first_line = next((line.strip() for line in row["text"].splitlines() if line.strip()), "")
        signature = re.sub(r"\d+", "#", first_line)[:40]
        if signature:
            opening_counts[signature] = opening_counts.get(signature, 0) + 1
    repeated_structures = [
        {"opening_signature": key, "count": count}
        for key, count in sorted(opening_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        if count >= 3
    ]

    prompt_version_comparison = {}
    for row in rows:
        version = str(row.get("prompt_version") or "v1")
        group = prompt_version_comparison.setdefault(version, {
            "count": 0, "impressions": [], "trust": [], "conversation": [],
            "follow_conversion_estimate": [], "corrections": 0, "deletions": 0,
        })
        group["count"] += 1
        group["impressions"].append(row.get("impressions"))
        group["trust"].append(row["four_axes"].get("trust_score"))
        group["conversation"].append(row["four_axes"].get("conversation_score"))
        if row.get("follow_conversion_estimate") is not None:
            group["follow_conversion_estimate"].append(row["follow_conversion_estimate"])
        group["corrections"] += int(bool(row.get("correction_required")))
        group["deletions"] += int(bool(row.get("delete_or_hide_required")))
    for group in prompt_version_comparison.values():
        def average(values):
            present = [float(value) for value in values if value is not None]
            return round(sum(present) / len(present), 4) if present else None
        count = max(1, group["count"])
        group.update({
            "average_impressions": average(group.pop("impressions")),
            "average_trust_score": average(group.pop("trust")),
            "average_conversation_score": average(group.pop("conversation")),
            "follow_conversion_estimate": average(group["follow_conversion_estimate"]),
            "correction_rate": group.pop("corrections") / count,
            "deletion_rate": group.pop("deletions") / count,
            "quality_eval_average": None,
            "adoption_rate": 1.0,
        })

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
                "unverified_x_claim", "manual_delete",
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
        "ranking": "spread_trust_conversation_business_balanced",
        "reviewed_count": len(rows),
        "top_impressions_3": top3,
        "top_trust_3": trust_top3,
        "top_conversation_3": conversation_top3,
        "top_growth_3": growth_top3,
        "bottom_3": bottom3,
        "quality_errors": quality_errors[-20:],
        "growth_score_weights": growth_weights,
        "four_axis_method": {
            "spread": "impressions, impressions/hour, reposts",
            "trust": "bookmarks, quotes, profile clicks, constructive replies, corrections",
            "conversation": "replies, unique repliers, manual reply candidates, depth",
            "business": "follow estimate, profile transitions, external clicks/conversions; missing fields normalize safely",
        },
        "x_timing_analysis": x_timing_analysis,
        "performance_breakdown": {
            "post_type": performance_breakdown("post_type"),
            "hook_type": performance_breakdown("hook_type"),
            "critique_axis": performance_breakdown("critique_axis"),
            "posted_hour_jst": performance_breakdown("posted_hour_jst"),
            "prompt_version": performance_breakdown("prompt_version"),
            "review_strategy_experiment": performance_breakdown(
                "review_strategy_experiment"),
            "review_strategy_variant": performance_breakdown(
                "review_strategy_variant"),
        },
        "repeated_structures": repeated_structures,
        "all_posts": rows,
        "winner_groups": {
            winner: [row["tweet_id"] for row in rows if winner in row.get("winner_types", [])]
            for winner in ("viral_winner", "trust_winner",
                           "conversation_winner", "conversion_winner")
        },
        "prompt_version_comparison": prompt_version_comparison,
    }
    from usage_reports import xai_roi  # noqa: E402
    review_payload["xai_attribution_daily"] = xai_roi(days=1)
    review_payload["xai_attribution_weekly"] = xai_roi(days=7)
    from threads_api import daily_review_summary  # noqa: E402
    review_payload["threads"] = daily_review_summary()
    from review_strategy import (  # noqa: E402
        activate_strategy,
        deactivate_strategy,
        evaluate_strategy_performance,
        strategy_status,
        summarize_operational_logs,
    )
    review_payload["operational_log_summary"] = summarize_operational_logs(
        dirs["log"], start_jst, now_jst)
    current_strategy = strategy_status(ROOT_DIR)
    prior_strategy = current_strategy.get("strategy", {})
    prior_evaluation = evaluate_strategy_performance(
        review_payload,
        prior_strategy,
        root_dir=ROOT_DIR,
        now=now_jst,
    )
    review_payload["prior_strategy_evaluation"] = prior_evaluation
    if prior_evaluation.get("status") == "rollback":
        review_payload["prior_strategy_rollback"] = deactivate_strategy(
            ROOT_DIR,
            reason=str(prior_evaluation.get("reason") or "automatic_rollback"),
            now=now_jst,
        )
    review_payload["current_active_strategy"] = {
        "active": bool(prior_strategy) and (
            prior_evaluation.get("status") != "rollback"),
        "strategy": prior_strategy,
    }
    # Local aggregation is authoritative; one bounded LLM call adds trend analysis.
    # Failure is recorded but never makes the daily review fail or blocks posting.
    from report_ai import analyze_report, compact_daily_payload  # noqa: E402
    dated_file = reviews_dir / f"{now_jst:%Y-%m-%d}.json"
    llm_result = analyze_report(
        task_type="daily_review",
        payload=compact_daily_payload(review_payload),
        root_dir=ROOT_DIR,
        state_dir=dirs["state"],
        target_json_path=str(dated_file),
        batch_metadata={"review_date": review_date, "root_dir": str(ROOT_DIR)},
        batch_dedupe_key=f"daily_review:{review_date}",
    )
    review_payload["llm_analysis"] = llm_result.get("analysis")
    review_payload["llm_route"] = llm_result.get("route", {})
    review_payload["llm_error"] = llm_result.get("error", "")
    review_payload["llm_usage"] = llm_result.get("usage_event", {})
    review_payload["llm_pending"] = bool(llm_result.get("pending"))
    review_payload["llm_batch_job"] = llm_result.get("batch_job", {})
    review_payload["chatgpt_strategy_activation"] = activate_strategy(
        review_payload["llm_analysis"],
        review_payload,
        root_dir=ROOT_DIR,
        now=now_jst,
    )
    if review_payload["llm_error"]:
        log(f"[WARN] report: LLM analysis unavailable; local review retained ({review_payload['llm_error']})")
    strategy_activation = review_payload["chatgpt_strategy_activation"]
    if strategy_activation.get("activated"):
        log(
            "[INFO] report: ChatGPT impression strategy activated "
            f"until {strategy_activation.get('expires_at', '')}"
        )
    else:
        log(
            "[INFO] report: ChatGPT impression strategy not activated "
            f"({strategy_activation.get('reason', '')})"
        )
    try:
        from discord_notify import notify_review_strategy_result  # noqa: E402
        review_payload["chatgpt_strategy_discord_sent"] = (
            notify_review_strategy_result(
                strategy_activation,
                prior_evaluation=prior_evaluation,
            )
        )
    except Exception:
        review_payload["chatgpt_strategy_discord_sent"] = False
    latest_file = dirs["state"] / "daily_review_latest.json"
    payload_text = json.dumps(review_payload, ensure_ascii=False, indent=2)
    atomic_write_text(dated_file, payload_text)
    atomic_write_text(latest_file, payload_text)
    reports_dir = ROOT_DIR / "reports" / "daily"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_lines = [f"# Daily Review {review_date}", "", f"Reviewed: {len(rows)} posts", ""]
    if review_payload.get("llm_analysis"):
        report_lines.append(review_payload["llm_analysis"].get("summary", ""))
        report_lines.extend(["", "## Recommendations", ""])
        report_lines.extend(f"- {item}" for item in review_payload["llm_analysis"].get("recommendations", []))
        strategy = review_payload["llm_analysis"].get(
            "impression_strategy") or {}
        report_lines.extend(["", "## Impression Strategy", ""])
        report_lines.append(str(strategy.get("summary") or ""))
        policy = strategy.get("next_day_policy") or {}
        report_lines.extend([
            "",
            f"- Post types: {', '.join(policy.get('post_type_priority', []))}",
            f"- Hooks: {', '.join(policy.get('hook_type_priority', []))}",
            f"- Hours JST: {', '.join(str(v) for v in policy.get('preferred_hours_jst', []))}",
            f"- Activation: {review_payload['chatgpt_strategy_activation'].get('reason', '')}",
        ])
    (reports_dir / f"{review_date}.md").write_text("\n".join(report_lines).strip() + "\n", encoding="utf-8")
    from metrics_db import (apply_additive_migrations, db_path as metrics_db_path,
                            write as db_write)  # noqa: E402
    review_db = metrics_db_path()
    apply_additive_migrations(review_db)
    llm_route = review_payload.get("llm_route") or {}
    db_write("""INSERT OR REPLACE INTO daily_reviews
      (review_date,generated_at,review_model,top_posts_json,bottom_posts_json,winning_patterns_json,
       losing_patterns_json,recommendations_json,input_tokens,output_tokens,estimated_cost_usd)
       VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
        review_date, now_jst.isoformat(), llm_route.get("used_model") or llm_route.get("model", "local"),
        json.dumps(top3, ensure_ascii=False), json.dumps(bottom3, ensure_ascii=False),
        json.dumps(growth_top3, ensure_ascii=False), json.dumps(bottom3, ensure_ascii=False),
        json.dumps((review_payload.get("llm_analysis") or {}).get("recommendations", []), ensure_ascii=False),
        int((review_payload.get("llm_usage") or {}).get("input_tokens", 0)),
        int((review_payload.get("llm_usage") or {}).get("output_tokens", 0)),
        float((review_payload.get("llm_usage") or {}).get("estimated_cost_usd", 0))), review_db)
    db_write("""UPDATE daily_reviews SET four_axes_json=?,winner_groups_json=?,
      prompt_version_comparison_json=?,local_only=? WHERE review_date=?""", (
        json.dumps({
            "spread": [row["four_axes"].get("spread_score") for row in rows],
            "trust": [row["four_axes"].get("trust_score") for row in rows],
            "conversation": [row["four_axes"].get("conversation_score") for row in rows],
            "conversion": [row["four_axes"].get("business_score") for row in rows],
        }, ensure_ascii=False),
        json.dumps(review_payload["winner_groups"], ensure_ascii=False),
        json.dumps(prompt_version_comparison, ensure_ascii=False),
        int(not bool(review_payload.get("llm_analysis"))), review_date,
    ), review_db)
    for row in rows:
        db_write("""INSERT INTO post_quality_dimensions
          (tweet_id,anger_score,personal_attack_score,partisan_bias_score,claim_risk,
           trust_score,correction_required,manual_delete_required,
           follow_conversion_estimate,winner_types_json,updated_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(tweet_id) DO UPDATE SET
           anger_score=excluded.anger_score,
           personal_attack_score=excluded.personal_attack_score,
           partisan_bias_score=excluded.partisan_bias_score,
           claim_risk=excluded.claim_risk,trust_score=excluded.trust_score,
           correction_required=excluded.correction_required,
           manual_delete_required=excluded.manual_delete_required,
           follow_conversion_estimate=excluded.follow_conversion_estimate,
           winner_types_json=excluded.winner_types_json,updated_at=excluded.updated_at""", (
            row["tweet_id"], row.get("anger_score"), row.get("personal_attack_score"),
            row.get("partisan_bias_score"), row.get("claim_risk"),
            row.get("trust_score"), int(bool(row.get("correction_required"))),
            int(bool(row.get("delete_or_hide_required"))),
            row.get("follow_conversion_estimate"),
            json.dumps(row.get("winner_types", []), ensure_ascii=False),
            now_jst.isoformat(),
        ), review_db)

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
        "chatgpt_strategy_activated": bool(
            review_payload["chatgpt_strategy_activation"].get("activated")),
        "chatgpt_strategy_reason": review_payload[
            "chatgpt_strategy_activation"].get("reason", ""),
    })
    atomic_write_text(state_file, json.dumps(state, ensure_ascii=False, indent=2))

    log(f"[INFO] report: learning data appended to {patterns_dir}")
    log(f"[INFO] report: full review saved to {dated_file}")
    log("[INFO] report: next post generation will use the latest winning patterns")
    return 0


def _run_long_report(task_type: str, output_folder: str, premium_requested: bool = False) -> int:
    """Create a local-only long report. It never calls the X posting API."""
    load_env()
    dirs = ensure_dirs()
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from report_ai import analyze_report  # noqa: E402

    reviews_dir = dirs["state"] / "daily_reviews"
    review_files = sorted(reviews_dir.glob("*.json"))[-7:]
    compact = []
    for path in review_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            compact.append({
                "date": path.stem,
                "reviewed_count": data.get("reviewed_count", 0),
                "performance_breakdown": data.get("performance_breakdown", {}),
                "llm_analysis": data.get("llm_analysis"),
            })
        except (OSError, json.JSONDecodeError):
            continue
    today = datetime.now(JST).date()
    week_start = today - timedelta(days=today.weekday())
    result = analyze_report(
        task_type=task_type,
        payload={"daily_reviews": compact, "requested_sections": [
            "weekly winners and losers", "growing and weakening topics", "post types", "hooks",
            "critique axes", "posting times", "X Search effect", "YouTube Shorts ideas",
            "long YouTube ideas", "note article ideas", "next week allocation",
            "prompt improvement proposals", "code improvement proposals for human approval only",
        ]},
        root_dir=ROOT_DIR,
        state_dir=dirs["state"],
        premium_requested=premium_requested,
        batch_metadata={
            "root_dir": str(ROOT_DIR), "report_date": today.isoformat(),
            "week_start": week_start.isoformat(),
            "week_end": (week_start + timedelta(days=6)).isoformat(),
        },
        batch_dedupe_key=f"{task_type}:{week_start.isoformat()}",
    )
    if result.get("pending"):
        job = result.get("batch_job") or {}
        log(f"[INFO] {task_type}: Batch API submitted ({job.get('batch_id', 'pending')}); "
            "result will be collected automatically")
        return 0
    if result.get("error"):
        log(f"[INFO] {task_type}: {result['error']}; no local report generated")
        return 0
    analysis = result.get("analysis") or {}
    out_dir = ROOT_DIR / "outputs" / output_folder
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{datetime.now(JST):%Y-%m-%d_%H%M}.md"
    lines = [f"# {task_type.replace('_', ' ').title()}", "", analysis.get("summary", "")]
    for title, key in (("Strengths", "strengths"), ("Weaknesses", "weaknesses"),
                       ("Recommendations", "recommendations"), ("Timing", "timing_findings")):
        lines.extend(["", f"## {title}", ""])
        lines.extend(f"- {item}" for item in analysis.get(key, []))
    expansion = {}
    if task_type == "weekly_report":
        recommendations = analysis.get("recommendations", [])
        expansion = {
            "youtube_shorts": recommendations[:3],
            "youtube_long": recommendations[:2],
            "note_articles": recommendations[:3],
            "prompt_improvements": analysis.get("weaknesses", [])[:3],
            "code_improvements_human_approval_required": recommendations[-3:],
        }
        for title, key in (("YouTube Shorts Candidates", "youtube_shorts"),
                           ("YouTube Long-form Candidates", "youtube_long"),
                           ("note Article Candidates", "note_articles"),
                           ("Prompt Improvement Proposals", "prompt_improvements"),
                           ("Code Improvement Proposals (Human Approval Required)", "code_improvements_human_approval_required")):
            lines.extend(["", f"## {title}", ""])
            lines.extend(f"- {item}" for item in expansion[key])
    out_file.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    if task_type == "weekly_report":
        weekly_dir = ROOT_DIR / "reports" / "weekly"
        weekly_dir.mkdir(parents=True, exist_ok=True)
        weekly_file = weekly_dir / f"{datetime.now(JST):%Y-%m-%d}.md"
        weekly_file.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        from metrics_db import write as db_write  # noqa: E402
        today = datetime.now(JST).date()
        week_start = today - timedelta(days=today.weekday())
        db_write("""INSERT OR REPLACE INTO weekly_reviews
          (week_start,week_end,generated_at,review_model,summary_json,media_expansion_json,
           recommendations_json,estimated_cost_usd) VALUES (?,?,?,?,?,?,?,?)""", (
            week_start.isoformat(), (week_start + timedelta(days=6)).isoformat(), datetime.now(JST).isoformat(),
            (result.get("route") or {}).get("used_model") or (result.get("route") or {}).get("model", "local"),
            json.dumps({"summary": analysis.get("summary", "")}, ensure_ascii=False),
            json.dumps(expansion, ensure_ascii=False),
            json.dumps(analysis.get("recommendations", []), ensure_ascii=False),
            float((result.get("usage_event") or {}).get("estimated_cost_usd", 0))))
    log(f"[INFO] {task_type}: local-only report saved to {out_file}")
    return 0


def cmd_weekly_report() -> int:
    rc = _run_long_report("weekly_report", "weekly_reports")
    try:
        from quality_evals import export_human_review_sample  # noqa: E402
        log(f"[INFO] human-review sample: {export_human_review_sample()}")
    except Exception as exc:
        log(f"[WARN] human-review sample skipped ({type(exc).__name__})")
    if rc == 0:
        try:
            from content_pipeline import build_content_pipeline  # noqa: E402
            pipeline = build_content_pipeline()
            log(f"[INFO] content pipeline: status={pipeline.get('status')} "
                f"shorts={len(pipeline.get('shorts', []))} "
                f"note={len(pipeline.get('note_articles', []))}")
        except Exception as exc:
            log(f"[WARN] content pipeline skipped ({type(exc).__name__})")
    return rc


def cmd_premium_report() -> int:
    return _run_long_report("premium_report", "premium_reports", premium_requested=True)


def cmd_collect_metrics() -> int:
    load_env()
    dirs = ensure_dirs()
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from post_metrics import collect  # noqa: E402
    try:
        history = json.loads((dirs["state"] / "posted_urls.json").read_text(encoding="utf-8"))
    except Exception:
        history = []
    result = collect(history if isinstance(history, list) else [])
    log(f"[INFO] collect-metrics: {json.dumps(result, ensure_ascii=False)}")
    return 0


def cmd_batch_collect() -> int:
    load_env()
    dirs = ensure_dirs()
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from model_router import ModelRouter  # noqa: E402
    from openai_batch import collect  # noqa: E402
    pricing = ModelRouter(ROOT_DIR / "config" / "openai_model_pricing.json").pricing
    result = collect(state_dir=dirs["state"], pricing=pricing)
    log(f"[INFO] batch-collect: {json.dumps(result, ensure_ascii=False)}")
    return 0


def cmd_batch_status() -> int:
    load_env()
    dirs = ensure_dirs()
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from openai_batch import status  # noqa: E402
    rows = status(dirs["state"])
    if not rows:
        print("OpenAI Batch jobs: 0")
        return 0
    for row in rows:
        print(f"{row['status']:<12} {row['task_type']:<16} {row['model']:<20} "
              f"{row['custom_id']} error={row.get('error_type') or '-'}")
    return 0


def cmd_preview_extensions() -> int:
    load_env()
    dirs = ensure_dirs()
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from phase2 import save_extension_previews  # noqa: E402
    try:
        history = json.loads((dirs["state"] / "posted_urls.json").read_text(encoding="utf-8"))
    except Exception:
        history = []
    if not history:
        log("[INFO] preview-extensions: no post history")
        return 0
    paths = save_extension_previews(history[-1], ROOT_DIR)
    log(f"[INFO] preview-extensions: saved={len(paths)} auto_post=false")
    return 0


def cmd_db_status() -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from metrics_db import (apply_additive_migrations, db_path, init_db,
                            migrate_json_state, table_counts)  # noqa: E402
    path = db_path()
    ok = init_db(path)
    apply_additive_migrations(path)
    migrate_json_state(path.parent, path)
    print(f"SQLite: {'OK' if ok else 'fallback'}")
    print(f"Path: {path}")
    for name, count in table_counts(path).items():
        print(f"{name}: {count}")
    return 0


def cmd_budget_status() -> int:
    # Compatibility labels retained for older diagnostics:
    # OpenAI今月使用額 / 今月の予測着地
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from api_budget import (budget_configuration, effective_xai_limit, forecast,
                            xai_ledger_verified)  # noqa: E402
    from usage_reports import openai_usage_breakdown  # noqa: E402
    result = forecast()
    actual, projected = result["actual"], result["projected"]
    cfg = budget_configuration()
    xai_effective = effective_xai_limit()
    print("=== Budget status ===")
    print("OpenAI")
    print(f"- configured budget: ${cfg['providers']['openai']:.2f}")
    print(f"- used: ${actual['openai']:.4f}")
    print(f"- reserved: ${cfg['provider_reserves']['openai']:.2f}")
    print(f"- remaining: ${max(0, cfg['providers']['openai'] - actual['openai']):.4f}")
    print(f"- projected month end: ${projected['openai']:.4f}")
    print("xAI")
    print(f"- configured budget: ${cfg['providers']['xai']:.2f}")
    print(f"- effective budget: ${xai_effective:.2f}")
    print(f"- ledger verified: {str(xai_ledger_verified()).lower()}")
    print(f"- used: ${actual['xai']:.4f}")
    print(f"- reserved: ${cfg['provider_reserves']['xai']:.2f}")
    print(f"- remaining: ${max(0, xai_effective - actual['xai']):.4f}")
    print(f"- projected month end: ${projected['xai']:.4f}")
    print("X API")
    print(f"- configured budget: ${cfg['providers']['x']:.2f}")
    print(f"- used: ${actual['x']:.4f}")
    print(f"- reserved: ${cfg['provider_reserves']['x']:.2f}")
    print(f"- remaining: ${max(0, cfg['providers']['x'] - actual['x']):.4f}")
    print(f"- projected month end: ${projected['x']:.4f}")
    print("Total")
    print(f"- configured budget: ${cfg['configured_total']:.2f}")
    print(f"- effective spendable budget: ${cfg['effective_spendable']:.2f}")
    print(f"- used: ${actual['total']:.4f}")
    print(f"- remaining: ${max(0, cfg['effective_spendable'] - actual['total']):.4f}")
    print(f"- usage ratio: {result['usage_ratio'] * 100:.2f}%")
    print(f"- current warning stage: {result['current_warning_stage']}")
    print(f"- restriction level: {result['restriction_level']}")
    print(f"- USD/JPY rate: {cfg['usd_jpy_rate']:g}")
    print(f"- JPY display budget: JPY {cfg['jpy_budget_display']:,.0f}")
    print(f"- JPY projected month end: JPY {result['projected_jpy']:,.0f}")
    if not cfg["consistent"]:
        print("WARNING: provider budget sum does not match total budget")
    average = actual["xai"] / actual["xai_requests"] \
        if actual["xai_requests"] else 0
    print(f"xAI average cost/request: ${average:.6f}")
    print(f"X Search Post Reads: {actual['x_search_reads']}")
    print(f"X post creates: {actual['x_post_creates']}")
    print(f"Owned Reads: {actual['x_owned_reads']}")
    for model, cost in sorted(actual["models"].items()):
        print(f"OpenAI {model}: ${cost:.4f}")
    for task_type, row in openai_usage_breakdown()["task_types"].items():
        print(f"OpenAI {task_type}: ${row['cost_usd']:.4f} ({row['calls']} calls)")
    try:
        from audit_tools import verify_xai_ledger  # noqa: E402
        print("xAI ledger recommendation: "
              + verify_xai_ledger()["recommendation"])
    except Exception:
        pass
    print("Content pipeline weekly budget: $"
          + f"{float(os.environ.get('CONTENT_PIPELINE_WEEKLY_BUDGET_USD', '0.40')):.2f}")
    print(f"Projected month end: ${projected['total']:.4f}")
    return 0


def cmd_cost_forecast() -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from api_budget import budget_configuration, forecast  # noqa: E402
    result = forecast()
    actual, projected = result["actual"], result["projected"]
    cfg = budget_configuration()
    usd_jpy = cfg["usd_jpy_rate"]
    print("=== Cost forecast ===")
    print(f"News monitor runs this month: {actual['monitor_runs']}")
    print(f"X Search runs: {actual['x_search_runs']}")
    print(f"X Search reads: {actual['x_search_reads']}")
    print(f"X post creates: {actual['x_post_creates']}")
    print(f"Owned Reads: {actual['x_owned_reads']}")
    if actual["models"]:
        for model, cost in sorted(actual["models"].items()):
            print(f"OpenAI {model}: ${cost:.4f}")
    else:
        print("OpenAI model usage: $0.0000")
    print(f"OpenAI API used: ${actual['openai']:.4f}")
    print(f"xAI API used: ${actual['xai']:.4f}")
    print(f"X API used: ${actual['x']:.4f}")
    print(f"Total used: ${actual['total']:.4f} / about JPY {actual['total'] * usd_jpy:.0f}")
    print(f"7-day average: ${result['average_daily_usd_7d']:.4f}/day")
    print(f"Projected OpenAI: ${projected['openai']:.4f}")
    print(f"Projected xAI: ${projected['xai']:.4f}")
    print(f"Projected X API: ${projected['x']:.4f}")
    print(f"Projected total: ${projected['total']:.4f} / about JPY {result['projected_jpy']:.0f}")
    print(f"Current budget usage ratio: {result['usage_ratio'] * 100:.2f}%")
    print(f"Projected budget usage ratio: {result['projected_usage_ratio'] * 100:.2f}%")
    print(f"85% warning predicted date: {result['threshold_dates']['warning_85'] or 'not this month'}")
    print(f"93% restriction predicted date: {result['threshold_dates']['restrict_93'] or 'not this month'}")
    print(f"100% stop predicted date: {result['threshold_dates']['hard_stop_100'] or 'not this month'}")
    print(f"JPY budget display: JPY {cfg['jpy_budget_display']:,.0f}")
    print(f"Projected JPY remaining: JPY {result['remaining_jpy']:.0f}")
    print(f"Exchange rate: 1 USD = {usd_jpy:g} JPY")
    print(f"xAI X Search runs: {actual['xai_search_runs']}")
    print(f"xAI tool calls: {actual['xai_tool_calls']}")
    print(f"xAI requests: {actual['xai_requests']}")
    average = actual['xai'] / actual['xai_requests'] if actual['xai_requests'] else 0
    print(f"xAI average actual/request: ${average:.6f}")
    if result["warning"]:
        print("Cost warning: projected monthly cost reached 85% of budget")
    if result["pause_x_search"]:
        print("Paid features restricted: projected monthly cost reached 93% of budget")
    if result["block_non_breaking"]:
        print("Paid API operations stopped: projected monthly cost reached 100% of budget")
    return 0


def cmd_xai_roi() -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from usage_reports import xai_roi  # noqa: E402
    print(json.dumps(xai_roi(), ensure_ascii=False, indent=2))
    return 0


def cmd_openai_usage_breakdown() -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from usage_reports import openai_usage_breakdown  # noqa: E402
    print(json.dumps(openai_usage_breakdown(), ensure_ascii=False, indent=2))
    return 0


def cmd_engagement_queue() -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from engagement_queue import build_all  # noqa: E402
    result = build_all()
    print(json.dumps(result, ensure_ascii=False))
    print("X write operations: 0 (human approval required)")
    return 0


def cmd_engagement_brief() -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from engagement_queue import engagement_brief  # noqa: E402
    path = engagement_brief()
    print(f"Engagement brief: {path}")
    print("X write operations: 0 (human action only)")
    return 0


def cmd_eval_quality(mode: str, limit: int | None, confirm_full: bool) -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from quality_evals import run_quality_eval  # noqa: E402
    result = run_quality_eval(mode, limit, confirm_full)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("error") else 0


def cmd_import_human_review(source: str) -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from growth_tracking import import_human_reviews  # noqa: E402
    result = import_human_reviews(Path(source))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_import_conversions(source: str) -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from growth_tracking import import_conversions  # noqa: E402
    result = import_conversions(Path(source))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_follower_status(capture: bool = False) -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from growth_tracking import capture_follower_snapshot, follower_status  # noqa: E402
    if capture:
        print(json.dumps(capture_follower_snapshot(), ensure_ascii=False))
    print(json.dumps(follower_status(), ensure_ascii=False, indent=2))
    print("Follower attribution is a time-window estimate.")
    return 0


def cmd_quality_dashboard() -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from quality_evals import quality_dashboard  # noqa: E402
    print(json.dumps(quality_dashboard(), ensure_ascii=False, indent=2))
    return 0


def cmd_queue_status() -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from engagement_queue import status_counts  # noqa: E402
    labels = {"pending_quote": "pending quote candidates", "pending_reply": "pending reply candidates",
              "approved": "approved",
              "rejected": "rejected", "posted_manually": "posted manually", "expired": "expired"}
    for key, value in status_counts().items():
        print(f"{labels.get(key, key)}: {value}")
    return 0


def cmd_queue_update(queue_type: str, item_id: int, status: str) -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from engagement_queue import update_status  # noqa: E402
    ok = update_status(queue_type, item_id, status)
    print("updated" if ok else "not updated")
    return 0 if ok else 1


def cmd_queue_mark_posted(queue_type: str, item_id: int, post_id: str,
                          selected_option: str = "", notes: str = "") -> int:
    load_env(require=False)
    ensure_dirs()
    from engagement_queue import mark_posted  # noqa: E402
    ok = mark_posted(queue_type, item_id, post_id,
                     selected_option=selected_option, notes=notes)
    print(json.dumps({
        "updated": ok, "queue_type": queue_type, "queue_id": item_id,
        "manual_post_id": post_id, "x_writes": 0,
    }, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def cmd_import_engagement_results(source: str) -> int:
    load_env(require=False)
    ensure_dirs()
    from engagement_queue import import_engagement_results  # noqa: E402
    print(json.dumps(import_engagement_results(Path(source)),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_engagement_performance() -> int:
    load_env(require=False)
    ensure_dirs()
    from engagement_queue import engagement_performance  # noqa: E402
    print(json.dumps(engagement_performance(), ensure_ascii=False, indent=2))
    return 0


def cmd_verify_xai_ledger() -> int:
    load_env(require=False)
    ensure_dirs()
    from audit_tools import verify_xai_ledger  # noqa: E402
    result = verify_xai_ledger()
    print("xAI ledger verification")
    print("------------------------")
    labels = {
        "request_id_uniqueness": "request_id uniqueness",
        "ticks_conversion": "ticks conversion",
        "duplicate_aggregation": "duplicate aggregation",
        "cache_billing": "cache billing",
        "monthly_boundary": "monthly boundary",
        "actual_vs_estimated": "actual vs estimated",
        "tool_call_count": "tool call count",
    }
    for key, value in result["checks"].items():
        print(f"{labels.get(key, key):28}: {'PASS' if value['pass'] else 'FAIL'}")
    print("\nRecommendation:")
    print(result["recommendation"])
    return 0 if result["passed"] else 1


def cmd_discovery_provider_status() -> int:
    load_env(require=False)
    ensure_dirs()
    from audit_tools import discovery_provider_status  # noqa: E402
    print(json.dumps(discovery_provider_status(), ensure_ascii=False, indent=2))
    return 0


def cmd_follower_conversion_analysis() -> int:
    load_env(require=False)
    ensure_dirs()
    from growth_analytics import follower_conversion_analysis  # noqa: E402
    print(json.dumps(follower_conversion_analysis(), ensure_ascii=False, indent=2))
    return 0


def cmd_conversion_dashboard() -> int:
    load_env(require=False)
    ensure_dirs()
    from growth_analytics import conversion_dashboard  # noqa: E402
    print(json.dumps(conversion_dashboard(), ensure_ascii=False, indent=2))
    return 0


def cmd_digest_comparison() -> int:
    load_env(require=False)
    ensure_dirs()
    from growth_analytics import digest_comparison  # noqa: E402
    print(json.dumps(digest_comparison(), ensure_ascii=False, indent=2))
    return 0


def cmd_build_content_pipeline(week_start: str | None = None) -> int:
    load_env(require=False)
    ensure_dirs()
    from content_pipeline import build_content_pipeline  # noqa: E402
    result = build_content_pipeline(week_start)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Publish operations: 0")
    return 0 if result.get("status") in {
        "draft", "insufficient_data", "budget_restricted"
    } else 1


def cmd_generate_free_note(article_type: str | None, topic: str | None,
                           dry_run: bool) -> int:
    load_env(require=False)
    ensure_dirs()
    from free_note import generate_free_note  # noqa: E402
    result = generate_free_note(
        article_type=article_type, topic=topic, dry_run=dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Automatic note publish operations: 0")
    print("X write operations: 0")
    return 0 if result.get("status") in {
        "draft", "skipped", "update_candidate", "disabled"
    } else 1


def cmd_note_drafts() -> int:
    load_env(require=False)
    ensure_dirs()
    from free_note import list_notes  # noqa: E402
    print(json.dumps(list_notes(), ensure_ascii=False, indent=2))
    return 0


def cmd_note_status(content_id: str, status: str) -> int:
    load_env(require=False)
    ensure_dirs()
    from free_note import update_status  # noqa: E402
    result = update_status(content_id, status)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Automatic note publish operations: 0")
    return 0


def cmd_note_mark_published(content_id: str, url: str) -> int:
    load_env(require=False)
    ensure_dirs()
    from free_note import mark_published  # noqa: E402
    result = mark_published(content_id, url)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_note_discord_send(content_id: str, force: bool) -> int:
    load_env(require=False)
    ensure_dirs()
    from free_note import send_note_discord  # noqa: E402
    sent = send_note_discord(content_id, force=force)
    print(json.dumps({
        "content_id": content_id, "sent": sent,
        "automatic_note_publish": False, "x_writes": 0,
    }, ensure_ascii=False, indent=2))
    return 0 if sent else 1


def cmd_note_generate_cover(content_id: str) -> int:
    load_env(require=False)
    ensure_dirs()
    from free_note import generate_note_cover  # noqa: E402
    result = generate_note_cover(content_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Automatic note publish operations: 0")
    print("X write operations: 0")
    return 0


def cmd_amazon_link_set(content_id: str, url: str,
                        item_id: str | None, isbn: str | None) -> int:
    load_env(require=False)
    ensure_dirs()
    from amazon_associate import set_manual_link  # noqa: E402
    result = set_manual_link(
        content_id, url, item_id=item_id, isbn=isbn)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Amazon purchase operations: 0")
    print("Automatic note publish operations: 0")
    return 0


def cmd_amazon_links_status(content_id: str | None) -> int:
    load_env(require=False)
    ensure_dirs()
    from amazon_associate import links_status  # noqa: E402
    print(json.dumps(
        links_status(content_id), ensure_ascii=False, indent=2))
    return 0


def cmd_import_amazon_links(file_path: str) -> int:
    load_env(require=False)
    ensure_dirs()
    from amazon_associate import import_links  # noqa: E402
    result = import_links(Path(file_path).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Amazon purchase operations: 0")
    print("Automatic note publish operations: 0")
    return 0 if result.get("failed", 0) == 0 else 1


def cmd_amazon_links_disable(content_id: str) -> int:
    load_env(require=False)
    ensure_dirs()
    from amazon_associate import disable_for_note  # noqa: E402
    result = disable_for_note(content_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("Amazon purchase operations: 0")
    print("Automatic note publish operations: 0")
    return 0


def cmd_note_pipeline_status() -> int:
    load_env(require=False)
    ensure_dirs()
    from free_note import pipeline_status  # noqa: E402
    print(json.dumps(pipeline_status(), ensure_ascii=False, indent=2))
    return 0


def cmd_free_note_due() -> int:
    load_env(require=False)
    ensure_dirs()
    from free_note import generate_due  # noqa: E402
    results = generate_due()
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print("Automatic note publish operations: 0")
    print("X write operations: 0")
    return 0 if all(
        row.get("status") in {"draft", "skipped", "update_candidate", "disabled"}
        for row in results
    ) else 1


def cmd_profile_audit() -> int:
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from profile_audit import run  # noqa: E402
    print(f"Profile audit: {run(ROOT_DIR)}")
    print("Profile write operations: 0")
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
    print(f"OPENAI_MODEL_CLASSIFIER: {os.environ.get('OPENAI_MODEL_CLASSIFIER', '(未設定→gpt-5.4-nano)')}")
    print(f"OPENAI_MODEL_DEFAULT : {os.environ.get('OPENAI_MODEL_DEFAULT', '(未設定→gpt-5.4-mini)')}")
    print(f"OPENAI_MODEL_IMPORTANT: {os.environ.get('OPENAI_MODEL_IMPORTANT', '(未設定→gpt-5.6-luna)')}")
    print(f"OPENAI_MODEL_DAILY_REVIEW: {os.environ.get('OPENAI_MODEL_DAILY_REVIEW', '(未設定→important)')}")
    print(f"OPENAI_MONTHLY_BUDGET_USD: {os.environ.get('OPENAI_MONTHLY_BUDGET_USD', '(未設定→15.0)')}")
    usage_file = state_dir / "openai_usage.json"
    try:
        with open(usage_file, "r", encoding="utf-8") as f:
            usage = json.load(f)
        print(f"OpenAI今月推定額USD   : {float(usage.get('estimated_cost_usd', 0.0)):.4f}")
        print(f"OpenAI今月API calls   : {int(usage.get('calls', 0) or 0)}")
    except Exception:
        print("OpenAI今月使用量      : まだ記録なし")

    try:
        from audit_tools import discovery_provider_status, verify_xai_ledger  # noqa: E402
        provider = discovery_provider_status()
        ledger = verify_xai_ledger()
        print(f"X_TOPIC_DISCOVERY_PROVIDER: {provider['configured_provider']}")
        print(f"Discovery duplicate detected: "
              f"{provider['duplicate_provider_execution_detected']}")
        print(f"xAI ledger recommendation: {ledger['recommendation']}")
    except Exception as exc:
        print(f"Growth audit status unavailable: {type(exc).__name__}")
    try:
        from metrics_db import table_counts  # noqa: E402
        counts = table_counts()
        print(f"Quality eval runs     : {counts.get('quality_eval_runs', 0)}")
        print(f"Engagement results   : {counts.get('engagement_results', 0)}")
        print(f"Conversion events    : {counts.get('conversion_events', 0)}")
        print(f"Content pipeline runs: {counts.get('content_pipeline_runs', 0)}")
    except Exception:
        pass

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


def cmd_discord_test() -> int:
    load_env()
    ensure_dirs()
    from discord_notify import notify  # noqa: E402
    sent = notify(
        "test",
        "🔔 Discord通知テスト",
        "政治ニュースBot「久世ゆい」からのテスト通知です。",
        level="info",
        fields={"結果": "Webhook接続を確認しました"},
        force=True,
    )
    if sent:
        print("[INFO] Discord test notification sent.")
        return 0
    print("[ERROR] Discord test notification failed.")
    return 1


def cmd_discord_note_draft_test() -> int:
    load_env()
    ensure_dirs()
    from discord_notify import notify_note_draft_ready  # noqa: E402
    sent = notify_note_draft_ready({
        "title": "note draft専用チャンネル",
        "summary": "🔔 note draft通知の接続テストです。実際の記事は公開していません。",
        "status": "test",
        "source": "local_bot.py",
    }, test=True)
    if sent:
        print("[INFO] Discord note draft test notification sent.")
        return 0
    print("[ERROR] Discord note draft test notification failed.")
    return 1


def cmd_discord_log(source: str, lines: int) -> int:
    load_env()
    ensure_dirs()
    from discord_notify import notify_log_excerpt  # noqa: E402
    sent, count = notify_log_excerpt(
        source,
        lines=lines,
        log_dir=resolve_dir("LOG_DIR", "logs"),
        force=True,
    )
    if sent:
        print(f"[INFO] Discord log sent: source={source} lines={count}")
        return 0
    print(f"[ERROR] Discord log failed: source={source}")
    return 1


def _threads_output(action: str, **kwargs) -> int:
    """Run a Threads command and emit secret-free JSON."""
    load_env()
    ensure_dirs()
    import threads_api

    actions = {
        "auth_url": lambda: threads_api.authorization_url(
            scope_profile=kwargs.get("scope_profile", "basic")),
        "exchange_code": lambda: threads_api.exchange_code(kwargs["code"]),
        "token_status": lambda: threads_api.token_status(),
        "refresh_token": lambda: threads_api.refresh_token(
            force=bool(kwargs.get("force"))),
        "profile": lambda: threads_api.profile_status(),
        "status": lambda: threads_api.status(),
        "generate": lambda: threads_api.generate(
            dry_run=bool(kwargs.get("dry_run")),
            x_post_id=kwargs.get("x_post_id")),
        "drafts": lambda: threads_api.drafts(),
        "publish": lambda: threads_api.publish(int(kwargs["draft_id"])),
        "collect_metrics": lambda: threads_api.collect_metrics(),
        "comparison": lambda: threads_api.platform_comparison(),
        "run": lambda: threads_api.run_scheduled(),
        "endpoints": lambda: __import__(
            "threads_oauth_server").endpoint_urls(),
    }
    result = actions[action]()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if isinstance(result, dict) and result.get("reason") in {
        "threads_api_failed", "refresh_failed",
    }:
        return 1
    return 0


def _threads_full_output(action: str, **kwargs) -> int:
    """Run read-first Threads analytics or an explicitly approved action."""
    load_env()
    ensure_dirs()
    import threads_full_api as api

    actions = {
        "permissions": lambda: api.permissions(probe=True),
        "profile_sync": lambda: api.profile_sync(
            dry_run=kwargs.get("dry_run", False)),
        "sync_posts": lambda: api.sync_posts(
            dry_run=kwargs.get("dry_run", False),
            since=kwargs.get("since", ""), until=kwargs.get("until", "")),
        "sync_replies": lambda: api.sync_replies(
            dry_run=kwargs.get("dry_run", False)),
        "sync_mentions": lambda: api.sync_mentions(
            dry_run=kwargs.get("dry_run", False)),
        "post_insights": lambda: api.collect_post_insights(
            dry_run=kwargs.get("dry_run", False)),
        "account_insights": lambda: api.collect_account_insights(
            dry_run=kwargs.get("dry_run", False)),
        "search": lambda: api.search(
            kwargs["query"], search_type=kwargs.get("search_type", ""),
            search_mode=kwargs.get("search_mode", "KEYWORD"),
            since=kwargs.get("since", ""), until=kwargs.get("until", ""),
            hours=kwargs.get("hours", 0),
            dry_run=kwargs.get("dry_run", False)),
        "trends": lambda: api.trends(
            hours=kwargs.get("hours", 24),
            dry_run=kwargs.get("dry_run", False)),
        "quota": lambda: api.quota_status(
            dry_run=kwargs.get("dry_run", False)),
        "container": lambda: api.container_status(kwargs["container_id"]),
        "daily_report": lambda: api.daily_report(
            dry_run=kwargs.get("dry_run", False)),
        "weekly_report": lambda: api.weekly_report(
            dry_run=kwargs.get("dry_run", False)),
        "comparison": lambda: api.x_comparison(kwargs.get("days", 30)),
        "reply_draft": lambda: api.reply_draft(
            kwargs["target_id"], kwargs.get("text", "")),
        "reply_publish": lambda: api.apply_action(
            kwargs["draft_id"], confirm=kwargs.get("confirm", False)),
        "quote_draft": lambda: api.quote_draft(
            kwargs["target_id"], kwargs.get("text", "")),
        "quote_publish": lambda: api.apply_action(
            kwargs["draft_id"], confirm=kwargs.get("confirm", False)),
        "moderation_draft": lambda: api.moderation_draft(
            kwargs["target_id"], kwargs["moderation_action"]),
        "moderation_apply": lambda: api.apply_action(
            kwargs["draft_id"], confirm=kwargs.get("confirm", False)),
        "retention": lambda: api.data_retention_run(),
        "user_delete": lambda: api.user_data_delete(
            kwargs["user_id"], confirm=kwargs.get("confirm", False)),
        "full_sync": lambda: api.full_sync(
            dry_run=kwargs.get("dry_run", False)),
        "location": lambda: api.location_get(kwargs["location_id"]),
        "profile_discovery": lambda: api.profile_discovery(
            kwargs["username"], dry_run=kwargs.get("dry_run", False)),
    }
    if action == "direct_action":
        draft = api.direct_action_draft(
            kwargs["action_type"], kwargs["target_id"],
            reason=kwargs.get("reason", ""))
        if not kwargs.get("confirm") or draft.get("status") != "draft":
            result = {
                **draft,
                "reason": (
                    draft.get("reason") or "confirm_required"),
                "external_writes": 0,
            }
        else:
            result = api.apply_action(
                int(draft["draft_id"]), confirm=True)
    elif action in {"format_preview", "format_validate"}:
        spec_path = Path(kwargs["spec_file"]).resolve()
        if ROOT_DIR not in spec_path.parents:
            raise ValueError("Threads format spec must be inside repository")
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        result = (
            api.format_preview(spec) if action == "format_preview"
            else api.validate_format(spec))
    elif action == "format_publish":
        result = api.apply_action(
            kwargs["draft_id"], confirm=kwargs.get("confirm", False))
    else:
        result = actions[action]()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if isinstance(result, dict) and result.get("status") in {
        "failed", "rejected", "ambiguous",
    }:
        return 1
    return 0


def _crosspost_output(action: str, **kwargs) -> int:
    """Run the preview-first video cross-post pipeline."""
    load_env(require=False)
    ensure_dirs()
    import crosspost

    publication_id = kwargs.get("publication_id", "")
    actions = {
        "status": lambda: crosspost.status(),
        "candidates": lambda: crosspost.candidates(),
        "generate_copy": lambda: crosspost.generate_copy(
            publication_id, dry_run=kwargs.get("dry_run", True)),
        "render": lambda: crosspost.render_renditions(
            publication_id, dry_run=kwargs.get("dry_run", True)),
        "validate": lambda: crosspost.validate(
            publication_id, dry_run=kwargs.get("dry_run", True)),
        "prepare": lambda: crosspost.prepare(
            publication_id, dry_run=kwargs.get("dry_run", True)),
        "publish": lambda: crosspost.publish(
            publication_id, confirm=kwargs.get("confirm", False),
            dry_run=kwargs.get("dry_run", False)),
        "reconcile": lambda: crosspost.reconcile(publication_id),
        "metrics": lambda: crosspost.metrics_sync(
            publication_id, dry_run=kwargs.get("dry_run", True)),
        "report": lambda: crosspost.report(
            publication_id, dry_run=kwargs.get("dry_run", True)),
        "emergency_stop": lambda: crosspost.emergency_stop(),
        "instagram_auth_url": lambda: crosspost.instagram_auth_url(),
        "instagram_exchange": lambda: crosspost.instagram_exchange_code(
            kwargs.get("code", ""), dry_run=kwargs.get("dry_run", True)),
        "instagram_token": lambda: crosspost.token_status("instagram"),
        "instagram_profile": lambda: crosspost.instagram_profile(
            dry_run=kwargs.get("dry_run", True)),
        "instagram_reel_status": lambda: crosspost.instagram_reel_status(
            kwargs.get("creation_id", ""),
            dry_run=kwargs.get("dry_run", True)),
        "youtube_token": lambda: crosspost.token_status("youtube"),
    }
    result = actions[action]()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if isinstance(result, dict) and result.get("status") in {
        "failed", "ambiguous",
    }:
        return 1
    return 0


def _growth_output(action: str, **kwargs) -> int:
    """Run the local-only Phase A social content factory."""
    load_env(require=False)
    ensure_dirs()
    import social_content_factory as factory
    from runtime_config import audit as config_audit

    actions = {
        "config_audit": lambda: config_audit(),
        "packet": lambda: factory.generate_packet(
            topic_key=kwargs.get("topic_key"),
            content_id=kwargs.get("content_id"),
        ),
        "inventory_status": lambda: factory.inventory_status(),
        "inventory_build": lambda: factory.build_inventory(
            dry_run=kwargs.get("dry_run", True)),
        "variants": lambda: factory.generate_variants(
            dry_run=kwargs.get("dry_run", True)),
        "hypotheses": lambda: factory.hypotheses(
            dry_run=kwargs.get("dry_run", True)),
        "growth_status": lambda: factory.growth_status(),
        "daily_report": lambda: factory.daily_report(
            dry_run=kwargs.get("dry_run", True)),
        "weekly_report": lambda: factory.weekly_report(
            dry_run=kwargs.get("dry_run", True)),
        "shorts": lambda: factory.promote_short_candidates(
            dry_run=kwargs.get("dry_run", True)),
        "articles": lambda: factory.promote_article_candidates(
            dry_run=kwargs.get("dry_run", True)),
        "visuals": lambda: factory.visual_candidates(
            dry_run=kwargs.get("dry_run", True)),
        "threads": lambda: factory.thread_candidates(
            dry_run=kwargs.get("dry_run", True)),
        "replies": lambda: factory.reply_candidate_list(
            dry_run=kwargs.get("dry_run", True)),
        "quotes": lambda: factory.quote_candidate_list(
            dry_run=kwargs.get("dry_run", True)),
        "source_health": lambda: factory.source_health(),
        "budget": lambda: factory.budget_simulation(
            x_posts=kwargs.get("x_posts", 14),
            threads_posts=kwargs.get("threads_posts", 6),
            visuals=kwargs.get("visuals", 2),
            short_candidates=kwargs.get("short_candidates", 5),
            days=kwargs.get("days", 30),
        ),
        "full_cycle": lambda: factory.full_cycle(
            dry_run=kwargs.get("dry_run", True)),
    }
    result = actions[action]()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _current_affairs_output(action: str, **kwargs) -> int:
    """Run the local-only current-affairs Phase A engine."""
    load_env(require=False)
    ensure_dirs()
    import current_affairs

    actions = {
        "status": lambda: current_affairs.status(),
        "list": lambda: current_affairs.categories(),
        "classify": lambda: current_affairs.classify_items(
            content_id=kwargs.get("content_id"),
            dry_run=kwargs.get("dry_run", True)),
        "mix": lambda: current_affairs.mix_config(),
        "candidates": lambda: current_affairs.category_candidates(
            category=kwargs.get("category"),
            dry_run=kwargs.get("dry_run", True)),
        "performance": lambda: current_affairs.performance(
            category=kwargs.get("category")),
        "exclusions": lambda: current_affairs.exclusions(),
        "sources": lambda: current_affairs.source_health(),
        "daily": lambda: current_affairs.daily_report(
            dry_run=kwargs.get("dry_run", True)),
        "weekly": lambda: current_affairs.weekly_report(
            dry_run=kwargs.get("dry_run", True)),
        "full_cycle": lambda: current_affairs.full_cycle(
            dry_run=kwargs.get("dry_run", True)),
    }
    result = actions[action]()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def _social_anger_output(action: str, **kwargs) -> int:
    """Run the local-only evidence-based social anger Phase A engine."""
    load_env(require=False)
    ensure_dirs()
    import social_anger

    actions = {
        "status": lambda: social_anger.status(),
        "assess": lambda: social_anger.assess_items(
            content_id=kwargs.get("content_id"),
            dry_run=kwargs.get("dry_run", True)),
        "candidates": lambda: social_anger.candidate_cycle(
            content_id=kwargs.get("content_id"),
            dry_run=kwargs.get("dry_run", True)),
        "targets": lambda: social_anger.targets_report(),
        "risk": lambda: social_anger.risk_report(),
        "solution_gaps": lambda: social_anger.solution_gaps(),
        "daily": lambda: social_anger.daily_report(
            dry_run=kwargs.get("dry_run", True)),
        "weekly": lambda: social_anger.weekly_report(
            dry_run=kwargs.get("dry_run", True)),
        "full_cycle": lambda: social_anger.full_cycle(
            dry_run=kwargs.get("dry_run", True)),
    }
    result = actions[action]()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_review_strategy_status() -> int:
    """Show the currently active validated ChatGPT review strategy."""
    load_env(require=False)
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from review_strategy import strategy_status  # noqa: E402
    print(json.dumps(
        strategy_status(ROOT_DIR), ensure_ascii=False, indent=2))
    return 0


def cmd_review_strategy_disable(confirm: bool) -> int:
    """Disable the active ChatGPT strategy while preserving its audit trail."""
    load_env(require=False)
    if not confirm:
        print(json.dumps({
            "deactivated": False,
            "reason": "confirmation_required",
        }, ensure_ascii=False, indent=2))
        return 2
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from review_strategy import deactivate_strategy  # noqa: E402
    print(json.dumps(
        deactivate_strategy(
            ROOT_DIR, reason="manual_cli_disable"),
        ensure_ascii=False,
        indent=2,
    ))
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
    sub.add_parser("weekly-report", help="週次AI分析をローカル保存（既定では無効・X投稿なし）")
    sub.add_parser("premium-report", help="手動プレミアム分析をローカル保存（既定では無効・X投稿なし）")
    sub.add_parser("collect-metrics", help="1h/24h/72h投稿指標を未取得窓だけ収集")
    sub.add_parser("daily-review", help="日次レビューを実行")
    sub.add_parser(
        "review-strategy-status",
        help="ChatGPT日次レビューの有効方針を表示")
    p_strategy_disable = sub.add_parser(
        "review-strategy-disable",
        help="ChatGPT日次レビュー方針を監査履歴を残して停止")
    p_strategy_disable.add_argument(
        "--confirm", action="store_true", help="停止を実行")
    sub.add_parser("weekly-review", help="週次レビューをローカル保存")
    sub.add_parser("preview-extensions", help="拡張投稿案をプレビュー保存（投稿なし）")
    sub.add_parser("budget-status", help="今月のOpenAI/X/合計費用を表示")
    sub.add_parser("cost-forecast", help="過去7日から月末費用を予測")
    sub.add_parser("xai-roi", help="xAI由来投稿と非xAI投稿の費用対効果を比較")
    sub.add_parser("openai-usage-breakdown", help="OpenAI使用量をtask_type別に表示")
    sub.add_parser("db-status", help="SQLiteテーブル件数を表示")
    sub.add_parser("engagement-queue", help="人間承認用の引用・返信候補を保存（X送信なし）")
    sub.add_parser("engagement-brief", help="当日の手動引用・返信対応レポートを保存")
    sub.add_parser("queue-status", help="人間承認キューの状態を表示")
    p_queue = sub.add_parser("queue-update", help="人間承認キューの状態を更新")
    p_queue.add_argument("--type", required=True, choices=["quote", "reply"])
    p_queue.add_argument("--id", required=True, type=int)
    p_queue.add_argument("--status", required=True,
                         choices=["pending", "approved", "rejected", "expired",
                                  "posted_manually", "result_pending", "result_collected"])
    sub.add_parser("profile-audit", help="プロフィール透明性チェックリストを生成（変更なし）")
    sub.add_parser("batch-collect", help="OpenAI Batch APIの完了結果を回収")
    sub.add_parser("batch-status", help="OpenAI Batch APIジョブの状態を表示")
    p_eval = sub.add_parser("eval-quality", help="政治投稿の意味品質Evalsを実行")
    p_eval.add_argument("--mode", choices=["rule-only", "sample", "full"], default="rule-only")
    p_eval.add_argument("--limit", type=int)
    p_eval.add_argument("--confirm-full", action="store_true")
    p_human = sub.add_parser("import-human-review", help="人間評価CSV/JSONを取り込む")
    p_human.add_argument("--file", required=True)
    p_conversion = sub.add_parser("import-conversions", help="外部転換イベントCSVを取り込む")
    p_conversion.add_argument("--file", required=True)
    p_follower = sub.add_parser("follower-status", help="フォロワー推移を表示")
    p_follower.add_argument("--capture", action="store_true",
                            help="Owned Readを使って現在値を保存")
    sub.add_parser("quality-dashboard", help="直近の品質・安全・prompt版比較を表示")

    sub.add_parser("discord-test", help="Discord Webhookへテスト通知を送信")
    sub.add_parser(
        "discord-note-draft-test",
        help="note draft専用Discord Webhookへ接続テストを送信",
    )
    p_discord_log = sub.add_parser(
        "discord-log", help="直近ログを集計し、結果だけをDiscordへ送信")
    p_discord_log.add_argument(
        "--source", choices=["bot", "supervisor", "errors", "attempts"], default="bot")
    p_discord_log.add_argument("--lines", type=int, default=40)

    p_mark = sub.add_parser("queue-mark-posted", help="Record a manually posted X ID")
    p_mark.add_argument("--type", required=True, choices=["quote", "reply"])
    p_mark.add_argument("--id", required=True, type=int)
    p_mark.add_argument("--post-id", required=True)
    p_mark.add_argument("--selected-option", default="")
    p_mark.add_argument("--notes", default="")
    p_engagement_import = sub.add_parser(
        "import-engagement-results", help="Import manual engagement result CSV")
    p_engagement_import.add_argument("--file", required=True)
    sub.add_parser("engagement-performance", help="Report manual engagement performance")
    sub.add_parser("verify-xai-ledger", help="Verify the canonical xAI ledger")
    sub.add_parser("discovery-provider-status", help="Show paid discovery exclusivity")
    sub.add_parser("follower-conversion-analysis", help="Estimate follower conversion by post")
    sub.add_parser("conversion-dashboard", help="Report external conversion events")
    sub.add_parser("digest-comparison", help="Compare morning and evening digests")
    p_pipeline = sub.add_parser(
        "build-content-pipeline", help="Build local weekly Shorts and note drafts")
    p_pipeline.add_argument("--week-start")
    note_types = [
        "weekly_top5", "legislative_process", "cabinet_decision_vs_law",
        "social_insurance_burden", "party_policy_comparison",
        "evergreen_institutional_explainer", "weekly_deep_dive",
    ]
    p_free_note = sub.add_parser(
        "generate-free-note", help="無料note記事を生成してローカル保存")
    p_free_note.add_argument("--type", choices=note_types)
    p_free_note.add_argument("--topic")
    p_free_note.add_argument("--dry-run", action="store_true")
    sub.add_parser("note-drafts", help="無料note下書き一覧を表示")
    p_note_status = sub.add_parser(
        "note-status", help="無料note下書きの人間承認ステータスを更新")
    p_note_status.add_argument("--content-id", required=True)
    p_note_status.add_argument("--status", required=True, choices=[
        "draft", "reviewing", "approved", "revision_required", "rejected"])
    p_note_published = sub.add_parser(
        "note-mark-published", help="手動公開済みnoteのURLを記録")
    p_note_published.add_argument("--content-id", required=True)
    p_note_published.add_argument("--url", required=True)
    p_note_discord = sub.add_parser(
        "note-discord-send", help="保存済みnote下書きをDiscordへ送信")
    p_note_discord.add_argument("--content-id", required=True)
    p_note_discord.add_argument("--force", action="store_true")
    p_note_cover = sub.add_parser(
        "note-generate-cover",
        help="保存済みnote下書きの1280x670見出し画像を生成",
    )
    p_note_cover.add_argument("--content-id", required=True)
    p_amazon_link = sub.add_parser(
        "amazon-link-set",
        help="手動作成したAmazonアソシエイトリンクをnote下書きへ登録",
    )
    p_amazon_link.add_argument("--content-id", required=True)
    target_group = p_amazon_link.add_mutually_exclusive_group(required=True)
    target_group.add_argument("--item-id")
    target_group.add_argument("--isbn")
    p_amazon_link.add_argument("--url", required=True)
    p_amazon_status = sub.add_parser(
        "amazon-links-status", help="note下書きのAmazonリンク状態を表示")
    p_amazon_status.add_argument("--content-id")
    p_amazon_import = sub.add_parser(
        "import-amazon-links", help="AmazonアソシエイトリンクCSVを一括登録")
    p_amazon_import.add_argument("--file", required=True)
    p_amazon_disable = sub.add_parser(
        "amazon-links-disable",
        help="指定noteから関連書籍欄とアソシエイト設定を削除",
    )
    p_amazon_disable.add_argument("--content-id", required=True)
    sub.add_parser(
        "note-pipeline-status", help="無料noteパイプラインの状態を表示")
    sub.add_parser(
        "free-note-due", help="期限到来済みの無料note生成枠を処理")

    sub.add_parser(
        "config-audit", help=".env・typed config・runtime・文書の不整合を監査")
    p_packet = sub.add_parser(
        "content-packet-generate", help="確認済みテーマからコンテンツパケットを生成")
    p_packet.add_argument("--topic-key")
    p_packet.add_argument("--content-id")
    sub.add_parser(
        "content-inventory-status", help="コンテンツ在庫の状態と件数を表示")
    for command, help_text in (
        ("content-inventory-build", "確認済みニュースとEvergreenから在庫を構築"),
        ("content-variants-generate", "X・Threads向け複数角度候補を生成"),
        ("content-hypotheses", "検証可能なコンテンツ仮説を生成"),
        ("growth-daily-report", "成長KPIの日次レポートを保存"),
        ("growth-weekly-report", "成長KPIの週次レポートを保存"),
        ("short-candidates", "Short動画昇格候補を生成"),
        ("article-candidates", "X記事・note・長尺の昇格候補を生成"),
        ("visual-candidates", "自動図解候補を生成"),
        ("thread-candidates", "3〜5投稿のXスレッド候補を生成"),
        ("reply-candidates", "低リスク返信の人間承認候補を生成"),
        ("quote-candidates", "公式一次資料の引用候補を生成"),
        ("growth-full-cycle", "外部投稿なしでPhase A全工程を実行"),
    ):
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--dry-run", action="store_true")
    sub.add_parser("growth-status", help="需要発見・在庫・昇格エンジンの状態を表示")
    sub.add_parser("current-affairs-status", help="Current affairs Phase A status")
    sub.add_parser("category-list", help="List current affairs categories")
    p_category_classify = sub.add_parser("category-classify", help="Classify local topics")
    p_category_classify.add_argument("--content-id")
    p_category_classify.add_argument("--dry-run", action="store_true")
    sub.add_parser("category-mix-status", help="Show category mix safeguards")
    p_category_candidates = sub.add_parser("category-candidates", help="Build local category candidates")
    p_category_candidates.add_argument("--category")
    p_category_candidates.add_argument("--dry-run", action="store_true")
    p_category_performance = sub.add_parser("category-performance", help="Show category-relative performance")
    p_category_performance.add_argument("--category")
    sub.add_parser("category-exclusions", help="Show brand-fit exclusions")
    sub.add_parser("category-source-health", help="Show category source registry")
    for command, help_text in (
        ("current-affairs-daily-report", "Save category daily report"),
        ("current-affairs-weekly-report", "Save category weekly proposal"),
        ("current-affairs-full-cycle", "Run local-only current affairs Phase A"),
    ):
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--dry-run", action="store_true")
    sub.add_parser(
        "social-anger-status",
        help="事実と制度に基づく社会的怒りPhase Aの状態を表示")
    p_social_anger_assess = sub.add_parser(
        "social-anger-assess", help="負担・決定・責任と怒りの妥当性を評価")
    p_social_anger_assess.add_argument("--content-id")
    p_social_anger_assess.add_argument("--dry-run", action="store_true")
    p_social_anger_candidates = sub.add_parser(
        "social-anger-candidates", help="X・Threads向け複数角度候補を生成")
    p_social_anger_candidates.add_argument("--content-id")
    p_social_anger_candidates.add_argument("--dry-run", action="store_true")
    sub.add_parser(
        "social-anger-targets", help="批判対象の集中度を表示")
    sub.add_parser(
        "social-anger-risk-report", help="怒り搾取・集団攻撃・名誉毀損リスクを表示")
    sub.add_parser(
        "social-anger-solution-gaps", help="批判から改善策への未接続を表示")
    for command, help_text in (
        ("social-anger-daily-report", "社会的怒りPhase Aの日次レポートを保存"),
        ("social-anger-weekly-report", "社会的怒りPhase Aの週次提案を保存"),
        ("social-anger-full-cycle", "外部投稿なしで社会的怒りPhase A全工程を実行"),
    ):
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--dry-run", action="store_true")
    sub.add_parser("source-health", help="公式情報源レジストリの状態を表示")
    p_growth_budget = sub.add_parser(
        "growth-budget-simulation", help="新しい候補生産量のAPI費用を試算")
    p_growth_budget.add_argument("--x-posts", type=int, default=14)
    p_growth_budget.add_argument("--threads-posts", type=int, default=6)
    p_growth_budget.add_argument("--visuals", type=int, default=2)
    p_growth_budget.add_argument("--short-candidates", type=int, default=5)
    p_growth_budget.add_argument("--days", type=int, default=30)

    p_threads_auth = sub.add_parser(
        "threads-auth-url", help="Meta公式OAuth認可URLを表示")
    p_threads_auth.add_argument(
        "--scope-profile", choices=["basic", "full-analysis"],
        default="basic")
    p_threads_exchange = sub.add_parser(
        "threads-exchange-code", help="OAuthコードを長期トークンへ交換")
    p_threads_exchange.add_argument("--code", required=True)
    sub.add_parser("threads-token-status", help="Threadsトークン状態を表示")
    p_threads_refresh = sub.add_parser(
        "threads-refresh-token", help="期限接近時にThreadsトークンを更新")
    p_threads_refresh.add_argument("--force", action="store_true")
    sub.add_parser("threads-profile", help="Threadsプロフィール接続状態を表示")
    sub.add_parser("threads-status", help="Threads連携の稼働状態を表示")
    p_threads_generate = sub.add_parser(
        "threads-generate", help="Threads専用ドラフトを生成")
    p_threads_generate.add_argument("--dry-run", action="store_true")
    p_threads_generate.add_argument("--x-post-id")
    sub.add_parser("threads-drafts", help="Threadsドラフト一覧を表示")
    p_threads_publish = sub.add_parser(
        "threads-publish", help="承認済みThreadsドラフトを明示投稿")
    p_threads_publish.add_argument("--draft-id", type=int, required=True)
    sub.add_parser(
        "threads-collect-metrics", help="Threads Insightsを収集")
    sub.add_parser(
        "platform-comparison", help="同一コンテンツのX/Threads指標を比較")
    sub.add_parser("threads-run", help="Threadsの現在スケジュール枠を処理")
    sub.add_parser(
        "threads-endpoints", help="Meta管理画面へ登録する公開URLを表示")
    sub.add_parser(
        "threads-web", help="Threads OAuth callbackサーバーを起動")
    sub.add_parser(
        "threads-permissions", help="現在権限と不足権限を安全に表示")
    for command, help_text in (
        ("threads-profile-sync", "自分のThreadsプロフィールを同期"),
        ("threads-sync-posts", "自分のThreads投稿を増分同期"),
        ("threads-sync-replies", "返信ツリーと自分の返信を同期"),
        ("threads-sync-mentions", "自分へのメンションを同期"),
        ("threads-collect-post-insights", "投稿別Insightsを収集"),
        ("threads-collect-account-insights", "アカウントInsightsを収集"),
        ("threads-trends", "収集済み標本内の相対トレンドを集計"),
        ("threads-daily-report", "Threads日次レポートをローカル生成"),
        ("threads-weekly-report", "Threads週次レポートをローカル生成"),
        ("threads-full-sync", "取得・分析のみのThreads一括同期"),
    ):
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--dry-run", action="store_true")
        if command == "threads-sync-posts":
            item.add_argument("--since", default="")
            item.add_argument("--until", default="")
        if command == "threads-trends":
            item.add_argument("--hours", type=int, default=24)
    p_threads_search = sub.add_parser(
        "threads-search", help="公式APIでキーワードまたはトピックタグ検索")
    p_threads_search.add_argument("--query", required=True)
    p_threads_search.add_argument(
        "--search-type", choices=["TOP", "RECENT"], default="RECENT")
    p_threads_search.add_argument(
        "--search-mode", choices=["KEYWORD", "TAG"], default="KEYWORD")
    p_threads_search.add_argument("--since", default="")
    p_threads_search.add_argument("--until", default="")
    p_threads_search.add_argument("--hours", type=int, default=0)
    p_threads_search.add_argument("--dry-run", action="store_true")
    p_threads_quota = sub.add_parser(
        "threads-quota-status", help="Threads公式公開枠を取得")
    p_threads_quota.add_argument("--dry-run", action="store_true")
    p_threads_container = sub.add_parser(
        "threads-container-status", help="投稿コンテナ状態を取得")
    p_threads_container.add_argument("--container-id", required=True)
    p_threads_compare = sub.add_parser(
        "threads-x-comparison", help="XとThreadsの同一コンテンツ比較")
    p_threads_compare.add_argument("--days", type=int, default=30)
    p_reply_draft = sub.add_parser(
        "threads-reply-draft", help="人間承認用の返信案を保存")
    p_reply_draft.add_argument("--reply-to-id", required=True)
    p_reply_draft.add_argument("--text", default="")
    p_reply_publish = sub.add_parser(
        "threads-reply-publish", help="承認済み返信案を明示投稿")
    p_reply_publish.add_argument("--draft-id", type=int, required=True)
    p_reply_publish.add_argument("--confirm", action="store_true")
    p_quote_draft = sub.add_parser(
        "threads-quote-draft", help="人間承認用の引用投稿案を保存")
    p_quote_draft.add_argument("--post-id", required=True)
    p_quote_draft.add_argument("--text", default="")
    p_quote_publish = sub.add_parser(
        "threads-quote-publish", help="承認済み引用投稿案を明示投稿")
    p_quote_publish.add_argument("--draft-id", type=int, required=True)
    p_quote_publish.add_argument("--confirm", action="store_true")
    p_repost = sub.add_parser(
        "threads-repost", help="人間確認後に公式APIでリポスト")
    p_repost.add_argument("--post-id", required=True)
    p_repost.add_argument("--confirm", action="store_true")
    p_delete = sub.add_parser(
        "threads-delete", help="人間確認後に自分の投稿を削除")
    p_delete.add_argument("--post-id", required=True)
    p_delete.add_argument("--reason", required=True)
    p_delete.add_argument("--confirm", action="store_true")
    p_moderation_draft = sub.add_parser(
        "threads-moderation-draft", help="返信モデレーション案を保存")
    p_moderation_draft.add_argument("--reply-id", required=True)
    p_moderation_draft.add_argument(
        "--action", choices=["hide", "unhide"], required=True)
    p_moderation_apply = sub.add_parser(
        "threads-moderation-apply", help="人間確認後に返信管理を適用")
    p_moderation_apply.add_argument("--draft-id", type=int, required=True)
    p_moderation_apply.add_argument("--confirm", action="store_true")
    sub.add_parser(
        "threads-data-retention-run", help="Threads保存期限を適用")
    p_user_delete = sub.add_parser(
        "threads-user-data-delete", help="指定連携ユーザーのデータを削除")
    p_user_delete.add_argument("--user-id", required=True)
    p_user_delete.add_argument("--confirm", action="store_true")
    p_format_preview = sub.add_parser(
        "threads-format-preview", help="投稿形式JSONを検証して承認案を保存")
    p_format_preview.add_argument("--spec-file", required=True)
    p_format_validate = sub.add_parser(
        "threads-format-validate", help="投稿形式JSONを検証（保存なし）")
    p_format_validate.add_argument("--spec-file", required=True)
    p_format_publish = sub.add_parser(
        "threads-format-publish", help="承認済み形式投稿案を明示投稿")
    p_format_publish.add_argument("--draft-id", type=int, required=True)
    p_format_publish.add_argument("--confirm", action="store_true")
    p_location = sub.add_parser(
        "threads-location-get", help="公式APIで位置情報IDを取得")
    p_location.add_argument("--location-id", required=True)
    p_profile_discovery = sub.add_parser(
        "threads-profile-discovery",
        help="公式APIで完全一致の公開プロフィールを確認")
    p_profile_discovery.add_argument("--username", required=True)
    p_profile_discovery.add_argument("--dry-run", action="store_true")

    sub.add_parser(
        "crosspost-status", help="動画クロス投稿のローカル状態を表示")
    sub.add_parser(
        "crosspost-candidates", help="動画クロス投稿候補を表示")
    for command, help_text in (
        ("crosspost-generate-copy", "4媒体向け投稿文を個別生成"),
        ("crosspost-render-renditions", "4媒体向け動画をFFmpegで生成"),
        ("crosspost-validate", "動画・投稿文・安全条件を検証"),
        ("crosspost-prepare", "外部送信なしで公開準備を検証"),
        ("crosspost-reconcile", "媒体別結果を照合"),
        ("crosspost-metrics-sync", "媒体別指標の同期計画を確認"),
        ("crosspost-report", "クロスプラットフォーム分析を表示"),
    ):
        item = sub.add_parser(command, help=help_text)
        item.add_argument("--publication-id", default="")
        item.add_argument("--dry-run", action="store_true")
    p_cross_publish = sub.add_parser(
        "crosspost-publish",
        help="多重スイッチと明示確認を満たす場合のみクロス投稿")
    p_cross_publish.add_argument("--publication-id", default="")
    p_cross_publish.add_argument("--dry-run", action="store_true")
    p_cross_publish.add_argument("--confirm", action="store_true")
    sub.add_parser(
        "crosspost-emergency-stop", help="クロス投稿を緊急停止")

    sub.add_parser(
        "instagram-auth-url", help="Instagram Login公式OAuth URLを生成")
    p_instagram_exchange = sub.add_parser(
        "instagram-exchange-code", help="Instagram認証コード交換")
    p_instagram_exchange.add_argument("--code", required=True)
    p_instagram_exchange.add_argument("--dry-run", action="store_true")
    sub.add_parser(
        "instagram-token-status", help="Instagramトークン設定状態を表示")
    p_instagram_profile = sub.add_parser(
        "instagram-profile", help="Instagramプロアカウント状態を確認")
    p_instagram_profile.add_argument("--dry-run", action="store_true")
    p_instagram_reel = sub.add_parser(
        "instagram-reel-status", help="Instagram Reelコンテナ状態を確認")
    p_instagram_reel.add_argument("--creation-id", required=True)
    p_instagram_reel.add_argument("--dry-run", action="store_true")
    sub.add_parser(
        "youtube-token-status", help="YouTubeトークン設定状態を表示")

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
    if args.command == "weekly-report":
        return cmd_weekly_report()
    if args.command == "premium-report":
        return cmd_premium_report()
    if args.command == "collect-metrics":
        return cmd_collect_metrics()
    if args.command == "daily-review":
        return cmd_report()
    if args.command == "review-strategy-status":
        return cmd_review_strategy_status()
    if args.command == "review-strategy-disable":
        return cmd_review_strategy_disable(args.confirm)
    if args.command == "weekly-review":
        return cmd_weekly_report()
    if args.command == "preview-extensions":
        return cmd_preview_extensions()
    if args.command == "budget-status":
        return cmd_budget_status()
    if args.command == "cost-forecast":
        return cmd_cost_forecast()
    if args.command == "xai-roi":
        return cmd_xai_roi()
    if args.command == "openai-usage-breakdown":
        return cmd_openai_usage_breakdown()
    if args.command == "db-status":
        return cmd_db_status()
    if args.command == "engagement-queue":
        return cmd_engagement_queue()
    if args.command == "engagement-brief":
        return cmd_engagement_brief()
    if args.command == "queue-status":
        return cmd_queue_status()
    if args.command == "queue-update":
        return cmd_queue_update(args.type, args.id, args.status)
    if args.command == "queue-mark-posted":
        return cmd_queue_mark_posted(
            args.type, args.id, args.post_id, args.selected_option, args.notes)
    if args.command == "import-engagement-results":
        return cmd_import_engagement_results(args.file)
    if args.command == "engagement-performance":
        return cmd_engagement_performance()
    if args.command == "verify-xai-ledger":
        return cmd_verify_xai_ledger()
    if args.command == "discovery-provider-status":
        return cmd_discovery_provider_status()
    if args.command == "profile-audit":
        return cmd_profile_audit()
    if args.command == "batch-collect":
        return cmd_batch_collect()
    if args.command == "batch-status":
        return cmd_batch_status()
    if args.command == "eval-quality":
        return cmd_eval_quality(args.mode, args.limit, args.confirm_full)
    if args.command == "import-human-review":
        return cmd_import_human_review(args.file)
    if args.command == "import-conversions":
        return cmd_import_conversions(args.file)
    if args.command == "follower-status":
        return cmd_follower_status(args.capture)
    if args.command == "quality-dashboard":
        return cmd_quality_dashboard()
    if args.command == "follower-conversion-analysis":
        return cmd_follower_conversion_analysis()
    if args.command == "conversion-dashboard":
        return cmd_conversion_dashboard()
    if args.command == "digest-comparison":
        return cmd_digest_comparison()
    if args.command == "build-content-pipeline":
        return cmd_build_content_pipeline(args.week_start)
    if args.command == "generate-free-note":
        return cmd_generate_free_note(args.type, args.topic, args.dry_run)
    if args.command == "note-drafts":
        return cmd_note_drafts()
    if args.command == "note-status":
        return cmd_note_status(args.content_id, args.status)
    if args.command == "note-mark-published":
        return cmd_note_mark_published(args.content_id, args.url)
    if args.command == "note-discord-send":
        return cmd_note_discord_send(args.content_id, args.force)
    if args.command == "note-generate-cover":
        return cmd_note_generate_cover(args.content_id)
    if args.command == "amazon-link-set":
        return cmd_amazon_link_set(
            args.content_id, args.url, args.item_id, args.isbn)
    if args.command == "amazon-links-status":
        return cmd_amazon_links_status(args.content_id)
    if args.command == "import-amazon-links":
        return cmd_import_amazon_links(args.file)
    if args.command == "amazon-links-disable":
        return cmd_amazon_links_disable(args.content_id)
    if args.command == "note-pipeline-status":
        return cmd_note_pipeline_status()
    if args.command == "free-note-due":
        return cmd_free_note_due()
    if args.command == "config-audit":
        return _growth_output("config_audit")
    if args.command == "content-packet-generate":
        return _growth_output(
            "packet", topic_key=args.topic_key, content_id=args.content_id)
    if args.command == "content-inventory-status":
        return _growth_output("inventory_status")
    if args.command == "content-inventory-build":
        return _growth_output("inventory_build", dry_run=args.dry_run)
    if args.command == "content-variants-generate":
        return _growth_output("variants", dry_run=args.dry_run)
    if args.command == "content-hypotheses":
        return _growth_output("hypotheses", dry_run=args.dry_run)
    if args.command == "growth-status":
        return _growth_output("growth_status")
    if args.command == "growth-daily-report":
        return _growth_output("daily_report", dry_run=args.dry_run)
    if args.command == "growth-weekly-report":
        return _growth_output("weekly_report", dry_run=args.dry_run)
    if args.command == "short-candidates":
        return _growth_output("shorts", dry_run=args.dry_run)
    if args.command == "article-candidates":
        return _growth_output("articles", dry_run=args.dry_run)
    if args.command == "visual-candidates":
        return _growth_output("visuals", dry_run=args.dry_run)
    if args.command == "thread-candidates":
        return _growth_output("threads", dry_run=args.dry_run)
    if args.command == "reply-candidates":
        return _growth_output("replies", dry_run=args.dry_run)
    if args.command == "quote-candidates":
        return _growth_output("quotes", dry_run=args.dry_run)
    if args.command == "source-health":
        return _growth_output("source_health")
    if args.command == "growth-budget-simulation":
        return _growth_output(
            "budget", x_posts=args.x_posts, threads_posts=args.threads_posts,
            visuals=args.visuals, short_candidates=args.short_candidates,
            days=args.days)
    if args.command == "growth-full-cycle":
        return _growth_output("full_cycle", dry_run=args.dry_run)
    if args.command == "current-affairs-status":
        return _current_affairs_output("status")
    if args.command == "category-list":
        return _current_affairs_output("list")
    if args.command == "category-classify":
        return _current_affairs_output("classify", content_id=args.content_id, dry_run=args.dry_run)
    if args.command == "category-mix-status":
        return _current_affairs_output("mix")
    if args.command == "category-candidates":
        return _current_affairs_output("candidates", category=args.category, dry_run=args.dry_run)
    if args.command == "category-performance":
        return _current_affairs_output("performance", category=args.category)
    if args.command == "category-exclusions":
        return _current_affairs_output("exclusions")
    if args.command == "category-source-health":
        return _current_affairs_output("sources")
    if args.command == "current-affairs-daily-report":
        return _current_affairs_output("daily", dry_run=args.dry_run)
    if args.command == "current-affairs-weekly-report":
        return _current_affairs_output("weekly", dry_run=args.dry_run)
    if args.command == "current-affairs-full-cycle":
        return _current_affairs_output("full_cycle", dry_run=args.dry_run)
    if args.command == "social-anger-status":
        return _social_anger_output("status")
    if args.command == "social-anger-assess":
        return _social_anger_output(
            "assess", content_id=args.content_id, dry_run=args.dry_run)
    if args.command == "social-anger-candidates":
        return _social_anger_output(
            "candidates", content_id=args.content_id, dry_run=args.dry_run)
    if args.command == "social-anger-targets":
        return _social_anger_output("targets")
    if args.command == "social-anger-risk-report":
        return _social_anger_output("risk")
    if args.command == "social-anger-solution-gaps":
        return _social_anger_output("solution_gaps")
    if args.command == "social-anger-daily-report":
        return _social_anger_output("daily", dry_run=args.dry_run)
    if args.command == "social-anger-weekly-report":
        return _social_anger_output("weekly", dry_run=args.dry_run)
    if args.command == "social-anger-full-cycle":
        return _social_anger_output("full_cycle", dry_run=args.dry_run)
    if args.command == "threads-auth-url":
        return _threads_output(
            "auth_url", scope_profile=args.scope_profile)
    if args.command == "threads-exchange-code":
        return _threads_output("exchange_code", code=args.code)
    if args.command == "threads-token-status":
        return _threads_output("token_status")
    if args.command == "threads-refresh-token":
        return _threads_output("refresh_token", force=args.force)
    if args.command == "threads-profile":
        return _threads_output("profile")
    if args.command == "threads-status":
        return _threads_output("status")
    if args.command == "threads-generate":
        return _threads_output(
            "generate", dry_run=args.dry_run, x_post_id=args.x_post_id)
    if args.command == "threads-drafts":
        return _threads_output("drafts")
    if args.command == "threads-publish":
        return _threads_output("publish", draft_id=args.draft_id)
    if args.command == "threads-collect-metrics":
        return _threads_output("collect_metrics")
    if args.command == "platform-comparison":
        return _threads_output("comparison")
    if args.command == "threads-run":
        return _threads_output("run")
    if args.command == "threads-endpoints":
        return _threads_output("endpoints")
    if args.command == "threads-permissions":
        return _threads_full_output("permissions")
    if args.command == "threads-profile-sync":
        return _threads_full_output(
            "profile_sync", dry_run=args.dry_run)
    if args.command == "threads-sync-posts":
        return _threads_full_output(
            "sync_posts", dry_run=args.dry_run,
            since=args.since, until=args.until)
    if args.command == "threads-sync-replies":
        return _threads_full_output(
            "sync_replies", dry_run=args.dry_run)
    if args.command == "threads-sync-mentions":
        return _threads_full_output(
            "sync_mentions", dry_run=args.dry_run)
    if args.command == "threads-collect-post-insights":
        return _threads_full_output(
            "post_insights", dry_run=args.dry_run)
    if args.command == "threads-collect-account-insights":
        return _threads_full_output(
            "account_insights", dry_run=args.dry_run)
    if args.command == "threads-search":
        return _threads_full_output(
            "search", query=args.query, search_type=args.search_type,
            search_mode=args.search_mode, since=args.since, until=args.until,
            hours=args.hours, dry_run=args.dry_run)
    if args.command == "threads-trends":
        return _threads_full_output(
            "trends", hours=args.hours, dry_run=args.dry_run)
    if args.command == "threads-quota-status":
        return _threads_full_output(
            "quota", dry_run=args.dry_run)
    if args.command == "threads-container-status":
        return _threads_full_output(
            "container", container_id=args.container_id)
    if args.command == "threads-daily-report":
        return _threads_full_output(
            "daily_report", dry_run=args.dry_run)
    if args.command == "threads-weekly-report":
        return _threads_full_output(
            "weekly_report", dry_run=args.dry_run)
    if args.command == "threads-x-comparison":
        return _threads_full_output("comparison", days=args.days)
    if args.command == "threads-reply-draft":
        return _threads_full_output(
            "reply_draft", target_id=args.reply_to_id, text=args.text)
    if args.command == "threads-reply-publish":
        return _threads_full_output(
            "reply_publish", draft_id=args.draft_id, confirm=args.confirm)
    if args.command == "threads-quote-draft":
        return _threads_full_output(
            "quote_draft", target_id=args.post_id, text=args.text)
    if args.command == "threads-quote-publish":
        return _threads_full_output(
            "quote_publish", draft_id=args.draft_id, confirm=args.confirm)
    if args.command == "threads-repost":
        return _threads_full_output(
            "direct_action", action_type="repost",
            target_id=args.post_id, confirm=args.confirm)
    if args.command == "threads-delete":
        return _threads_full_output(
            "direct_action", action_type="delete",
            target_id=args.post_id, reason=args.reason, confirm=args.confirm)
    if args.command == "threads-moderation-draft":
        return _threads_full_output(
            "moderation_draft", target_id=args.reply_id,
            moderation_action=args.action)
    if args.command == "threads-moderation-apply":
        return _threads_full_output(
            "moderation_apply", draft_id=args.draft_id,
            confirm=args.confirm)
    if args.command == "threads-data-retention-run":
        return _threads_full_output("retention")
    if args.command == "threads-user-data-delete":
        return _threads_full_output(
            "user_delete", user_id=args.user_id, confirm=args.confirm)
    if args.command == "threads-full-sync":
        return _threads_full_output(
            "full_sync", dry_run=args.dry_run)
    if args.command == "threads-format-preview":
        return _threads_full_output(
            "format_preview", spec_file=args.spec_file)
    if args.command == "threads-format-validate":
        return _threads_full_output(
            "format_validate", spec_file=args.spec_file)
    if args.command == "threads-format-publish":
        return _threads_full_output(
            "format_publish", draft_id=args.draft_id,
            confirm=args.confirm)
    if args.command == "threads-location-get":
        return _threads_full_output(
            "location", location_id=args.location_id)
    if args.command == "threads-profile-discovery":
        return _threads_full_output(
            "profile_discovery", username=args.username,
            dry_run=args.dry_run)
    if args.command == "crosspost-status":
        return _crosspost_output("status")
    if args.command == "crosspost-candidates":
        return _crosspost_output("candidates")
    if args.command == "crosspost-generate-copy":
        return _crosspost_output(
            "generate_copy", publication_id=args.publication_id,
            dry_run=args.dry_run)
    if args.command == "crosspost-render-renditions":
        return _crosspost_output(
            "render", publication_id=args.publication_id,
            dry_run=args.dry_run)
    if args.command == "crosspost-validate":
        return _crosspost_output(
            "validate", publication_id=args.publication_id,
            dry_run=args.dry_run)
    if args.command == "crosspost-prepare":
        return _crosspost_output(
            "prepare", publication_id=args.publication_id,
            dry_run=args.dry_run)
    if args.command == "crosspost-publish":
        return _crosspost_output(
            "publish", publication_id=args.publication_id,
            dry_run=args.dry_run, confirm=args.confirm)
    if args.command == "crosspost-reconcile":
        return _crosspost_output(
            "reconcile", publication_id=args.publication_id,
            dry_run=args.dry_run)
    if args.command == "crosspost-metrics-sync":
        return _crosspost_output(
            "metrics", publication_id=args.publication_id,
            dry_run=args.dry_run)
    if args.command == "crosspost-report":
        return _crosspost_output(
            "report", publication_id=args.publication_id,
            dry_run=args.dry_run)
    if args.command == "crosspost-emergency-stop":
        return _crosspost_output("emergency_stop")
    if args.command == "instagram-auth-url":
        return _crosspost_output("instagram_auth_url")
    if args.command == "instagram-exchange-code":
        return _crosspost_output(
            "instagram_exchange", code=args.code, dry_run=args.dry_run)
    if args.command == "instagram-token-status":
        return _crosspost_output("instagram_token")
    if args.command == "instagram-profile":
        return _crosspost_output(
            "instagram_profile", dry_run=args.dry_run)
    if args.command == "instagram-reel-status":
        return _crosspost_output(
            "instagram_reel_status", creation_id=args.creation_id,
            dry_run=args.dry_run)
    if args.command == "youtube-token-status":
        return _crosspost_output("youtube_token")
    if args.command == "threads-web":
        load_env()
        ensure_dirs()
        from threads_oauth_server import run_server
        run_server()
        return 0
    if args.command == "discord-test":
        return cmd_discord_test()
    if args.command == "discord-note-draft-test":
        return cmd_discord_note_draft_test()
    if args.command == "discord-log":
        return cmd_discord_log(args.source, args.lines)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
