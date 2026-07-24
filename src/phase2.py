"""Conditional low-cost classification and preview-only extensions."""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from api_budget import estimate_openai, finalize, reserve
from metrics_db import connect, db_path, init_db

JST = ZoneInfo("Asia/Tokyo")


def classification_needed(item: dict) -> bool:
    topic = str(item.get("topic_key", ""))
    genre = str(item.get("genre", ""))
    confidence = float(item.get("classification_confidence", 1.0) or 0)
    return (not topic or topic == "未分類" or not genre or genre == "未分類"
            or confidence < 0.65 or bool(item.get("ambiguous_verification")))


def classifier_calls_today(path: Path | None = None) -> int:
    init_db(path)
    day = datetime.now(JST).date().isoformat()
    try:
        with closing(connect(path)) as conn:
            return int(conn.execute("""SELECT COUNT(*) FROM api_usage_events
                WHERE provider='openai' AND operation='classifier' AND timestamp LIKE ? AND success=1""",
                (day + "%",)).fetchone()[0])
    except Exception:
        return 0


def classify_if_needed(item: dict, client_factory=None, path: Path | None = None) -> dict | None:
    if not classification_needed(item):
        return item
    if os.environ.get("OPENAI_CLASSIFIER_ENABLED", "false").lower() not in {"1", "true", "yes"}:
        return None
    limit = int(os.environ.get("OPENAI_CLASSIFIER_MAX_CALLS_PER_DAY", "6"))
    if classifier_calls_today(path) >= limit:
        return None
    model = os.environ.get("OPENAI_MODEL_CLASSIFIER", "gpt-5.4-nano")
    max_output = int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS_CLASSIFIER", "600"))
    estimated = estimate_openai(model, 1200, max_output)
    reservation, reason = reserve(
        "openai", "classifier", model, estimated,
        metadata={"classification_reason": "ambiguous_local_classification"},
        path=path)
    if not reservation:
        return None
    try:
        if client_factory is None:
            from openai import OpenAI
            client_factory = OpenAI
        client = client_factory(api_key=os.environ.get("OPENAI_API_KEY", ""), max_retries=0,
                                timeout=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "90")))
        schema = {"type": "object", "properties": {
            "topic_key": {"type": "string"}, "genre": {"type": "string"},
            "verified": {"type": "boolean"}, "classification_confidence": {"type": "number"}},
            "required": ["topic_key", "genre", "verified", "classification_confidence"],
            "additionalProperties": False}
        response = client.responses.create(
            model=model,
            instructions="Classify only supplied Japanese news metadata. Do not add facts.",
            input=json.dumps({"title": item.get("title", ""), "summary": item.get("summary", ""),
                              "source": item.get("source_name", "")}, ensure_ascii=False),
            max_output_tokens=max_output,
            text={"format": {"type": "json_schema", "name": "news_classification",
                              "strict": True, "schema": schema}}, store=False)
        data = json.loads(response.output_text)
        usage = response.usage
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        details = getattr(usage, "input_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0)
        actual = estimate_openai(model, inp, out, cached) or 0
        finalize(reservation, actual, success=True, input_tokens=inp, cached_tokens=cached, output_tokens=out, path=path)
        enriched = dict(item); enriched.update(data)
        return enriched if data.get("verified", False) else None
    except Exception as exc:
        finalize(reservation, 0, success=False, error_type=type(exc).__name__, path=path)
        return None


def save_extension_previews(post: dict, root: Path) -> list[Path]:
    """Save deterministic ideas only; never call any publishing API."""
    if not any(os.environ.get(name, "true").lower() in {"1", "true", "yes"} for name in (
        "THREAD_PREVIEW_ENABLED", "POLL_PREVIEW_ENABLED", "QUOTE_PREVIEW_ENABLED",
        "REPLY_PREVIEW_ENABLED", "SHORTS_PREVIEW_ENABLED", "NOTE_PREVIEW_ENABLED")):
        return []
    out_dir = root / "outputs" / "previews" / datetime.now(JST).date().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(JST).strftime("%H%M%S")
    topic = post.get("topic_key") or post.get("title", "")
    payload = {
        "created_at": datetime.now(JST).isoformat(), "source_topic": topic,
        "auto_post": False,
        "thread_outline": ["事実", "制度上の争点", "確認すべき一次資料"],
        "poll_preview": {"question": f"{topic}で最優先すべき論点は？", "options": ["透明性", "実効性", "費用", "安全性"]},
        "quote_preview": "一次資料を確認した上で論点を補足する案",
        "reply_preview": "出典と訂正情報がある場合のみ人間が確認して返信する案",
        "shorts_outline": ["15秒の問題提起", "30秒の制度説明", "15秒の結論"],
        "note_outline": ["背景", "一次資料", "制度の争点", "反対論", "結論"],
        "longform_outline": ["概要", "関係者", "制度比較", "影響", "確認事項"],
    }
    path = out_dir / f"{stem}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return [path]
