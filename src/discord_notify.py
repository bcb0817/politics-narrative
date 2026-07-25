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
               r"NOTE_DRAFT_DISCORD_WEBHOOK_URL|DISCORD_NOTE_WEBHOOK_URL|"
               r"INSTAGRAM_ACCESS_TOKEN|INSTAGRAM_APP_SECRET|YOUTUBE_ACCESS_TOKEN|"
               r"MEDIA_PUBLICATION_ACCESS_KEY|MEDIA_PUBLICATION_SECRET_KEY|"
               r"MEDIA_FUNNEL_TOKEN_SECRET)\s*[=:]\s*)"
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
        "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_APP_SECRET",
        "YOUTUBE_ACCESS_TOKEN", "MEDIA_PUBLICATION_ACCESS_KEY",
        "MEDIA_PUBLICATION_SECRET_KEY", "MEDIA_FUNNEL_TOKEN_SECRET",
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


def notify_crosspost_ready(result: dict[str, Any], *, dry_run: bool = False) -> bool:
    """Notify only the cross-post preparation result; never include URLs/tokens."""
    if dry_run:
        return False
    platforms = result.get("platforms") or {}
    return notify(
        "crosspost_ready",
        "🎬 動画クロス投稿準備",
        "4媒体向け素材と投稿文の準備結果です。",
        level="info",
        fields={
            "publication_id": result.get("publication_id"),
            "公開予定": result.get("target_publish_at"),
            "品質": result.get("quality_status") or result.get("status"),
            "YouTube": (platforms.get("youtube") or {}).get("status"),
            "X": (platforms.get("x") or {}).get("status"),
            "Threads": (platforms.get("threads") or {}).get("status"),
            "Instagram": (platforms.get("instagram") or {}).get("status"),
        },
    )


def notify_crosspost_result(result: dict[str, Any], *, dry_run: bool = False) -> bool:
    """Send one result summary without signed URLs or credential material."""
    if dry_run:
        return False
    platforms = result.get("platforms") or {}
    return notify(
        "crosspost_result",
        "📊 動画クロス投稿結果",
        "成功済み投稿は維持し、失敗媒体だけを照合します。",
        level="success" if result.get("status") == "published" else "warning",
        fields={
            "publication_id": result.get("publication_id"),
            "全体状態": result.get("status"),
            "公開時間差": result.get("publication_skew_seconds"),
            "YouTube": (platforms.get("youtube") or {}).get("status"),
            "X": (platforms.get("x") or {}).get("status"),
            "Threads": (platforms.get("threads") or {}).get("status"),
            "Instagram": (platforms.get("instagram") or {}).get("status"),
        },
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
    manual_required = int(draft.get("amazon_manual_required") or 0)
    amazon_lines = [
        f"**関連書籍候補:** {_clean(draft.get('amazon_item_count'), 10)}件",
        f"**Amazonリンク作成待ち:** {manual_required}件",
        f"**公式API取得済み:** {_clean(draft.get('amazon_paapi_ready'), 10)}件",
    ]
    if manual_required:
        amazon_lines.extend([
            "**🟠 Amazonリンク設定待ち**",
            "公開前にSiteStripeでリンクを作成し、amazon-link-setコマンドで登録してください。",
        ])
    return "\n".join([
        "📝 **無料note下書きができました**",
        f"**タイトル:** {_clean(draft.get('title'), 300)}",
        f"**記事タイプ:** {_clean(draft.get('article_type'), 80)}",
        f"**文字数:** {_clean(draft.get('character_count'), 20)}",
        f"**読了目安:** {_clean(draft.get('reading_minutes'), 20)}分",
        f"**公開候補日:** {_clean(draft.get('target_publish_date'), 30)}",
        f"**状態:** {_clean(draft.get('status'), 40)}",
        *amazon_lines,
        "",
        "**確認事項:** 事実と一次資料／賛否両論の公平性／タイトルの強さ／AI的な定型表現",
        f"**ローカル保存先:** {_clean(draft.get('path'), 600)}",
        "",
        "cover.png・article.md・sources.md・review.mdを確認し、人が承認後にnoteへ手動公開してください。",
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
        for path in paths[:4]:
            resolved = Path(path)
            handle = resolved.open("rb")
            handles.append(handle)
            content_type = (
                "image/png" if resolved.suffix.lower() == ".png"
                else "text/markdown; charset=utf-8"
            )
            files.append(
                ("files[]", (resolved.name, handle, content_type))
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
            "X投稿": tweet_url,
        },
    )


def notify_error(where: str, error: Any, **context: Any) -> bool:
    error_line = _clean(error, 300).splitlines()[0]
    return notify(
        "error",
        "🚨 処理に失敗しました",
        error_line or "詳細はローカルログを確認してください。",
        level="error",
        fields={"処理": where, "結果": "失敗"},
    )


_RESULT_REASON_LABELS = {
    "no_news": "投稿価値のあるニュースがありませんでした",
    "no_unattempted_slot": "今回処理する新しい投稿枠はありませんでした",
    "already_attempted": "この投稿枠は処理済みです",
    "already_posted": "この投稿枠は投稿済みです",
    "post_interval": "前回投稿からの間隔が短いため見送りました",
    "daily_limit": "本日の投稿上限に達しています",
    "quality_gate": "品質基準を満たさなかったため見送りました",
    "ban_risk": "安全性リスクが高いため見送りました",
    "duplicate": "直前の内容と重複するため見送りました",
    "topic_cooldown": "同じテーマのクールダウン中です",
    "budget_guard": "API予算上限のため処理を見送りました",
    "success": "投稿に成功しました",
}


def _result_reason(reason: Any) -> str:
    raw = str(reason or "").strip()
    return _RESULT_REASON_LABELS.get(raw, _clean(raw, 240)) or "投稿条件を満たしませんでした"


def notify_run_log(record: dict[str, Any], *, exit_code: int = 0) -> bool:
    """Send only the run outcome; detailed diagnostics remain local."""
    decision = str(record.get("decision") or "unknown")
    success = decision == "post"
    level = "success" if success else ("error" if exit_code else "warning")
    icon = "✅" if success else ("🚨" if exit_code else "⏭️")
    title = (
        f"{icon} 投稿完了" if success
        else f"{icon} 処理失敗" if exit_code
        else f"{icon} 今回は投稿なし"
    )
    description = (
        _clean(record.get("title") or record.get("tweet_text"), 700)
        if success
        else _result_reason(record.get("reason"))
    )
    return notify(
        "run_log",
        title,
        description,
        level=level,
        fields={
            "結果": "投稿成功" if success else (
                "失敗" if exit_code else "投稿見送り"),
        },
    )


def notify_attempt_since(path: Path, previous_size: int, *, exit_code: int = 0) -> bool:
    """Send only the newest outcome; successful posts are notified elsewhere."""
    try:
        if not path.exists() or path.stat().st_size <= previous_size:
            if exit_code == 0:
                return False
            return notify(
                "run_log",
                "🚨 処理に失敗しました",
                "詳細はローカルログを確認してください。",
                level="error",
                fields={"結果": "失敗"},
            )
        with path.open("rb") as handle:
            handle.seek(max(0, previous_size))
            appended = handle.read().decode("utf-8", errors="replace")
        rows = [line for line in appended.splitlines() if line.strip()]
        if not rows:
            return False
        record = json.loads(rows[-1])
        if str(record.get("decision") or "") == "post" and exit_code == 0:
            # post.py already emits the concise post-success notification.
            return False
        return notify_run_log(record, exit_code=exit_code)
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
    """Inspect a local log and send counts/result only, never raw log lines."""
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
    error_count = sum(
        "[ERROR]" in line or '"level":"error"' in line.lower()
        for line in selected
    )
    warning_count = sum(
        "[WARN]" in line or '"level":"warning"' in line.lower()
        for line in selected
    )
    if error_count:
        result = "エラーが見つかりました。詳細はローカルログを確認してください。"
        level = "error"
        icon = "🚨"
    elif warning_count:
        result = "警告があります。Botは処理を継続しています。"
        level = "warning"
        icon = "⚠️"
    else:
        result = "異常は見つかりませんでした。"
        level = "success"
        icon = "✅"
    sent = notify(
        "log",
        f"{icon} ログ確認結果",
        result,
        level=level,
        fields={
            "対象": filename,
            "確認行数": len(selected),
            "エラー": error_count,
            "警告": warning_count,
        },
        force=force,
    )
    return sent, len(selected)
