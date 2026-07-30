"""Cost-bounded xAI X Search radar. Never treats X posts as verified facts."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from api_budget import (effective_xai_limit, finalize, forecast, reserve,
                        usage_totals)
from metrics_db import (apply_additive_migrations, connect, db_path, init_db,
                        write)
from publishing_policy import normalize_topic_key
from xai_cost import ticks_to_usd

JST = ZoneInfo("Asia/Tokyo")
def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _state_dir() -> Path:
    raw = os.environ.get("STATE_DIR", "data")
    path = Path(raw) if Path(raw).is_absolute() else _root() / raw
    path.mkdir(parents=True, exist_ok=True)
    return path


def _enabled() -> bool:
    return os.environ.get("XAI_ENABLED", "true").lower() in {"1", "true", "yes"}


def local_volatility_score(path: Path | None = None,
                           now_jst: datetime | None = None) -> float:
    """Estimate political volatility using only local, independently sourced data."""
    now_jst = now_jst or datetime.now(JST)
    try:
        with closing(connect(path)) as conn:
            row = conn.execute("""SELECT COUNT(*) candidates,
              COALESCE(SUM(is_major_update),0) major,
              COALESCE(MAX(x_attention_score),0) attention
              FROM news_candidates WHERE fetched_at LIKE ?""",
              (now_jst.date().isoformat() + "%",)).fetchone()
            repeated = conn.execute("""SELECT COALESCE(MAX(c),0) FROM (
              SELECT COUNT(*) c FROM news_candidates WHERE fetched_at LIKE ?
              AND topic_key<>'' GROUP BY topic_key)""",
              (now_jst.date().isoformat() + "%",)).fetchone()[0]
        score = min(10.0, float(row["candidates"] or 0) * .12
                    + float(row["major"] or 0) * 2.0
                    + float(row["attention"] or 0) * .35
                    + max(0, int(repeated or 0) - 1) * .6)
        return round(score, 3)
    except Exception:
        return 5.0


def _runtime_state() -> dict:
    try:
        data = json.loads((_state_dir() / "xai_runtime_state.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_runtime_state(data: dict) -> None:
    (_state_dir() / "xai_runtime_state.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def effective_schedule(now_jst: datetime | None = None,
                       path: Path | None = None) -> set[str]:
    now_jst = now_jst or datetime.now(JST)
    schedule = {
        value.strip() for value in os.environ.get(
            "XAI_SEARCH_SCHEDULE", "06:00,12:00,18:00").split(",") if value.strip()
    }
    schedule = set(sorted(schedule)[:max(1, int(os.environ.get("XAI_MAX_CALLS_PER_DAY", "3")))])
    monthly_xai = float(usage_totals(path, now_jst).get("xai", 0) or 0)
    projected_xai = float(forecast(path, now_jst).get("projected", {}).get("xai", 0) or 0)
    if forecast(path, now_jst).get("restriction_level", 0) >= 5:
        reduced = {"06:00", "18:00"} & schedule
        return reduced or set(sorted(schedule)[:2])
    if max(monthly_xai, projected_xai) > 1.80:
        return {"06:00", "18:00"} & schedule or set(sorted(schedule)[::max(1, len(schedule)-1)])
    adaptive = os.environ.get("XAI_ADAPTIVE_SCHEDULE_ENABLED", "true").lower() in {
        "1", "true", "yes"
    }
    low_threshold = float(os.environ.get("XAI_VOLATILITY_THRESHOLD_LOW", "3.0"))
    if adaptive and local_volatility_score(path, now_jst) < low_threshold:
        reduced = {"06:00", "18:00"} & schedule
        return reduced or {min(schedule), max(schedule)}
    return schedule


def should_run(now_jst: datetime | None = None, path: Path | None = None) -> bool:
    now_jst = now_jst or datetime.now(JST)
    return now_jst.strftime("%H:%M") in effective_schedule(now_jst, path)


def load_cache(now: datetime | None = None, allow_expired: bool = False) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    try:
        payload = json.loads((_state_dir() / "xai_search_latest.json").read_text(encoding="utf-8"))
        expires = datetime.fromisoformat(payload.get("expires_at", ""))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if allow_expired or now <= expires:
            return payload.get("topics", []) if isinstance(payload.get("topics"), list) else []
    except Exception:
        pass
    return []


def _runs_today(path: Path | None = None) -> int:
    init_db(path)
    try:
        with closing(connect(path)) as conn:
            return int(conn.execute("""SELECT COUNT(*) FROM xai_usage_events
              WHERE operation='x_search_radar' AND timestamp LIKE ?""",
              (datetime.now(JST).date().isoformat() + "%",)).fetchone()[0])
    except Exception:
        return 0


def _already_ran(now_jst: datetime) -> bool:
    try:
        payload = json.loads((_state_dir() / "xai_search_latest.json").read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(payload["generated_at"]).astimezone(JST)
        return generated.strftime("%Y-%m-%d %H:%M") == now_jst.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return False


def _schema(max_topics: int, max_posts: int) -> dict:
    topic = {
        "type": "object",
        "properties": {
            "topic_key": {"type": "string"},
            "attention_score": {"type": "number", "minimum": 0, "maximum": 10},
            "velocity_score": {"type": "number", "minimum": 0, "maximum": 10},
            "main_claims": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "counter_claims": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "representative_post_ids": {
                "type": "array", "items": {"type": "string"}, "maxItems": max_posts
            },
            "verification_required": {"type": "boolean"},
        },
        "required": ["topic_key", "attention_score", "velocity_score", "main_claims",
                     "counter_claims", "representative_post_ids",
                     "verification_required"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {
        "generated_at": {"type": "string"},
        "topics": {"type": "array", "items": topic, "maxItems": max_topics},
    }, "required": ["generated_at", "topics"], "additionalProperties": False}


def _sanitize(payload: dict, max_topics: int, max_posts: int) -> dict:
    clean = {"generated_at": datetime.now(timezone.utc).isoformat(), "topics": []}
    for row in (payload.get("topics") or [])[:max_topics]:
        topic = {
            "topic_key": str(row.get("topic_key", ""))[:100],
            "attention_score": max(0, min(10, float(row.get("attention_score", 0) or 0))),
            "velocity_score": max(0, min(10, float(row.get("velocity_score", 0) or 0))),
            "main_claims": [str(v)[:160] for v in (row.get("main_claims") or [])[:5]],
            "counter_claims": [str(v)[:160] for v in (row.get("counter_claims") or [])[:5]],
            "representative_post_ids": [
                str(v)[:30] for v in (row.get("representative_post_ids") or [])[:max_posts]
            ],
            "verification_required": True,
        }
        if topic["topic_key"]:
            clean["topics"].append(topic)
    return clean


def _compact_candidate_input(candidates: list[dict] | None,
                             max_topics: int) -> list[dict]:
    """Reduce local RSS/official candidates to bounded metadata only."""
    compact = []
    seen = set()
    for item in candidates or []:
        title = str(item.get("title", "") or "").strip()
        if not title:
            continue
        key = str(item.get("topic_key", "") or "").strip() or normalize_topic_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        summary = str(item.get("summary", "") or "").strip()
        compact.append({
            "topic_key": key[:100],
            "summary": summary[:180] or title[:180],
            "source_type": str(item.get("source_type", "rss") or "rss")[:30],
        })
        if len(compact) >= max_topics:
            break
    return compact


def _record_usage(model: str, ticks: int, input_tokens: int, output_tokens: int,
                  tool_calls: int, success: bool, error_type: str = "",
                  schedule_slot: str = "", cache_used: bool = False,
                  path: Path | None = None, request_id: str = "",
                  cached_input_tokens: int = 0, successful_tool_calls: int | None = None,
                  estimated_cost_usd: float = 0.0) -> str:
    apply_additive_migrations(path)
    now = datetime.now(JST).isoformat()
    request_id = request_id or f"xai-{uuid.uuid4()}"
    actual_cost = ticks_to_usd(ticks)
    cost_source = "actual" if ticks > 0 else "estimated"
    successful_tool_calls = (
        tool_calls if success else 0
        if successful_tool_calls is None else successful_tool_calls
    )
    metadata = {"cost_in_usd_ticks": ticks, "tool_call_count": tool_calls,
                "schedule_slot": schedule_slot, "cache_used": cache_used}
    write("""INSERT OR IGNORE INTO xai_usage_events
      (request_id,timestamp,model,operation,schedule_slot,input_tokens,output_tokens,
       cached_input_tokens,tool_call_count,successful_tool_call_count,cost_in_usd_ticks,
       actual_cost_usd,estimated_cost_usd,cost_source,cache_used,success,error_type,metadata_json)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
      request_id, now, model, "x_search_radar", schedule_slot, input_tokens, output_tokens,
      cached_input_tokens, tool_calls, successful_tool_calls, ticks, actual_cost,
      estimated_cost_usd, cost_source, int(cache_used), int(success), error_type,
      json.dumps(metadata, ensure_ascii=False)), path)
    history = _state_dir() / "xai_usage_history.jsonl"
    with open(history, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"request_id": request_id, "timestamp": now,
          "provider": "xai", "model": model,
          "operation": "x_search_radar", "input_tokens": input_tokens,
          "output_tokens": output_tokens, "cached_input_tokens": cached_input_tokens,
          "tool_call_count": tool_calls,
          "successful_tool_call_count": successful_tool_calls,
          "cost_in_usd_ticks": ticks, "actual_cost_usd": actual_cost,
          "estimated_cost_usd": estimated_cost_usd, "cost_source": cost_source,
          "schedule_slot": schedule_slot, "cache_used": cache_used,
          "success": success, "error_type": error_type}, ensure_ascii=False) + "\n")
    return request_id


def _tool_call_count(response) -> int:
    """Count X Search calls exposed by the Responses-compatible API."""
    usage = getattr(response, "usage", None)
    details = getattr(usage, "server_side_tool_usage_details", None)
    if isinstance(details, dict):
        successful = int(details.get("x_search_calls", 0) or 0)
    else:
        successful = int(getattr(details, "x_search_calls", 0) or 0)
    if successful:
        return successful

    def is_x_search_item(item) -> bool:
        item_type = getattr(item, "type", "")
        name = getattr(item, "name", "")
        return item_type == "x_search_call" or (
            item_type == "custom_tool_call"
            and name in {"x_search", "x_semantic_search", "x_keyword_search",
                         "x_user_search", "x_thread_fetch"}
        )

    output_count = sum(1 for item in (getattr(response, "output", None) or [])
                       if is_x_search_item(item))
    # xAI's OpenAI-compatible response currently exposes successful hosted-tool
    # usage here even when the output item is represented as custom_tool_call.
    usage_count = int(getattr(getattr(response, "usage", None),
                              "num_server_side_tools_used", 0) or 0)
    return max(output_count, usage_count)


def _save_cache(payload: dict, now_jst: datetime, *,
                notify_discord: bool = False) -> list[dict]:
    ttl = int(os.environ.get("XAI_SEARCH_CACHE_TTL_MINUTES", "360"))
    payload["expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=ttl)).isoformat()
    payload["provider"] = "xai"
    latest = _state_dir() / "xai_search_latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history_dir = _state_dir() / "xai_search_history"
    history_dir.mkdir(parents=True, exist_ok=True)
    with open(history_dir / f"{now_jst.date().isoformat()}.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    if notify_discord and os.environ.get(
        "X_DISCORD_RESEARCH_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from discord_notify import notify_x_research
            topics = payload.get("topics") or []
            notify_x_research({
                "provider": "xAI X Search",
                "lookback_minutes": int(os.environ.get(
                    "XAI_SEARCH_LOOKBACK_MINUTES", "360")),
                "query_count": 1,
                "resource_count": 1,
                "topic_count": len(topics),
                "queries": [
                    str(topic.get("topic_key") or "")
                    for topic in topics[:5]
                ],
                "topics": [{
                    "topic_key": topic.get("topic_key"),
                    "attention_score": topic.get("attention_score"),
                    "velocity_score": topic.get("velocity_score"),
                    "main_claims": topic.get("main_claims") or [],
                    "counter_claims": topic.get("counter_claims") or [],
                    "representative_post_ids": (
                        topic.get("representative_post_ids") or []),
                    "externally_corroborated": bool(
                        topic.get("externally_corroborated")),
                } for topic in topics[:5]],
                "corroborated_topic_count": sum(
                    bool(topic.get("externally_corroborated"))
                    for topic in topics),
            })
        except Exception:
            pass
    return payload["topics"]


def search(now_jst: datetime | None = None, client_factory=None,
           path: Path | None = None, candidates: list[dict] | None = None,
           notify_discord: bool = False) -> list[dict]:
    """Run one xAI request at configured slots; failures degrade to cache/RSS."""
    now_jst = now_jst or datetime.now(JST)
    path = path or db_path()
    if not _enabled() or os.environ.get("X_TOPIC_DISCOVERY_PROVIDER", "xai") != "xai":
        return []
    if not should_run(now_jst, path) or _already_ran(now_jst):
        return load_cache()
    from audit_tools import guard_provider_execution
    if not guard_provider_execution("xai", now_jst):
        print("Discovery provider conflict: native_x already executed in this slot")
        return load_cache()
    runtime = _runtime_state()
    if runtime.get("paused_date") == now_jst.date().isoformat():
        return load_cache()
    if forecast(path).get("pause_x_search"):
        print("xAI X Search paused: projected monthly budget reached the 93% restriction stage")
        return load_cache()
    actual_xai = float(usage_totals(path, now_jst).get("xai", 0) or 0)
    if actual_xai >= effective_xai_limit():
        print("xAI X Search stopped: monthly budget reached")
        return load_cache()
    daily_cap = min(
        int(os.environ.get("XAI_SEARCH_MAX_CALLS_PER_DAY", "3")),
        int(os.environ.get("XAI_MAX_CALLS_PER_DAY", "3")),
    )
    if _runs_today(path) >= daily_cap:
        return load_cache()
    model = os.environ.get("XAI_MODEL", "grok-4.5")
    max_topics = int(os.environ.get("XAI_SEARCH_MAX_TOPICS_PER_RUN", "5"))
    max_posts = int(os.environ.get("XAI_SEARCH_MAX_REPRESENTATIVE_POSTS_PER_TOPIC", "3"))
    maximum_cost = float(os.environ.get(
        "XAI_COST_PER_RUN_HARD_LIMIT_USD",
        os.environ.get("XAI_MAX_COST_PER_CALL_USD", "0.020")))
    max_attempts = 1
    compact_candidates = _compact_candidate_input(candidates, max_topics)
    try:
        if client_factory is None:
            from openai import OpenAI
            client_factory = OpenAI
        key = os.environ.get("XAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError("xai_api_key_missing")
        client = client_factory(api_key=key, base_url="https://api.x.ai/v1",
                                timeout=float(os.environ.get("XAI_TIMEOUT_SECONDS", "120")), max_retries=0)
        last_payload = None
        for attempt in range(max_attempts):
            if _runs_today(path) >= daily_cap:
                break
            reservation, reason = reserve("xai", "x_search_radar", model, maximum_cost, 1,
                                          metadata={"attempt": attempt + 1}, path=path)
            if not reservation:
                if "budget" in reason:
                    print("xAI budget reached -> X radar disabled")
                    print("Continuing with RSS and official sources")
                break
            try:
                retry_note = (" This is a retry: you must execute X Search and return any genuinely "
                              "observed topics; do not answer from model memory.") if attempt else ""
                response = client.responses.create(
                    model=model,
                    instructions=("Use X Search once and answer in Japanese JSON. Compare only the "
                                  "provided topic candidates with current X attention. Representative "
                                  "accounts may be official institutions, politicians, journalists, "
                                  "researchers, or media. X is not a factual authority, so "
                                  "verification_required must be true. Do not generate post text, "
                                  "quote post bodies, or target private people."),
                    input=json.dumps({
                        "lookback_minutes": int(os.environ.get(
                            "XAI_SEARCH_LOOKBACK_MINUTES", "360")),
                        "language": "ja",
                        "candidate_topics": compact_candidates,
                        "requested_account_categories": [
                            "official", "politician", "journalist",
                            "researcher", "media",
                        ],
                        "request": (
                            "Return attention, velocity, short opposing claims, and "
                            f"representative post IDs for at most {max_topics} topics."
                            f"{retry_note}"
                        ),
                    }, ensure_ascii=False, separators=(",", ":")),
                    tools=[{"type": "x_search"}], tool_choice="required",
                    max_tool_calls=int(os.environ.get(
                        "XAI_MAX_TOOL_CALLS_PER_REQUEST", "1")),
                    parallel_tool_calls=False,
                    extra_body={"max_turns": 1},
                    text={"format": {"type": "json_schema", "name": "x_radar_topics",
                                      "strict": True, "schema": _schema(max_topics, max_posts)}},
                    store=False,
                )
                payload = _sanitize(json.loads(response.output_text), max_topics, max_posts)
                candidate_keys = {
                    str(row.get("topic_key") or "").lower()
                    for row in compact_candidates
                    if row.get("topic_key")
                }
                for topic in payload["topics"]:
                    key = str(topic.get("topic_key") or "").lower()
                    topic["externally_corroborated"] = any(
                        key == candidate
                        or key in candidate
                        or candidate in key
                        for candidate in candidate_keys
                    )
                usage = response.usage
                ticks = int(getattr(usage, "cost_in_usd_ticks", 0) or 0)
                inp = int(getattr(usage, "input_tokens", 0) or 0)
                out = int(getattr(usage, "output_tokens", 0) or 0)
                input_details = getattr(usage, "input_tokens_details", None)
                cached = int(
                    (input_details.get("cached_tokens", 0) if isinstance(input_details, dict)
                     else getattr(input_details, "cached_tokens", 0)) or 0
                )
                tool_calls = _tool_call_count(response)
                semantic_success = tool_calls == 1
                error_type = ("" if semantic_success else
                              "x_search_not_called" if tool_calls == 0 else "x_search_call_cap_exceeded")
                finalize(reservation, ticks_to_usd(ticks), success=semantic_success,
                         input_tokens=inp, output_tokens=out, resource_count=tool_calls,
                         error_type=error_type, path=path)
                _record_usage(
                    model, ticks, inp, out, tool_calls, semantic_success, error_type,
                    now_jst.strftime("%H:%M"), False, path,
                    request_id=str(getattr(response, "id", "") or ""),
                    cached_input_tokens=cached,
                    successful_tool_calls=tool_calls if semantic_success else 0,
                    estimated_cost_usd=maximum_cost,
                )
                actual_cost = ticks_to_usd(ticks)
                warning = float(os.environ.get("XAI_COST_PER_RUN_WARNING_USD", "0.012"))
                hard_limit = float(os.environ.get("XAI_COST_PER_RUN_HARD_LIMIT_USD", "0.020"))
                if actual_cost > warning:
                    print("xAI cost warning: request exceeded configured target")
                if actual_cost > hard_limit:
                    _save_runtime_state({
                        "paused_date": now_jst.date().isoformat(),
                        "reason": "per_run_hard_limit",
                        "actual_cost_usd": actual_cost,
                        "updated_at": datetime.now(JST).isoformat(),
                    })
                    print("xAI X Search paused for the rest of today: per-run hard limit exceeded")
                if tool_calls > 1:
                    print(f"xAI X Search call cap exceeded ({tool_calls}); stopping retries")
                    break
                if not semantic_success:
                    print("xAI response did not execute X Search; retrying within configured caps")
                    continue
                last_payload = payload
                if payload["topics"]:
                    return _save_cache(
                        payload, now_jst,
                        notify_discord=notify_discord)
                if attempt + 1 < max_attempts:
                    print("xAI X Search returned no topics; retrying once")
            except Exception as exc:
                finalize(reservation, 0, success=False, error_type=type(exc).__name__,
                         resource_count=0, path=path)
                _record_usage(model, 0, 0, 0, 0, False, type(exc).__name__,
                              now_jst.strftime("%H:%M"), False, path,
                              estimated_cost_usd=0.0)
                raise
        if last_payload is not None:
            return _save_cache(
                last_payload, now_jst,
                notify_discord=notify_discord)
        return load_cache()
    except Exception as exc:
        print(f"xAI X Search unavailable -> continuing with RSS and official sources ({type(exc).__name__})")
        return load_cache()


def apply_verified_attention(items: list[dict], topics: list[dict]) -> list[dict]:
    """Attach xAI attention only to independently sourced RSS/official candidates."""
    latest_cost = 0.0
    discovered_at = ""
    try:
        apply_additive_migrations()
        with closing(connect()) as conn:
            event = conn.execute("""SELECT timestamp,actual_cost_usd,estimated_cost_usd,
                cost_source FROM xai_usage_events WHERE operation='x_search_radar'
                AND success=1 ORDER BY id DESC LIMIT 1""").fetchone()
        if event:
            discovered_at = event["timestamp"] or ""
            latest_cost = float(
                event["actual_cost_usd"] if event["cost_source"] == "actual"
                else event["estimated_cost_usd"] or 0
            )
    except Exception:
        pass
    matches: list[tuple[dict, dict]] = []
    for item in items:
        haystack = f"{item.get('title','')} {item.get('summary','')}".lower()
        best = None
        for topic in topics:
            key = str(topic.get("topic_key", "")).lower()
            tokens = [token for token in key.replace("・", " ").split() if len(token) >= 2]
            if key and (key in haystack or any(token in haystack for token in tokens)):
                if best is None or topic.get("attention_score", 0) > best.get("attention_score", 0):
                    best = topic
        if best:
            matches.append((item, best))
    allocated = latest_cost / len(matches) if matches else 0.0
    for item, best in matches:
            item["discovered_via"] = list(dict.fromkeys(
                (item.get("discovered_via") or []) + ["xai"]))
            item["x_attention_score"] = float(best.get("attention_score", 0) or 0)
            item["x_velocity_score"] = float(best.get("velocity_score", 0) or 0)
            item["xai_topic_match"] = True
            item["xai_attention_score"] = item["x_attention_score"]
            item["xai_velocity_score"] = item["x_velocity_score"]
            item["xai_discovered_at"] = discovered_at
            item["xai_cost_allocated_usd"] = round(allocated, 10)
            item["xai_topic_metadata"] = {k: best.get(k) for k in (
                "topic_key", "main_claims", "counter_claims",
                "representative_post_ids")}
            item["verified"] = True  # verified by the existing RSS/official item, not by xAI.
    return items
