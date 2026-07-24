"""Best-effort Discord webhook notifications.

The webhook URL is read only from the environment. Notification failures are
logged locally and never interrupt posting or daemon execution.
"""

from __future__ import annotations

import os
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests


JST = ZoneInfo("Asia/Tokyo")
_COLORS = {
    "info": 0x3498DB,
    "success": 0x2ECC71,
    "warning": 0xF1C40F,
    "error": 0xE74C3C,
}


def _enabled() -> bool:
    return os.environ.get("DISCORD_NOTIFICATIONS_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _event_enabled(event: str) -> bool:
    key = f"DISCORD_NOTIFY_{event.upper()}"
    defaults = {
        "STARTUP": "true",
        "POST_SUCCESS": "true",
        "ERROR": "true",
        "RUN_LOG": "true",
        "LOG": "true",
        "SKIP": "false",
    }
    return os.environ.get(key, defaults.get(event.upper(), "true")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _clean(value: Any, limit: int = 1000) -> str:
    text = sanitize(str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


_SECRET_PATTERNS = (
    re.compile(r"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]+", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:xai|grok)-[A-Za-z0-9_-]{16,}\b", re.I),
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]{16,}=*"),
    re.compile(r"(?i)\b((?:API_KEY|API_KEY_SECRET|ACCESS_TOKEN|ACCESS_TOKEN_SECRET|"
               r"OPENAI_API_KEY|XAI_API_KEY|X_BEARER_TOKEN|DISCORD_WEBHOOK_URL|"
               r"NOTE_DRAFT_DISCORD_WEBHOOK_URL|DISCORD_NOTE_WEBHOOK_URL)\s*[=:]\s*)"
               r"[^\s,;]+"),
)


def sanitize(text: str) -> str:
    """Remove known credential formats and configured secret values."""
    clean = str(text or "")
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)\\b(Bearer"):
            clean = pattern.sub(r"\1[REDACTED]", clean)
        elif "(?:API_KEY|" in pattern.pattern:
            clean = pattern.sub(r"\1[REDACTED]", clean)
        else:
            clean = pattern.sub("[REDACTED]", clean)
    for key in (
        "DISCORD_WEBHOOK_URL", "NOTE_DRAFT_DISCORD_WEBHOOK_URL",
        "DISCORD_NOTE_WEBHOOK_URL",
        "OPENAI_API_KEY", "XAI_API_KEY", "X_BEARER_TOKEN",
        "API_KEY", "API_KEY_SECRET", "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET",
    ):
        secret = os.environ.get(key, "").strip()
        if len(secret) >= 8:
            clean = clean.replace(secret, "[REDACTED]")
    return clean


def notify(
    event: str,
    title: str,
    description: str = "",
    *,
    level: str = "info",
    fields: dict[str, Any] | None = None,
    force: bool = False,
    timeout: float | None = None,
    webhook_env: str = "DISCORD_WEBHOOK_URL",
    username_env: str = "DISCORD_WEBHOOK_USERNAME",
    footer: str = "久世ゆい・政治ニュースBot",
) -> bool:
    """Send one embed and return whether Discord accepted it."""
    if not force and (not _enabled() or not _event_enabled(event)):
        return False
    webhook_url = os.environ.get(webhook_env, "").strip()
    if not webhook_url:
        return False

    embed: dict[str, Any] = {
        "title": _clean(title, 256),
        "description": _clean(description, 4000),
        "color": _COLORS.get(level, _COLORS["info"]),
        "timestamp": datetime.now(JST).isoformat(),
        "footer": {"text": _clean(footer, 2048)},
    }
    if fields:
        embed["fields"] = [
            {
                "name": _clean(name, 256),
                "value": _clean(value, 1000) or "-",
                "inline": True,
            }
            for name, value in fields.items()
            if value is not None and str(value).strip()
        ][:25]

    try:
        response = requests.post(
            webhook_url,
            json={
                "username": os.environ.get(username_env, "久世ゆい Bot"),
                "allowed_mentions": {"parse": []},
                "embeds": [embed],
            },
            timeout=timeout or float(os.environ.get("DISCORD_WEBHOOK_TIMEOUT_SECONDS", "10")),
        )
        return 200 <= response.status_code < 300
    except (requests.RequestException, ValueError):
        return False


def notify_note_draft_ready(draft: dict[str, Any], *, test: bool = False) -> bool:
    """Send a note-draft notification to its dedicated Discord channel."""
    enabled = os.environ.get(
        "NOTE_DRAFT_DISCORD_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if not enabled and not test:
        return False
    return notify(
        "note_draft",
        "📝 note draft連携テスト" if test else "📝 note draftを作成しました",
        _clean(
            draft.get("summary") or draft.get("content")
            or "note draft専用Discord通知の送信準備が完了しました。",
            3000,
        ),
        level="success" if not test else "info",
        fields={
            "タイトル": draft.get("title") or ("接続テスト" if test else ""),
            "ステータス": draft.get("status") or ("test" if test else "draft"),
            "対象週": draft.get("week_start"),
            "ファイル": draft.get("path"),
            "生成元": draft.get("source"),
        },
        force=True,
        webhook_env="NOTE_DRAFT_DISCORD_WEBHOOK_URL",
        username_env="NOTE_DRAFT_DISCORD_WEBHOOK_USERNAME",
        footer="久世ゆい・note draft通知",
    )


def _note_webhook_settings() -> tuple[bool, str, str]:
    enabled_raw = os.environ.get(
        "DISCORD_NOTE_ENABLED",
        os.environ.get("NOTE_DRAFT_DISCORD_ENABLED", "false"),
    )
    enabled = enabled_raw.strip().lower() in {"1", "true", "yes", "on"}
    url = (
        os.environ.get("DISCORD_NOTE_WEBHOOK_URL", "").strip()
        or os.environ.get("NOTE_DRAFT_DISCORD_WEBHOOK_URL", "").strip()
    )
    username = (
        os.environ.get("DISCORD_NOTE_WEBHOOK_USERNAME", "").strip()
        or os.environ.get(
            "NOTE_DRAFT_DISCORD_WEBHOOK_USERNAME", "久世ゆい note Bot"
        )
    )
    return enabled, url, username


def _note_summary(draft: dict[str, Any]) -> str:
    return "\n".join([
        "📝 **無料note下書きができました**",
        f"**タイトル:** {_clean(draft.get('title'), 300)}",
        f"**記事タイプ:** {_clean(draft.get('article_type'), 80)}",
        f"**文字数:** {_clean(draft.get('character_count'), 20)}",
        f"**読了目安:** {_clean(draft.get('reading_minutes'), 20)}分",
        f"**公開候補日:** {_clean(draft.get('target_publish_date'), 30)}",
        f"**状態:** {_clean(draft.get('status'), 40)}",
        "",
        "**確認事項:** 事実と一次資料／賛否両論の公平性／タイトルの強さ／AI的な定型表現",
        f"**ローカル保存先:** {_clean(draft.get('path'), 600)}",
        "",
        "article.md・sources.md・review.mdを確認し、人が承認後にnoteへ手動公開してください。",
    ])


def notify_note_draft_files(
    draft: dict[str, Any],
    paths: list[Path],
) -> tuple[bool, str | None]:
    """Send a concise summary and review files; never send the full article inline.

    Attachment failure falls back to a summary-only message. Any Discord
    failure remains non-fatal to article generation.
    """
    enabled, webhook_url, username = _note_webhook_settings()
    if not enabled or not webhook_url:
        return False, None
    payload = {
        "username": _clean(username, 80),
        "content": _clean(_note_summary(draft), 1900),
        "allowed_mentions": {"parse": []},
    }
    timeout = float(os.environ.get("DISCORD_WEBHOOK_TIMEOUT_SECONDS", "10"))
    handles = []
    try:
        files = []
        for path in paths[:3]:
            resolved = Path(path)
            handle = resolved.open("rb")
            handles.append(handle)
            files.append(
                ("files[]", (resolved.name, handle, "text/markdown; charset=utf-8"))
            )
        response = requests.post(
            webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true",
            data={"payload_json": json.dumps(payload, ensure_ascii=False)},
            files=files,
            timeout=timeout,
        )
        if 200 <= response.status_code < 300:
            try:
                message_id = str(response.json().get("id") or "") or None
            except (ValueError, AttributeError):
                message_id = None
            return True, message_id
    except (OSError, requests.RequestException, ValueError):
        pass
    finally:
        for handle in handles:
            handle.close()

    try:
        response = requests.post(
            webhook_url + ("&" if "?" in webhook_url else "?") + "wait=true",
            json=payload,
            timeout=timeout,
        )
        if not 200 <= response.status_code < 300:
            return False, None
        try:
            message_id = str(response.json().get("id") or "") or None
        except (ValueError, AttributeError):
            message_id = None
        return True, message_id
    except (requests.RequestException, ValueError):
        return False, None


def notify_startup() -> bool:
    return notify(
        "startup",
        "🟢 Botを起動しました",
        "政治ニュースの監視を開始します。",
        fields={
            "投稿": f"POST_ENABLED={os.environ.get('POST_ENABLED', 'false')}",
            "監視時間": os.environ.get("ACTIVE_HOURS", "5-23"),
            "監視間隔": f"{os.environ.get('MONITOR_INTERVAL_MINUTES', '60')}分",
        },
    )


def notify_post_success(post: dict[str, Any]) -> bool:
    tweet_id = _clean(post.get("tweet_id"), 40)
    tweet_url = f"https://x.com/i/web/status/{tweet_id}" if tweet_id else ""
    return notify(
        "post_success",
        "✅ Xへの投稿に成功しました",
        _clean(post.get("tweet_text") or post.get("title"), 1800),
        level="success",
        fields={
            "投稿形式": post.get("post_type"),
            "品質スコア": post.get("effective_score"),
            "モデル": post.get("openai_model"),
            "X投稿": tweet_url,
            "ニュース": post.get("title"),
            "情報源": post.get("source_name"),
            "スロット": post.get("slot_key"),
        },
    )


def notify_error(where: str, error: Any, **context: Any) -> bool:
    return notify(
        "error",
        "🚨 Botでエラーが発生しました",
        _clean(error, 1800),
        level="error",
        fields={"発生箇所": where, **context},
    )


def notify_run_log(record: dict[str, Any], *, exit_code: int = 0) -> bool:
    decision = str(record.get("decision") or "unknown")
    success = decision == "post"
    level = "success" if success else ("error" if exit_code else "warning")
    icon = "✅" if success else ("🚨" if exit_code else "⏭️")
    return notify(
        "run_log",
        f"{icon} 監視処理ログ",
        _clean(record.get("title") or "監視枠の処理が完了しました", 1800),
        level=level,
        fields={
            "判定": decision,
            "理由": record.get("reason"),
            "スロット": record.get("slot_key") or record.get("selected_slot"),
            "投稿形式": record.get("post_type"),
            "品質スコア": record.get("effective_score"),
            "BANリスク": record.get("ban_risk"),
            "モデル": record.get("openai_model"),
            "終了コード": exit_code,
        },
    )


def notify_attempt_since(path: Path, previous_size: int, *, exit_code: int = 0) -> bool:
    """Send the newest attempt appended after previous_size."""
    try:
        if not path.exists() or path.stat().st_size <= previous_size:
            return notify(
                "run_log",
                "ℹ️ 監視処理ログ",
                "監視処理は完了しましたが、新しい投稿判定レコードはありません。",
                level="info" if exit_code == 0 else "error",
                fields={"終了コード": exit_code},
            )
        with path.open("rb") as handle:
            handle.seek(max(0, previous_size))
            appended = handle.read().decode("utf-8", errors="replace")
        rows = [line for line in appended.splitlines() if line.strip()]
        if not rows:
            return False
        return notify_run_log(json.loads(rows[-1]), exit_code=exit_code)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False


_LOG_FILES = {
    "bot": "bot.log",
    "supervisor": "supervisor.log",
    "errors": "errors.jsonl",
    "attempts": "post_attempts.jsonl",
}


def notify_log_excerpt(
    source: str,
    *,
    lines: int = 40,
    log_dir: Path | None = None,
    force: bool = False,
) -> tuple[bool, int]:
    """Send a sanitized tail from one whitelisted log file."""
    filename = _LOG_FILES.get(str(source).lower())
    if not filename:
        return False, 0
    directory = log_dir or Path(os.environ.get("LOG_DIR", "logs"))
    path = directory / filename
    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False, 0
    selected = raw_lines[-max(1, min(int(lines), 200)):]
    content = sanitize("\n".join(selected))
    if not content:
        content = "(ログは空です)"

    # Discord embed descriptions are limited to 4096 characters.
    chunks = []
    while content and len(chunks) < 5:
        chunks.append(content[:3500])
        content = content[3500:]
    sent = True
    for index, chunk in enumerate(chunks, start=1):
        safe_chunk = chunk.replace("```", "'''")
        sent = notify(
            "log",
            f"📄 {filename}（{index}/{len(chunks)}）",
            f"```text\n{safe_chunk}\n```",
            level="info",
            fields={"取得行数": len(selected)},
            force=force,
        ) and sent
    return sent, len(selected)
