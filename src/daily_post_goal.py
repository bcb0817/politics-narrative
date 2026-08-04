"""Daily X posting-goal analysis and bounded automatic remediation.

The goal counts unique successful X publications in JST. Threads posts are
reported separately, so cross-posting never inflates the X result. Remediation
may widen verified candidate selection, but it cannot lower quality, safety,
duplicate, interval, quota, or budget gates.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from metrics_db import db_path

JST = ZoneInfo("Asia/Tokyo")

TRANSIENT_REASONS = {
    "post_to_x_failed", "network_error", "rate_limited", "budget_blocked",
    "model_route_skip", "openai_monthly_budget_guard",
    "candidate_generation_failed", "politics_api_call_limit",
}
SUPPLY_REASONS = {
    "no_news", "no_qualified_news", "effective_score_below_threshold",
    "topic_cooldown", "duplicate_topic", "semantic_topic_cooldown",
}


def _parse(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def _attempts(path: Path, target_date: date) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        stamp = _parse(row.get("ts_jst"))
        if stamp and stamp.date() == target_date:
            rows.append(row)
    return rows


def _published_counts(path: Path, target_date: date) -> tuple[int, int]:
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as conn:
        x_count = int(conn.execute(
            "SELECT COUNT(DISTINCT tweet_id) FROM published_posts "
            "WHERE substr(posted_at,1,10)=?", (target_date.isoformat(),)
        ).fetchone()[0])
        threads_count = int(conn.execute(
            "SELECT COUNT(DISTINCT threads_post_id) FROM threads_posts "
            "WHERE status='published' AND substr(published_at,1,10)=?",
            (target_date.isoformat(),),
        ).fetchone()[0])
    return x_count, threads_count


def _remediation(shortfall: int, reasons: Counter[str],
                 hours_left: float) -> list[dict]:
    if shortfall <= 0:
        return [{
            "priority": "maintain",
            "action": "現在の投稿スケジュールと品質基準を維持する",
        }]
    actions: list[dict] = []
    if not reasons:
        actions.append({
            "priority": "P0",
            "action": "Bot・Windowsタスク・投稿履歴の整合性を確認する",
            "because": "未達なのに当日の投稿試行ログがない",
        })
    if any(reason in reasons for reason in TRANSIENT_REASONS):
        actions.append({
            "priority": "P0",
            "action": "失敗スロットを再試行し、低コスト生成フォールバックを優先する",
            "because": "API・生成・ネットワーク系の一時失敗がある",
        })
    if any(reason in reasons for reason in SUPPLY_REASONS):
        actions.append({
            "priority": "P1",
            "action": "公式・RSS候補の選定幅を1件から3件へ広げる",
            "because": "候補不足、重複、クールダウンによる見送りが多い",
        })
    if reasons.get("post_disabled"):
        actions.append({
            "priority": "P0",
            "action": "投稿スイッチ停止を運用アラートとして通知する",
            "because": "安全停止を自動解除せず、停止原因を明示する",
        })
    if hours_left > 0:
        actions.append({
            "priority": "P1",
            "action": f"残り時間の必要ペースを{round(shortfall / hours_left, 2)}件/時として監視する",
            "because": "未達を早期に検知し、候補探索を前倒しする",
        })
    else:
        actions.append({
            "priority": "P1",
            "action": "翌日の候補選定幅と検証済み常設候補の供給を増やす",
            "because": "当日の安全な補完時間が終了している",
        })
    actions.append({
        "priority": "guardrail",
        "action": "品質・安全・重複・一次情報・予算・投稿間隔は緩和しない",
    })
    return actions


def build_report(*, path: Path | None = None, log_dir: Path | None = None,
                 now: datetime | None = None, target: int | None = None,
                 report_date: date | None = None) -> dict:
    """Return a read-only report for one JST calendar date."""
    now = (now or datetime.now(JST)).astimezone(JST)
    target = max(1, int(target or os.environ.get("DAILY_POST_TARGET", "20")))
    target_date = report_date or now.date()
    x_count, threads_count = _published_counts(Path(path or db_path()), target_date)
    directory = log_dir or Path(os.environ.get("LOG_DIR", "logs"))
    attempts = _attempts(directory / "post_attempts.jsonl", target_date)
    reasons = Counter(
        str(row.get("reason") or "unknown") for row in attempts
        if row.get("decision") != "post"
    )
    shortfall = max(0, target - x_count)
    day_start = datetime.combine(target_date, time.min, JST)
    day_end = datetime.combine(target_date, time.max, JST)
    effective_now = min(max(now, day_start), day_end)
    elapsed_hours = max((effective_now - day_start).total_seconds() / 3600, 1 / 60)
    pace_forecast = min(96.0, x_count / elapsed_hours * 24)
    hours_left = max(0.0, (day_end - now).total_seconds() / 3600)
    return {
        "report_date": target_date.isoformat(),
        "generated_at": now.isoformat(),
        "timezone": "Asia/Tokyo",
        "target": {
            "platform": "x", "posts": target,
            "counting_rule": "successful_unique_publications",
        },
        "actual": {
            "x": x_count, "threads": threads_count,
            "threads_crosspost_coverage": (
                round(threads_count / x_count, 4) if x_count else None),
        },
        "achievement": {
            "met": x_count >= target, "shortfall": shortfall,
            "rate": round(x_count / target, 4),
        },
        "attempts": {
            "total": len(attempts),
            "skip_reasons": dict(reasons.most_common()),
        },
        "analysis": {
            "primary_reason": reasons.most_common(1)[0][0] if reasons else (
                None if x_count >= target else "no_attempt_log"),
            "pace_forecast": round(pace_forecast, 2),
            "on_pace": pace_forecast >= target,
            "hours_left": round(hours_left, 2),
            "required_posts_per_hour": (
                round(shortfall / hours_left, 2) if hours_left else None),
        },
        "remediation": _remediation(shortfall, reasons, hours_left),
        "safety": {
            "external_request_made": False,
            "publishing_attempted": False,
            "quality_threshold_changed": False,
            "duplicate_gate_changed": False,
            "budget_gate_changed": False,
        },
    }


def save_report(report: dict, output_dir: Path | None = None) -> Path:
    directory = output_dir or Path("data") / "daily_post_goal"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{report['report_date']}.json"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    target.write_text(text, encoding="utf-8")
    (directory / "latest.json").write_text(text, encoding="utf-8")
    return target


def apply_remediation(report: dict, state_dir: Path | None = None,
                      now: datetime | None = None) -> dict:
    """Persist bounded next-day runtime guidance; never post or edit .env."""
    now = (now or datetime.now(JST)).astimezone(JST)
    directory = state_dir or Path("data") / "daily_post_goal"
    directory.mkdir(parents=True, exist_ok=True)
    met = bool((report.get("achievement") or {}).get("met"))
    report_day = date.fromisoformat(str(report["report_date"]))
    effective_day = now.date() if report_day < now.date() else report_day
    shortfall = int((report.get("achievement") or {}).get("shortfall") or 0)
    reasons = (report.get("attempts") or {}).get("skip_reasons") or {}
    supply_limited = any(reason in reasons for reason in SUPPLY_REASONS)
    policy = {
        "version": 1,
        "active": not met,
        "generated_at": now.isoformat(),
        "source_report_date": report_day.isoformat(),
        "effective_date": effective_day.isoformat(),
        "expires_at": datetime.combine(
            effective_day + timedelta(days=1), time.min, JST).isoformat(),
        "target": int((report.get("target") or {}).get("posts") or 20),
        "shortfall": shortfall,
        "primary_reason": (report.get("analysis") or {}).get("primary_reason"),
        "prefilter_top_n": 3 if supply_limited or shortfall >= 5 else 2,
        "verified_evergreen_max_per_day": 3 if shortfall >= 5 else 2,
        "prefer_official_sources": True,
        "retry_transient_slots": any(
            reason in reasons for reason in TRANSIENT_REASONS),
        "quality_threshold_locked": True,
        "safety_gate_locked": True,
        "duplicate_gate_locked": True,
        "budget_gate_locked": True,
        "human_approval_required": False,
    }
    target = directory / "remediation.json"
    target.write_text(json.dumps(policy, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return policy


def load_active_remediation(state_dir: Path | None = None,
                            now: datetime | None = None) -> dict:
    now = (now or datetime.now(JST)).astimezone(JST)
    path = (state_dir or Path("data") / "daily_post_goal") / "remediation.json"
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
        expires_at = _parse(policy.get("expires_at"))
        if not policy.get("active") or not expires_at or now >= expires_at:
            return {}
        if str(policy.get("effective_date")) > now.date().isoformat():
            return {}
        return policy
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
