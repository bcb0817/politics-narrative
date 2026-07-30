"""Official Meta Threads API integration with a preview-first rollout.

This module never automates a browser, changes a profile, replies, quotes,
reposts, likes, follows, or publishes while THREADS_POST_ENABLED is false.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import requests

from api_budget import finalize, reserve
from metrics_db import apply_additive_migrations, connect, db_path, write
from openai_usage import calculate_cost, load_pricing, usage_from_response
from social_anger import (
    evaluate_production_candidate as evaluate_social_anger_candidate,
    production_prompt_context as social_anger_prompt_context,
)


JST = ZoneInfo("Asia/Tokyo")
PROMPT_VERSION = "threads-public-accountability-v3"
POST_TYPES = {
    "conversation_explainer", "issue_question", "steelman_comparison",
    "policy_context", "evergreen_explainer", "daily_digest",
}
POST_STATES = {
    "generated", "container_created", "publish_pending", "published",
    "failed", "ambiguous",
}
INITIAL_SCOPES = (
    "threads_basic", "threads_content_publish", "threads_manage_insights",
)
KNOWN_SCOPES = (
    "threads_basic", "threads_content_publish", "threads_manage_insights",
    "threads_read_replies", "threads_manage_replies",
    "threads_keyword_search", "threads_manage_mentions", "threads_delete",
    "threads_location_tagging", "threads_profile_discovery",
)
SCOPE_PROFILES = {
    "basic": INITIAL_SCOPES,
    "full-analysis": (
        "threads_basic", "threads_content_publish", "threads_manage_insights",
        "threads_read_replies", "threads_manage_replies",
        "threads_keyword_search", "threads_manage_mentions", "threads_delete",
        "threads_location_tagging", "threads_profile_discovery",
    ),
}
INTERNAL_TERMS = (
    "post_type", "hook_type", "decision_reason", "quality_score",
    "safety_score", "similarity_to_x", "prompt_version", "system_prompt",
    "モデル名", "JSONキー", "AIとして",
)
HIGH_RISK_TERMS = (
    "死亡", "死去", "逮捕", "犯罪者", "病気", "開戦", "投票日",
    "投票方法", "民族", "国籍", "宗教",
)
ATTACK_TERMS = (
    "売国奴", "非国民", "消えろ", "死ね", "無能な人間", "犯罪者だ",
)
WINDOW_HOURS = {"15m": .25, "1h": 1, "6h": 6, "24h": 24, "72h": 72}
_CIRCUIT = {"failures": 0, "opened_at": None}


def _root() -> Path:
    return Path(__file__).resolve().parent.parent


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _now() -> datetime:
    return datetime.now(JST)


def _state_dir() -> Path:
    raw = os.environ.get("STATE_DIR", "data")
    path = Path(raw)
    path = path if path.is_absolute() else _root() / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fallback_path(name: str) -> Path:
    path = _state_dir() / "threads" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _append_fallback(name: str, payload: dict) -> None:
    safe = {
        key: value for key, value in payload.items()
        if key not in {"access_token", "app_secret", "code"}
    }
    try:
        with open(_fallback_path(name), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False) + "\n")
    except OSError:
        pass


def settings() -> dict:
    scopes = tuple(
        value.strip() for value in os.environ.get(
            "THREADS_OAUTH_SCOPES", ",".join(INITIAL_SCOPES)).split(",")
        if value.strip()
    )
    allowed_scopes = tuple(
        scope for scope in scopes if scope in KNOWN_SCOPES)
    reply_control = os.environ.get(
        "THREADS_REPLY_CONTROL", "everyone").strip()
    if reply_control not in {
        "everyone", "accounts_you_follow", "mentioned_only",
        "parent_post_author_only", "followers_only",
    }:
        reply_control = "everyone"
    platform_limit = min(
        500, max(1, _int("THREADS_PLATFORM_LIMIT_CHARS", 500)))
    return {
        "enabled": _bool("THREADS_ENABLED", "true"),
        "post_enabled": _bool("THREADS_POST_ENABLED", "false"),
        "insights_enabled": _bool("THREADS_INSIGHTS_ENABLED", "true"),
        "app_id": os.environ.get("THREADS_APP_ID", "").strip(),
        "app_secret": os.environ.get("THREADS_APP_SECRET", "").strip(),
        "redirect_uri": os.environ.get("THREADS_REDIRECT_URI", "").strip(),
        "public_base_url": os.environ.get(
            "THREADS_PUBLIC_BASE_URL", "").strip().rstrip("/"),
        "access_token": os.environ.get("THREADS_ACCESS_TOKEN", "").strip(),
        "user_id": os.environ.get("THREADS_USER_ID", "").strip(),
        "username": os.environ.get("THREADS_USERNAME", "").strip(),
        "expires_at": os.environ.get(
            "THREADS_TOKEN_EXPIRES_AT", "").strip(),
        "scopes": allowed_scopes,
        "base_url": os.environ.get(
            "THREADS_API_BASE_URL", "https://graph.threads.net").rstrip("/"),
        "api_version": os.environ.get(
            "THREADS_API_VERSION", "v1.0").strip("/"),
        "timeout": max(1, _int("THREADS_API_TIMEOUT_SECONDS", 30)),
        "max_retries": max(0, min(5, _int(
            "THREADS_API_MAX_RETRIES", 2))),
        "retry_base_seconds": max(
            0.0, _float("THREADS_API_RETRY_BASE_SECONDS", 2.0)),
        "circuit_breaker_enabled": _bool(
            "THREADS_API_CIRCUIT_BREAKER_ENABLED", "true"),
        "circuit_breaker_failures": max(
            1, _int("THREADS_API_CIRCUIT_BREAKER_FAILURES", 5)),
        "circuit_breaker_cooldown_minutes": max(
            1, _int("THREADS_API_CIRCUIT_BREAKER_COOLDOWN_MINUTES", 30)),
        "oauth_state_ttl_seconds": max(
            60, min(1800, _int("THREADS_OAUTH_STATE_TTL_SECONDS", 600))),
        "daily_min": max(0, _int("THREADS_DAILY_POST_MIN", 2)),
        "daily_max": max(1, min(3, _int("THREADS_DAILY_POST_MAX", 3))),
        "schedule": tuple(
            value.strip() for value in os.environ.get(
                "THREADS_POST_SCHEDULE", "08:30,13:00,20:30").split(",")
            if re.fullmatch(r"\d{2}:\d{2}", value.strip())
        ),
        "min_interval_minutes": max(
            0, _int("THREADS_MIN_POST_INTERVAL_MINUTES", 180)),
        "topic_cooldown_hours": max(
            0, _int("THREADS_TOPIC_COOLDOWN_HOURS", 8)),
        "reply_control": reply_control,
        "model": os.environ.get(
            "THREADS_MODEL", "gpt-5.4-mini").strip(),
        "fallback_model": os.environ.get(
            "THREADS_MODEL_FALLBACK", "gpt-5-mini").strip(),
        "reasoning_effort": os.environ.get(
            "THREADS_REASONING_EFFORT", "low").strip(),
        "max_output_tokens": max(
            100, _int("THREADS_MAX_OUTPUT_TOKENS", 900)),
        "target_min_chars": max(
            1, _int("THREADS_TARGET_MIN_CHARS", 180)),
        "target_max_chars": min(
            platform_limit, _int("THREADS_TARGET_MAX_CHARS", 450)),
        "platform_limit": platform_limit,
        "max_similarity": max(0.0, min(
            1.0, _float("THREADS_MAX_TEXT_SIMILARITY_TO_X", .80))),
        "min_delay_after_x_minutes": max(
            0, _int("THREADS_MIN_DELAY_AFTER_X_MINUTES", 30)),
        "style_mode": os.environ.get(
            "THREADS_STYLE_MODE", "conversation").strip(),
        "question_ending_target_ratio": max(0.0, min(
            1.0, _float("THREADS_QUESTION_ENDING_TARGET_RATIO", .40))),
        "emoji_target_ratio": max(0.0, min(
            1.0, _float("THREADS_EMOJI_TARGET_RATIO", .15))),
        "emoji_max_per_post": max(
            0, _int("THREADS_EMOJI_MAX_PER_POST", 1)),
        "hashtag_max_per_post": max(
            0, _int("THREADS_HASHTAG_MAX_PER_POST", 1)),
        "reuse_verified_x_topics": _bool(
            "THREADS_REUSE_VERIFIED_X_TOPICS", "true"),
        "copy_x_text": False,
        "auto_publish_text": False,
        "auto_reply_enabled": False,
        "auto_quote_enabled": False,
        "auto_repost_enabled": False,
        "auto_follow_enabled": False,
        "auto_like_enabled": False,
        "image_post_enabled": False,
        "token_auto_refresh": _bool(
            "THREADS_TOKEN_AUTO_REFRESH_ENABLED", "true"),
        "refresh_before_days": max(
            0, _int("THREADS_TOKEN_REFRESH_BEFORE_EXPIRY_DAYS", 7)),
        "metrics_enabled": _bool("THREADS_METRICS_ENABLED", "true"),
        "metrics_windows": tuple(dict.fromkeys([
            "15m", "1h", "6h",
            *[
                value.strip() for value in os.environ.get(
                    "THREADS_METRICS_WINDOWS", "1h,24h,72h").split(",")
                if value.strip() in WINDOW_HOURS
            ],
            "24h", "72h",
        ])),
        "monthly_openai_budget": max(
            0.0, _float("THREADS_OPENAI_MONTHLY_BUDGET_USD", 1.0)),
        "max_cost_per_post": max(
            0.0, _float("THREADS_OPENAI_MAX_COST_PER_POST_USD", .03)),
    }


def _env_path() -> Path:
    return _root() / ".env"


def _update_env(values: dict[str, str]) -> None:
    path = _env_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(values)
    output = []
    for line in lines:
        match = re.match(r"^([A-Z0-9_]+)=", line)
        key = match.group(1) if match else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.replace(temporary, path)
    for key, value in values.items():
        os.environ[key] = value


def _parse_datetime(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)
    except (TypeError, ValueError):
        return None


def _token_event(event_type: str, success: bool, expires_at: str = "",
                 error_type: str = "", metadata: dict | None = None,
                 path: Path | None = None) -> None:
    safe_metadata = {
        key: value for key, value in (metadata or {}).items()
        if key not in {"access_token", "app_secret", "code"}
    }
    try:
        apply_additive_migrations(path)
        write("""INSERT INTO threads_token_events
          (event_at,event_type,expires_at,success,error_type,metadata_json)
          VALUES (?,?,?,?,?,?)""", (
            _now().isoformat(), event_type, expires_at, int(success),
            error_type, json.dumps(safe_metadata, ensure_ascii=False),
        ), path)
    except Exception:
        _append_fallback("token_events.jsonl", {
            "event_at": _now().isoformat(), "event_type": event_type,
            "expires_at": expires_at, "success": success,
            "error_type": error_type, "metadata": safe_metadata,
        })


def _record_threads_call(endpoint: str, success: bool,
                         error_type: str = "",
                         path: Path | None = None) -> None:
    """Record call counts only; Meta pricing is not estimated here."""
    try:
        apply_additive_migrations(path)
        write("""INSERT INTO api_usage_events
          (timestamp,provider,operation,model_or_endpoint,resource_count,
           estimated_cost_usd,success,fallback_used,error_type,metadata_json,
           task_type_source)
          VALUES (?,'threads','threads_api_call',?,1,0,?,0,?,'{}','explicit')""",
              (_now().isoformat(), endpoint, int(success), error_type), path)
    except Exception:
        _append_fallback("api_calls.jsonl", {
            "timestamp": _now().isoformat(), "endpoint": endpoint,
            "success": success, "error_type": error_type,
        })


def _hash_payload(payload: Any) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _error_class(status: int, payload: dict | None = None) -> str:
    message = json.dumps(payload or {}, ensure_ascii=False).lower()
    if status == 429:
        return "rate_limited"
    if status in {401} or any(
        value in message for value in ("expired", "invalid oauth", "token")
    ):
        return "token_invalid"
    if status in {403} or any(
        value in message for value in ("permission", "scope", "not authorized")
    ):
        return "permission_denied"
    if 400 <= status < 500:
        return "client_error"
    if status >= 500:
        return "server_error"
    return ""


class ThreadsClient:
    def __init__(self, session=None, path: Path | None = None):
        self.session = session or requests.Session()
        self.path = path

    def _record_call(self, *, request_id: str, method: str, endpoint: str,
                     status_code: int, success: bool, duration_ms: int,
                     retry_count: int, error_class: str,
                     payload: dict | None = None) -> None:
        _record_threads_call(
            endpoint, success, error_class, self.path)
        try:
            from metrics_db import apply_threads_full_migrations
            apply_threads_full_migrations(self.path)
            now = _now().isoformat()
            write("""INSERT INTO threads_api_calls
              (request_id,called_at,method,endpoint,status_code,success,
               duration_ms,retry_count,error_class,permission_error,
               token_error,rate_limited,created_at,updated_at,source,
               api_version,raw_response_hash)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                request_id, now, method.upper(), endpoint, status_code,
                int(success), duration_ms, retry_count, error_class,
                int(error_class == "permission_denied"),
                int(error_class == "token_invalid"),
                int(error_class == "rate_limited"), now, now,
                "meta_official_api", settings()["api_version"],
                _hash_payload(payload or {}),
            ), self.path)
        except Exception:
            pass

    @staticmethod
    def _circuit_open(cfg: dict) -> bool:
        if not cfg["circuit_breaker_enabled"] or not _CIRCUIT["opened_at"]:
            return False
        opened = _CIRCUIT["opened_at"]
        elapsed = (_now() - opened).total_seconds()
        if elapsed >= cfg["circuit_breaker_cooldown_minutes"] * 60:
            _CIRCUIT.update({"failures": 0, "opened_at": None})
            return False
        return True

    def _request(self, method: str, path: str, *, token: str = "",
                 params: dict | None = None, data: dict | None = None,
                 json_body: dict | None = None) -> dict:
        cfg = settings()
        params = dict(params or {})
        data = dict(data or {})
        headers = {
            "Accept": "application/json",
            "X-Client-Request-ID": str(uuid.uuid4()),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{cfg['base_url']}/{path.lstrip('/')}"
        if self._circuit_open(cfg):
            raise RuntimeError("threads_api_circuit_open")
        attempts = 1 + (
            cfg["max_retries"] if method.upper() == "GET" else 0)
        request_id = headers["X-Client-Request-ID"]
        for attempt in range(attempts):
            started = time.monotonic()
            status = 0
            payload: dict = {}
            try:
                response = self.session.request(
                    method, url, params=params or None, data=data or None,
                    json=json_body, headers=headers, timeout=cfg["timeout"],
                )
                raw_status = getattr(response, "status_code", 200)
                status = raw_status if isinstance(raw_status, int) else 200
                try:
                    candidate = response.json()
                    payload = candidate if isinstance(candidate, dict) else {}
                except (ValueError, TypeError):
                    payload = {
                        "_non_json_response": True,
                        "_content_hash": hashlib.sha256(
                            bytes(getattr(response, "content", b""))
                        ).hexdigest(),
                    }
                response.raise_for_status()
                if payload.get("_non_json_response"):
                    raise RuntimeError("threads_api_invalid_response")
                _CIRCUIT.update({"failures": 0, "opened_at": None})
                self._record_call(
                    request_id=request_id, method=method, endpoint=path,
                    status_code=status, success=True,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    retry_count=attempt, error_class="", payload=payload)
                return payload
            except Exception as exc:
                response = getattr(exc, "response", None)
                if response is not None:
                    raw_status = getattr(response, "status_code", status)
                    status = raw_status if isinstance(raw_status, int) else status
                classification = _error_class(status, payload)
                retryable = (
                    method.upper() == "GET"
                    and attempt + 1 < attempts
                    and (
                        isinstance(exc, (
                            requests.Timeout, requests.ConnectionError))
                        or status == 429 or status >= 500
                    )
                )
                error_name = classification or type(exc).__name__
                self._record_call(
                    request_id=request_id, method=method, endpoint=path,
                    status_code=status, success=False,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    retry_count=attempt, error_class=error_name,
                    payload=payload)
                if not retryable:
                    _CIRCUIT["failures"] += 1
                    if (
                        cfg["circuit_breaker_enabled"]
                        and _CIRCUIT["failures"]
                        >= cfg["circuit_breaker_failures"]
                    ):
                        _CIRCUIT["opened_at"] = _now()
                    raise
                retry_after = 0.0
                if response is not None:
                    try:
                        retry_after = float(
                            response.headers.get("Retry-After", 0) or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                delay = retry_after or (
                    cfg["retry_base_seconds"] * (2 ** attempt))
                if delay > 0:
                    time.sleep(min(delay, 60.0))
        raise RuntimeError("threads_api_request_failed")

    def paginate(self, path: str, *, token: str = "",
                 params: dict | None = None, max_pages: int = 20) -> list[dict]:
        """Follow official cursor paging while never trusting a next URL."""
        return self.paginate_with_metadata(
            path, token=token, params=params, max_pages=max_pages)["data"]

    def paginate_with_metadata(
            self, path: str, *, token: str = "",
            params: dict | None = None, max_pages: int = 20) -> dict:
        """Return rows plus page count and whether another page was truncated."""
        output: list[dict] = []
        current = dict(params or {})
        seen: set[str] = set()
        pages = 0
        truncated = False
        for _ in range(max(1, min(100, max_pages))):
            payload = self._request("GET", path, token=token, params=current)
            pages += 1
            rows = payload.get("data") or []
            if not isinstance(rows, list):
                raise RuntimeError("threads_api_invalid_data_schema")
            output.extend(row for row in rows if isinstance(row, dict))
            after = str(
                ((payload.get("paging") or {}).get("cursors") or {})
                .get("after") or "")
            if not after or after in seen:
                break
            seen.add(after)
            current["after"] = after
            current.pop("before", None)
        else:
            truncated = bool(after)
        return {
            "data": output,
            "page_count": pages,
            "pagination_truncated": truncated,
        }

    @staticmethod
    def _resource(path: str) -> str:
        version = settings()["api_version"]
        return f"{version}/{path.lstrip('/')}" if version else path.lstrip("/")

    def exchange_code(self, code: str) -> dict:
        cfg = settings()
        short = self._request(
            "POST", "oauth/access_token",
            data={
                "client_id": cfg["app_id"],
                "client_secret": cfg["app_secret"],
                "grant_type": "authorization_code",
                "redirect_uri": cfg["redirect_uri"],
                "code": code,
            },
        )
        short_token = str(short.get("access_token") or "")
        if not short_token:
            raise RuntimeError("threads_short_token_missing")
        long_lived = self._request(
            "GET", "access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": cfg["app_secret"],
                "access_token": short_token,
            },
        )
        token = str(long_lived.get("access_token") or "")
        if not token:
            raise RuntimeError("threads_long_token_missing")
        profile = self.profile(token)
        return {
            "access_token": token,
            "user_id": str(profile.get("id") or short.get("user_id") or ""),
            "username": str(profile.get("username") or ""),
            "display_name": str(profile.get("name") or ""),
            "expires_in": int(long_lived.get("expires_in") or 5184000),
        }

    def refresh(self, token: str) -> dict:
        return self._request(
            "GET", "refresh_access_token", params={
                "grant_type": "th_refresh_token",
                "access_token": token,
            })

    def profile(self, token: str | None = None) -> dict:
        return self._request(
            "GET", self._resource("me"),
            token=token or settings()["access_token"],
            params={"fields": (
                "id,username,name,is_verified,threads_profile_picture_url,"
                "threads_biography,recently_searched_keywords,"
                "is_eligible_for_geo_gating"
            )},
        )

    def create_container(self, text: str, **options) -> dict:
        cfg = settings()
        user = cfg["user_id"] or "me"
        payload = {"media_type": "TEXT", "text": text}
        payload.update({
            key: value for key, value in options.items()
            if value is not None
        })
        return self._request(
            "POST", self._resource(f"{user}/threads"),
            token=cfg["access_token"],
            data=payload,
        )

    def publish_container(self, creation_id: str) -> dict:
        cfg = settings()
        user = cfg["user_id"] or "me"
        return self._request(
            "POST", self._resource(f"{user}/threads_publish"),
            token=cfg["access_token"], data={"creation_id": creation_id})

    def container_status(self, creation_id: str) -> dict:
        cfg = settings()
        return self._request(
            "GET", self._resource(creation_id),
            token=cfg["access_token"],
            params={"fields": "id,status,error_message"})

    def insights(self, post_id: str) -> dict:
        cfg = settings()
        return self._request(
            "GET", self._resource(f"{post_id}/insights"),
            token=cfg["access_token"],
            params={"metric": "views,likes,replies,reposts,quotes,shares"})

    def account_insights(self, *, metrics: str, breakdown: str = "") -> dict:
        params = {"metric": metrics}
        if breakdown:
            params["breakdown"] = breakdown
        return self._request(
            "GET", self._resource("me/threads_insights"),
            token=settings()["access_token"], params=params)

    def publishing_limit(self) -> dict:
        granted = set(settings()["scopes"])
        fields = [
            "quota_usage", "config", "reply_quota_usage", "reply_config",
        ]
        if "threads_delete" in granted:
            fields.extend(["delete_quota_usage", "delete_config"])
        if "threads_location_tagging" in granted:
            fields.extend([
                "location_search_quota_usage", "location_search_config"])
        return self._request(
            "GET", "me/threads_publishing_limit",
            token=settings()["access_token"],
            params={"fields": ",".join(fields)})

    def debug_token(self) -> dict:
        cfg = settings()
        return self._request(
            "GET", "debug_token", token=cfg["access_token"],
            params={"input_token": cfg["access_token"]})

    def own_posts(self, **params) -> list[dict]:
        defaults = {
            "fields": (
                "id,media_product_type,media_type,media_url,gif_url,permalink,"
                "owner,username,text,timestamp,shortcode,thumbnail_url,children,"
                "is_quote_post,quoted_post,reposted_post,has_replies,alt_text,"
                "link_attachment_url,poll_attachment,location_id,topic_tag,"
                "is_verified,profile_picture_url"),
            "limit": 50,
        }
        defaults.update({k: v for k, v in params.items() if v is not None})
        return self.paginate(
            self._resource("me/threads"), token=settings()["access_token"],
            params=defaults)

    def replies(self, post_id: str, **params) -> list[dict]:
        defaults = {
            "fields": (
                "id,text,timestamp,media_type,media_url,gif_url,permalink,"
                "username,is_reply,is_reply_owned_by_me,root_post,replied_to,"
                "hide_status,reply_audience,reply_approval_status"),
            "reverse": "false",
        }
        defaults.update({k: v for k, v in params.items() if v is not None})
        return self.paginate(
            self._resource(f"{post_id}/conversation"),
            token=settings()["access_token"], params=defaults)

    def own_replies(self, **params) -> list[dict]:
        defaults = {"fields": (
            "id,text,timestamp,media_type,permalink,username,is_reply,"
            "is_reply_owned_by_me,root_post,replied_to,hide_status,"
            "reply_audience,reply_approval_status"), "limit": 50}
        defaults.update({k: v for k, v in params.items() if v is not None})
        return self.paginate(
            self._resource("me/replies"), token=settings()["access_token"],
            params=defaults)

    def mentions(self, **params) -> list[dict]:
        defaults = {"fields": (
            "id,media_type,permalink,username,text,timestamp,shortcode,"
            "is_quote_post,has_replies,topic_tag,is_verified"), "limit": 50}
        defaults.update({k: v for k, v in params.items() if v is not None})
        return self.paginate(
            self._resource("me/mentions"), token=settings()["access_token"],
            params=defaults)

    def keyword_search(self, query: str, *, search_type: str = "RECENT",
                       search_mode: str = "KEYWORD", limit: int = 50,
                       since: str = "", until: str = "",
                       return_metadata: bool = False):
        params = {
            "q": query, "search_type": search_type,
            "search_mode": search_mode, "limit": limit,
            "fields": (
                "id,media_type,media_url,link_attachment_url,permalink,"
                "username,text,timestamp,shortcode,is_quote_post,has_replies,"
                "topic_tag,is_verified,"
                "profile_picture_url"),
        }
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        result = self.paginate_with_metadata(
            self._resource("keyword_search"),
            token=settings()["access_token"], params=params)
        return result if return_metadata else result["data"]

    def repost(self, post_id: str) -> dict:
        return self._request(
            "POST", self._resource(f"{post_id}/repost"),
            token=settings()["access_token"])

    def delete(self, post_id: str) -> dict:
        return self._request(
            "DELETE", self._resource(post_id),
            token=settings()["access_token"])

    def manage_reply(self, reply_id: str, hide: bool) -> dict:
        return self._request(
            "POST", self._resource(f"{reply_id}/manage_reply"),
            token=settings()["access_token"],
            data={"hide": str(bool(hide)).lower()})

    def location(self, location_id: str) -> dict:
        return self._request(
            "GET", self._resource(location_id),
            token=settings()["access_token"],
            params={"fields": (
                "id,address,city,country,name,latitude,longitude,postal_code"
            )})

    def public_profile(self, username: str) -> dict:
        return self._request(
            "GET", self._resource("profile_lookup"),
            token=settings()["access_token"], params={"username": username})

    def public_profile_posts(self, username: str,
                             **params) -> list[dict]:
        defaults = {
            "username": username,
            "fields": (
                "id,media_type,permalink,username,text,timestamp,shortcode,"
                "is_quote_post,has_replies,topic_tag,is_verified"),
            "limit": 50,
        }
        defaults.update({k: v for k, v in params.items() if v is not None})
        return self.paginate(
            self._resource("profile_posts"),
            token=settings()["access_token"], params=defaults)


def create_oauth_state(path: Path | None = None,
                       now: datetime | None = None) -> str:
    """Create a one-time OAuth state. Only its SHA-256 hash is persisted."""
    now = now or _now()
    state = secrets.token_urlsafe(32)
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
    expires = now + timedelta(seconds=settings()["oauth_state_ttl_seconds"])
    apply_additive_migrations(path)
    try:
        with closing(connect(path)) as conn:
            conn.execute(
                "DELETE FROM threads_oauth_states WHERE expires_at<?",
                (now.isoformat(),))
            conn.commit()
    except sqlite3.Error:
        raise RuntimeError("threads_oauth_state_store_unavailable") from None
    inserted = write("""INSERT INTO threads_oauth_states
      (state_hash,created_at,expires_at,used_at) VALUES (?,?,?,NULL)""", (
        digest, now.isoformat(), expires.isoformat(),
    ), path)
    if not inserted:
        raise RuntimeError("threads_oauth_state_store_unavailable")
    return state


def consume_oauth_state(state: str, path: Path | None = None,
                        now: datetime | None = None) -> bool:
    """Atomically consume an unexpired OAuth state exactly once."""
    if not state or len(state) > 512:
        return False
    now = now or _now()
    digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
    apply_additive_migrations(path)
    try:
        with closing(connect(path)) as conn:
            cur = conn.execute(
                """UPDATE threads_oauth_states SET used_at=?
                   WHERE state_hash=? AND used_at IS NULL AND expires_at>=?""",
                (now.isoformat(), digest, now.isoformat()))
            conn.commit()
            return cur.rowcount == 1
    except sqlite3.Error:
        return False


def authorization_url(path: Path | None = None,
                      scope_profile: str = "basic") -> dict:
    cfg = settings()
    if scope_profile not in SCOPE_PROFILES:
        raise ValueError("unsupported Threads OAuth scope profile")
    if not cfg["app_id"] or not cfg["redirect_uri"]:
        raise ValueError("THREADS_APP_ID and THREADS_REDIRECT_URI are required")
    redirect = urlsplit(cfg["redirect_uri"])
    if (
        redirect.scheme != "https" or not redirect.netloc
        or redirect.fragment or redirect.username or redirect.password
    ):
        raise ValueError("THREADS_REDIRECT_URI must be public HTTPS")
    if cfg["public_base_url"]:
        expected = f"{cfg['public_base_url']}/threads/callback"
        if not hmac.compare_digest(cfg["redirect_uri"], expected):
            raise ValueError(
                "THREADS_REDIRECT_URI must match THREADS_PUBLIC_BASE_URL")
    state = create_oauth_state(path)
    query = urlencode({
        "client_id": cfg["app_id"],
        "redirect_uri": cfg["redirect_uri"],
        "scope": ",".join(SCOPE_PROFILES[scope_profile]),
        "response_type": "code",
        "state": state,
    })
    return {
        "authorization_url": f"https://threads.net/oauth/authorize?{query}",
        "redirect_uri": cfg["redirect_uri"],
        "requested_scopes": list(SCOPE_PROFILES[scope_profile]),
        "scope_profile": scope_profile,
        "state_created": True,
    }


def _user_digest(user_id: str) -> str:
    secret = settings()["app_secret"]
    if not secret:
        raise ValueError("THREADS_APP_SECRET is required")
    return hmac.new(
        secret.encode("utf-8"), user_id.encode("utf-8"),
        hashlib.sha256).hexdigest()


def clear_user_credentials(user_id: str, *, detach_history: bool = False,
                           path: Path | None = None) -> dict:
    """Remove local credentials for one verified Meta user."""
    cfg = settings()
    configured_match = bool(
        user_id and cfg["user_id"]
        and hmac.compare_digest(str(user_id), str(cfg["user_id"])))
    if configured_match:
        _update_env({
            "THREADS_ACCESS_TOKEN": "",
            "THREADS_USER_ID": "",
            "THREADS_USERNAME": "",
            "THREADS_TOKEN_EXPIRES_AT": "",
            "THREADS_POST_ENABLED": "false",
        })
    detached = 0
    if detach_history:
        apply_additive_migrations(path)
        try:
            with closing(connect(path)) as conn:
                cur = conn.execute(
                    """UPDATE threads_posts SET threads_user_id=NULL,
                       updated_at=? WHERE threads_user_id=?""",
                    (_now().isoformat(), str(user_id)))
                detached = int(cur.rowcount)
                conn.commit()
        except sqlite3.Error:
            _append_fallback("deletion_events.jsonl", {
                "event_at": _now().isoformat(),
                "event": "history_detach_failed",
                "user_hash": _user_digest(str(user_id)),
            })
            raise RuntimeError("threads_history_detach_failed") from None
    return {
        "credentials_cleared": configured_match,
        "history_links_removed": detached,
        "threads_posting_disabled": configured_match,
    }


def create_deletion_receipt(user_id: str, path: Path | None = None) -> str:
    confirmation = secrets.token_hex(16)
    apply_additive_migrations(path)
    inserted = write("""INSERT INTO threads_deletion_receipts
      (confirmation_hash,user_hash,requested_at,completed_at,status)
      VALUES (?,?,?,?,?)""", (
        hashlib.sha256(confirmation.encode("utf-8")).hexdigest(),
        _user_digest(user_id), _now().isoformat(), _now().isoformat(),
        "completed",
    ), path)
    if not inserted:
        raise RuntimeError("threads_deletion_receipt_unavailable")
    return confirmation


def deletion_status(confirmation: str,
                    path: Path | None = None) -> dict:
    if not confirmation or len(confirmation) > 128:
        return {"found": False, "status": "not_found"}
    digest = hashlib.sha256(confirmation.encode("utf-8")).hexdigest()
    try:
        with closing(connect(path)) as conn:
            row = conn.execute(
                """SELECT status,requested_at,completed_at
                   FROM threads_deletion_receipts
                   WHERE confirmation_hash=?""", (digest,)).fetchone()
        if not row:
            return {"found": False, "status": "not_found"}
        return {"found": True, **dict(row)}
    except sqlite3.Error:
        return {"found": False, "status": "unavailable"}


def exchange_code(code: str, client: ThreadsClient | None = None,
                  path: Path | None = None) -> dict:
    cfg = settings()
    if not all((cfg["app_id"], cfg["app_secret"], cfg["redirect_uri"], code)):
        raise ValueError("Threads OAuth configuration is incomplete")
    try:
        result = (client or ThreadsClient(path=path)).exchange_code(code)
        expires_at = (
            _now() + timedelta(seconds=int(result["expires_in"]))
        ).isoformat()
        _update_env({
            "THREADS_ACCESS_TOKEN": result["access_token"],
            "THREADS_USER_ID": result["user_id"],
            "THREADS_USERNAME": result["username"],
            "THREADS_TOKEN_EXPIRES_AT": expires_at,
        })
        _token_event("code_exchanged", True, expires_at, path=path)
        return {
            "configured": True,
            "user_id": result["user_id"],
            "username": result["username"],
            "expires_at": expires_at,
            "token_saved": True,
        }
    except Exception as exc:
        _token_event(
            "code_exchange_failed", False, error_type=type(exc).__name__,
            path=path)
        raise RuntimeError("Threads code exchange failed") from None


def token_status(now: datetime | None = None) -> dict:
    cfg = settings()
    now = now or _now()
    expires = _parse_datetime(cfg["expires_at"])
    days = None
    if expires:
        days = round((expires.astimezone(JST) - now).total_seconds() / 86400, 2)
    refresh_required = bool(
        cfg["access_token"] and (
            not expires or days is None
            or days <= cfg["refresh_before_days"]
        )
    )
    return {
        "configured": bool(
            cfg["app_id"] and cfg["redirect_uri"] and cfg["user_id"]),
        "token_present": bool(cfg["access_token"]),
        "user_id": cfg["user_id"],
        "username": cfg["username"],
        "scopes": list(cfg["scopes"]),
        "expires_at": cfg["expires_at"] or None,
        "days_remaining": days,
        "refresh_required": refresh_required,
    }


def refresh_token(client: ThreadsClient | None = None,
                  force: bool = False, path: Path | None = None) -> dict:
    cfg = settings()
    status = token_status()
    if not cfg["access_token"]:
        return {"refreshed": False, "reason": "token_missing"}
    if not force and not status["refresh_required"]:
        return {"refreshed": False, "reason": "not_due", **status}
    try:
        result = (client or ThreadsClient(path=path)).refresh(
            cfg["access_token"])
        token = str(result.get("access_token") or cfg["access_token"])
        expires_at = (
            _now() + timedelta(seconds=int(
                result.get("expires_in") or 5184000))
        ).isoformat()
        _update_env({
            "THREADS_ACCESS_TOKEN": token,
            "THREADS_TOKEN_EXPIRES_AT": expires_at,
        })
        _token_event("token_refreshed", True, expires_at, path=path)
        return {
            "refreshed": True, "expires_at": expires_at,
            "token_present": True,
        }
    except Exception as exc:
        _update_env({"THREADS_POST_ENABLED": "false"})
        _token_event(
            "token_refresh_failed", False, cfg["expires_at"],
            type(exc).__name__, path=path)
        return {
            "refreshed": False,
            "reason": "refresh_failed",
            "threads_posting_disabled": True,
            "x_bot_affected": False,
            "error_type": type(exc).__name__,
        }


def profile_status(client: ThreadsClient | None = None,
                   path: Path | None = None) -> dict:
    cfg = settings()
    if not cfg["access_token"]:
        return {
            "threads_user_id": cfg["user_id"],
            "username": cfg["username"],
            "display_name": None,
            "token_valid": False,
        }
    try:
        result = (client or ThreadsClient(path=path)).profile()
        return {
            "threads_user_id": str(result.get("id") or cfg["user_id"]),
            "username": str(result.get("username") or cfg["username"]),
            "display_name": result.get("name"),
            "token_valid": True,
        }
    except Exception:
        return {
            "threads_user_id": cfg["user_id"],
            "username": cfg["username"],
            "display_name": None,
            "token_valid": False,
        }


def _candidate_query(path: Path | None = None,
                     x_post_id: str | None = None) -> dict | None:
    apply_additive_migrations(path)
    where = "AND p.tweet_id=?" if x_post_id else ""
    params: tuple = (x_post_id,) if x_post_id else ()
    query = f"""SELECT p.tweet_id,p.text x_text,p.posted_at,p.topic_key,
      p.post_type,g.id generated_post_id,g.quality_score,g.ban_risk,
      g.threads_text politics_threads_text,
      n.id news_id,n.title,n.summary,n.source_name,n.source_url,
      n.source_type,n.genre,n.metadata_json,n.verified,n.final_news_score,
      n.source_reliability_score,
      COALESCE(q.correction_required,0) correction_required,
      COALESCE(q.manual_delete_required,0) manual_delete_required,
      COALESCE(q.personal_attack_score,0) personal_attack_score
      FROM published_posts p
      JOIN generated_posts g ON g.id=p.generated_post_id
      JOIN news_candidates n ON n.id=g.news_candidate_id
      LEFT JOIN post_quality_dimensions q ON q.tweet_id=p.tweet_id
      WHERE n.verified=1 AND COALESCE(q.correction_required,0)=0
      AND COALESCE(q.manual_delete_required,0)=0
      {where}
      ORDER BY p.posted_at DESC LIMIT 30"""
    try:
        with closing(connect(path)) as conn:
            rows = [dict(row) for row in conn.execute(query, params)]
            recent_topics = {
                str(row["topic_key"] or "")
                for row in conn.execute(
                    """SELECT topic_key FROM threads_posts
                       WHERE published_at>=? AND status='published'""",
                    ((_now() - timedelta(
                        hours=settings()["topic_cooldown_hours"])).isoformat(),),
                )
            }
    except sqlite3.Error:
        return None
    cfg = settings()
    now = _now()
    for row in rows:
        if str(row.get("topic_key") or "") in recent_topics:
            continue
        posted = _parse_datetime(row.get("posted_at"))
        if posted and (
            now - posted.astimezone(JST)
        ).total_seconds() < cfg["min_delay_after_x_minutes"] * 60:
            continue
        if float(row.get("quality_score") or 0) < 7.0:
            continue
        if float(row.get("ban_risk") or 0) > 2.0:
            continue
        if float(row.get("personal_attack_score") or 0) > 2.0:
            continue
        try:
            metadata = json.loads(row.pop("metadata_json", "") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        for key in (
            "affected_group", "decision_maker", "beneficiary", "cost_bearer",
            "responsible_entity", "major_incident", "incident_phase",
            "term_evidence",
        ):
            if key in metadata:
                row[key] = metadata[key]
        row["content_id"] = str(row.get("news_id") or "")
        return row
    return None


def similarity_to_x(threads_text: str, x_text: str) -> float:
    normalize = lambda value: re.sub(r"\s+", "", str(value or "")).lower()
    return round(SequenceMatcher(
        None, normalize(threads_text), normalize(x_text)).ratio(), 4)


def _emoji_count(text: str) -> int:
    return len(re.findall(
        r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text))


def _is_serious_source(source: dict) -> bool:
    source_text = " ".join([
        str(source.get("title") or ""),
        str(source.get("summary") or ""),
    ])
    return any(term in source_text for term in HIGH_RISK_TERMS)


def _avoid_repeated_closing(question: bool,
                            path: Path | None = None) -> bool:
    """Do not use the same question/non-question ending three times."""
    try:
        with closing(connect(path)) as conn:
            rows = conn.execute(
                """SELECT question_included FROM threads_generation_runs
                   WHERE decision='generated' ORDER BY id DESC LIMIT 2"""
            ).fetchall()
        if len(rows) == 2 and all(
            bool(row["question_included"]) == question for row in rows
        ):
            return not question
    except sqlite3.Error:
        pass
    return question


def _local_text(source: dict, question: bool = False,
                emoji: bool = False) -> str:
    title = re.sub(r"\s+", " ", str(source.get("title") or "")).strip()
    summary = re.sub(r"\s+", " ", str(source.get("summary") or "")).strip()
    topic = title[:80] or str(source.get("topic_key") or "今回の政策")
    detail = summary[:105] or (
        "公式情報を確認すると、決定そのものだけでなく、"
        "実施主体と継続的な負担の設計を分けて見る必要があります。"
    )
    try:
        social = social_anger_prompt_context(
            source, persist=False)["prompt_context"]
    except Exception:
        social = {}
    affected = "、".join(social.get("affected_group") or []) or "影響を受ける人"
    responsible = (
        "、".join(social.get("responsible_entity") or [])
        or "決定・監督する側"
    )
    improvement = str(social.get("proposed_improvement") or (
        "対象者、費用、実施主体、期限、見直し条件を公開する。"
    ))
    ending = ""
    if question:
        ending = str(social.get("public_question") or (
            "実施主体は、検証結果と見直し期限をいつ公開しますか。"
        ))
        if ending.endswith("か。"):
            ending = ending[:-1] + "？"
        elif not ending.endswith(("？", "?")):
            ending = ending.rstrip("。") + "？"
    lead = "🧵 " if emoji else ""
    ending_block = f"\n\n{ending}" if ending else ""
    text = (
        f"{lead}{topic}。まず確認できる事実は次の通りです。\n\n"
        f"{detail}\n\n"
        f"影響を受けるのは{affected}。説明と検証の責任は"
        f"{responsible}にあります。求めたい改善は、{improvement}"
        "評価に必要なのは、実施前後で何を測り、結果が悪ければ"
        "誰がいつ見直すかという検証手順です。"
        f"{ending_block}"
    )
    limit = settings()["target_max_chars"]
    if len(text) <= limit:
        return text
    if ending_block:
        body_limit = max(1, limit - len(ending_block))
        return text[:body_limit].rstrip() + ending_block
    return text[:limit].rstrip()


def quality_check(text: str, source: dict, x_text: str) -> dict:
    cfg = settings()
    reasons = []
    length = len(text)
    similarity = similarity_to_x(text, x_text)
    if length < cfg["target_min_chars"]:
        reasons.append("too_short")
    if length > cfg["target_max_chars"] or length > cfg["platform_limit"]:
        reasons.append("too_long")
    if similarity > cfg["max_similarity"]:
        reasons.append("too_similar_to_x")
    if any(term.lower() in text.lower() for term in INTERNAL_TERMS):
        reasons.append("internal_label")
    if any(term in text for term in ATTACK_TERMS):
        reasons.append("personal_attack")
    if not bool(source.get("verified")):
        reasons.append("unverified_source")
    if any(term in text for term in HIGH_RISK_TERMS) and (
        float(source.get("source_reliability_score") or 0) < 8
    ):
        reasons.append("high_risk_unconfirmed")
    if _emoji_count(text) > max(
        0, _int("THREADS_EMOJI_MAX_PER_POST", 1)):
        reasons.append("too_many_emoji")
    if (
        _bool("THREADS_EMOJI_REQUIRED", "true")
        and not _is_serious_source(source)
        and _emoji_count(text) == 0
    ):
        reasons.append("emoji_required")
    if len(re.findall(r"#[^\s#]+", text)) > max(
        0, _int("THREADS_HASHTAG_MAX_PER_POST", 1)):
        reasons.append("too_many_hashtags")
    try:
        social_review = evaluate_social_anger_candidate(
            source, text, platform="threads", persist=True)
        reasons.extend(
            f"social_anger:{value}"
            for value in social_review.get("safety_violations") or []
        )
    except Exception:
        social_review = {
            "production_publish_connected": False,
            "phase": "B",
            "effective_score": 0,
        }
    quality = max(0.0, 10.0 - len(set(reasons)) * 2.0)
    return {
        "passed": not reasons and quality >= 8.0,
        "quality_score": quality,
        "safety_score": 10.0 if not reasons else max(0.0, quality),
        "similarity_to_x": similarity,
        "reasons": sorted(set(reasons)),
        "question_included": text.rstrip().endswith(("？", "?")),
        "emoji_count": _emoji_count(text),
        "social_anger_connected": bool(
            social_review.get("production_publish_connected")),
        "social_anger_phase": social_review.get("phase", "B"),
        "social_anger_effective_score": float(
            social_review.get("effective_score", 0) or 0),
    }


THREADS_SCHEMA = {
    "type": "object",
    "properties": {
        "threads_post_type": {
            "type": "string", "enum": sorted(POST_TYPES)},
        "text": {"type": "string"},
        "question_included": {"type": "boolean"},
        "decision_reason": {"type": "string"},
    },
    "required": [
        "threads_post_type", "text", "question_included",
        "decision_reason",
    ],
    "additionalProperties": False,
}


def _threads_month_spent(path: Path | None = None) -> float:
    try:
        with closing(connect(path)) as conn:
            value = conn.execute(
                """SELECT COALESCE(SUM(estimated_cost_usd),0)
                   FROM api_usage_events
                   WHERE provider='openai' AND timestamp LIKE ?
                   AND operation IN
                   ('threads_generation','threads_regeneration',
                    'threads_daily_review')""",
                (_now().strftime("%Y-%m") + "%",),
            ).fetchone()[0]
        return float(value or 0)
    except sqlite3.Error:
        return 0.0


def _openai_text(source: dict, client_factory=None,
                 path: Path | None = None) -> tuple[dict, dict]:
    cfg = settings()
    prepared = str(source.get("politics_threads_text") or "").strip()
    if prepared:
        from politics_multistage import fit_platform_text
        prepared = fit_platform_text(prepared, cfg["target_max_chars"])
        if prepared:
            return {
                "threads_post_type": "policy_context",
                "text": prepared,
                "question_included": prepared.endswith(("？", "?")),
                "decision_reason": "politics_multistage_independent_threads_text",
            }, {
                "model": "politics_multistage_reuse",
                "input_tokens": 0, "output_tokens": 0,
                "estimated_cost_usd": 0.0,
            }
    if (
        not os.environ.get("OPENAI_API_KEY", "").strip()
        or _threads_month_spent(path) + cfg["max_cost_per_post"]
        > cfg["monthly_openai_budget"]
    ):
        return {}, {"reason": "threads_openai_budget_or_key_unavailable"}
    reservation_id, reason = reserve(
        "openai", "threads_generation", cfg["model"],
        cfg["max_cost_per_post"], metadata={"platform": "threads"},
        path=path)
    if not reservation_id:
        return {}, {"reason": reason}
    try:
        if client_factory is None:
            from openai import OpenAI
            client_factory = OpenAI
        client = client_factory(
            api_key=os.environ["OPENAI_API_KEY"],
            timeout=max(10, cfg["timeout"]), max_retries=0)
        review_guidance = ""
        if source.get("review_strategy_variant") == "treatment":
            try:
                from review_strategy import (
                    load_active_strategy,
                    render_prompt_guidance,
                )
                review_guidance = render_prompt_guidance(
                    load_active_strategy(_root()))
            except Exception:
                review_guidance = ""
        response = client.responses.create(
            model=cfg["model"],
            instructions=(
                "確認済み政治ニュースからThreads専用の日本語投稿を作る。"
                "X本文をコピーせず、要点、制度背景、論点の順で180〜450文字。"
                "確認済み事実、影響を受ける人、決定・監督責任、具体的な改善要求を"
                "読みやすく整理する。社会の不満を扱う場合も怒りを煽らず、"
                "事実と評価を分ける。政党・個人・属性集団への敵意、群衆行動の誘導、"
                "意図の推測、未確認の断定は禁止。質問は必要な場合だけ。"
                "重大事件を除き絵文字は必ず1個、重大事件は0個。"
                "ハッシュタグ最大1。"
            ),
            input=json.dumps({
                "title": source.get("title"),
                "summary": source.get("summary"),
                "topic_key": source.get("topic_key"),
                "x_text_to_avoid_copying": source.get("x_text"),
                "public_accountability_context": social_anger_prompt_context(
                    source, persist=False)["prompt_context"],
                "validated_daily_review_guidance": review_guidance,
            }, ensure_ascii=False),
            max_output_tokens=cfg["max_output_tokens"],
            text={"format": {
                "type": "json_schema", "name": "threads_post",
                "strict": True, "schema": THREADS_SCHEMA,
            }},
            reasoning={"effort": cfg["reasoning_effort"]},
            store=False,
        )
        payload = json.loads(response.output_text)
        input_tokens, cached, output_tokens = usage_from_response(response)
        pricing = load_pricing(_root() / "config" / "openai_model_pricing.json")
        cost = calculate_cost(
            pricing, cfg["model"], input_tokens, cached, output_tokens)
        finalize(
            reservation_id, cost, success=True, input_tokens=input_tokens,
            cached_tokens=cached, output_tokens=output_tokens, path=path)
        return payload, {
            "model": cfg["model"], "input_tokens": input_tokens,
            "output_tokens": output_tokens, "estimated_cost_usd": cost,
        }
    except Exception as exc:
        finalize(
            reservation_id, 0.0, success=False,
            error_type=type(exc).__name__, path=path)
        return {}, {"reason": type(exc).__name__}


def _save_generation(record: dict, path: Path | None = None) -> int | None:
    try:
        apply_additive_migrations(path)
        return write("""INSERT INTO threads_generation_runs
          (source_content_id,source_x_post_id,topic_key,threads_post_type,text,
           model,prompt_version,similarity_to_x,quality_score,safety_score,
           decision,decision_reason,input_tokens,output_tokens,
           estimated_cost_usd,question_included,emoji_count,
           review_strategy_id,review_strategy_experiment,
           review_strategy_variant,created_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            record.get("source_content_id"), record.get("source_x_post_id"),
            record.get("topic_key"), record.get("threads_post_type"),
            record.get("text"), record.get("model"),
            record.get("prompt_version"), record.get("similarity_to_x"),
            record.get("quality_score"), record.get("safety_score"),
            record.get("decision"), record.get("decision_reason"),
            record.get("input_tokens", 0), record.get("output_tokens", 0),
            record.get("estimated_cost_usd", 0),
            int(bool(record.get("question_included"))),
            int(record.get("emoji_count", 0)),
            record.get("review_strategy_id"),
            record.get("review_strategy_experiment"),
            record.get("review_strategy_variant", "inactive"),
            record.get("created_at"),
        ), path)
    except Exception:
        _append_fallback("generation_runs.jsonl", record)
        return None


def generate(*, dry_run: bool = False, x_post_id: str | None = None,
             path: Path | None = None, client_factory=None,
             now: datetime | None = None) -> dict:
    cfg = settings()
    now = now or _now()
    if not cfg["enabled"]:
        return {"status": "skipped", "reason": "threads_disabled"}
    source = _candidate_query(path, x_post_id)
    if not source:
        return {"status": "skipped", "reason": "no_eligible_verified_source"}
    try:
        from review_strategy import assignment_for, load_active_strategy
        active_review_strategy = load_active_strategy(_root(), now=now)
        review_variant = assignment_for(
            source, strategy=active_review_strategy, now=now)
    except Exception:
        active_review_strategy = {}
        review_variant = "inactive"
    source["review_strategy_id"] = str(
        active_review_strategy.get("strategy_id") or "")
    source["review_strategy_experiment"] = str(
        active_review_strategy.get("experiment_name") or "")
    source["review_strategy_variant"] = review_variant
    question = (
        int(hashlib.sha256(
            str(source["tweet_id"]).encode()).hexdigest(), 16) % 10 < 4
    )
    question = _avoid_repeated_closing(question, path)
    emoji = bool(
        _bool("THREADS_EMOJI_REQUIRED", "true")
        and not _is_serious_source(source)
    )
    payload = {}
    usage = {}
    if not dry_run:
        payload, usage = _openai_text(
            source, client_factory=client_factory, path=path)
    text = str(payload.get("text") or _local_text(source, question, emoji))
    checked = quality_check(text, source, str(source.get("x_text") or ""))
    if not checked["passed"] and payload:
        text = _local_text(source, question=False)
        checked = quality_check(
            text, source, str(source.get("x_text") or ""))
        usage["regenerated_locally"] = True
    post_type = str(
        payload.get("threads_post_type") or "conversation_explainer")
    if post_type not in POST_TYPES:
        post_type = "conversation_explainer"
    record = {
        "source_content_id": f"x-{source['tweet_id']}",
        "source_x_post_id": source["tweet_id"],
        "topic_key": source.get("topic_key"),
        "threads_post_type": post_type,
        "text": text,
        "model": usage.get("model", "local_preview"),
        "prompt_version": PROMPT_VERSION,
        "similarity_to_x": checked["similarity_to_x"],
        "quality_score": checked["quality_score"],
        "safety_score": checked["safety_score"],
        "decision": "generated" if checked["passed"] else "rejected",
        "decision_reason": (
            str(payload.get("decision_reason") or "verified_topic_preview")
            if checked["passed"] else ",".join(checked["reasons"])
        ),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "estimated_cost_usd": usage.get("estimated_cost_usd", 0.0),
        "question_included": checked["question_included"],
        "emoji_count": checked["emoji_count"],
        "social_anger_connected": checked.get(
            "social_anger_connected", False),
        "social_anger_phase": checked.get("social_anger_phase", "B"),
        "social_anger_effective_score": checked.get(
            "social_anger_effective_score", 0.0),
        "review_strategy_id": source.get("review_strategy_id", ""),
        "review_strategy_experiment": source.get(
            "review_strategy_experiment", ""),
        "review_strategy_variant": source.get(
            "review_strategy_variant", "inactive"),
        "created_at": now.isoformat(),
    }
    draft_id = _save_generation(record, path)
    return {
        "status": record["decision"], "draft_id": draft_id,
        "source_content_id": record["source_content_id"],
        "source_x_post_id": record["source_x_post_id"],
        "topic_key": record["topic_key"],
        "threads_post_type": post_type,
        "social_anger_connected": record["social_anger_connected"],
        "social_anger_phase": record["social_anger_phase"],
        "social_anger_effective_score": record[
            "social_anger_effective_score"],
        "review_strategy_id": record["review_strategy_id"],
        "review_strategy_experiment": record[
            "review_strategy_experiment"],
        "review_strategy_variant": record["review_strategy_variant"],
        "text": text,
        "character_count": len(text),
        "quality_score": record["quality_score"],
        "safety_score": record["safety_score"],
        "similarity_to_x": record["similarity_to_x"],
        "question_included": record["question_included"],
        "emoji_count": record["emoji_count"],
        "dry_run": dry_run,
        "threads_api_calls": 0,
        "threads_published": False,
        "x_writes": 0,
        "estimated_cost_usd": record["estimated_cost_usd"],
    }


def drafts(limit: int = 30, path: Path | None = None) -> list[dict]:
    apply_additive_migrations(path)
    try:
        with closing(connect(path)) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT id,source_content_id,source_x_post_id,topic_key,
                   threads_post_type,text,similarity_to_x,quality_score,
                   safety_score,decision,decision_reason,created_at
                   FROM threads_generation_runs ORDER BY id DESC LIMIT ?""",
                (max(1, min(100, limit)),),
            )]
    except sqlite3.Error:
        return []


def _today_post_count(path: Path | None = None,
                      now: datetime | None = None) -> int:
    now = now or _now()
    try:
        with closing(connect(path)) as conn:
            return int(conn.execute(
                """SELECT COUNT(*) FROM threads_posts
                   WHERE status='published' AND published_at LIKE ?""",
                (now.date().isoformat() + "%",),
            ).fetchone()[0])
    except sqlite3.Error:
        return 0


def _load_draft(draft_id: int, path: Path | None = None) -> dict:
    apply_additive_migrations(path)
    with closing(connect(path)) as conn:
        row = conn.execute(
            "SELECT * FROM threads_generation_runs WHERE id=?",
            (draft_id,)).fetchone()
    if not row:
        raise ValueError("threads draft not found")
    return dict(row)


def _client_post_key(draft: dict, now: datetime | None = None) -> str:
    now = now or _now()
    return (
        f"threads:{draft['source_content_id']}:{draft['topic_key']}:"
        f"{now.date().isoformat()}"
    )


def publish(draft_id: int, client: ThreadsClient | None = None,
            path: Path | None = None,
            now: datetime | None = None) -> dict:
    cfg = settings()
    now = now or _now()
    if not cfg["enabled"] or not cfg["post_enabled"]:
        return {
            "published": False, "reason": "threads_posting_disabled",
            "threads_api_calls": 0,
        }
    if not cfg["access_token"] or not cfg["user_id"]:
        return {
            "published": False, "reason": "threads_credentials_missing",
            "threads_api_calls": 0,
        }
    if _today_post_count(path, now) >= cfg["daily_max"]:
        return {
            "published": False, "reason": "threads_daily_limit",
            "threads_api_calls": 0,
        }
    draft = _load_draft(draft_id, path)
    if draft["decision"] != "generated":
        return {
            "published": False, "reason": "draft_not_approved_for_publish",
            "threads_api_calls": 0,
        }
    key = _client_post_key(draft, now)
    apply_additive_migrations(path)
    try:
        with closing(connect(path)) as conn:
            existing = conn.execute(
                "SELECT * FROM threads_posts WHERE client_post_key=?",
                (key,)).fetchone()
            last = conn.execute(
                """SELECT published_at FROM threads_posts
                   WHERE status='published'
                   ORDER BY published_at DESC LIMIT 1""").fetchone()
        if existing:
            return {
                "published": False,
                "reason": "duplicate_client_post_key",
                "status": existing["status"],
                "threads_api_calls": 0,
            }
        if last and _parse_datetime(last["published_at"]):
            age = (
                now - _parse_datetime(last["published_at"]).astimezone(JST)
            ).total_seconds() / 60
            if age < cfg["min_interval_minutes"]:
                return {
                    "published": False,
                    "reason": "threads_min_interval",
                    "threads_api_calls": 0,
                }
    except sqlite3.Error:
        return {
            "published": False, "reason": "threads_sqlite_unavailable",
            "threads_api_calls": 0,
        }
    post_id = write("""INSERT INTO threads_posts
      (client_post_key,generation_run_id,threads_user_id,topic_key,text,status,
       scheduled_at,reply_control,source_content_id,source_x_post_id,
       created_at,updated_at)
      VALUES (?,?,?,?,?,'generated',?,?,?,?,?,?)""", (
        key, draft_id, cfg["user_id"], draft["topic_key"], draft["text"],
        now.isoformat(), cfg["reply_control"], draft["source_content_id"],
        draft["source_x_post_id"], now.isoformat(), now.isoformat(),
    ), path)
    api = client or ThreadsClient(path=path)
    calls = 0
    try:
        created = api.create_container(str(draft["text"]))
        calls += 1
        creation_id = str(created.get("id") or "")
        if not creation_id:
            raise RuntimeError("threads_creation_id_missing")
        write("""UPDATE threads_posts SET creation_id=?,
          status='container_created',container_created_at=?,updated_at=?
          WHERE id=?""", (
            creation_id, _now().isoformat(), _now().isoformat(), post_id,
        ), path)
        write("""UPDATE threads_posts SET status='publish_pending',updated_at=?
          WHERE id=?""", (_now().isoformat(), post_id), path)
        published = api.publish_container(creation_id)
        calls += 1
        threads_post_id = str(published.get("id") or "")
        if not threads_post_id:
            raise RuntimeError("threads_post_id_missing")
        write("""UPDATE threads_posts SET threads_post_id=?,status='published',
          published_at=?,updated_at=? WHERE id=?""", (
            threads_post_id, _now().isoformat(), _now().isoformat(), post_id,
        ), path)
        return {
            "published": True, "threads_post_id": threads_post_id,
            "creation_id_saved": True, "threads_api_calls": calls,
        }
    except requests.Timeout:
        write("""UPDATE threads_posts SET status='ambiguous',
          error_type='Timeout',updated_at=? WHERE id=?""",
              (_now().isoformat(), post_id), path)
        return {
            "published": False, "reason": "ambiguous_timeout",
            "status": "ambiguous", "threads_api_calls": calls,
        }
    except Exception as exc:
        write("""UPDATE threads_posts SET status='failed',error_type=?,
          updated_at=? WHERE id=?""", (
            type(exc).__name__, _now().isoformat(), post_id,
        ), path)
        return {
            "published": False, "reason": "threads_api_failed",
            "status": "failed", "error_type": type(exc).__name__,
            "threads_api_calls": calls,
        }


def run_scheduled(path: Path | None = None,
                  now: datetime | None = None) -> dict:
    cfg = settings()
    now = now or _now()
    current = now.strftime("%H:%M")
    if current not in cfg["schedule"]:
        return {"status": "skipped", "reason": "not_scheduled_slot"}
    generated = generate(path=path, now=now)
    if generated.get("status") != "generated":
        return generated
    if not cfg["post_enabled"]:
        return {**generated, "status": "preview_saved"}
    return publish(int(generated["draft_id"]), path=path, now=now)


def _metric_values(payload: dict) -> dict[str, int | None]:
    output = {
        "views": None, "likes": None, "replies": None,
        "reposts": None, "quotes": None, "shares": None,
    }
    for row in payload.get("data") or []:
        name = str(row.get("name") or "")
        if name not in output:
            continue
        values = row.get("values") or []
        value = (
            values[-1].get("value") if values and isinstance(values[-1], dict)
            else (row.get("total_value") or {}).get("value")
        )
        output[name] = int(value) if value is not None else None
    return output


def collect_metrics(client: ThreadsClient | None = None,
                    path: Path | None = None,
                    now: datetime | None = None) -> dict:
    cfg = settings()
    now = now or _now()
    if not cfg["metrics_enabled"] or not cfg["insights_enabled"]:
        return {"collected": 0, "failed": 0, "reason": "metrics_disabled"}
    if not cfg["access_token"]:
        return {"collected": 0, "failed": 0, "reason": "token_missing"}
    apply_additive_migrations(path)
    try:
        with closing(connect(path)) as conn:
            posts = [dict(row) for row in conn.execute(
                """SELECT * FROM threads_posts
                   WHERE status='published' AND threads_post_id IS NOT NULL""")]
            existing = {
                (str(row["threads_post_id"]), str(row["measurement_window"]))
                for row in conn.execute(
                    "SELECT threads_post_id,measurement_window FROM threads_metrics")
            }
    except sqlite3.Error:
        return {"collected": 0, "failed": 0, "reason": "sqlite_unavailable"}
    api = client or ThreadsClient(path=path)
    collected = failed = 0
    for post in posts:
        published = _parse_datetime(post.get("published_at"))
        if not published:
            continue
        age_hours = (now - published.astimezone(JST)).total_seconds() / 3600
        for window in cfg["metrics_windows"]:
            if (
                age_hours < WINDOW_HOURS[window]
                or (str(post["threads_post_id"]), window) in existing
            ):
                continue
            try:
                values = _metric_values(api.insights(
                    str(post["threads_post_id"])))
                numeric = [
                    values[name] for name in (
                        "likes", "replies", "reposts", "quotes", "shares")
                    if values[name] is not None
                ]
                views = values["views"]
                engagement = (
                    round(sum(numeric) / views, 8)
                    if views and views > 0 else None
                )
                views_per_hour = (
                    round(views / max(1, WINDOW_HOURS[window]), 8)
                    if views is not None else None
                )
                write("""INSERT OR IGNORE INTO threads_metrics
                  (threads_post_id,measurement_window,measured_at,views,likes,
                   replies,reposts,quotes,shares,engagement_rate,views_per_hour)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                    post["threads_post_id"], window, now.isoformat(),
                    values["views"], values["likes"], values["replies"],
                    values["reposts"], values["quotes"], values["shares"],
                    engagement, views_per_hour,
                ), path)
                collected += 1
            except Exception:
                failed += 1
    return {"collected": collected, "failed": failed}


def platform_comparison(path: Path | None = None) -> dict:
    apply_additive_migrations(path)
    try:
        with closing(connect(path)) as conn:
            rows = [dict(row) for row in conn.execute("""
              SELECT t.source_content_id,t.source_x_post_id,t.threads_post_type,
              t.text,t.published_at,g.question_included,
              tm.views,tm.replies,tm.engagement_rate,tm.views_per_hour,
              xm.impressions,xm.replies x_replies,
              xm.engagement_rate x_engagement_rate,
              xm.impressions_per_hour
              FROM threads_posts t
              LEFT JOIN threads_generation_runs g ON g.id=t.generation_run_id
              LEFT JOIN threads_metrics tm ON tm.threads_post_id=t.threads_post_id
                AND tm.measurement_window='24h'
              LEFT JOIN post_metrics xm ON xm.tweet_id=t.source_x_post_id
                AND xm.measurement_window='24h'
              WHERE t.status='published'""")]
    except sqlite3.Error:
        rows = []
    matched = [
        row for row in rows
        if row.get("views") is not None and row.get("impressions") is not None
    ]
    if len(matched) < 3:
        return {
            "decision": "insufficient_data",
            "same_content_id_count": len(matched),
            "sample_size": len(matched),
            "x_average": None,
            "threads_average": None,
            "threads_strong_post_type": None,
            "x_strong_post_type": None,
            "question_comparison": None,
            "threads_time_comparison": None,
            "note": "Metric definitions differ; no winner is declared.",
        }
    def average(key):
        values = [float(row[key]) for row in matched if row.get(key) is not None]
        return round(sum(values) / len(values), 6) if values else None
    type_scores: dict[str, list[float]] = {}
    for row in matched:
        if row.get("engagement_rate") is not None:
            type_scores.setdefault(
                str(row.get("threads_post_type") or "unknown"), []
            ).append(float(row["engagement_rate"]))
    strongest = max(
        type_scores,
        key=lambda key: sum(type_scores[key]) / len(type_scores[key]),
        default=None,
    )
    return {
        "decision": "measured_without_cross_platform_winner",
        "same_content_id_count": len(matched),
        "sample_size": len(matched),
        "x_average": {
            "impressions": average("impressions"),
            "engagement_rate": average("x_engagement_rate"),
            "initial_velocity": average("impressions_per_hour"),
        },
        "threads_average": {
            "views": average("views"),
            "engagement_rate": average("engagement_rate"),
            "initial_velocity": average("views_per_hour"),
        },
        "threads_strong_post_type": strongest,
        "x_strong_post_type": None,
        "question_comparison": {
            str(flag): round(sum(
                float(row.get("engagement_rate") or 0)
                for row in matched if bool(row.get("question_included")) == flag
            ) / max(1, sum(
                bool(row.get("question_included")) == flag for row in matched
            )), 6)
            for flag in (False, True)
        },
        "threads_time_comparison": {},
        "note": "Metric definitions differ; results are not a simple winner.",
    }


def daily_review_summary(path: Path | None = None) -> dict:
    comparison = platform_comparison(path)
    try:
        with closing(connect(path)) as conn:
            rows = [dict(row) for row in conn.execute("""
              SELECT g.threads_post_type,g.question_included,g.emoji_count,
              g.similarity_to_x,g.quality_score,g.safety_score,
              g.review_strategy_id,g.review_strategy_experiment,
              g.review_strategy_variant,
              p.published_at,m.views,m.replies,m.reposts,m.quotes,
              m.engagement_rate,m.views_per_hour
              FROM threads_generation_runs g
              LEFT JOIN threads_posts p ON p.generation_run_id=g.id
              LEFT JOIN threads_metrics m ON m.threads_post_id=p.threads_post_id
                AND m.measurement_window='24h'
              WHERE g.created_at>=?""",
              ((_now() - timedelta(days=1)).isoformat(),))]
    except sqlite3.Error:
        rows = []
    safe_rows = [
        row for row in rows
        if float(row.get("safety_score") or 0) >= 9
        and float(row.get("quality_score") or 0) >= 8
    ]
    strategy_variants = {}
    for variant in ("treatment", "control"):
        group = [
            row for row in safe_rows
            if row.get("review_strategy_variant") == variant
            and row.get("views") is not None
        ]
        strategy_variants[variant] = {
            "sample_size": len(group),
            "average_views": round(
                sum(float(row.get("views") or 0) for row in group)
                / len(group), 3,
            ) if group else None,
            "average_views_per_hour": round(
                sum(float(row.get("views_per_hour") or 0) for row in group)
                / len(group), 3,
            ) if group else None,
            "average_engagement_rate": round(
                sum(float(row.get("engagement_rate") or 0) for row in group)
                / len(group), 6,
            ) if group else None,
        }
    return {
        "sample_size": len(rows),
        "safe_success_sample_size": len(safe_rows),
        "comparison": comparison,
        "review_strategy_variants": strategy_variants,
        "winning_patterns_exclude_unsafe": True,
    }


def status(path: Path | None = None,
           now: datetime | None = None) -> dict:
    cfg = settings()
    now = now or _now()
    apply_additive_migrations(path)
    today = _today_post_count(path, now)
    last = None
    last_error = None
    try:
        with closing(connect(path)) as conn:
            row = conn.execute(
                """SELECT threads_post_id,published_at,status FROM threads_posts
                   ORDER BY id DESC LIMIT 1""").fetchone()
            if row:
                last = dict(row)
            error = conn.execute(
                """SELECT error_type FROM threads_posts
                   WHERE error_type IS NOT NULL AND error_type<>''
                   ORDER BY id DESC LIMIT 1""").fetchone()
            if error:
                last_error = error["error_type"]
    except sqlite3.Error:
        pass
    next_slot = None
    for value in cfg["schedule"]:
        try:
            hour, minute = map(int, value.split(":"))
            candidate = now.replace(
                hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now:
                next_slot = candidate.isoformat()
                break
        except ValueError:
            continue
    return {
        "threads_enabled": cfg["enabled"],
        "posting_enabled": cfg["post_enabled"],
        "token_configured": bool(cfg["access_token"]),
        "token_valid": bool(
            cfg["access_token"]
            and (
                not _parse_datetime(cfg["expires_at"])
                or _parse_datetime(cfg["expires_at"]) > now
            )
        ),
        "token_expiration": cfg["expires_at"] or None,
        "user_id": cfg["user_id"],
        "username": cfg["username"],
        "today_posts": today,
        "daily_limit": cfg["daily_max"],
        "next_scheduled_slot": next_slot,
        "last_post": last,
        "last_api_error": last_error,
        "metrics_enabled": cfg["metrics_enabled"],
        "automatic_replies": False,
        "automatic_profile_changes": False,
    }
