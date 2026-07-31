"""Command adapter used by local_bot.py."""

from __future__ import annotations

import json
from pathlib import Path

from .workflow import ShortVideoFactory


def run(action: str, **kwargs) -> int:
    db = Path(kwargs.pop("db")) if kwargs.get("db") else None
    factory = ShortVideoFactory(db)
    aliases = {
        "audio-generate": "audio",
        "caption-generate": "captions",
    }
    action = aliases.get(action, action)
    actions = {
        "status": factory.status,
        "candidates": lambda: factory.candidates(kwargs.get("limit", 20)),
        "project-create": lambda: factory.project_create(
            kwargs["topic_id"], kwargs.get("angle") or
            "制度の変化を60秒で解説", kwargs.get("force", False)),
        "script-generate": lambda: factory.script_generate(kwargs["video_id"]),
        "script-check": lambda: factory.script_check(kwargs["video_id"]),
        "audio": lambda: factory.audio_generate(kwargs["video_id"]),
        "captions": lambda: factory.captions_generate(kwargs["video_id"]),
        "visual-plan": lambda: factory.visual_plan(kwargs["video_id"]),
        "render": lambda: factory.render(
            kwargs["video_id"], kwargs.get("dry_run", False)),
        "quality-check": lambda: factory.quality_check(kwargs["video_id"]),
        "platform-variants": lambda: factory.platform_variants(kwargs["video_id"]),
        "publish-plan": lambda: factory.publish_plan(kwargs["video_id"]),
        "queue": lambda: factory.queue(
            kwargs["video_id"], tuple(kwargs.get("platforms") or ("x", "threads")),
            kwargs.get("scheduled_at") or ""),
        "publish": lambda: factory.publish(
            kwargs["video_id"], kwargs["platform"], kwargs.get("confirm", False),
            kwargs.get("dry_run", True), kwargs.get("public_url") or ""),
        "metrics-sync": lambda: factory.metrics_sync(
            kwargs.get("video_id") or "", kwargs.get("dry_run", True)),
        "queue-run": lambda: factory.run_queue(
            kwargs.get("live", False), kwargs.get("limit", 10)),
        "scheduled-run": lambda: factory.scheduled_run(
            kwargs.get("live", False)),
        "experiment-report": factory.experiment_report,
        "daily-report": lambda: factory.report(False),
        "weekly-report": lambda: factory.report(True),
        "full-cycle": lambda: factory.full_cycle(
            kwargs["topic_id"], kwargs.get("dry_run", True)),
        "emergency-stop": factory.emergency_stop,
        "emergency-resume": lambda: factory.emergency_resume(
            kwargs.get("confirm", False)),
    }
    try:
        result = actions[action]()
    except Exception as exc:
        result = {
            "status": "blocked",
            "error_type": type(exc).__name__,
            "reason": str(exc).strip("'")[:300],
            "external_writes": 0,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") not in {"failed", "blocked"} else 1
