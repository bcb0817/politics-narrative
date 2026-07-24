"""Asynchronous OpenAI Batch API support for non-urgent bot reviews.

Original post generation deliberately remains synchronous. Batch jobs are limited
to review analysis, are deduplicated in SQLite, and never write to X.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from api_budget import estimate_openai, finalize as finalize_budget, reserve as reserve_budget
from metrics_db import connect, init_db, write
from openai_usage import record_usage


JST = ZoneInfo("Asia/Tokyo")
TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}
ACTIVE_STATUSES = {"validating", "in_progress", "finalizing", "cancelling"}


def _value(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def enabled(task_type: str) -> bool:
    if os.environ.get("OPENAI_BATCH_ENABLED", "false").strip().lower() not in {"1", "true", "yes"}:
        return False
    if task_type == "daily_review" and os.environ.get(
        "DAILY_REVIEW_MODE", "synchronous"
    ).strip().lower() == "synchronous":
        return False
    allowed = {item.strip() for item in os.environ.get(
        "OPENAI_BATCH_TASKS", "weekly_report,quality_eval"
    ).split(",") if item.strip()}
    return task_type in allowed


def _client(client_factory=None):
    if client_factory is None:
        from openai import OpenAI
        client_factory = OpenAI
    return client_factory(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        timeout=max(15.0, float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "90"))),
        max_retries=0,
    )


def _custom_id(task_type: str, payload: dict, dedupe_key: str = "") -> str:
    raw = dedupe_key or json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return f"{task_type}-{digest}"[:64]


def _existing(custom_id: str, db_file: Path) -> dict | None:
    init_db(db_file)
    with closing(connect(db_file)) as conn:
        row = conn.execute(
            "SELECT * FROM openai_batch_jobs WHERE custom_id=?", (custom_id,)
        ).fetchone()
    return dict(row) if row else None


def submit_analysis(
    *, task_type: str, payload: dict, model: str, max_output_tokens: int,
    schema: dict, state_dir: Path, pricing: dict, reasoning_effort: str = "none",
    target_json_path: str = "", metadata: dict | None = None, dedupe_key: str = "",
    client_factory=None,
) -> dict:
    """Upload one Responses request and create a 24-hour OpenAI batch."""
    db_file = state_dir / "bot_metrics.db"
    init_db(db_file)
    custom_id = _custom_id(task_type, payload, dedupe_key)
    previous = _existing(custom_id, db_file)
    if previous:
        analysis = None
        if previous["status"] == "completed" and previous.get("result_json"):
            try:
                analysis = json.loads(previous["result_json"])
            except json.JSONDecodeError:
                pass
        error = "" if previous["status"] not in (TERMINAL_STATUSES - {"completed"}) else (
            "batch_" + previous["status"]
        )
        return {"pending": previous["status"] not in TERMINAL_STATUSES,
                "deduplicated": True, "job": previous, "analysis": analysis, "error": error}

    # Batch pricing is 50% of the same model's synchronous token rates.
    maximum = estimate_openai(model, 6000, max_output_tokens)
    maximum = None if maximum is None else round(maximum * 0.5, 8)
    reservation_id, reason = reserve_budget(
        "openai", task_type, model, maximum, resource_count=1,
        metadata={"processing_mode": "batch", "custom_id": custom_id}, path=db_file,
    )
    if not reservation_id:
        return {"pending": False, "error": reason, "job": None}

    body = {
        "model": model,
        "instructions": (
            "You analyze performance data for a Japanese political-news X bot. "
            "Use only supplied metrics. Separate observations from recommendations. "
            "Do not invent political facts, model details, prices, or credentials."
        ),
        "input": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        "max_output_tokens": int(max_output_tokens),
        "text": {"format": {"type": "json_schema", "name": "performance_analysis",
                             "strict": True, "schema": schema}},
        "store": False,
    }
    if reasoning_effort not in {"none", "off", "false", "minimal"}:
        body["reasoning"] = {"effort": reasoning_effort}
    request = {"custom_id": custom_id, "method": "POST", "url": "/v1/responses", "body": body}
    batch_dir = state_dir / "openai_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    input_path = batch_dir / f"{custom_id}.jsonl"
    input_path.write_text(json.dumps(request, ensure_ascii=False) + "\n", encoding="utf-8")

    try:
        client = _client(client_factory)
        with open(input_path, "rb") as handle:
            uploaded = client.files.create(file=handle, purpose="batch")
        input_file_id = str(_value(uploaded, "id", ""))
        batch = client.batches.create(
            input_file_id=input_file_id,
            endpoint="/v1/responses",
            completion_window="24h",
            metadata={"app": "politics-narrative", "task_type": task_type,
                      "custom_id": custom_id},
        )
        batch_id = str(_value(batch, "id", ""))
        status = str(_value(batch, "status", "validating"))
        now = datetime.now(JST).isoformat()
        job_id = write("""INSERT INTO openai_batch_jobs
          (batch_id,custom_id,task_type,model,status,input_file_id,reservation_id,
           target_json_path,submitted_at,result_json,error_type,metadata_json)
          VALUES (?,?,?,?,?,?,?,?,?,'','',?)""", (
            batch_id, custom_id, task_type, model, status, input_file_id, reservation_id,
            target_json_path, now, json.dumps(metadata or {}, ensure_ascii=False)), db_file)
        if not job_id:
            try:
                client.batches.cancel(batch_id)
            except Exception:
                pass
            raise RuntimeError("batch_job_persistence_failed")
        return {"pending": True, "deduplicated": False, "error": "",
                "job": {"batch_id": batch_id, "custom_id": custom_id, "status": status,
                        "model": model, "reservation_id": reservation_id}}
    except Exception as exc:
        finalize_budget(reservation_id, 0, success=False, error_type=type(exc).__name__, path=db_file)
        return {"pending": False, "error": type(exc).__name__, "job": None}


def _content_text(content: Any) -> str:
    raw = _value(content, "content", None)
    if raw is None and hasattr(content, "read"):
        raw = content.read()
    if raw is None:
        raw = _value(content, "text", "")
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw or "")


def _response_output_text(body: dict) -> str:
    if body.get("output_text"):
        return str(body["output_text"])
    chunks = []
    for output in body.get("output", []) or []:
        for content in output.get("content", []) or []:
            if content.get("type") == "output_text" and content.get("text"):
                chunks.append(str(content["text"]))
    return "".join(chunks)


def _usage(body: dict) -> tuple[int, int, int]:
    usage = body.get("usage") or {}
    inp = int(usage.get("input_tokens", 0) or 0)
    out = int(usage.get("output_tokens", 0) or 0)
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0)
    return inp, max(0, min(cached, inp)), out


def _apply_daily_result(job: dict, analysis: dict, usage_event: dict, db_file: Path) -> None:
    target = Path(job.get("target_json_path") or "")
    if not target.is_file():
        return
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    payload["llm_analysis"] = analysis
    payload["llm_pending"] = False
    payload["llm_error"] = ""
    payload["llm_usage"] = usage_event
    route = payload.setdefault("llm_route", {})
    route["used_model"] = job.get("model", "")
    route["processing_mode"] = "batch"
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    target.write_text(rendered, encoding="utf-8")
    latest = target.parent.parent / "daily_review_latest.json"
    latest.write_text(rendered, encoding="utf-8")
    try:
        metadata = json.loads(job.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    root_dir = Path(metadata.get("root_dir") or target.parent.parent.parent)
    report = root_dir / "reports" / "daily" / f"{target.stem}.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Daily Review {target.stem}", "", f"Reviewed: {payload.get('reviewed_count', 0)} posts",
             "", analysis.get("summary", ""), "", "## Recommendations", ""]
    lines.extend(f"- {item}" for item in analysis.get("recommendations", []))
    report.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    write("""UPDATE daily_reviews SET review_model=?,recommendations_json=?,input_tokens=?,
          output_tokens=?,estimated_cost_usd=? WHERE review_date=?""", (
        job.get("model", ""), json.dumps(analysis.get("recommendations", []), ensure_ascii=False),
        int(usage_event.get("input_tokens", 0)), int(usage_event.get("output_tokens", 0)),
        float(usage_event.get("estimated_cost_usd", 0)), target.stem), db_file)


def _apply_weekly_result(job: dict, analysis: dict, usage_event: dict, db_file: Path) -> None:
    try:
        metadata = json.loads(job.get("metadata_json") or "{}")
    except json.JSONDecodeError:
        metadata = {}
    root_dir = Path(metadata.get("root_dir") or Path(__file__).resolve().parent.parent)
    stamp = metadata.get("report_date") or datetime.now(JST).date().isoformat()
    expansion = {
        "youtube_shorts": analysis.get("recommendations", [])[:3],
        "youtube_long": analysis.get("recommendations", [])[:2],
        "note_articles": analysis.get("recommendations", [])[:3],
        "prompt_improvements": analysis.get("weaknesses", [])[:3],
        "code_improvements_human_approval_required": analysis.get("recommendations", [])[-3:],
    }
    lines = ["# Weekly Report", "", analysis.get("summary", "")]
    for title, key in (("Strengths", "strengths"), ("Weaknesses", "weaknesses"),
                       ("Recommendations", "recommendations"), ("Timing", "timing_findings")):
        lines.extend(["", f"## {title}", ""])
        lines.extend(f"- {item}" for item in analysis.get(key, []))
    text = "\n".join(lines).strip() + "\n"
    out_dir = root_dir / "outputs" / "weekly_reports"
    report_dir = root_dir / "reports" / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stamp}_batch.md").write_text(text, encoding="utf-8")
    (report_dir / f"{stamp}.md").write_text(text, encoding="utf-8")
    week_start = metadata.get("week_start") or stamp
    week_end = metadata.get("week_end") or stamp
    write("""INSERT OR REPLACE INTO weekly_reviews
      (week_start,week_end,generated_at,review_model,summary_json,media_expansion_json,
       recommendations_json,estimated_cost_usd) VALUES (?,?,?,?,?,?,?,?)""", (
        week_start, week_end, datetime.now(JST).isoformat(), job.get("model", ""),
        json.dumps({"summary": analysis.get("summary", "")}, ensure_ascii=False),
        json.dumps(expansion, ensure_ascii=False),
        json.dumps(analysis.get("recommendations", []), ensure_ascii=False),
        float(usage_event.get("estimated_cost_usd", 0))), db_file)


def collect(*, state_dir: Path, pricing: dict, client_factory=None) -> dict:
    """Poll unfinished jobs once, persist completed results, and finalize cost."""
    db_file = state_dir / "bot_metrics.db"
    init_db(db_file)
    with closing(connect(db_file)) as conn:
        jobs = [dict(row) for row in conn.execute(
            "SELECT * FROM openai_batch_jobs WHERE status NOT IN ('completed','failed','expired','cancelled')"
        ).fetchall()]
    if not jobs:
        return {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
    client = _client(client_factory)
    counts = {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
    for job in jobs:
        counts["checked"] += 1
        try:
            batch = client.batches.retrieve(job["batch_id"])
            status = str(_value(batch, "status", job["status"]))
            output_file_id = str(_value(batch, "output_file_id", "") or "")
            error_file_id = str(_value(batch, "error_file_id", "") or "")
            write("UPDATE openai_batch_jobs SET status=?,output_file_id=?,error_file_id=? WHERE id=?",
                  (status, output_file_id, error_file_id, job["id"]), db_file)
            if status not in TERMINAL_STATUSES:
                counts["pending"] += 1
                continue
            if status != "completed" or not output_file_id:
                finalize_budget(job["reservation_id"], 0, success=False, error_type=status, path=db_file)
                write("UPDATE openai_batch_jobs SET completed_at=?,error_type=? WHERE id=?",
                      (datetime.now(JST).isoformat(), status, job["id"]), db_file)
                counts["failed"] += 1
                continue
            text = _content_text(client.files.content(output_file_id))
            match = None
            for line in text.splitlines():
                item = json.loads(line)
                if item.get("custom_id") == job["custom_id"]:
                    match = item
                    break
            response = (match or {}).get("response") or {}
            if int(response.get("status_code", 0) or 0) != 200:
                raise RuntimeError("batch_item_failed")
            body = response.get("body") or {}
            analysis = json.loads(_response_output_text(body))
            inp, cached, out = _usage(body)
            standard_cost = estimate_openai(job["model"], inp, out, cached) or 0.0
            actual_cost = round(standard_cost * 0.5, 8)
            # Preserve the legacy JSON accounting while applying the Batch discount.
            class UsageResponse:
                usage = body.get("usage") or {}
            usage_event = record_usage(
                response=UsageResponse(), model=job["model"], task_type=job["task_type"],
                pricing=pricing, state_path=state_dir / "openai_usage.json",
                history_dir=state_dir / "openai_usage_history", cost_multiplier=0.5,
            )
            finalize_budget(job["reservation_id"], actual_cost, success=True,
                            input_tokens=inp, cached_tokens=cached, output_tokens=out,
                            resource_count=1, path=db_file)
            job["target_json_path"] = job.get("target_json_path", "")
            if job["task_type"] == "daily_review":
                _apply_daily_result(job, analysis, usage_event, db_file)
            elif job["task_type"] == "weekly_report":
                _apply_weekly_result(job, analysis, usage_event, db_file)
            write("""UPDATE openai_batch_jobs SET status='completed',completed_at=?,result_json=?,error_type=''
                     WHERE id=?""", (datetime.now(JST).isoformat(),
                                      json.dumps(analysis, ensure_ascii=False), job["id"]), db_file)
            counts["completed"] += 1
        except Exception as exc:
            # Retrieval/network errors remain retryable; item parsing failures are visible but do not block posts.
            write("""UPDATE openai_batch_jobs SET
                     status=CASE WHEN status='completed' THEN 'result_error' ELSE status END,
                     error_type=? WHERE id=?""", (type(exc).__name__, job["id"]), db_file)
            counts["pending"] += 1
    return counts


def status(state_dir: Path) -> list[dict]:
    db_file = state_dir / "bot_metrics.db"
    init_db(db_file)
    with closing(connect(db_file)) as conn:
        return [dict(row) for row in conn.execute(
            """SELECT custom_id,task_type,model,status,submitted_at,completed_at,error_type
               FROM openai_batch_jobs ORDER BY id DESC LIMIT 50"""
        ).fetchall()]
