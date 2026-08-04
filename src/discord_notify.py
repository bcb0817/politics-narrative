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


def _notify_embed_report(
    event: str,
    embeds: list[dict[str, Any]],
    *,
    attachment_name: str = "",
    attachment_text: str = "",
    force: bool = False,
    timeout: float | None = None,
) -> bool:
    """Send a readable multi-embed report with an optional Markdown detail."""
    if not force and (not _enabled() or not _event_enabled(event)):
        return False
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url or not embeds:
        return False

    safe_embeds = []
    for source in embeds[:10]:
        embed = {
            "title": _clean(source.get("title"), 256),
            "description": _clean(source.get("description"), 3900),
            "color": source.get("color", _COLORS["info"]),
        }
        fields = []
        for field in (source.get("fields") or [])[:25]:
            if not isinstance(field, dict):
                continue
            fields.append({
                "name": _clean(field.get("name"), 256),
                "value": _clean(field.get("value"), 1000) or "-",
                "inline": bool(field.get("inline", False)),
            })
        if fields:
            embed["fields"] = fields
        safe_embeds.append(embed)
    safe_embeds[-1]["timestamp"] = datetime.now(JST).isoformat()
    safe_embeds[-1]["footer"] = {
        "text": "久世ゆい・X Searchリサーチ"
    }
    payload = {
        "username": os.environ.get(
            "DISCORD_WEBHOOK_USERNAME", "久世ゆい Bot"),
        "allowed_mentions": {"parse": []},
        "embeds": safe_embeds,
    }
    try:
        request_kwargs: dict[str, Any] = {
            "timeout": timeout or float(os.environ.get(
                "DISCORD_WEBHOOK_TIMEOUT_SECONDS", "10")),
        }
        safe_attachment = sanitize(attachment_text)
        if attachment_name and safe_attachment:
            request_kwargs["data"] = {
                "payload_json": json.dumps(payload, ensure_ascii=False)
            }
            request_kwargs["files"] = {
                "files[0]": (
                    _clean(attachment_name, 120),
                    safe_attachment.encode("utf-8"),
                    "text/markdown; charset=utf-8",
                )
            }
        else:
            request_kwargs["json"] = payload
        response = requests.post(webhook_url, **request_kwargs)
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


def notify_threads_research(report: dict[str, Any], *,
                            dry_run: bool = False) -> bool:
    """Send one concise Threads research and local-analysis result."""
    if dry_run:
        return False

    searches = report.get("searches") or []
    query_names = list(dict.fromkeys(
        _clean(row.get("query"), 80)
        for row in searches
        if isinstance(row, dict) and row.get("query")
    ))[:8]
    representatives = report.get("representative_posts") or []
    content_lines = []
    for index, row in enumerate(representatives[:3], 1):
        if not isinstance(row, dict):
            continue
        excerpt = _clean(row.get("text"), 220)
        permalink = str(row.get("permalink") or "").strip()
        if permalink.startswith("https://"):
            content_lines.append(f"{index}. {excerpt}\n{permalink}")
        elif excerpt:
            content_lines.append(f"{index}. {excerpt}")

    entities = report.get("top_entities") or []
    analysis_lines = []
    for row in entities[:5]:
        if not isinstance(row, dict):
            continue
        name = _clean(row.get("entity"), 60)
        score = row.get("trend_score")
        state = _clean(row.get("state"), 30)
        posts = int(row.get("post_count") or 0)
        verification = (
            "公式・報道照合あり"
            if row.get("eligible_for_post")
            else "Threads内の未検証シグナル"
        )
        analysis_lines.append(
            f"• **{name}**: {score}点 / {state} / {posts}件 / {verification}"
        )

    searched_count = int(report.get("search_run_count") or len(searches))
    result_count = int(report.get("result_count") or 0)
    unique_posts = int(report.get("unique_post_count") or 0)
    eligible_count = int(report.get("eligible_entity_count") or 0)
    level = (
        "success" if eligible_count
        else "warning" if searched_count and not unique_posts
        else "info"
    )
    return notify(
        "threads_research",
        "🔎 Threadsリサーチ・分析結果",
        (
            "Threads公式APIで取得した公開投稿の標本をローカル分析しました。"
            "Threadsの反応だけを事実認定や投稿根拠には使用しません。"
        ),
        level=level,
        fields={
            "検索条件": (
                f"対象: 直近{int(report.get('lookback_hours') or 24)}時間\n"
                f"検索語: {', '.join(query_names) or '記録なし'}"
            ),
            "取得結果": (
                f"検索実行: {searched_count}回\n"
                f"API結果: {result_count}件\n"
                f"重複除外後: {unique_posts}件"
            ),
            "リサーチ内容": (
                "\n".join(content_lines) or "該当する公開投稿はありませんでした。"
            ),
            "分析結果": (
                "\n".join(analysis_lines)
                or "比較可能なトレンド標本が不足しています。"
            ),
            "投稿判断": (
                f"公式・報道との照合条件を満たす話題: {eligible_count}件\n"
                "未照合のThreads情報は投稿候補に採用しません。"
            ),
            "注意点": (
                "取得結果はThreads全体の順位ではなく、"
                "指定検索語で収集できた範囲の相対分析です。"
            ),
        },
    )


def notify_x_research(report: dict[str, Any], *,
                      dry_run: bool = False) -> bool:
    """Send a scan-friendly X Search report plus a full Markdown attachment."""
    if dry_run:
        return False

    provider = _clean(report.get("provider"), 80) or "X Search"
    queries = list(dict.fromkeys(
        _clean(value, 80) for value in (report.get("queries") or []) if value
    ))[:8]
    topics = sorted(
        (row for row in (report.get("topics") or [])
         if isinstance(row, dict)),
        key=lambda row: (
            float(row.get("attention_score") or 0),
            float(row.get("velocity_score") or 0),
        ),
        reverse=True,
    )

    corroborated = int(report.get("corroborated_topic_count") or 0)
    query_count = int(report.get("query_count") or len(queries))
    resource_count = int(report.get("resource_count") or 0)
    topic_count = int(report.get("topic_count") or len(topics))
    lookback = int(report.get("lookback_minutes") or 360)
    if corroborated:
        conclusion = (
            f"**今回の結論：公式情報と照合できた話題が{corroborated}件あります。**\n"
            "投稿候補にする場合は、下記の根拠と反対意見を確認します。"
        )
    else:
        conclusion = (
            "**今回の結論：現時点で、そのまま投稿根拠にできる話題はありません。**\n"
            "X上の反応は注目テーマの発見だけに使用します。"
        )
    color = _COLORS["success"] if corroborated else _COLORS["info"]
    embeds: list[dict[str, Any]] = [{
        "title": "🔎 X Search リサーチレポート",
        "description": (
            f"{conclusion}\n\n"
            f"調査元：{provider}\n"
            f"対象期間：直近{lookback}分\n"
            f"検索テーマ：{', '.join(queries) or '候補ニュース連動'}"
        ),
        "color": color,
        "fields": [
            {"name": "検索", "value": f"**{query_count}** 回",
             "inline": True},
            {"name": "分析対象", "value": f"**{topic_count}** 件",
             "inline": True},
            {"name": "公式照合済み", "value": f"**{corroborated}** 件",
             "inline": True},
        ],
    }]

    number_icons = ("1️⃣", "2️⃣", "3️⃣", "4️⃣")
    for index, row in enumerate(topics[:4]):
        key = _clean(row.get("topic_key"), 110) or "名称未設定"
        main_claims = [
            _clean(value, 210) for value in (row.get("main_claims") or [])
            if value
        ][:2]
        counter_claims = [
            _clean(value, 210)
            for value in (row.get("counter_claims") or [])
            if value
        ][:2]
        links = []
        for post_id in (row.get("representative_post_ids") or [])[:3]:
            digits = re.sub(r"\D", "", str(post_id or ""))
            if digits:
                links.append(
                    f"[X投稿を確認]("
                    f"https://x.com/i/web/status/{digits})")
        verified = bool(row.get("externally_corroborated"))
        description_parts = [
            "**主な見方**\n" + (
                "\n".join(f"• {claim}" for claim in main_claims)
                if main_claims else "• 明確な主張を抽出できませんでした。"),
            "**反対・補足の見方**\n" + (
                "\n".join(f"• {claim}" for claim in counter_claims)
                if counter_claims else "• 比較できる反対意見が不足しています。"),
        ]
        embeds.append({
            "title": f"{number_icons[index]} {key}",
            "description": "\n\n".join(description_parts),
            "color": _COLORS["success"] if verified else _COLORS["warning"],
            "fields": [
                {"name": "注目度",
                 "value": f"{row.get('attention_score', 0)}点",
                 "inline": True},
                {"name": "拡散速度",
                 "value": f"{row.get('velocity_score', 0)}点",
                 "inline": True},
                {"name": "確認状態",
                 "value": "✅ 公式・報道照合あり"
                 if verified else "⚠️ X内のみ・未検証",
                 "inline": True},
                {"name": "根拠リンク",
                 "value": " ｜ ".join(links) if links else "代表投稿なし",
                 "inline": False},
            ],
        })

    embeds.append({
        "title": "🧭 Botの投稿判断",
        "description": (
            f"✅ 公式情報と照合できた話題：**{corroborated}件**\n"
            "⛔ 未照合のX情報：投稿の事実根拠には使用しません\n"
            "📌 X Searchの役割：世論全体の順位ではなく、"
            "指定テーマ内の注目論点を見つけること"
        ),
        "color": color,
        "fields": [{
            "name": "データ量",
            "value": f"検索・取得呼び出し：{resource_count}件",
            "inline": False,
        }],
    })

    markdown = [
        "# X Search リサーチ・分析結果",
        "",
        f"- 調査元: {provider}",
        f"- 対象期間: 直近{lookback}分",
        f"- 検索テーマ: {', '.join(queries) or '候補ニュース連動'}",
        f"- 分析対象: {topic_count}件",
        f"- 公式・報道照合済み: {corroborated}件",
        "",
        "## トピック詳細",
    ]
    for index, row in enumerate(topics, 1):
        markdown.extend([
            "",
            f"### {index}. {_clean(row.get('topic_key'), 180)}",
            "",
            f"- 注目度: {row.get('attention_score', 0)}",
            f"- 拡散速度: {row.get('velocity_score', 0)}",
            "- 確認状態: " + (
                "公式・報道照合あり"
                if row.get("externally_corroborated")
                else "X内のみ・未検証"),
            "",
            "#### 主な見方",
        ])
        markdown.extend(
            f"- {_clean(value, 500)}"
            for value in (row.get("main_claims") or [])
            if value)
        markdown.extend(["", "#### 反対・補足の見方"])
        markdown.extend(
            f"- {_clean(value, 500)}"
            for value in (row.get("counter_claims") or [])
            if value)
        post_ids = [
            re.sub(r"\D", "", str(value or ""))
            for value in (row.get("representative_post_ids") or [])
        ]
        post_ids = [value for value in post_ids if value]
        if post_ids:
            markdown.extend(["", "#### 代表投稿"])
            markdown.extend(
                f"- https://x.com/i/web/status/{post_id}"
                for post_id in post_ids[:5])
    markdown.extend([
        "",
        "## 注意",
        "",
        "X Searchの結果は、指定テーマで取得できた範囲の相対分析です。",
        "未照合のX情報だけを事実認定や投稿根拠には使用しません。",
    ])
    return _notify_embed_report(
        "x_research",
        embeds,
        attachment_name="x-search-research-report.md",
        attachment_text="\n".join(markdown),
    )


def notify_integrated_research(report: dict[str, Any], *,
                               dry_run: bool = False) -> bool:
    """Notify only the bounded outcome of a cross-source research run."""
    if dry_run:
        return False
    topics = report.get("topics") or []
    result_lines = []
    for row in topics[:5]:
        if not isinstance(row, dict):
            continue
        result_lines.append(
            f"• **{_clean(row.get('title'), 100)}**\n"
            f"  信頼度 {float(row.get('confidence') or 0):.2f} / "
            f"投稿価値 {float(row.get('posting_value_score') or 0):.1f} / "
            f"{'候補' if row.get('post_eligible') else '見送り'}"
        )
    return notify(
        "integrated_research",
        "🧭 統合リサーチ結果",
        "公式・報道の事実と、X・Threadsの反応標本を分けて照合しました。",
        level="success" if int(report.get("eligible_count") or 0) else "info",
        fields={
            "結果": (
                f"分析: {int(report.get('topic_count') or len(topics))}件\n"
                f"投稿候補: {int(report.get('eligible_count') or 0)}件\n"
                f"見送り: {int(report.get('skipped_count') or 0)}件"
            ),
            "主要テーマ": "\n".join(result_lines) or "対象テーマなし",
            "注意": (
                "SNS反応は検索で取得できた標本です。"
                "未検証の反応だけでは投稿しません。"
            ),
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
            "監視間隔": f"{os.environ.get('MONITOR_INTERVAL_MINUTES', '45')}分",
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


def notify_review_strategy_result(
    activation: dict[str, Any],
    *,
    prior_evaluation: dict[str, Any] | None = None,
) -> bool:
    """Send only the daily strategy result, never raw prompts or metrics."""
    evaluation = prior_evaluation or {}
    activated = bool(activation.get("activated"))
    rolled_back = evaluation.get("status") == "rollback"
    if rolled_back:
        title = "⚠️ ChatGPT投稿方針を自動停止"
        description = "対照群比較または安全性評価で悪化を検知したため、旧方針を停止しました。"
        level = "warning"
    elif activated:
        title = "✅ ChatGPT投稿方針を更新"
        description = "日次レビューの検証条件を満たした方針を本番投稿へ反映しました。"
        level = "success"
    else:
        title = "ℹ️ ChatGPT投稿方針は変更なし"
        description = "検証条件を満たさなかったため、Botは安全な既定方針で継続します。"
        level = "info"
    policy = activation.get("policy") or {}
    return notify(
        "review_strategy",
        title,
        description,
        level=level,
        fields={
            "結果": activation.get("reason") or evaluation.get("reason"),
            "実験": policy.get("experiment_name")
            or evaluation.get("experiment_name"),
            "施策群": evaluation.get("treatment_count"),
            "対照群": evaluation.get("control_count"),
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


def notify_disaster_update(result: dict[str, Any], *, dry_run: bool = False) -> bool:
    """Notify one concise disaster result without raw logs or personal data."""
    if dry_run:
        return False
    snapshot = result.get("snapshot") or {}
    delta = result.get("delta") or {}
    eligible = bool(result.get("publish_eligible"))
    changes = [
        row for row in delta.get("changes", [])
        if row.get("delta_status") not in {"unchanged", "unavailable"}
    ][:3]
    return notify(
        "disaster_update",
        "【熊本地震・朝夕更新準備】" if eligible
        else "【熊本地震・定点更新見送り】",
        "公式情報の候補を準備しました。" if eligible
        else "前回投稿から重大な公式変化が確認できなかったため、今回の投稿を見送りました。",
        level="info" if eligible else "warning",
        fields={
            "時点": snapshot.get("cutoff_at"),
            "スナップショット": snapshot.get("snapshot_id"),
            "前回からの重要変化": "／".join(
                str(row.get("metric_key")) for row in changes)
                or "重大な変化なし",
            "投稿判定": "投稿候補" if eligible else "見送り",
            "理由": result.get("decision_reason"),
        },
    )


def notify_disaster_correction(result: dict[str, Any], *,
                               dry_run: bool = False) -> bool:
    """Notify a correction candidate; automatic correction posting stays off."""
    if dry_run or not result.get("correction_required"):
        return False
    return notify(
        "disaster_correction",
        "【熊本地震・訂正候補】",
        "公式発表の訂正または集計範囲変更を検知しました。",
        level="warning",
        fields={
            "対象投稿": result.get("snapshot_id"),
            "訂正項目": result.get("metric_key"),
            "旧情報": result.get("previous_value"),
            "新情報": result.get("current_value"),
            "公式情報源": result.get("source_name"),
            "自動投稿": "OFF",
        },
    )


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


def notify_daily_post_goal(report: dict[str, Any]) -> bool:
    """Send only the operational summary; never include raw attempts/logs."""
    achievement = report.get("achievement") or {}
    actual = report.get("actual") or {}
    analysis = report.get("analysis") or {}
    met = bool(achievement.get("met"))
    actions = report.get("remediation") or []
    next_action = str(actions[0].get("action") or "") if actions else ""
    return notify(
        "daily_post_goal",
        "✅ 日次投稿目標達成" if met else "⚠️ 日次投稿目標未達",
        "Xの成功投稿をJST日次で集計しました。",
        level="success" if met else "warning",
        fields={
            "日付": report.get("report_date"),
            "X投稿": f"{actual.get('x', 0)} / {(report.get('target') or {}).get('posts', 20)}",
            "Threads公開": actual.get("threads", 0),
            "不足": achievement.get("shortfall", 0),
            "主因": analysis.get("primary_reason") or "なし",
            "優先対策": next_action or "現行運用を維持",
        },
    )
