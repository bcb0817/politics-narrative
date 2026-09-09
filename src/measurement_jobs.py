"""Safe orchestration for post metrics, replies, and follower snapshots."""

from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from metrics_db import connect, db_path, init_db
from post_metrics import WINDOWS, collect as collect_post_metrics

JST = ZoneInfo("Asia/Tokyo")
FOLLOWER_WINDOWS = {"publish": timedelta(0), **WINDOWS}
DEFAULT_FOLLOWER_TOLERANCE_MINUTES = 10
X_USER_CREDENTIALS = (
    "API_KEY", "API_KEY_SECRET", "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET",
)


def _create_schema(path: Path) -> None:
    init_db(path)
    with closing(connect(path)) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS follower_snapshot_plans (
          id INTEGER PRIMARY KEY,
          tweet_id TEXT NOT NULL,
          measurement_window TEXT NOT NULL,
          due_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          snapshot_id INTEGER,
          attempted_at TEXT,
          error_class TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(tweet_id, measurement_window)
        );
        CREATE INDEX IF NOT EXISTS idx_follower_plan_due
          ON follower_snapshot_plans(status,due_at);
        """)
        connection.commit()


def ensure_follower_plans(
        path: Path | None = None, now: datetime | None = None) -> dict:
    """Create idempotent publish/15m/1h/6h/24h/72h plans for owned posts."""
    path = Path(path or db_path())
    now = now or datetime.now(JST)
    _create_schema(path)
    created = 0
    with closing(connect(path)) as connection:
        posts = connection.execute(
            """SELECT tweet_id,posted_at FROM published_posts
               WHERE tweet_id IS NOT NULL AND tweet_id<>''"""
        ).fetchall()
        for post in posts:
            try:
                posted_at = datetime.fromisoformat(str(post["posted_at"]))
                if posted_at.tzinfo is None:
                    posted_at = posted_at.replace(tzinfo=JST)
            except (TypeError, ValueError):
                continue
            for window, delta in FOLLOWER_WINDOWS.items():
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO follower_snapshot_plans
                       (tweet_id,measurement_window,due_at,status,created_at)
                       VALUES (?,?,?,'pending',?)""",
                    (str(post["tweet_id"]), window,
                     (posted_at + delta).isoformat(), now.isoformat()),
                )
                created += int(cursor.rowcount > 0)
        connection.commit()
    return {"posts": len(posts), "created": created}


def _credential_state() -> dict:
    missing_user = [name for name in X_USER_CREDENTIALS
                    if not os.environ.get(name, "").strip()]
    missing_reply = [] if os.environ.get("BEARER_TOKEN", "").strip() else [
        "BEARER_TOKEN"]
    return {
        "x_owned_metrics": (
            "configured_not_verified" if not missing_user
            else "missing_credentials"),
        "x_follower_snapshots": (
            "configured_not_verified" if not missing_user
            else "missing_credentials"),
        "x_replies": (
            "configured_not_verified" if not missing_reply
            else "missing_credentials"),
        "missing_x_user_credentials": missing_user,
        "missing_x_reply_credentials": missing_reply,
    }


def _follower_tolerance() -> timedelta:
    raw = os.environ.get(
        "MEASUREMENT_FOLLOWER_TOLERANCE_MINUTES",
        str(DEFAULT_FOLLOWER_TOLERANCE_MINUTES),
    )
    try:
        minutes = max(1, min(180, int(raw)))
    except (TypeError, ValueError):
        minutes = DEFAULT_FOLLOWER_TOLERANCE_MINUTES
    return timedelta(minutes=minutes)


def _as_jst(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def _classify_pending_plans(connection, now: datetime,
                            tolerance: timedelta) -> dict:
    rows = connection.execute(
        """SELECT id,tweet_id,measurement_window,due_at
           FROM follower_snapshot_plans
           WHERE status='pending' ORDER BY due_at"""
    ).fetchall()
    eligible = []
    overdue = []
    future = []
    lower_bound = now - tolerance
    for row in rows:
        try:
            due_at = _as_jst(row["due_at"])
        except (TypeError, ValueError):
            overdue.append(row)
            continue
        if due_at > now:
            future.append(row)
        elif due_at < lower_bound:
            overdue.append(row)
        else:
            eligible.append(row)
    return {"eligible_now": eligible, "overdue_unrecoverable": overdue,
            "future": future}


def _matching_snapshot(connection, due_at: str,
                       tolerance: timedelta):
    """Return a snapshot only when its actual capture time fits this due window."""
    try:
        target = _as_jst(due_at)
    except (TypeError, ValueError):
        return None
    best = None
    for row in connection.execute(
            "SELECT id,captured_at FROM follower_snapshots"):
        try:
            distance = abs((_as_jst(row["captured_at"]) - target).total_seconds())
        except (TypeError, ValueError):
            continue
        if distance <= tolerance.total_seconds() and (
                best is None or distance < best[0]):
            best = (distance, row)
    return best[1] if best else None


def _candidate_plans(connection) -> list[dict]:
    candidates = []
    posts = connection.execute(
        """SELECT tweet_id,posted_at FROM published_posts
           WHERE tweet_id IS NOT NULL AND tweet_id<>''"""
    ).fetchall()
    for post in posts:
        try:
            posted_at = _as_jst(post["posted_at"])
        except (TypeError, ValueError):
            continue
        for window, delta in FOLLOWER_WINDOWS.items():
            candidates.append({
                "tweet_id": str(post["tweet_id"]),
                "measurement_window": window,
                "due_at": (posted_at + delta).isoformat(),
            })
    return candidates


def reconcile_local(*, path: Path | None = None,
                    now: datetime | None = None,
                    apply: bool = False) -> dict:
    """Preview or apply plan-only reconciliation without any external request."""
    path = Path(path or db_path())
    now = now or datetime.now(JST)
    result = {
        "status": "applied" if apply else "dry_run",
        "applied": apply,
        "local_database_only": True,
        "external_request_made": False,
        "external_api_calls": 0,
        "new_plans_would_create": 0,
        "new_plans_created": 0,
        "historical_would_mark_missed": 0,
        "historical_marked_missed": 0,
        "existing_snapshots_would_reconcile": 0,
        "existing_snapshots_reconciled": 0,
    }
    if not path.exists():
        return result
    if apply:
        _create_schema(path)

    tolerance = _follower_tolerance()
    with closing(connect(path)) as connection:
        has_plans = bool(connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='follower_snapshot_plans'"""
        ).fetchone())
        existing = set()
        if has_plans:
            existing = {
                (str(row["tweet_id"]), str(row["measurement_window"]))
                for row in connection.execute(
                    """SELECT tweet_id,measurement_window
                       FROM follower_snapshot_plans""")
            }
        missing = [
            row for row in _candidate_plans(connection)
            if (row["tweet_id"], row["measurement_window"]) not in existing
        ]
        result["new_plans_would_create"] = len(missing)

        if apply:
            for row in missing:
                connection.execute(
                    """INSERT OR IGNORE INTO follower_snapshot_plans
                       (tweet_id,measurement_window,due_at,status,created_at)
                       VALUES (?,?,?,'pending',?)""",
                    (row["tweet_id"], row["measurement_window"],
                     row["due_at"], now.isoformat()))
            connection.commit()
            result["new_plans_created"] = len(missing)

        pending = []
        if has_plans or apply:
            pending.extend(dict(row) for row in connection.execute(
                """SELECT id,tweet_id,measurement_window,due_at
                   FROM follower_snapshot_plans WHERE status='pending'"""))
        if not apply:
            pending.extend({**row, "id": None} for row in missing)

        missed = []
        reconciled = []
        for row in pending:
            try:
                due_at = _as_jst(row["due_at"])
            except (TypeError, ValueError):
                due_at = now - tolerance - timedelta(seconds=1)
            if due_at >= now - tolerance:
                continue
            snapshot = _matching_snapshot(
                connection, row["due_at"], tolerance)
            if snapshot:
                reconciled.append((row, snapshot))
            else:
                missed.append(row)

        result["historical_would_mark_missed"] = len(missed)
        result["existing_snapshots_would_reconcile"] = len(reconciled)
        if apply:
            for row, snapshot in reconciled:
                connection.execute(
                    """UPDATE follower_snapshot_plans
                       SET status='complete',snapshot_id=?,attempted_at=?,
                           error_class=NULL WHERE id=?""",
                    (snapshot["id"], now.isoformat(), row["id"]))
            for row in missed:
                connection.execute(
                    """UPDATE follower_snapshot_plans
                       SET status='missed',attempted_at=?,
                           error_class='measurement_window_expired'
                       WHERE id=?""",
                    (now.isoformat(), row["id"]))
            connection.commit()
            result["historical_marked_missed"] = len(missed)
            result["existing_snapshots_reconciled"] = len(reconciled)
    return result


def status(path: Path | None = None, now: datetime | None = None) -> dict:
    """Return local evidence only; never contacts X or Threads."""
    path = Path(path or db_path())
    now = now or datetime.now(JST)
    planned = {"posts": 0, "created": 0}
    if not path.exists():
        return {
            "read_only": True, "external_request_made": False,
            "plans": {
                **planned, "counts": {}, "due": 0, "eligible_now": 0,
                "overdue_unrecoverable": 0,
                "follower_tolerance_minutes": int(
                    _follower_tolerance().total_seconds() // 60),
            },
            "records": {
                "x_metrics": 0, "follower_snapshots": 0, "x_replies": 0},
            **_credential_state(),
        }
    with closing(connect(path)) as connection:
        planned["posts"] = int(connection.execute(
            """SELECT COUNT(*) FROM published_posts
               WHERE tweet_id IS NOT NULL AND tweet_id<>''""").fetchone()[0])
        has_plans = bool(connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='follower_snapshot_plans'"""
        ).fetchone())
        plan_counts = {}
        due = 0
        eligible_now = 0
        overdue_unrecoverable = 0
        if has_plans:
            plan_counts = {
                str(row["status"]): int(row["n"])
                for row in connection.execute(
                    """SELECT status,COUNT(*) AS n
                       FROM follower_snapshot_plans GROUP BY status""")
            }
            due = int(connection.execute(
                """SELECT COUNT(*) FROM follower_snapshot_plans
                   WHERE status='pending' AND due_at<=?""",
                (now.isoformat(),)).fetchone()[0])
            classified = _classify_pending_plans(
                connection, now, _follower_tolerance())
            eligible_now = len(classified["eligible_now"])
            overdue_unrecoverable = len(
                classified["overdue_unrecoverable"])
        metric_rows = int(connection.execute(
            "SELECT COUNT(*) FROM post_metrics").fetchone()[0])
        reply_rows = 0
        try:
            reply_rows = int(connection.execute(
                "SELECT COUNT(*) FROM post_reply_events").fetchone()[0])
        except Exception:
            pass
        snapshots = int(connection.execute(
            "SELECT COUNT(*) FROM follower_snapshots").fetchone()[0])
    credentials = _credential_state()
    for key, rows in (
        ("x_owned_metrics", metric_rows),
        ("x_follower_snapshots", snapshots),
        ("x_replies", reply_rows),
    ):
        if rows and credentials[key] == "configured_not_verified":
            credentials[key] = "working_cached"
    return {
        "read_only": True,
        "external_request_made": False,
        "plans": {
            **planned, "counts": plan_counts, "due": due,
            "eligible_now": eligible_now,
            "overdue_unrecoverable": overdue_unrecoverable,
            "follower_tolerance_minutes": int(
                _follower_tolerance().total_seconds() // 60),
        },
        "records": {
            "x_metrics": metric_rows, "follower_snapshots": snapshots,
            "x_replies": reply_rows,
        },
        **credentials,
    }


def run(*, path: Path | None = None, now: datetime | None = None,
        execute: bool = False, client_factory=None,
        follower_client_factory=None, reply_client_factory=None) -> dict:
    """Run due reads only when explicitly enabled; default is a zero-call plan."""
    path = Path(path or db_path())
    now = now or datetime.now(JST)
    planned = ensure_follower_plans(path, now)
    local = status(path, now)
    local["plans"]["created"] = planned["created"]
    if not execute:
        return {**local, "status": "dry_run", "executed": False}

    from growth_tracking import capture_follower_snapshot
    from reply_metrics import collect_x_replies

    with closing(connect(path)) as connection:
        history = [
            {"tweet_id": row["tweet_id"], "posted_at_jst": row["posted_at"]}
            for row in connection.execute(
                """SELECT tweet_id,posted_at FROM published_posts
                   WHERE tweet_id IS NOT NULL AND tweet_id<>''""")
        ]
        tolerance = _follower_tolerance()
        classified = _classify_pending_plans(connection, now, tolerance)

        reconciled = 0
        reconciled_overdue = 0
        eligible_rows = []
        for row in classified["eligible_now"]:
            snapshot = _matching_snapshot(connection, row["due_at"], tolerance)
            if snapshot:
                connection.execute(
                    """UPDATE follower_snapshot_plans
                       SET status='complete',snapshot_id=?,attempted_at=?,
                           error_class=NULL WHERE id=?""",
                    (snapshot["id"], now.isoformat(), row["id"]))
                reconciled += 1
            else:
                eligible_rows.append(row)
        for row in classified["overdue_unrecoverable"]:
            snapshot = _matching_snapshot(connection, row["due_at"], tolerance)
            if snapshot:
                connection.execute(
                    """UPDATE follower_snapshot_plans
                       SET status='complete',snapshot_id=?,attempted_at=?,
                           error_class=NULL WHERE id=?""",
                    (snapshot["id"], now.isoformat(), row["id"]))
                reconciled += 1
                reconciled_overdue += 1
            else:
                connection.execute(
                    """UPDATE follower_snapshot_plans
                       SET status='missed',attempted_at=?,
                           error_class='measurement_window_expired'
                       WHERE id=?""",
                    (now.isoformat(), row["id"]))
        connection.commit()

    metrics = collect_post_metrics(
        history, now=now, client_factory=client_factory, path=path)
    follower = {"captured": False, "reason": "no_eligible_plan"}
    if eligible_rows:
        follower = capture_follower_snapshot(
            client_factory=follower_client_factory, path=path, now=now)
        with closing(connect(path)) as connection:
            if follower.get("captured"):
                for row in eligible_rows:
                    snapshot = _matching_snapshot(
                        connection, row["due_at"], tolerance)
                    if snapshot:
                        connection.execute(
                            """UPDATE follower_snapshot_plans
                               SET status='complete',snapshot_id=?,
                                   attempted_at=?,error_class=NULL WHERE id=?""",
                            (snapshot["id"], now.isoformat(), row["id"]))
                    else:
                        connection.execute(
                            """UPDATE follower_snapshot_plans
                               SET attempted_at=?,
                                   error_class='snapshot_outside_tolerance'
                               WHERE id=?""",
                            (now.isoformat(), row["id"]))
            else:
                for row in eligible_rows:
                    connection.execute(
                        """UPDATE follower_snapshot_plans
                           SET attempted_at=?,error_class=? WHERE id=?""",
                        (now.isoformat(), follower.get("reason"), row["id"]))
            connection.commit()
    replies = collect_x_replies(
        path=path, client_factory=reply_client_factory)
    return {
        "status": "completed", "executed": True,
        "external_writes": 0, "metrics": metrics,
        "follower_snapshot": follower, "replies": replies,
        "follower_plans": {
            "reconciled": reconciled,
            "missed": (
                len(classified["overdue_unrecoverable"])
                - reconciled_overdue),
            "eligible_for_capture": len(eligible_rows),
        },
    }
