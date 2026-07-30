"""Conservative retention cleanup for disposable runtime artifacts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def _int(name: str, default: int, low: int = 1, high: int = 3650) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


@dataclass
class CleanupItem:
    action: str
    path: str
    bytes: int = 0
    reason: str = ""


POLICIES = (
    ("data/politics_analysis_cache", "STORAGE_POLITICS_CACHE_DAYS", 2),
    ("data/article_content_cache", "STORAGE_ARTICLE_CACHE_DAYS", 2),
    ("data/x_search_history", "STORAGE_SEARCH_HISTORY_DAYS", 45),
    ("data/xai_search_history", "STORAGE_SEARCH_HISTORY_DAYS", 45),
    ("data/openai_batches", "STORAGE_OPENAI_BATCH_DAYS", 30),
    ("outputs/previews", "STORAGE_PREVIEW_DAYS", 14),
    ("outputs/note/failed", "STORAGE_FAILED_DRAFT_DAYS", 30),
)

PROTECTED_TOP_LEVEL = {
    "backups", ".git", ".venv", "venv", "config", "production",
}


def settings() -> dict:
    return {
        "enabled": os.environ.get(
            "STORAGE_CLEANUP_ENABLED", "true"
        ).strip().lower() in {"1", "true", "yes", "on"},
        "schedule": os.environ.get(
            "STORAGE_CLEANUP_SCHEDULE", "03:30").strip(),
        "log_retention_days": _int("STORAGE_LOG_RETENTION_DAYS", 30),
        "log_max_mb": _int("STORAGE_LOG_MAX_MB", 10, 1, 1024),
        "max_deletions": _int(
            "STORAGE_CLEANUP_MAX_DELETIONS", 5000, 1, 100000),
        "pycache_days": _int("STORAGE_PYCACHE_DAYS", 2),
    }


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _files(directory: Path) -> Iterable[Path]:
    if not directory.is_dir() or directory.is_symlink():
        return []
    return (
        path for path in directory.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def _old(path: Path, cutoff: datetime) -> bool:
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, tz=JST) < cutoff
    except OSError:
        return False


def _remove_file(
    path: Path, *, root: Path, dry_run: bool, reason: str,
    items: list[CleanupItem],
) -> None:
    if not _inside(root, path) or path.is_symlink():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    items.append(CleanupItem(
        "would_delete" if dry_run else "deleted",
        str(path.relative_to(root)), size, reason))
    if not dry_run:
        try:
            path.unlink()
        except OSError:
            items[-1].action = "delete_failed"


def _prune_empty_dirs(base: Path, *, root: Path, dry_run: bool) -> int:
    if not base.is_dir() or base.is_symlink():
        return 0
    count = 0
    directories = sorted(
        (path for path in base.rglob("*")
         if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts), reverse=True)
    for path in directories:
        if not _inside(root, path):
            continue
        try:
            if any(path.iterdir()):
                continue
            if not dry_run:
                path.rmdir()
            count += 1
        except OSError:
            pass
    return count


def _rotate_logs(
    root: Path, *, now: datetime, dry_run: bool, items: list[CleanupItem],
    max_deletions: int,
) -> None:
    log_dir = root / "logs"
    if not log_dir.is_dir() or log_dir.is_symlink():
        return
    cfg = settings()
    max_bytes = cfg["log_max_mb"] * 1024 * 1024
    cutoff = now - timedelta(days=cfg["log_retention_days"])
    for path in sorted(_files(log_dir), key=str):
        if len(items) >= max_deletions:
            return
        name = path.name
        is_active = path.suffix in {".log", ".jsonl"}
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if is_active and size > max_bytes:
            rotated = path.with_name(
                f"{path.name}.{now:%Y%m%d-%H%M%S}")
            items.append(CleanupItem(
                "would_rotate" if dry_run else "rotated",
                str(path.relative_to(root)), size,
                f"log_size>{cfg['log_max_mb']}MB"))
            if not dry_run:
                try:
                    path.replace(rotated)
                except OSError:
                    items[-1].action = "rotate_failed"
        elif not is_active and _old(path, cutoff):
            _remove_file(
                path, root=root, dry_run=dry_run,
                reason=f"rotated_log_older_than_{cfg['log_retention_days']}d",
                items=items)


def _clean_pycache(
    root: Path, *, now: datetime, dry_run: bool, items: list[CleanupItem],
    max_deletions: int,
) -> None:
    cutoff = now - timedelta(days=settings()["pycache_days"])
    directories = [root / "__pycache__"]
    for base in (root / "src", root / "tests"):
        if base.is_dir() and not base.is_symlink():
            directories.extend(base.rglob("__pycache__"))
    for directory in directories:
        if len(items) >= max_deletions:
            return
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            relative = directory.relative_to(root)
        except ValueError:
            continue
        if relative.parts and relative.parts[0] in PROTECTED_TOP_LEVEL:
            continue
        for path in _files(directory):
            if len(items) >= max_deletions:
                return
            if path.suffix in {".pyc", ".pyo"} and _old(path, cutoff):
                _remove_file(
                    path, root=root, dry_run=dry_run,
                    reason=f"pycache_older_than_{settings()['pycache_days']}d",
                    items=items)
        _prune_empty_dirs(directory, root=root, dry_run=dry_run)


def run_cleanup(
    root: Path, *, dry_run: bool = True,
    now: datetime | None = None,
) -> dict:
    root = Path(root).resolve()
    now = now or datetime.now(JST)
    cfg = settings()
    result = {
        "enabled": cfg["enabled"], "dry_run": bool(dry_run),
        "started_at": now.isoformat(), "items": [],
        "deleted_files": 0, "rotated_files": 0,
        "bytes_reclaimed": 0, "empty_dirs_removed": 0,
        "limit_reached": False,
        "protected": [
            "backups/", "data/*.db*", "data/*.json",
            "outputs/note/{drafts,approved,published}/",
            "outputs/disaster_updates/", "outputs/special_research/",
            "outputs/crosspost/",
        ],
    }
    if not cfg["enabled"]:
        result["reason"] = "storage_cleanup_disabled"
        return result
    items: list[CleanupItem] = []
    for relative, env_name, default_days in POLICIES:
        if len(items) >= cfg["max_deletions"]:
            break
        days = _int(env_name, default_days)
        base = root / relative
        if not _inside(root, base) or base.is_symlink():
            continue
        cutoff = now - timedelta(days=days)
        for path in sorted(_files(base), key=str):
            if len(items) >= cfg["max_deletions"]:
                break
            if _old(path, cutoff):
                _remove_file(
                    path, root=root, dry_run=dry_run,
                    reason=f"{relative}_older_than_{days}d", items=items)
        result["empty_dirs_removed"] += _prune_empty_dirs(
            base, root=root, dry_run=dry_run)
    _rotate_logs(
        root, now=now, dry_run=dry_run, items=items,
        max_deletions=cfg["max_deletions"])
    _clean_pycache(
        root, now=now, dry_run=dry_run, items=items,
        max_deletions=cfg["max_deletions"])
    result["items"] = [asdict(item) for item in items]
    result["deleted_files"] = sum(
        item.action in {"deleted", "would_delete"} for item in items)
    result["rotated_files"] = sum(
        item.action in {"rotated", "would_rotate"} for item in items)
    result["bytes_reclaimed"] = sum(
        item.bytes for item in items
        if item.action in {"deleted", "would_delete"})
    result["limit_reached"] = len(items) >= cfg["max_deletions"]
    result["completed_at"] = datetime.now(JST).isoformat()
    return result


def compact_summary(result: dict) -> dict:
    return {
        key: result.get(key) for key in (
            "enabled", "dry_run", "deleted_files", "rotated_files",
            "bytes_reclaimed", "empty_dirs_removed", "limit_reached",
            "started_at", "completed_at",
        )
    }


def save_report(root: Path, result: dict) -> Path:
    path = Path(root) / "logs" / "storage_cleanup_latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
