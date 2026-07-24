"""Human-approval queues. This module contains no X write operations."""

from __future__ import annotations

import json
import os
import csv
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from api_budget import estimate_openai, estimate_x, finalize, reserve
from metrics_db import connect, db_path, init_db, write
from xai_radar import load_cache

JST = ZoneInfo("Asia/Tokyo")
VALID_STATUSES = {
    "pending", "approved", "rejected", "expired", "posted_manually",
    "result_pending", "result_collected",
}
SAFE_AUTHOR_TYPES = {"official", "politician", "journalist", "researcher", "media"}


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _output_dir(queue_type: str) -> Path:
    name = "quote_posts" if queue_type == "quote" else "replies"
    path = _root() / "outputs" / "manual_queue" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _verified_topic(topic_key: str, path: Path | None = None) -> bool:
    if not topic_key:
        return False


def _created_today_count(queue_type: str, path: Path | None = None) -> int:
    init_db(path)
    try:
        with closing(connect(path)) as conn:
            return int(conn.execute("""SELECT COUNT(*) FROM engagement_queue
              WHERE queue_type=? AND created_at LIKE ?""",
              (queue_type, datetime.now(JST).date().isoformat() + "%",)).fetchone()[0])
    except Exception:
        return 0
    init_db(path)
    try:
        with closing(connect(path)) as conn:
            row = conn.execute("""SELECT 1 FROM news_candidates WHERE verified=1 AND
              (topic_key=? OR title LIKE ?) ORDER BY id DESC LIMIT 1""",
              (topic_key, f"%{topic_key}%")).fetchone()
            return bool(row)
    except Exception:
        return False


def _comment_options(topic: dict, client_factory=None, path: Path | None = None) -> list[dict]:
    """Generate three compact original comments; never includes source post text."""
    fallback = [
        {"style": "補足", "text": f"この論点は、{topic.get('topic_key','政策')}の制度面と実施条件を分けて確認したいです。"},
        {"style": "賛成＋条件", "text": "方向性には賛成です。ただし、対象・財源・検証期限を明示することが条件です。"},
        {"style": "反論", "text": "結論を急ぐ前に、一次資料と反対側の最も強い根拠も並べる必要があります。"},
    ]
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return fallback
    model = os.environ.get("OPENAI_MODEL_POST", "gpt-5.4-mini")
    estimated = estimate_openai(model, 900, 500)
    reservation, _ = reserve("openai", "engagement_queue", model, estimated, path=path)
    if not reservation:
        return fallback
    try:
        if client_factory is None:
            from openai import OpenAI
            client_factory = OpenAI
        client = client_factory(api_key=os.environ["OPENAI_API_KEY"], max_retries=0,
                                timeout=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "90")))
        schema = {"type": "object", "properties": {"options": {"type": "array", "minItems": 3,
          "maxItems": 3, "items": {"type": "object", "properties": {
              "style": {"type": "string", "enum": ["補足", "賛成＋条件", "反論"]},
              "text": {"type": "string"}}, "required": ["style", "text"],
              "additionalProperties": False}}}, "required": ["options"], "additionalProperties": False}
        response = client.responses.create(model=model,
            instructions=("Create three original Japanese quote-comment drafts. Do not quote or imitate another post. "
                          "No personal attacks, unsupported criminal claims, URLs, or partisan-group attacks."),
            input=json.dumps({"topic_key": topic.get("topic_key"), "summary": topic.get("summary"),
                              "main_claims": topic.get("main_claims", [])[:3],
                              "counter_claims": topic.get("counter_claims", [])[:3]}, ensure_ascii=False),
            max_output_tokens=500,
            text={"format": {"type": "json_schema", "name": "quote_options", "strict": True, "schema": schema}},
            store=False)
        data = json.loads(response.output_text)["options"]
        usage = response.usage
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        actual = estimate_openai(model, inp, out) or 0
        finalize(reservation, actual, success=True, input_tokens=inp, output_tokens=out, path=path)
        return [{"style": str(v["style"]), "text": str(v["text"])[:240]} for v in data]
    except Exception as exc:
        finalize(reservation, 0, success=False, error_type=type(exc).__name__, path=path)
        return fallback


def _comment_options(topic: dict, client_factory=None, path: Path | None = None) -> list[dict]:
    """Generate three clean Japanese drafts without quoting the source post."""
    topic_key = str(topic.get("topic_key") or "政策")
    return [
        {
            "style": "補足",
            "text": f"{topic_key}は、制度の対象範囲と実施条件を一次資料で確認したい論点です。",
        },
        {
            "style": "賛成・条件",
            "text": "方向性への評価とは別に、対象者・費用・検証期限が明示されているかが重要です。",
        },
        {
            "style": "反論",
            "text": "結論を急ぐ前に、賛成側と反対側の最も強い根拠を同じ基準で比べる必要があります。",
        },
    ]


def _insert(item: dict, path: Path | None = None) -> int | None:
    init_db(path)
    now = datetime.now(JST)
    return write("""INSERT OR IGNORE INTO engagement_queue
      (queue_type,source_post_id,author_handle,author_type,topic_key,source_verified,
       reason_selected,content_json,risk_flags_json,status,created_at,updated_at,expires_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
        item["queue_type"], item["post_id"], item.get("author_handle", ""), item.get("author_type", ""),
        item.get("topic_key", ""), int(bool(item.get("source_verified"))), item.get("reason_selected", ""),
        json.dumps(item, ensure_ascii=False), json.dumps(item.get("risk_flags", []), ensure_ascii=False),
        "pending", now.isoformat(), now.isoformat(), (now + timedelta(days=7)).isoformat()), path)


def _export(queue_type: str, path: Path | None = None) -> tuple[Path, Path]:
    init_db(path)
    today = datetime.now(JST).date().isoformat()
    rows = []
    try:
        with closing(connect(path)) as conn:
            db_rows = conn.execute("""SELECT * FROM engagement_queue WHERE queue_type=?
              AND created_at LIKE ? ORDER BY id""", (queue_type, today + "%")).fetchall()
        for row in db_rows:
            content = json.loads(row["content_json"] or "{}")
            content.update({"id": row["id"], "status": row["status"]})
            rows.append(content)
    except Exception:
        pass
    out = _output_dir(queue_type)
    json_path = out / f"{today}.json"
    md_path = out / f"{today}.md"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    title = "コメント付きリポスト" if queue_type == "quote" else "返信"
    lines = [f"# {title}候補 — {today}", "", "自動送信なし。人間確認用です。", ""]
    for row in rows:
        lines += [f"## #{row.get('id')} @{row.get('author_handle','')} [{row.get('status')}]",
                  f"- topic: {row.get('topic_key','')}", f"- reason: {row.get('reason_selected','')}"]
        for option in row.get("comment_options", row.get("reply_options", [])):
            lines.append(f"- {option.get('style','案')}: {option.get('text','')}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def _export(queue_type: str, path: Path | None = None) -> tuple[Path, Path]:
    """Export the queue in UTF-8 JSON and readable Japanese Markdown."""
    init_db(path)
    today = datetime.now(JST).date().isoformat()
    rows = []
    try:
        with closing(connect(path)) as conn:
            db_rows = conn.execute("""SELECT * FROM engagement_queue WHERE queue_type=?
              AND created_at LIKE ? ORDER BY id""", (queue_type, today + "%")).fetchall()
        for row in db_rows:
            content = json.loads(row["content_json"] or "{}")
            content.update({"id": row["id"], "status": row["status"]})
            rows.append(content)
    except Exception:
        pass
    out = _output_dir(queue_type)
    json_path = out / f"{today}.json"
    md_path = out / f"{today}.md"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    title = "引用投稿候補" if queue_type == "quote" else "返信候補"
    lines = [f"# {title} — {today}", "", "自動送信なし。人間確認用です。", ""]
    for row in rows:
        lines.extend([
            f"## #{row.get('id')} @{row.get('author_handle', '')} [{row.get('status')}]",
            f"- topic: {row.get('topic_key', '')}",
            f"- reason: {row.get('reason_selected', '')}",
        ])
        for option in row.get("comment_options", row.get("reply_options", [])):
            lines.append(f"- {option.get('style', '案')}: {option.get('text', '')}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def build_quote_queue(client_factory=None, path: Path | None = None) -> int:
    if os.environ.get("QUOTE_CANDIDATE_QUEUE_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return 0
    daily_remaining = max(
        0, int(os.environ.get("QUOTE_CANDIDATES_MAX_PER_DAY", "5"))
        - _created_today_count("quote", path)
    )
    maximum = min(
        daily_remaining, int(os.environ.get("QUOTE_CANDIDATES_MAX_PER_RUN", "3"))
    )
    count = 0
    for topic in load_cache(allow_expired=True):
        if count >= maximum or not _verified_topic(str(topic.get("topic_key", "")), path):
            continue
        for post in topic.get("representative_posts", []):
            if count >= maximum:
                break
            if post.get("author_type") not in SAFE_AUTHOR_TYPES or not post.get("post_id"):
                continue
            item = {"queue_type": "quote", "post_id": str(post["post_id"]),
                    "author_handle": str(post.get("author_handle", "")),
                    "author_type": str(post.get("author_type", "")),
                    "topic_key": str(topic.get("topic_key", "")), "source_verified": True,
                    "reason_selected": str(post.get("reason_selected", "")),
                    "comment_options": _comment_options(topic, client_factory, path),
                    "risk_flags": [], "status": "pending"}
            if _insert(item, path):
                count += 1
    _export("quote", path)
    return count


def build_reply_queue(path: Path | None = None) -> int:
    """Queue only locally collected mentions; never searches or writes to X."""
    if os.environ.get("REPLY_CANDIDATE_QUEUE_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return 0
    source = _root() / os.environ.get("REPLY_SOURCE_FILE", "data/mentions_latest.json")
    if not source.exists() and os.environ.get("REPLY_MENTION_FETCH_ENABLED", "true").lower() in {"1", "true", "yes"}:
        _fetch_mentions(source, path)
    try:
        rows = json.loads(source.read_text(encoding="utf-8"))
    except Exception:
        rows = []
    daily_remaining = max(
        0, int(os.environ.get("REPLY_CANDIDATES_MAX_PER_DAY", "10"))
        - _created_today_count("reply", path)
    )
    maximum = min(
        daily_remaining, int(os.environ.get("REPLY_CANDIDATES_MAX_PER_RUN", "5"))
    )
    count = 0
    for row in rows if isinstance(rows, list) else []:
        if count >= maximum:
            break
        if not row.get("post_id") or not row.get("constructive") or row.get("author_type") == "suspected_bot":
            continue
        if row.get("abusive") or row.get("repeated_target") or row.get("unrelated"):
            continue
        item = {"queue_type": "reply", "post_id": str(row["post_id"]),
                "author_handle": str(row.get("author_handle", "")),
                "author_type": str(row.get("author_type", "other")),
                "topic_key": str(row.get("topic_key", "")), "source_verified": bool(row.get("source_verified")),
                "reason_selected": str(row.get("reason_selected", "constructive reply")),
                "reply_options": row.get("reply_options") or [
                    {"style": "回答案", "text": str(row.get("draft", "確認して回答します。"))[:240]}],
                "risk_flags": list(row.get("risk_flags") or []), "status": "pending"}
        if not row.get("reply_options"):
            item["reply_options"] = [{
                "style": "回答案",
                "text": str(row.get(
                    "draft", "ご指摘ありがとうございます。一次資料を確認し、事実と意見を分けて回答します。"
                ))[:240],
            }]
        if _insert(item, path):
            count += 1
    _export("reply", path)
    return count


def _fetch_mentions(source: Path, path: Path | None = None) -> int:
    """Read own mentions through X API; no reply/like/repost write endpoint is used."""
    required = ("API_KEY", "API_KEY_SECRET", "ACCESS_TOKEN", "ACCESS_TOKEN_SECRET")
    if any(not os.environ.get(key, "").strip() for key in required):
        return 0
    maximum = min(100, int(os.environ.get("REPLY_CANDIDATES_MAX_PER_DAY", "10")))
    reservation, _ = reserve("x", "owned_read", "mentions", estimate_x("owned_read_per_resource", maximum),
                             maximum, {"purpose": "manual_reply_queue"}, path=path)
    if not reservation:
        return 0
    try:
        import tweepy
        client = tweepy.Client(consumer_key=os.environ["API_KEY"], consumer_secret=os.environ["API_KEY_SECRET"],
                               access_token=os.environ["ACCESS_TOKEN"],
                               access_token_secret=os.environ["ACCESS_TOKEN_SECRET"], wait_on_rate_limit=False)
        me = client.get_me(user_auth=True)
        response = client.get_users_mentions(id=me.data.id, max_results=max(5, maximum), user_auth=True,
            expansions=["author_id"], tweet_fields=["author_id", "conversation_id", "created_at", "text"],
            user_fields=["username", "public_metrics"])
        users = {str(user.id): user for user in ((getattr(response, "includes", None) or {}).get("users", []))}
        rows = []
        abuse = ("死ね", "消えろ", "馬鹿", "クズ", "ゴミ")
        constructive_terms = ("?", "？", "教えて", "根拠", "訂正", "誤り", "補足", "一方で", "反対")
        for tweet in list(response.data or [])[:maximum]:
            text = str(getattr(tweet, "text", "") or "")[:280]
            if any(term in text for term in abuse) or not any(term in text for term in constructive_terms):
                continue
            user = users.get(str(getattr(tweet, "author_id", "")))
            rows.append({"post_id": str(tweet.id), "author_handle": str(getattr(user, "username", "") or ""),
                         "author_type": "other", "constructive": True, "source_verified": False,
                         "reason_selected": "自分の投稿への質問・訂正・建設的反論",
                         "draft": "ご指摘ありがとうございます。一次資料を確認し、事実と意見を分けて回答します。",
                         "context_excerpt": text})
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        actual_count = len(list(response.data or [])[:maximum])
        finalize(reservation, estimate_x("owned_read_per_resource", actual_count) or 0,
                 success=True, resource_count=actual_count, path=path)
        return len(rows)
    except Exception as exc:
        finalize(reservation, 0, success=False, error_type=type(exc).__name__, resource_count=0, path=path)
        return 0


def build_all(client_factory=None, path: Path | None = None) -> dict:
    return {"quote_candidates": build_quote_queue(client_factory, path),
            "reply_candidates": build_reply_queue(path), "x_writes": 0}


def status_counts(path: Path | None = None) -> dict:
    init_db(path)
    out = {"pending_quote": 0, "pending_reply": 0,
           **{key: 0 for key in VALID_STATUSES if key != "pending"}}
    try:
        with closing(connect(path)) as conn:
            for row in conn.execute("""SELECT queue_type,status,COUNT(*) count FROM engagement_queue
              GROUP BY queue_type,status"""):
                if row["status"] == "pending":
                    out[f"pending_{row['queue_type']}"] = int(row["count"])
                else:
                    out[row["status"]] += int(row["count"])
    except Exception:
        pass
    return out


def update_status(queue_type: str, item_id: int, status: str, path: Path | None = None) -> bool:
    if queue_type not in {"quote", "reply"} or status not in VALID_STATUSES:
        return False
    init_db(path)
    from metrics_db import apply_additive_migrations
    apply_additive_migrations(path)
    try:
        with closing(connect(path)) as conn:
            approved_at = datetime.now(JST).isoformat() if status == "approved" else None
            cur = conn.execute("""UPDATE engagement_queue SET status=?,updated_at=?,
              approved_at=COALESCE(?,approved_at)
              WHERE id=? AND queue_type=?""", (
                status, datetime.now(JST).isoformat(), approved_at,
                item_id, queue_type))
            conn.commit()
            changed = cur.rowcount == 1
        if changed:
            _export(queue_type, path)
        return changed
    except Exception:
        return False


def engagement_brief(path: Path | None = None, now: datetime | None = None) -> Path:
    """Create a human action brief. This function performs zero X writes."""
    now = now or datetime.now(JST)
    init_db(path)
    today = now.date().isoformat()
    rows = []
    counts = status_counts(path)
    try:
        with closing(connect(path)) as conn:
            rows = [dict(row) for row in conn.execute("""SELECT * FROM engagement_queue
              WHERE created_at LIKE ? ORDER BY
              CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END,id""",
              (today + "%",))]
    except Exception:
        pass
    quotes = [row for row in rows if row["queue_type"] == "quote" and row["status"] == "pending"][:3]
    replies = [row for row in rows if row["queue_type"] == "reply" and row["status"] == "pending"][:5]
    expired = [row for row in rows if row["status"] == "expired"]
    out_dir = _root() / "reports" / "engagement"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{today}.md"
    lines = [
        f"# 手動エンゲージメント対応 {today}", "",
        "自動送信は行いません。X上で内容を確認してから手動対応してください。", "",
        f"- 未処理件数: {counts.get('pending_quote', 0) + counts.get('pending_reply', 0)}",
        f"- 手動対応済み: {counts.get('posted_manually', 0)}",
        f"- 期限切れ: {len(expired)}",
        "- 推奨対応時間: 12:30〜13:00 / 20:30〜21:00", "",
        "## 今日の引用候補 上位3件", "",
    ]
    for row in quotes:
        lines.append(f"- #{row['id']} @{row['author_handle']} — {row['reason_selected']}")
    lines.extend(["", "## 今日の返信候補 上位5件", ""])
    for row in replies:
        lines.append(f"- #{row['id']} @{row['author_handle']} — {row['reason_selected']}")
    lines.extend(["", "## 期限切れ候補", ""])
    lines.extend(f"- #{row['id']} {row['queue_type']}" for row in expired)
    out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out


def mark_posted(
    queue_type: str,
    item_id: int,
    post_id: str,
    *,
    selected_option: str = "",
    notes: str = "",
    path: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """Record a human-created X post. This performs zero X writes."""
    if queue_type not in {"quote", "reply"} or not str(post_id).strip():
        return False
    now = now or datetime.now(JST)
    init_db(path)
    from metrics_db import apply_additive_migrations
    apply_additive_migrations(path)
    try:
        with closing(connect(path)) as conn:
            cur = conn.execute("""UPDATE engagement_queue SET
              status='posted_manually',updated_at=?,posted_manually_at=?,
              manual_post_id=?,manual_post_url=?,selected_option=?,
              operator_notes=?,result_measurement_due_at=?
              WHERE id=? AND queue_type=?""", (
                now.isoformat(), now.isoformat(), str(post_id).strip(),
                f"https://x.com/i/web/status/{str(post_id).strip()}",
                selected_option, notes, (now + timedelta(hours=1)).isoformat(),
                item_id, queue_type,
            ))
            conn.commit()
            changed = cur.rowcount == 1
        if changed:
            _export(queue_type, path)
        return changed
    except Exception:
        return False


def import_engagement_results(source: Path, path: Path | None = None) -> dict:
    """Import manually measured engagement without changing the source CSV."""
    init_db(path)
    from metrics_db import apply_additive_migrations
    apply_additive_migrations(path)
    inserted = duplicates = rejected = 0
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                queue_id = int(row.get("queue_id") or row.get("id") or 0)
                queue_type = str(row.get("queue_type") or row.get("type") or "")
                window = str(row.get("measurement_window") or "24h")
                if queue_id <= 0 or queue_type not in {"quote", "reply"}:
                    raise ValueError("invalid_queue_reference")
                with closing(connect(path)) as conn:
                    queue = conn.execute("""SELECT manual_post_id FROM engagement_queue
                        WHERE id=? AND queue_type=?""", (queue_id, queue_type)).fetchone()
                if not queue:
                    raise ValueError("queue_not_found")
                result = write("""INSERT OR IGNORE INTO engagement_results
                  (queue_id,queue_type,manual_post_id,measurement_window,measured_at,
                   impressions,likes,reposts,replies,quotes,profile_clicks,
                   estimated_follower_delta,source,metadata_json)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    queue_id, queue_type,
                    row.get("manual_post_id") or queue["manual_post_id"] or "",
                    window, row.get("measured_at") or datetime.now(JST).isoformat(),
                    int(float(row.get("impressions") or 0)),
                    int(float(row.get("likes") or 0)),
                    int(float(row.get("reposts") or 0)),
                    int(float(row.get("replies") or 0)),
                    int(float(row.get("quotes") or 0)),
                    int(float(row.get("profile_clicks") or 0)),
                    float(row.get("estimated_follower_delta") or 0),
                    "csv", json.dumps(row, ensure_ascii=False),
                ), path)
                if result:
                    inserted += 1
                    write("""UPDATE engagement_queue SET status='result_collected',
                      result_collected_at=?,updated_at=? WHERE id=?""", (
                        datetime.now(JST).isoformat(), datetime.now(JST).isoformat(),
                        queue_id), path)
                else:
                    duplicates += 1
            except (ValueError, TypeError):
                rejected += 1
    return {
        "inserted": inserted, "duplicates": duplicates, "rejected": rejected,
        "source_unchanged": True,
    }


def engagement_performance(path: Path | None = None) -> dict:
    init_db(path)
    from metrics_db import apply_additive_migrations
    apply_additive_migrations(path)
    minimum = int(os.environ.get("ENGAGEMENT_ROI_MIN_SAMPLE_SIZE", "10"))
    with closing(connect(path)) as conn:
        queue_rows = [dict(row) for row in conn.execute(
            "SELECT * FROM engagement_queue")]
        results = [dict(row) for row in conn.execute("""SELECT r.*,q.author_type,
            q.topic_key,q.posted_manually_at FROM engagement_results r
            JOIN engagement_queue q ON q.id=r.queue_id
              WHERE r.measurement_window IN ('24h','latest')""")]
        original = conn.execute("""SELECT COUNT(*) posts,
            AVG(m.impressions) average_impressions,
            AVG(m.profile_clicks) average_profile_clicks
            FROM published_posts p LEFT JOIN post_metrics m
            ON m.id=(SELECT id FROM post_metrics WHERE tweet_id=p.tweet_id
              ORDER BY CASE measurement_window WHEN '72h' THEN 3 WHEN '24h' THEN 2
              WHEN '1h' THEN 1 ELSE 0 END DESC,id DESC LIMIT 1)""").fetchone()
    for row in results:
        try:
            row["posted_hour_jst"] = datetime.fromisoformat(
                row.get("posted_manually_at") or "").astimezone(JST).hour
        except (TypeError, ValueError):
            row["posted_hour_jst"] = None

    def grouped(key: str) -> dict:
        out = {}
        for row in results:
            label = str(row.get(key) or "unknown")
            rec = out.setdefault(label, {
                "samples": 0, "impressions": 0, "reactions": 0,
                "profile_clicks": 0, "estimated_follows": 0.0,
            })
            rec["samples"] += 1
            rec["impressions"] += int(row.get("impressions") or 0)
            rec["reactions"] += sum(int(row.get(field) or 0) for field in (
                "likes", "reposts", "replies", "quotes"))
            rec["profile_clicks"] += int(row.get("profile_clicks") or 0)
            rec["estimated_follows"] += float(
                row.get("estimated_follower_delta") or 0)
        for rec in out.values():
            rec["average_impressions"] = round(
                rec["impressions"] / rec["samples"], 3)
            rec["average_reactions"] = round(
                rec["reactions"] / rec["samples"], 3)
        return out

    posted = sum(row["status"] in {
        "posted_manually", "result_pending", "result_collected"} for row in queue_rows)
    return {
        "candidates_generated": len(queue_rows),
        "approved": sum(row["status"] == "approved" for row in queue_rows),
        "rejected": sum(row["status"] == "rejected" for row in queue_rows),
        "manually_posted": posted,
        "manual_action_rate": round(posted / max(1, len(queue_rows)), 6),
        "result_samples": len(results),
        "decision": "insufficient_data" if len(results) < minimum else "measured",
        "minimum_sample_size": minimum,
        "by_queue_type": grouped("queue_type"),
        "by_author_type": grouped("author_type"),
        "by_topic_key": grouped("topic_key"),
        "by_posted_hour": grouped("posted_hour_jst"),
        "automated_original_comparison": dict(original) if original else {},
        "x_writes": 0,
    }
