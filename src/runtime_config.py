"""Typed runtime configuration and consistency audit.

The live ``.env`` remains the operator-owned source of truth.  This module
provides typed defaults, runtime coercion, and a read-only audit.  It never
writes ``.env``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "WEBHOOK")


@dataclass(frozen=True)
class ConfigSpec:
    name: str
    default: Any
    value_type: type
    used_by: str
    recommendation: str = ""


SPECS = (
    ConfigSpec("POST_ENABLED", False, bool, "src/post.py", "Phase Aでは現行値を維持"),
    ConfigSpec("THREADS_POST_ENABLED", False, bool, "src/threads_api.py", "定期公開の唯一の書込スイッチ"),
    ConfigSpec(
        "THREADS_AUTO_PUBLISH_TEXT",
        False,
        bool,
        "Threads API container parameter",
        "falseはコンテナ作成後に明示publishする安全モード",
    ),
    ConfigSpec("MONITOR_INTERVAL_MINUTES", 60, int, "local_bot.py, src/post.py"),
    ConfigSpec("ACTIVE_HOURS", "5-23", str, "local_bot.py, src/post.py"),
    ConfigSpec("MAX_POSTS_PER_RUN", 1, int, "src/post.py", "Phase Aでは1を維持"),
    ConfigSpec("ORIGINAL_DAILY_POST_MAX", 8, int, "src/post.py", "Phase Aでは8を維持"),
    ConfigSpec("MAX_DAILY_AUTOMATED_POSTS", 10, int, "src/post.py", "Phase Aでは10を維持"),
    ConfigSpec("MIN_POST_INTERVAL_MINUTES", 60, int, "src/post.py", "Phase Aでは60を維持"),
    ConfigSpec("EMOJI_MODE", "selective", str, "src/post.py"),
    ConfigSpec("EMOJI_TARGET_RATIO", 0.25, float, "src/post.py"),
    ConfigSpec("EMOJI_MAX_PER_POST", 1, int, "src/post.py"),
    ConfigSpec("X_DAILY_ORIGINAL_TARGET_MIN", 10, int, "src/social_content_factory.py"),
    ConfigSpec("X_DAILY_ORIGINAL_TARGET_MAX", 14, int, "src/social_content_factory.py"),
    ConfigSpec("X_DAILY_ORIGINAL_HARD_MAX", 16, int, "src/social_content_factory.py"),
    ConfigSpec("X_DAILY_BREAKING_MAX", 2, int, "src/social_content_factory.py"),
    ConfigSpec("X_DAILY_REPLY_QUOTE_TARGET_MIN", 2, int, "src/social_content_factory.py"),
    ConfigSpec("X_DAILY_REPLY_QUOTE_TARGET_MAX", 5, int, "src/social_content_factory.py"),
    ConfigSpec("X_DAILY_REPLY_QUOTE_HARD_MAX", 6, int, "src/social_content_factory.py"),
    ConfigSpec("X_DAILY_TOTAL_ACTION_HARD_MAX", 24, int, "src/social_content_factory.py"),
    ConfigSpec("X_MIN_POST_INTERVAL_MINUTES", 30, int, "Phase B capability"),
    ConfigSpec("X_BREAKING_MIN_INTERVAL_MINUTES", 15, int, "Phase B capability"),
    ConfigSpec("X_SAME_TOPIC_DIFFERENT_ANGLE_COOLDOWN_MINUTES", 90, int, "src/social_content_factory.py"),
    ConfigSpec("X_SAME_CLAIM_COOLDOWN_HOURS", 6, int, "src/social_content_factory.py"),
    ConfigSpec("THREADS_DAILY_POST_TARGET_MIN", 4, int, "src/social_content_factory.py"),
    ConfigSpec("THREADS_DAILY_POST_TARGET_MAX", 6, int, "src/social_content_factory.py"),
    ConfigSpec("THREADS_DAILY_POST_HARD_MAX", 7, int, "src/social_content_factory.py"),
    ConfigSpec("THREADS_MIN_POST_INTERVAL_MINUTES", 90, int, "Phase B capability"),
    ConfigSpec("THREADS_SAME_TOPIC_DIFFERENT_ANGLE_COOLDOWN_MINUTES", 120, int, "src/social_content_factory.py"),
    ConfigSpec("THREADS_SAME_CLAIM_COOLDOWN_HOURS", 6, int, "src/social_content_factory.py"),
    ConfigSpec("X_THREAD_ENABLED", True, bool, "src/social_content_factory.py", "候補生成のみ"),
    ConfigSpec("X_THREAD_AUTO_PUBLISH_ENABLED", False, bool, "投稿安全弁", "Phase Aはfalse"),
    ConfigSpec("X_AUTO_LOW_RISK_REPLY_ENABLED", False, bool, "投稿安全弁", "Phase Aはfalse"),
    ConfigSpec("THREADS_AUTO_LOW_RISK_REPLY_ENABLED", False, bool, "投稿安全弁", "Phase Aはfalse"),
    ConfigSpec("X_AUTO_QUOTE_ENABLED", False, bool, "投稿安全弁", "Phase Aはfalse"),
    ConfigSpec("MAX_CONTENT_CANDIDATES_PER_RUN", 2, int, "src/social_content_factory.py"),
    ConfigSpec("CONTENT_CANDIDATE_DELAY_MINUTES", 30, int, "src/social_content_factory.py"),
    ConfigSpec("SOCIAL_CONTENT_FACTORY_PHASE", "A", str, "src/social_content_factory.py"),
    ConfigSpec("SOCIAL_GROWTH_OPENAI_MONTHLY_BUDGET_USD", 18.0, float, "budget simulation only"),
    ConfigSpec("VIDEO_SCRIPT_OPENAI_MONTHLY_BUDGET_USD", 7.0, float, "budget simulation only"),
    ConfigSpec("XAI_DISCOVERY_MONTHLY_BUDGET_USD", 30.0, float, "budget simulation only"),
    ConfigSpec("MEDIA_GENERATION_MONTHLY_BUDGET_USD", 8.0, float, "budget simulation only"),
)


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _coerce(value: Any, value_type: type, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        if value_type is bool:
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        return value_type(value)
    except (TypeError, ValueError):
        return default


def typed_config(env_path: Path | None = None) -> dict[str, Any]:
    raw = _parse_env(env_path or ROOT / ".env")
    return {
        spec.name: _coerce(
            os.environ.get(spec.name, raw.get(spec.name)),
            spec.value_type,
            spec.default,
        )
        for spec in SPECS
    }


def _display(name: str, value: Any) -> Any:
    if any(marker in name.upper() for marker in SECRET_MARKERS):
        return "(configured)" if value else "(empty)"
    return value


def audit(env_path: Path | None = None, *, persist: bool = True) -> dict[str, Any]:
    env_path = env_path or ROOT / ".env"
    raw = _parse_env(env_path)
    runtime = typed_config(env_path)
    rows = []
    for spec in SPECS:
        raw_value = raw.get(spec.name)
        effective = runtime[spec.name]
        mismatch = ""
        if raw_value not in (None, ""):
            parsed = _coerce(raw_value, spec.value_type, spec.default)
            if parsed != effective:
                mismatch = "process_environment_overrides_env"
        rows.append(
            {
                "name": spec.name,
                "env_value": _display(spec.name, raw_value if raw_value is not None else "(unset)"),
                "code_default": _display(spec.name, spec.default),
                "runtime_value": _display(spec.name, effective),
                "mismatch": mismatch,
                "used_by": spec.used_by,
                "recommendation": spec.recommendation,
            }
        )

    document_findings = []
    persona = (ROOT / "config" / "bot_persona.md").read_text(
        encoding="utf-8", errors="replace"
    )
    if re.search(r"絵文字.*2[〜~-]5", persona):
        document_findings.append(
            {
                "file": "config/bot_persona.md",
                "issue": "persona_emoji_policy_conflicts_with_runtime",
                "recommendation": "selective / maximum one emojiへ統一",
            }
        )
    readme = (ROOT / "README.md").read_text(encoding="utf-8", errors="replace")
    if "対象プラットフォームは **X のみ**" in readme:
        document_findings.append(
            {
                "file": "README.md",
                "issue": "readme_x_only_conflicts_with_threads",
                "recommendation": "X・Threads・候補生産エンジンを記載",
            }
        )
    if re.search(r"Gmail.*(?:転送|送信).*(?:実装|自動)", readme):
        document_findings.append(
            {
                "file": "README.md",
                "issue": "gmail_feature_not_implemented",
                "recommendation": "未実装と明記",
            }
        )

    result = {
        "source_of_truth": ".env -> typed configuration -> runtime status -> audit -> docs",
        "env_modified": False,
        "rows": rows,
        "document_findings": document_findings,
        "mismatch_count": sum(bool(row["mismatch"]) for row in rows)
        + len(document_findings),
    }
    if persist:
        try:
            from metrics_db import connect, init_db
            from social_content_factory import apply_migrations

            init_db()
            apply_migrations()
            with connect() as conn:
                conn.execute(
                    """INSERT INTO config_audit_results
                       (audited_at,config_json,mismatch_count,env_modified)
                       VALUES(datetime('now'),?,?,0)""",
                    (json.dumps(result, ensure_ascii=False), result["mismatch_count"]),
                )
                conn.commit()
        except Exception:
            pass
    return result
