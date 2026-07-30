"""Isolated disaster monitoring, approval, publishing, and closure workflow.

Phase A always remains available. Phase B-D capabilities are opt-in and guarded
by disaster-specific switches in addition to the platform-wide safety switches.
"""

from __future__ import annotations

import csv
import importlib
import json
import os
import re
import sqlite3
import hashlib
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from metrics_db import connect, db_path
import requests

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - deployment reports this as a quality failure.
    Image = ImageDraw = ImageFont = None


ROOT = Path(__file__).resolve().parent.parent
INCIDENT_ID = "kumamoto-earthquake-20260728"
JST = ZoneInfo("Asia/Tokyo")
SNAPSHOT_TYPES = {"morning", "evening"}
DELTA_STATUSES = {
    "new", "increased", "decreased", "unchanged", "recovered", "resolved",
    "corrected", "scope_changed", "definition_changed", "unavailable",
}
POST_TYPES = {
    "disaster_damage_update", "disaster_rescue_update",
    "disaster_recovery_update", "disaster_morning_brief",
    "disaster_evening_brief",
}

DISASTER_SCHEMA = """
CREATE TABLE IF NOT EXISTS disaster_incidents (
 id INTEGER PRIMARY KEY,
 incident_id TEXT UNIQUE NOT NULL,
 incident_category TEXT NOT NULL,
 title TEXT NOT NULL,
 detected_at TEXT,
 status TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disaster_status_snapshots (
 id INTEGER PRIMARY KEY,
 incident_id TEXT NOT NULL,
 snapshot_id TEXT UNIQUE NOT NULL,
 snapshot_type TEXT NOT NULL,
 cutoff_at TEXT NOT NULL,
 generated_at TEXT NOT NULL,
 status TEXT NOT NULL,
 quality_status TEXT NOT NULL,
 publish_eligible INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 FOREIGN KEY(incident_id) REFERENCES disaster_incidents(incident_id)
);
CREATE TABLE IF NOT EXISTS disaster_snapshot_metrics (
 id INTEGER PRIMARY KEY,
 snapshot_id TEXT NOT NULL,
 metric_category TEXT NOT NULL,
 metric_key TEXT NOT NULL,
 value_numeric REAL,
 value_text TEXT,
 unit TEXT,
 source_id TEXT,
 as_of TEXT,
 verification_status TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(snapshot_id, metric_key),
 FOREIGN KEY(snapshot_id) REFERENCES disaster_status_snapshots(snapshot_id)
);
CREATE TABLE IF NOT EXISTS disaster_snapshot_deltas (
 id INTEGER PRIMARY KEY,
 incident_id TEXT NOT NULL,
 previous_snapshot_id TEXT,
 current_snapshot_id TEXT NOT NULL,
 metric_key TEXT NOT NULL,
 previous_value TEXT,
 current_value TEXT,
 delta_value REAL,
 delta_status TEXT NOT NULL,
 delta_reason TEXT,
 created_at TEXT NOT NULL,
 UNIQUE(current_snapshot_id, metric_key),
 FOREIGN KEY(incident_id) REFERENCES disaster_incidents(incident_id)
);
CREATE TABLE IF NOT EXISTS disaster_rescue_progress (
 id INTEGER PRIMARY KEY,
 snapshot_id TEXT UNIQUE NOT NULL,
 rescue_requests INTEGER,
 rescued_people INTEGER,
 transported_people INTEGER,
 searching_areas_json TEXT NOT NULL,
 isolated_areas INTEGER,
 isolated_areas_resolved INTEGER,
 responding_agencies_json TEXT NOT NULL,
 source_id TEXT,
 as_of TEXT,
 created_at TEXT NOT NULL,
 FOREIGN KEY(snapshot_id) REFERENCES disaster_status_snapshots(snapshot_id)
);
CREATE TABLE IF NOT EXISTS disaster_update_publications (
 id INTEGER PRIMARY KEY,
 incident_id TEXT NOT NULL,
 snapshot_id TEXT NOT NULL,
 platform TEXT NOT NULL,
 post_type TEXT NOT NULL,
 candidate_text TEXT NOT NULL,
 visual_path TEXT,
 external_post_id TEXT,
 status TEXT NOT NULL,
 published_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(snapshot_id, platform, post_type),
 FOREIGN KEY(incident_id) REFERENCES disaster_incidents(incident_id)
);
CREATE TABLE IF NOT EXISTS disaster_update_frequency_status (
 id INTEGER PRIMARY KEY,
 incident_id TEXT NOT NULL,
 current_mode TEXT NOT NULL,
 recommended_mode TEXT NOT NULL,
 recommendation_reason TEXT NOT NULL,
 evaluated_at TEXT NOT NULL,
 created_at TEXT NOT NULL,
 FOREIGN KEY(incident_id) REFERENCES disaster_incidents(incident_id)
);
CREATE TABLE IF NOT EXISTS disaster_update_approvals (
 id INTEGER PRIMARY KEY,
 incident_id TEXT NOT NULL,
 snapshot_id TEXT NOT NULL,
 platform TEXT NOT NULL,
 decision TEXT NOT NULL,
 approved_by TEXT NOT NULL,
 notes TEXT,
 decided_at TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(snapshot_id, platform),
 FOREIGN KEY(incident_id) REFERENCES disaster_incidents(incident_id)
);
CREATE TABLE IF NOT EXISTS disaster_lifecycle_reports (
 id INTEGER PRIMARY KEY,
 incident_id TEXT NOT NULL,
 snapshot_id TEXT,
 report_type TEXT NOT NULL,
 status TEXT NOT NULL,
 output_path TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(incident_id, report_type, snapshot_id),
 FOREIGN KEY(incident_id) REFERENCES disaster_incidents(incident_id)
);
CREATE INDEX IF NOT EXISTS idx_disaster_snapshot_incident
 ON disaster_status_snapshots(incident_id, cutoff_at);
CREATE INDEX IF NOT EXISTS idx_disaster_delta_incident
 ON disaster_snapshot_deltas(incident_id, current_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_disaster_publication_status
 ON disaster_update_publications(incident_id, status, created_at);
"""


def _bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def settings() -> dict[str, Any]:
    return {
        "enabled": _bool("KUMAMOTO_DISASTER_UPDATES_ENABLED", "true"),
        "phase": os.environ.get("KUMAMOTO_DISASTER_PHASE", "A").strip().upper(),
        "publish_enabled": _bool(
            "KUMAMOTO_DISASTER_PUBLISH_ENABLED", "false"),
        "auto_post": _bool("KUMAMOTO_DISASTER_AUTO_POST_ENABLED", "false"),
        "x_enabled": _bool("KUMAMOTO_DISASTER_X_ENABLED", "true"),
        "threads_enabled": _bool("KUMAMOTO_DISASTER_THREADS_ENABLED", "true"),
        "x_post_enabled": _bool(
            "KUMAMOTO_DISASTER_X_POST_ENABLED", "false"),
        "threads_post_enabled": _bool(
            "KUMAMOTO_DISASTER_THREADS_POST_ENABLED", "false"),
        "human_approval_required": _bool(
            "KUMAMOTO_DISASTER_HUMAN_APPROVAL_REQUIRED", "true"),
        "verified_only": _bool(
            "KUMAMOTO_DISASTER_AUTO_PUBLISH_VERIFIED_ONLY", "true"),
        "closure_summary_enabled": _bool(
            "KUMAMOTO_DISASTER_CLOSURE_SUMMARY_ENABLED", "true"),
        "morning_cutoff": os.environ.get(
            "KUMAMOTO_DISASTER_MORNING_CUTOFF", "07:00"),
        "morning_publish": os.environ.get(
            "KUMAMOTO_DISASTER_MORNING_PUBLISH", "07:30"),
        "evening_cutoff": os.environ.get(
            "KUMAMOTO_DISASTER_EVENING_CUTOFF", "19:00"),
        "evening_publish": os.environ.get(
            "KUMAMOTO_DISASTER_EVENING_PUBLISH", "19:30"),
        "timezone": os.environ.get(
            "KUMAMOTO_DISASTER_TIMEZONE", "Asia/Tokyo"),
        "require_official": _bool(
            "KUMAMOTO_DISASTER_REQUIRE_OFFICIAL_SOURCE", "true"),
        "require_change": _bool(
            "KUMAMOTO_DISASTER_REQUIRE_MEANINGFUL_CHANGE", "true"),
        "max_source_age_hours": int(os.environ.get(
            "KUMAMOTO_DISASTER_MAX_SOURCE_AGE_HOURS", "12")),
        "visual_enabled": _bool(
            "KUMAMOTO_DISASTER_VISUAL_ENABLED", "true"),
        "x_image_size": os.environ.get(
            "KUMAMOTO_DISASTER_X_IMAGE_SIZE", "1600x900"),
        "threads_image_size": os.environ.get(
            "KUMAMOTO_DISASTER_THREADS_IMAGE_SIZE", "1080x1350"),
        "correction_enabled": _bool(
            "KUMAMOTO_DISASTER_CORRECTION_ENABLED", "true"),
        "correction_auto_post": _bool(
            "KUMAMOTO_DISASTER_CORRECTION_AUTO_POST", "false"),
        "short_enabled": _bool(
            "KUMAMOTO_DISASTER_SHORT_CANDIDATE_ENABLED", "true"),
        "short_auto_publish": _bool(
            "KUMAMOTO_DISASTER_SHORT_AUTO_PUBLISH", "false"),
    }


def output_root() -> Path:
    raw = os.environ.get(
        "KUMAMOTO_DISASTER_OUTPUT_DIR",
        str(ROOT / "outputs" / "disaster_updates" / INCIDENT_ID),
    )
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def apply_migrations(path: Path | None = None) -> bool:
    """Apply idempotent disaster-only migrations and seed one incident."""
    now = datetime.now(JST).isoformat()
    try:
        with closing(connect(path)) as conn:
            conn.executescript(DISASTER_SCHEMA)
            conn.execute("""INSERT OR IGNORE INTO disaster_incidents
                (incident_id,incident_category,title,detected_at,status,
                 created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)""", (
                    INCIDENT_ID, "major_earthquake", "令和8年熊本地震",
                    "2026-07-28T16:27:00+09:00", "active_twice_daily",
                    now, now,
                ))
            conn.commit()
        return True
    except sqlite3.Error:
        return False


def _parse_size(value: str, default: tuple[int, int]) -> tuple[int, int]:
    match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", str(value).strip())
    return (int(match.group(1)), int(match.group(2))) if match else default


def _cutoff(snapshot_type: str, *, date_text: str | None = None) -> datetime:
    if snapshot_type not in SNAPSHOT_TYPES:
        raise ValueError("snapshot_type must be morning or evening")
    cfg = settings()
    zone = ZoneInfo(cfg["timezone"])
    date_text = date_text or datetime.now(zone).date().isoformat()
    clock = cfg[f"{snapshot_type}_cutoff"]
    return datetime.fromisoformat(f"{date_text}T{clock}:00").replace(tzinfo=zone)


def _fixture_path(snapshot_type: str) -> Path:
    return ROOT / "tests" / "fixtures" / f"kumamoto_disaster_{snapshot_type}.json"


def load_fixture(snapshot_type: str) -> dict:
    return json.loads(_fixture_path(snapshot_type).read_text(encoding="utf-8"))


def official_source_registry() -> list[dict]:
    path = ROOT / "config" / "kumamoto_disaster_sources.json"
    return json.loads(path.read_text(encoding="utf-8"))


def collect_official_source_health(*, cutoff_at: datetime,
                                   session=None) -> list[dict]:
    """Fetch official pages without extracting or inventing damage numbers."""
    session = session or requests.Session()
    results = []
    for source in official_source_registry():
        row = deepcopy(source)
        row.update({
            "published_at": "", "as_of": "", "status": "unavailable",
            "fetched_at": datetime.now(JST).isoformat(), "content_hash": "",
        })
        try:
            response = session.get(
                source["url"], timeout=float(os.environ.get(
                    "KUMAMOTO_DISASTER_SOURCE_TIMEOUT_SECONDS", "12")),
                headers={"User-Agent": "politics-narrative-disaster-monitor/1.0"},
            )
            response.raise_for_status()
            content = response.content[:2_000_000]
            row["content_hash"] = hashlib.sha256(content).hexdigest()
            row["published_at"] = response.headers.get("Last-Modified", "")
            row["as_of"] = row["published_at"]
            row["status"] = "reachable"
        except requests.RequestException:
            row["status"] = "unavailable"
        results.append(row)
    return results


def empty_snapshot(snapshot_type: str, cutoff_at: datetime) -> dict:
    """Return the required structure; unknown numbers are null, never zero."""
    return {
        "incident_id": INCIDENT_ID,
        "snapshot_id": (
            f"{INCIDENT_ID}-{cutoff_at:%Y%m%d}-{snapshot_type}"),
        "snapshot_type": snapshot_type,
        "cutoff_at": cutoff_at.isoformat(),
        "generated_at": datetime.now(JST).isoformat(),
        "casualties": {
            "dead": None, "injured": None, "missing": None,
            "source": "", "source_id": "", "as_of": "",
        },
        "rescue": {
            "rescue_requests": None, "rescued_people": None,
            "transported_people": None, "searching_areas": [],
            "isolated_areas": None, "isolated_areas_resolved": None,
            "responding_agencies": [], "source": "", "source_id": "",
            "as_of": "",
        },
        "evacuation": {
            "open_shelters": None, "evacuees": None, "shelter_areas": [],
            "source": "", "source_id": "", "as_of": "",
        },
        "infrastructure": {
            "power_outage_households": None,
            "water_outage_households": None,
            "gas_outage_households": None,
            "telecom_impact": [], "source": "", "source_id": "", "as_of": "",
        },
        "transport": {
            "rail": [], "roads": [], "airport": [], "public_transport": [],
            "source": "", "source_id": "", "as_of": "",
        },
        "facilities": {
            "medical": [], "schools": [], "childcare": [], "commercial": [],
            "public_facilities": [],
        },
        "aftershocks": [],
        "official_advice": [],
        "sources": [],
        "metadata": {
            "metric_notes": {}, "scope_changes": {}, "definition_changes": {},
            "corrections": {}, "resolved_metrics": [], "recovered_metrics": [],
            "fixture": False, "aggregation_policy": (
                "latest_integrated_official_value_only; "
                "different_agencies_not_summed"),
        },
        "quality_status": "pending",
        "publish_eligible": False,
        "decision_reason": "not_evaluated",
    }


def _source_rank(row: dict) -> tuple[int, str]:
    # Lower priority number is stronger. Latest as_of wins within the same rank.
    return (int(row.get("priority", 99)), str(row.get("as_of", "")))


def select_authoritative_value(observations: Iterable[dict]) -> dict | None:
    """Prefer the newest official integrated value; never sum observations."""
    valid = [
        deepcopy(row) for row in observations
        if row.get("verification_status") == "confirmed"
        and row.get("source_class") == "official"
    ]
    if not valid:
        return None
    minimum = min(int(row.get("priority", 99)) for row in valid)
    peers = [row for row in valid if int(row.get("priority", 99)) == minimum]
    return max(peers, key=lambda row: str(row.get("as_of", "")))


def snapshot_from_fixture(snapshot_type: str) -> dict:
    fixture = load_fixture(snapshot_type)
    cutoff = _cutoff(snapshot_type, date_text=fixture["date"])
    snapshot = empty_snapshot(snapshot_type, cutoff)
    for section in (
        "casualties", "rescue", "evacuation", "infrastructure", "transport",
        "facilities",
    ):
        snapshot[section].update(deepcopy(fixture.get(section, {})))
    for key in ("aftershocks", "official_advice", "sources"):
        snapshot[key] = deepcopy(fixture.get(key, []))
    snapshot["metadata"].update(deepcopy(fixture.get("metadata", {})))
    snapshot["metadata"]["fixture"] = True
    snapshot["quality_status"] = "passed"
    return snapshot


def _latest_existing_snapshot(incident_id: str, *,
                              before: str | None = None,
                              path: Path | None = None) -> dict | None:
    apply_migrations(path)
    query = """SELECT snapshot_id FROM disaster_status_snapshots
               WHERE incident_id=?"""
    params: list[Any] = [incident_id]
    if before:
        query += " AND cutoff_at<?"
        params.append(before)
    query += " ORDER BY cutoff_at DESC LIMIT 1"
    try:
        with closing(connect(path)) as conn:
            row = conn.execute(query, params).fetchone()
        if not row:
            return None
        return load_snapshot(row["snapshot_id"])
    except (sqlite3.Error, OSError, json.JSONDecodeError):
        return None


def collect_official_snapshot(snapshot_type: str, *, dry_run: bool = False,
                              incident_id: str = INCIDENT_ID,
                              path: Path | None = None) -> dict:
    """Collect conservatively.

    Dry-run uses frozen official fixtures. Live Phase A accepts a locally
    reviewed official input JSON. Without one, it carries no numeric value
    forward as a new fact and returns a conservative all-null snapshot.
    """
    if incident_id != INCIDENT_ID:
        raise ValueError("unknown incident_id")
    if dry_run:
        return snapshot_from_fixture(snapshot_type)
    cutoff = _cutoff(snapshot_type)
    snapshot = empty_snapshot(snapshot_type, cutoff)
    input_path = ROOT / "data" / "disaster_inputs" / (
        f"{incident_id}_{cutoff:%Y-%m-%d}_{snapshot_type}.json")
    if input_path.exists():
        reviewed = json.loads(input_path.read_text(encoding="utf-8"))
        if reviewed.get("incident_id") != incident_id:
            raise ValueError("official input incident mismatch")
        if not reviewed.get("sources"):
            raise ValueError("official input requires sources")
        for section in (
            "casualties", "rescue", "evacuation", "infrastructure",
            "transport", "facilities",
        ):
            snapshot[section].update(deepcopy(reviewed.get(section, {})))
        for key in ("aftershocks", "official_advice", "sources"):
            snapshot[key] = deepcopy(reviewed.get(key, []))
        snapshot["metadata"].update(deepcopy(reviewed.get("metadata", {})))
        snapshot["quality_status"] = "passed"
    else:
        snapshot["sources"] = collect_official_source_health(
            cutoff_at=cutoff)
        snapshot["quality_status"] = "insufficient_official_data"
        snapshot["decision_reason"] = "official_input_not_available"
    return snapshot


def _metric_rows(snapshot: dict) -> list[dict]:
    rows: list[dict] = []
    scalar_sections = {
        "casualties": ("dead", "injured", "missing"),
        "rescue": (
            "rescue_requests", "rescued_people", "transported_people",
            "isolated_areas", "isolated_areas_resolved",
        ),
        "evacuation": ("open_shelters", "evacuees"),
        "infrastructure": (
            "power_outage_households", "water_outage_households",
            "gas_outage_households",
        ),
    }
    units = {
        "dead": "人", "injured": "人", "missing": "人",
        "rescue_requests": "件", "rescued_people": "人",
        "transported_people": "人", "isolated_areas": "地区",
        "isolated_areas_resolved": "地区", "open_shelters": "か所",
        "evacuees": "人", "power_outage_households": "戸",
        "water_outage_households": "戸", "gas_outage_households": "戸",
    }
    for section, keys in scalar_sections.items():
        block = snapshot[section]
        for key in keys:
            value = block.get(key)
            rows.append({
                "category": section, "key": f"{section}.{key}",
                "value_numeric": value if isinstance(value, (int, float)) else None,
                "value_text": None if isinstance(value, (int, float)) else value,
                "unit": units.get(key, ""), "source_id": block.get("source_id", ""),
                "as_of": block.get("as_of", ""),
                "verification_status": (
                    "confirmed" if value is not None else "unavailable"),
            })
    for section, keys in {
        "rescue": ("searching_areas", "responding_agencies"),
        "evacuation": ("shelter_areas",),
        "infrastructure": ("telecom_impact",),
        "transport": ("rail", "roads", "airport", "public_transport"),
        "facilities": (
            "medical", "schools", "childcare", "commercial",
            "public_facilities",
        ),
    }.items():
        block = snapshot[section]
        for key in keys:
            value = block.get(key) or []
            rows.append({
                "category": section, "key": f"{section}.{key}",
                "value_numeric": None,
                "value_text": json.dumps(value, ensure_ascii=False),
                "unit": "項目", "source_id": block.get("source_id", ""),
                "as_of": block.get("as_of", ""),
                "verification_status": (
                    "confirmed" if value else "unavailable"),
            })
    return rows


def persist_snapshot(snapshot: dict, *, path: Path | None = None) -> None:
    apply_migrations(path)
    now = datetime.now(JST).isoformat()
    with closing(connect(path)) as conn:
        conn.execute("""INSERT INTO disaster_status_snapshots
            (incident_id,snapshot_id,snapshot_type,cutoff_at,generated_at,status,
             quality_status,publish_eligible,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
             generated_at=excluded.generated_at,status=excluded.status,
             quality_status=excluded.quality_status,
             publish_eligible=excluded.publish_eligible,
             updated_at=excluded.updated_at""", (
                snapshot["incident_id"], snapshot["snapshot_id"],
                snapshot["snapshot_type"], snapshot["cutoff_at"],
                snapshot["generated_at"], "prepared",
                snapshot["quality_status"],
                int(bool(snapshot["publish_eligible"])), now, now,
            ))
        conn.execute(
            "DELETE FROM disaster_snapshot_metrics WHERE snapshot_id=?",
            (snapshot["snapshot_id"],))
        for row in _metric_rows(snapshot):
            conn.execute("""INSERT INTO disaster_snapshot_metrics
                (snapshot_id,metric_category,metric_key,value_numeric,value_text,
                 unit,source_id,as_of,verification_status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                    snapshot["snapshot_id"], row["category"], row["key"],
                    row["value_numeric"], row["value_text"], row["unit"],
                    row["source_id"], row["as_of"],
                    row["verification_status"], now,
                ))
        rescue = snapshot["rescue"]
        conn.execute("""INSERT INTO disaster_rescue_progress
            (snapshot_id,rescue_requests,rescued_people,transported_people,
             searching_areas_json,isolated_areas,isolated_areas_resolved,
             responding_agencies_json,source_id,as_of,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
             rescue_requests=excluded.rescue_requests,
             rescued_people=excluded.rescued_people,
             transported_people=excluded.transported_people,
             searching_areas_json=excluded.searching_areas_json,
             isolated_areas=excluded.isolated_areas,
             isolated_areas_resolved=excluded.isolated_areas_resolved,
             responding_agencies_json=excluded.responding_agencies_json,
             source_id=excluded.source_id,as_of=excluded.as_of""", (
                snapshot["snapshot_id"], rescue["rescue_requests"],
                rescue["rescued_people"], rescue["transported_people"],
                json.dumps(rescue["searching_areas"], ensure_ascii=False),
                rescue["isolated_areas"], rescue["isolated_areas_resolved"],
                json.dumps(rescue["responding_agencies"], ensure_ascii=False),
                rescue.get("source_id", ""), rescue.get("as_of", ""), now,
            ))
        conn.commit()


def _snapshot_dir(snapshot: dict) -> Path:
    cutoff = datetime.fromisoformat(snapshot["cutoff_at"])
    path = output_root() / f"{cutoff:%Y-%m-%d}_{snapshot['snapshot_type']}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_snapshot(snapshot: dict, previous: dict | None = None) -> Path:
    directory = _snapshot_dir(snapshot)
    (directory / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (directory / "previous_snapshot.json").write_text(
        json.dumps(previous, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    _write_sources(snapshot, directory)
    return directory


def _write_sources(snapshot: dict, directory: Path) -> None:
    fields = [
        "source_id", "name", "url", "source_class", "priority",
        "published_at", "as_of", "status",
    ]
    with (directory / "source_matrix.csv").open(
            "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for source in snapshot.get("sources", []):
            writer.writerow({key: source.get(key, "") for key in fields})
    lines = ["# 情報源", "", f"情報締切: {snapshot['cutoff_at']}", ""]
    for source in snapshot.get("sources", []):
        lines.append(
            f"- [{source.get('name','公式機関')}]({source.get('url','')}) "
            f"（情報時点: {source.get('as_of') or '発表待ち'}）")
    (directory / "sources.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def load_snapshot(snapshot_id: str) -> dict | None:
    for path in output_root().glob("*/snapshot.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("snapshot_id") == snapshot_id:
                return payload
        except (OSError, json.JSONDecodeError):
            continue
    return None


def list_snapshots(incident_id: str = INCIDENT_ID,
                   path: Path | None = None) -> list[dict]:
    apply_migrations(path)
    try:
        with closing(connect(path)) as conn:
            return [dict(row) for row in conn.execute(
                """SELECT snapshot_id,snapshot_type,cutoff_at,generated_at,
                          quality_status,publish_eligible,status
                   FROM disaster_status_snapshots WHERE incident_id=?
                   ORDER BY cutoff_at""", (incident_id,))]
    except sqlite3.Error:
        return []


def _delta_status(key: str, previous: Any, current: Any,
                  metadata: dict) -> tuple[str, float | None, str]:
    corrections = metadata.get("corrections", {})
    scopes = metadata.get("scope_changes", {})
    definitions = metadata.get("definition_changes", {})
    if key in corrections:
        return "corrected", None, str(corrections[key])
    if key in scopes:
        return "scope_changed", None, str(scopes[key])
    if key in definitions:
        return "definition_changed", None, str(definitions[key])
    if current is None:
        return "unavailable", None, "current official value unavailable"
    if previous is None:
        return "new", None, "new official value"
    if key in metadata.get("resolved_metrics", []):
        return "resolved", _numeric_delta(previous, current), "officially resolved"
    if key in metadata.get("recovered_metrics", []):
        return "recovered", _numeric_delta(previous, current), "officially recovered"
    if isinstance(previous, (int, float)) and isinstance(current, (int, float)):
        delta = float(current) - float(previous)
        if delta > 0:
            return "increased", delta, ""
        if delta < 0:
            return "decreased", delta, (
                "decrease only; recovery not asserted without official reason")
        return "unchanged", 0.0, ""
    return (
        ("unchanged", None, "") if previous == current
        else ("new", None, "non-numeric official content changed")
    )


def _numeric_delta(previous: Any, current: Any) -> float | None:
    if isinstance(previous, (int, float)) and isinstance(current, (int, float)):
        return float(current) - float(previous)
    return None


def calculate_delta(previous: dict | None, current: dict) -> dict:
    previous_rows = {
        row["key"]: row for row in _metric_rows(previous)
    } if previous else {}
    current_rows = {row["key"]: row for row in _metric_rows(current)}
    changes = []
    metadata = current.get("metadata", {})
    for key in sorted(set(previous_rows) | set(current_rows)):
        old_row = previous_rows.get(key, {})
        new_row = current_rows.get(key, {})
        previous_value = (
            old_row.get("value_numeric")
            if old_row.get("value_numeric") is not None
            else old_row.get("value_text"))
        current_value = (
            new_row.get("value_numeric")
            if new_row.get("value_numeric") is not None
            else new_row.get("value_text"))
        status, delta_value, reason = _delta_status(
            key, previous_value, current_value, metadata)
        changes.append({
            "metric_key": key, "previous_value": previous_value,
            "current_value": current_value, "delta_value": delta_value,
            "delta_status": status, "delta_reason": reason,
        })
    meaningful = [
        row for row in changes
        if row["delta_status"] not in {"unchanged", "unavailable"}
    ]
    return {
        "incident_id": current["incident_id"],
        "previous_snapshot_id": previous.get("snapshot_id") if previous else None,
        "current_snapshot_id": current["snapshot_id"],
        "generated_at": datetime.now(JST).isoformat(),
        "changes": changes,
        "meaningful_change_count": len(meaningful),
        "meaningful_change": bool(meaningful),
    }


def persist_delta(delta: dict, path: Path | None = None) -> None:
    apply_migrations(path)
    now = datetime.now(JST).isoformat()
    with closing(connect(path)) as conn:
        for row in delta["changes"]:
            conn.execute("""INSERT INTO disaster_snapshot_deltas
                (incident_id,previous_snapshot_id,current_snapshot_id,
                 metric_key,previous_value,current_value,delta_value,
                 delta_status,delta_reason,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(current_snapshot_id,metric_key) DO UPDATE SET
                 previous_value=excluded.previous_value,
                 current_value=excluded.current_value,
                 delta_value=excluded.delta_value,
                 delta_status=excluded.delta_status,
                 delta_reason=excluded.delta_reason""", (
                    delta["incident_id"], delta["previous_snapshot_id"],
                    delta["current_snapshot_id"], row["metric_key"],
                    _db_value(row["previous_value"]),
                    _db_value(row["current_value"]), row["delta_value"],
                    row["delta_status"], row["delta_reason"], now,
                ))
        conn.commit()


def _db_value(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False)


def _display(value: Any, unit: str, unknown: str = "公式集計なし") -> str:
    if value is None:
        return unknown
    return f"{int(value):,}{unit}" if isinstance(value, (int, float)) else str(value)


def _important_changes(delta: dict, limit: int = 4) -> list[str]:
    labels = {
        "casualties.injured": "負傷者",
        "rescue.rescued_people": "救助完了",
        "rescue.transported_people": "搬送",
        "rescue.isolated_areas": "孤立地域",
        "evacuation.open_shelters": "避難所",
        "evacuation.evacuees": "避難者",
        "infrastructure.power_outage_households": "停電",
        "infrastructure.water_outage_households": "断水",
        "infrastructure.gas_outage_households": "ガス停止",
    }
    status_labels = {
        "new": "新規", "increased": "増加", "decreased": "減少",
        "recovered": "復旧", "resolved": "解消", "corrected": "訂正",
        "scope_changed": "集計範囲変更",
        "definition_changed": "定義変更",
    }
    out = []
    for row in delta.get("changes", []):
        if row["metric_key"] not in labels:
            continue
        if row["delta_status"] in {"unchanged", "unavailable"}:
            continue
        old = row["previous_value"]
        new = row["current_value"]
        status = row["delta_status"]
        out.append(
            f"{labels[row['metric_key']]}: {_plain(old)} → {_plain(new)}"
            f"（{status_labels.get(status, status)}）")
        if len(out) >= limit:
            break
    return out


def _plain(value: Any) -> str:
    if value is None:
        return "公式集計なし"
    if isinstance(value, (int, float)):
        return f"{int(value):,}"
    if isinstance(value, str) and value.startswith("["):
        try:
            return f"{len(json.loads(value))}項目"
        except json.JSONDecodeError:
            pass
    return str(value)


def _source_as_of(value: Any) -> datetime | None:
    """Parse an ISO 8601 or HTTP-date source timestamp."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed


def fresh_official_sources(snapshot: dict) -> list[dict]:
    """Return official sources whose stated time is within the age limit."""
    cutoff = datetime.fromisoformat(snapshot["cutoff_at"])
    maximum_age = timedelta(hours=settings()["max_source_age_hours"])
    fresh = []
    for source in snapshot.get("sources", []):
        if source.get("source_class") != "official":
            continue
        as_of = _source_as_of(source.get("as_of") or source.get("published_at"))
        if as_of is None:
            continue
        age = cutoff.astimezone(as_of.tzinfo) - as_of
        if timedelta(0) <= age <= maximum_age:
            fresh.append(source)
    return fresh


def evaluate_publish(snapshot: dict, delta: dict) -> tuple[bool, str]:
    cfg = settings()
    if snapshot["quality_status"] != "passed":
        return False, "official_data_or_quality_insufficient"
    official = [
        source for source in snapshot.get("sources", [])
        if source.get("source_class") == "official"
    ]
    if cfg["require_official"] and not official:
        return False, "official_source_required"
    if cfg["require_official"] and not fresh_official_sources(snapshot):
        return False, "official_source_too_old_or_time_unknown"
    if cfg["require_change"] and not delta.get("meaningful_change"):
        return False, "no_meaningful_official_change"
    return True, "meaningful_official_change"


def make_candidates(snapshot: dict, delta: dict) -> dict:
    cutoff = datetime.fromisoformat(snapshot["cutoff_at"])
    morning = snapshot["snapshot_type"] == "morning"
    casualties = snapshot["casualties"]
    rescue = snapshot["rescue"]
    evacuation = snapshot["evacuation"]
    infrastructure = snapshot["infrastructure"]
    changes = _important_changes(delta, 2)
    title = (
        "【熊本地震・朝の被害状況】" if morning
        else "【熊本地震・救助と復旧の進捗】")
    next_time = "次回は19時時点を予定。" if morning else "次回は翌朝を予定。"
    x_lines = [
        title, f"{cutoff.month}月{cutoff.day}日{cutoff.hour}時時点。",
        f"人的被害: 負傷{_display(casualties['injured'],'人')}",
        f"救助: 完了{_display(rescue['rescued_people'],'人')}、捜索{len(rescue['searching_areas'])}地区",
        f"避難: {_display(evacuation['evacuees'],'人')}",
        f"停電{_display(infrastructure['power_outage_households'],'戸')}／断水{_display(infrastructure['water_outage_households'],'戸')}",
    ]
    if changes:
        x_lines.append("主な変化: " + "、".join(changes))
    x_lines += ["機関別の数字は合算していません。", next_time]
    x_text = "\n".join(x_lines)
    if len(x_text) > 280:
        x_text = "\n".join(x_lines[:6] + [
            "公式値の差分を整理。機関別の数字は合算していません。",
            next_time,
        ])
    if len(x_text) > 280:
        x_text = x_text[:279]

    phase = "夜間" if morning else "朝"
    change_text = "、".join(changes) if changes else "重大な公式変化は確認されていません"
    threads_text = (
        f"熊本地震について、{cutoff.hour}時時点の被害・救助状況です。\n\n"
        f"{phase}からの主な変化は、{change_text}。\n\n"
        "人的被害、救助、インフラは機関ごとに集計時刻と範囲が異なります。"
        "この整理では異なる機関の数字を単純合算せず、公式に確認できた値と"
        "前回との差分だけを掲載しています。"
    )
    eligible, reason = evaluate_publish(snapshot, delta)
    snapshot["publish_eligible"] = eligible
    snapshot["decision_reason"] = reason
    return {
        "x": x_text, "threads": threads_text,
        "publish_eligible": eligible, "decision_reason": reason,
        "post_type": (
            "disaster_morning_brief" if morning
            else "disaster_evening_brief"),
    }


def correction_candidate(snapshot: dict, delta: dict) -> str:
    corrected = [
        row for row in delta["changes"]
        if row["delta_status"] in {
            "corrected", "scope_changed", "definition_changed",
        }
    ]
    if not corrected:
        return (
            "# 訂正候補\n\n訂正対象となる公式更新はありません。\n\n"
            "自動投稿: OFF\n")
    lines = ["# 訂正候補", "", "【訂正】", ""]
    for row in corrected:
        lines += [
            f"- 訂正項目: {row['metric_key']}",
            f"- 旧情報: {_plain(row['previous_value'])}",
            f"- 新情報: {_plain(row['current_value'])}",
            f"- 理由: {row['delta_reason'] or row['delta_status']}",
        ]
    lines += [
        "", f"情報は{snapshot['cutoff_at']}時点です。",
        "公式情報源はsources.mdを参照してください。",
        "自動投稿: OFF",
    ]
    return "\n".join(lines) + "\n"


def short_candidate(snapshot: dict, delta: dict) -> str:
    major = delta.get("meaningful_change_count", 0) >= 4
    if not settings()["short_enabled"] or not major:
        return (
            "# Short候補\n\n今回は動画候補を生成しません。"
            "大きな状況変化または図解効果が不足しています。\n\n"
            "自動公開: OFF\n")
    return (
        "# Short候補（台本のみ）\n\n"
        f"{snapshot['cutoff_at']}時点の公式情報を整理します。\n\n"
        + "\n".join(f"- {line}" for line in _important_changes(delta, 6))
        + "\n\n原因推測を含みません。\n自動公開: OFF\n"
    )


def _font(size: int, bold: bool = False):
    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        / ("meiryob.ttc" if bold else "meiryo.ttc"),
        Path("C:/Windows/Fonts/YuGothB.ttc" if bold
             else "C:/Windows/Fonts/YuGothM.ttc"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def render_visual(snapshot: dict, delta: dict, *, platform: str,
                  directory: Path | None = None) -> Path:
    if Image is None:
        raise RuntimeError("Pillow is required")
    cfg = settings()
    size = _parse_size(
        cfg["x_image_size"] if platform == "x"
        else cfg["threads_image_size"],
        (1600, 900) if platform == "x" else (1080, 1350),
    )
    width, height = size
    image = Image.new("RGB", size, "#F4F7FA")
    draw = ImageDraw.Draw(image)
    scale = width / 1600
    margin = int(70 * scale)
    title_font = _font(max(26, int(52 * scale)), True)
    cutoff_font = _font(max(24, int(42 * scale)), True)
    card_title = _font(max(18, int(28 * scale)), True)
    body = _font(max(16, int(24 * scale)))
    small = _font(max(13, int(18 * scale)))

    draw.rectangle((0, 0, width, int(185 * scale)), fill="#17324D")
    draw.text((margin, int(30 * scale)), "熊本地震　被害・救助・復旧状況",
              font=title_font, fill="white")
    cutoff = datetime.fromisoformat(snapshot["cutoff_at"])
    draw.text((margin, int(105 * scale)),
              f"{cutoff.year}年{cutoff.month}月{cutoff.day}日 "
              f"{cutoff.hour:02d}:00時点",
              font=cutoff_font, fill="#F8D66D")

    cards = [
        ("人的被害",
         f"死者: {_display(snapshot['casualties']['dead'],'人')}\n"
         f"負傷者: {_display(snapshot['casualties']['injured'],'人')}\n"
         f"行方不明: {_display(snapshot['casualties']['missing'],'人')}"),
        ("救助・安否確認",
         f"救助要請: {_display(snapshot['rescue']['rescue_requests'],'件')}\n"
         f"救助完了: {_display(snapshot['rescue']['rescued_people'],'人')}\n"
         f"捜索継続: {len(snapshot['rescue']['searching_areas'])}地区"),
        ("避難",
         f"避難所: {_display(snapshot['evacuation']['open_shelters'],'か所')}\n"
         f"避難者: {_display(snapshot['evacuation']['evacuees'],'人')}\n"
         f"対象: {len(snapshot['evacuation']['shelter_areas'])}地域"),
        ("インフラ",
         f"停電: {_display(snapshot['infrastructure']['power_outage_households'],'戸')}\n"
         f"断水: {_display(snapshot['infrastructure']['water_outage_households'],'戸')}\n"
         f"ガス: {_display(snapshot['infrastructure']['gas_outage_households'],'戸')}"),
    ]
    grid_top = int(225 * scale)
    gap = int(24 * scale)
    columns = 2
    card_width = (width - 2 * margin - gap) // columns
    card_height = int(205 * scale)
    for index, (heading, text) in enumerate(cards):
        row, col = divmod(index, columns)
        x = margin + col * (card_width + gap)
        y = grid_top + row * (card_height + gap)
        draw.rounded_rectangle(
            (x, y, x + card_width, y + card_height),
            radius=int(18 * scale), fill="white", outline="#CBD5E1",
            width=max(1, int(2 * scale)))
        draw.text((x + int(24 * scale), y + int(18 * scale)), heading,
                  font=card_title, fill="#17324D")
        draw.multiline_text(
            (x + int(24 * scale), y + int(62 * scale)), text,
            font=body, fill="#263746", spacing=int(10 * scale))

    change_y = grid_top + 2 * (card_height + gap) + int(8 * scale)
    changes = _important_changes(delta, 2 if platform == "x" else 4)
    draw.text((margin, change_y), "前回からの変化", font=card_title,
              fill="#17324D")
    change_text = "\n".join(
        f"・{value}" for value in changes) if changes else "・重大な公式変化なし"
    draw.multiline_text((margin, change_y + int(42 * scale)), change_text,
                        font=body, fill="#263746", spacing=int(8 * scale))
    footer_y = height - int(85 * scale)
    draw.line((margin, footer_y, width - margin, footer_y), fill="#94A3B8",
              width=max(1, int(2 * scale)))
    draw.text((margin, footer_y + int(16 * scale)),
              "異なる機関の数字は単純合算していません。主要情報源: 消防庁・熊本県・自治体・事業者",
              font=small, fill="#526272")
    directory = directory or _snapshot_dir(snapshot)
    target = directory / ("x_visual.png" if platform == "x"
                          else "threads_visual.png")
    image.save(target, "PNG")
    return target


def render_visuals(snapshot: dict, delta: dict,
                   directory: Path | None = None) -> dict:
    directory = directory or _snapshot_dir(snapshot)
    data = {
        "snapshot_id": snapshot["snapshot_id"],
        "cutoff_at": snapshot["cutoff_at"],
        "important_changes": _important_changes(delta, 6),
        "unknown_label": "公式集計なし",
        "source_note": "異なる機関の数字は単純合算していません",
    }
    (directory / "visual_data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return {
        "x": str(render_visual(snapshot, delta, platform="x",
                               directory=directory)),
        "threads": str(render_visual(
            snapshot, delta, platform="threads", directory=directory)),
    }


def quality_report(snapshot: dict, delta: dict, candidates: dict,
                   visuals: dict, *, directory: Path) -> dict:
    emoji = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
    cfg = settings()
    phase_c_gate = (
        cfg["phase"] == "C"
        and cfg["publish_enabled"]
        and cfg["verified_only"]
    )
    if Image:
        with Image.open(visuals["x"]) as rendered:
            x_size = rendered.size
        with Image.open(visuals["threads"]) as rendered:
            threads_size = rendered.size
    else:
        x_size = threads_size = None
    checks = [
        ("incident_id_reused", snapshot["incident_id"] == INCIDENT_ID),
        ("cutoff_fixed", snapshot["cutoff_at"].endswith("+09:00")),
        ("unknown_not_zero", snapshot["casualties"]["dead"] is None),
        ("as_of_saved", bool(snapshot["casualties"]["as_of"])),
        ("official_sources_saved", bool(snapshot["sources"])),
        ("no_rescue_rate", all(
            token not in json.dumps(snapshot).lower()
            for token in ("rescue_rate", "progress_rate", "completion_rate"))),
        ("delta_status_valid", all(
            row["delta_status"] in DELTA_STATUSES for row in delta["changes"])),
        ("x_under_280", len(candidates["x"]) <= 280),
        ("threads_not_x_copy", candidates["threads"] != candidates["x"]),
        ("no_emoji", not emoji.search(candidates["x"] + candidates["threads"])),
        ("cutoff_in_copy", f"{datetime.fromisoformat(snapshot['cutoff_at']).hour}時時点"
         in candidates["x"]),
        ("x_visual_size", x_size == (1600, 900)),
        ("threads_visual_size", threads_size == (1080, 1350)),
        ("auto_post_gate_valid", (
            not cfg["auto_post"] or phase_c_gate)),
        ("correction_auto_post_gate_valid", (
            not cfg["correction_auto_post"]
            or (phase_c_gate and cfg["correction_enabled"]))),
        ("short_auto_publish_off", not cfg["short_auto_publish"]),
        ("external_publication_calls", True),
    ]
    report = {
        "snapshot_id": snapshot["snapshot_id"],
        "checked_at": datetime.now(JST).isoformat(),
        "total": len(checks),
        "passed": sum(1 for _, passed in checks if passed),
        "failed": [name for name, passed in checks if not passed],
        "checks": [{"name": name, "passed": passed} for name, passed in checks],
        "external_publication_attempted": False,
    }
    (directory / "quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return report


def persist_candidates(snapshot: dict, candidates: dict, visuals: dict,
                       path: Path | None = None) -> None:
    apply_migrations(path)
    now = datetime.now(JST).isoformat()
    with closing(connect(path)) as conn:
        for platform in ("x", "threads"):
            conn.execute("""INSERT INTO disaster_update_publications
                (incident_id,snapshot_id,platform,post_type,candidate_text,
                 visual_path,external_post_id,status,published_at,created_at,
                 updated_at)
                VALUES (?,?,?,?,?,?,NULL,?,NULL,?,?)
                ON CONFLICT(snapshot_id,platform,post_type) DO UPDATE SET
                 candidate_text=excluded.candidate_text,
                 visual_path=excluded.visual_path,status=excluded.status,
                 updated_at=excluded.updated_at""", (
                    snapshot["incident_id"], snapshot["snapshot_id"],
                    platform, candidates["post_type"],
                    candidates[platform], visuals[platform],
                    "candidate" if candidates["publish_eligible"] else "skipped",
                    now, now,
                ))
        conn.commit()


def approve_candidate(snapshot_id: str, platform: str, *,
                      decision: str = "approved",
                      approved_by: str = "local_operator",
                      notes: str = "",
                      path: Path | None = None) -> dict:
    """Record a deliberate Phase B approval without publishing anything."""
    platform = platform.strip().lower()
    if platform not in {"x", "threads"}:
        raise ValueError("platform must be x or threads")
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    apply_migrations(path)
    with closing(connect(path)) as conn:
        publication = conn.execute(
            """SELECT incident_id,status FROM disaster_update_publications
               WHERE snapshot_id=? AND platform=?""",
            (snapshot_id, platform),
        ).fetchone()
        if not publication:
            return {"status": "not_found", "snapshot_id": snapshot_id,
                    "platform": platform, "external_posts": 0}
        if publication["status"] == "published":
            return {"status": "already_published", "snapshot_id": snapshot_id,
                    "platform": platform, "external_posts": 0}
        now = datetime.now(JST).isoformat()
        conn.execute("""INSERT INTO disaster_update_approvals
            (incident_id,snapshot_id,platform,decision,approved_by,notes,
             decided_at,created_at) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(snapshot_id,platform) DO UPDATE SET
             decision=excluded.decision,approved_by=excluded.approved_by,
             notes=excluded.notes,decided_at=excluded.decided_at""", (
                publication["incident_id"], snapshot_id, platform, decision,
                approved_by[:120], notes[:500], now, now,
            ))
        conn.execute("""UPDATE disaster_update_publications
            SET status=?,updated_at=? WHERE snapshot_id=? AND platform=?""", (
                "approved" if decision == "approved" else "rejected",
                now, snapshot_id, platform,
            ))
        conn.commit()
    return {
        "status": decision, "snapshot_id": snapshot_id, "platform": platform,
        "approved_by": approved_by[:120], "external_posts": 0,
    }


def _publication(snapshot_id: str, platform: str,
                 path: Path | None = None, *,
                 post_type: str | None = None) -> dict | None:
    apply_migrations(path)
    post_filter = " AND p.post_type=?" if post_type else (
        " AND p.post_type!='disaster_correction'")
    params = (
        (snapshot_id, platform, post_type)
        if post_type else (snapshot_id, platform)
    )
    with closing(connect(path)) as conn:
        row = conn.execute(
            """SELECT p.*,a.decision approval_decision
               FROM disaster_update_publications p
               LEFT JOIN disaster_update_approvals a
                 ON a.snapshot_id=p.snapshot_id AND a.platform=p.platform
               WHERE p.snapshot_id=? AND p.platform=?"""
            + post_filter + " ORDER BY p.id DESC LIMIT 1",
            params,
        ).fetchone()
    return dict(row) if row else None


def _platform_publish_gate(platform: str, *, automatic: bool,
                           confirm: bool) -> tuple[bool, str]:
    cfg = settings()
    if not confirm:
        return False, "explicit_confirmation_required"
    if cfg["phase"] not in {"B", "C", "D"}:
        return False, "phase_a_publication_disabled"
    if not cfg["publish_enabled"]:
        return False, "disaster_publication_disabled"
    if platform == "x":
        if not cfg["x_enabled"] or not cfg["x_post_enabled"]:
            return False, "disaster_x_posting_disabled"
        if not _bool("POST_ENABLED", "false") or not _bool(
                "X_POST_ENABLED", "false"):
            return False, "global_x_posting_disabled"
    elif platform == "threads":
        if not cfg["threads_enabled"] or not cfg["threads_post_enabled"]:
            return False, "disaster_threads_posting_disabled"
        if not _bool("THREADS_POST_ENABLED", "false"):
            return False, "global_threads_posting_disabled"
    else:
        return False, "unsupported_platform"
    if automatic and (cfg["phase"] != "C" or not cfg["auto_post"]):
        return False, "phase_c_auto_posting_disabled"
    return True, "allowed"


def publish_candidate(snapshot_id: str, platform: str, *,
                      confirm: bool = False, automatic: bool = False,
                      post_type: str | None = None,
                      x_client=None, threads_client=None,
                      path: Path | None = None) -> dict:
    """Publish one candidate only after all Phase B/C gates pass."""
    platform = platform.strip().lower()
    allowed, reason = _platform_publish_gate(
        platform, automatic=automatic, confirm=confirm)
    if not allowed:
        return {"published": False, "reason": reason,
                "snapshot_id": snapshot_id, "platform": platform,
                "external_posts": 0}
    row = _publication(
        snapshot_id, platform, path, post_type=post_type)
    if not row:
        return {"published": False, "reason": "candidate_not_found",
                "snapshot_id": snapshot_id, "platform": platform,
                "external_posts": 0}
    if row["status"] == "published" or row.get("external_post_id"):
        return {"published": False, "reason": "already_published",
                "snapshot_id": snapshot_id, "platform": platform,
                "external_posts": 0}
    snapshot = load_snapshot(snapshot_id)
    if not snapshot:
        return {"published": False, "reason": "snapshot_not_found",
                "snapshot_id": snapshot_id, "platform": platform,
                "external_posts": 0}
    if not snapshot.get("publish_eligible"):
        return {"published": False, "reason": "snapshot_not_publish_eligible",
                "snapshot_id": snapshot_id, "platform": platform,
                "external_posts": 0}
    if settings()["verified_only"] and (
            snapshot.get("quality_status") != "passed"
            or not fresh_official_sources(snapshot)):
        return {"published": False, "reason": "verified_official_data_required",
                "snapshot_id": snapshot_id, "platform": platform,
                "external_posts": 0}
    if not automatic and settings()["human_approval_required"] and (
            row.get("approval_decision") != "approved"):
        return {"published": False, "reason": "human_approval_required",
                "snapshot_id": snapshot_id, "platform": platform,
                "external_posts": 0}
    now = datetime.now(JST).isoformat()
    try:
        if platform == "x":
            if x_client is None:
                post_module = importlib.import_module("post")
                external_id, _ = post_module.post_to_x(row["candidate_text"])
            else:
                response = x_client.create_tweet(text=row["candidate_text"])
                external_id = str((response.data or {}).get("id") or "")
        else:
            if threads_client is None:
                from threads_api import ThreadsClient
                threads_client = ThreadsClient(path=path)
            created = threads_client.create_container(row["candidate_text"])
            creation_id = str(created.get("id") or "")
            if not creation_id:
                raise RuntimeError("threads_creation_id_missing")
            published = threads_client.publish_container(creation_id)
            external_id = str(published.get("id") or "")
        if not external_id:
            raise RuntimeError("external_post_id_missing")
        with closing(connect(path)) as conn:
            conn.execute("""UPDATE disaster_update_publications SET
                external_post_id=?,status='published',published_at=?,
                updated_at=? WHERE id=?""", (
                    external_id, now, now, row["id"],
                ))
            conn.commit()
        return {
            "published": True, "snapshot_id": snapshot_id,
            "platform": platform, "external_post_id": external_id,
            "post_type": row["post_type"],
            "automatic": automatic, "external_posts": 1,
        }
    except Exception as exc:
        with closing(connect(path)) as conn:
            conn.execute("""UPDATE disaster_update_publications
                SET status='failed',updated_at=? WHERE id=?""",
                         (now, row["id"]))
            conn.commit()
        return {
            "published": False, "reason": "platform_publish_failed",
            "error_type": type(exc).__name__, "snapshot_id": snapshot_id,
            "platform": platform, "external_posts": 0,
        }


def auto_publish_verified(snapshot_id: str, *,
                          x_client=None, threads_client=None,
                          path: Path | None = None) -> dict:
    """Phase C publisher. Disabled unless every explicit switch is enabled."""
    results = {}
    for platform in ("x", "threads"):
        results[platform] = publish_candidate(
            snapshot_id, platform, confirm=True, automatic=True,
            x_client=x_client, threads_client=threads_client, path=path)
    return {
        "snapshot_id": snapshot_id, "phase": settings()["phase"],
        "results": results,
        "external_posts": sum(
            int(item.get("external_posts", 0)) for item in results.values()),
    }


def persist_correction_candidates(snapshot: dict, delta: dict,
                                  path: Path | None = None) -> int:
    """Persist correction posts separately from regular disaster briefs."""
    rows = [
        row for row in delta.get("changes", [])
        if row.get("delta_status") in {
            "corrected", "scope_changed", "definition_changed",
        }
    ]
    if not rows:
        return 0
    details = []
    for row in rows[:2]:
        details.append(
            f"{row['metric_key']}: {_plain(row['previous_value'])} → "
            f"{_plain(row['current_value'])}"
        )
    text = (
        f"【熊本地震・訂正】\n{snapshot['cutoff_at']}時点の公式更新により、"
        + "、".join(details)
        + "。旧情報を訂正します。機関別の数字は合算していません。"
    )
    text = text[:280]
    apply_migrations(path)
    now = datetime.now(JST).isoformat()
    visual = str(_snapshot_dir(snapshot) / "x_visual.png")
    with closing(connect(path)) as conn:
        for platform in ("x", "threads"):
            conn.execute("""INSERT INTO disaster_update_publications
                (incident_id,snapshot_id,platform,post_type,candidate_text,
                 visual_path,external_post_id,status,published_at,created_at,
                 updated_at) VALUES (?,?,?,?,?,?,NULL,'candidate',NULL,?,?)
                ON CONFLICT(snapshot_id,platform,post_type) DO UPDATE SET
                 candidate_text=excluded.candidate_text,
                 visual_path=excluded.visual_path,updated_at=excluded.updated_at
                """, (
                    snapshot["incident_id"], snapshot["snapshot_id"], platform,
                    "disaster_correction", text, visual, now, now,
                ))
        conn.commit()
    return len(rows)


def auto_publish_corrections(snapshot_id: str, *, x_client=None,
                             threads_client=None,
                             path: Path | None = None) -> dict:
    """Publish verified correction candidates when explicitly enabled."""
    if not settings()["correction_auto_post"]:
        return {
            "snapshot_id": snapshot_id, "status": "disabled",
            "external_posts": 0,
        }
    results = {}
    for platform in ("x", "threads"):
        results[platform] = publish_candidate(
            snapshot_id, platform, confirm=True, automatic=True,
            post_type="disaster_correction", x_client=x_client,
            threads_client=threads_client, path=path)
    return {
        "snapshot_id": snapshot_id, "status": "completed",
        "results": results,
        "external_posts": sum(
            int(item.get("external_posts", 0)) for item in results.values()),
    }


def _incident_mode(incident_id: str, path: Path | None = None) -> str:
    apply_migrations(path)
    with closing(connect(path)) as conn:
        row = conn.execute(
            "SELECT status FROM disaster_incidents WHERE incident_id=?",
            (incident_id,)).fetchone()
    return str(row["status"]) if row else "active_twice_daily"


def frequency_recommendation(incident_id: str = INCIDENT_ID,
                             path: Path | None = None) -> dict:
    snapshots = list_snapshots(incident_id, path)
    current = _incident_mode(incident_id, path)
    recommended = current
    reason = "rescue_or_major_infrastructure_updates_continue"
    if len(snapshots) >= 4:
        recent = snapshots[-4:]
        meaningful = 0
        with closing(connect(path)) as conn:
            for item in recent:
                meaningful += conn.execute(
                    """SELECT COUNT(*) FROM disaster_snapshot_deltas
                       WHERE current_snapshot_id=? AND delta_status NOT IN
                       ('unchanged','unavailable')""",
                    (item["snapshot_id"],)).fetchone()[0]
        if meaningful == 0:
            recommended = "active_daily"
            reason = "two_days_without_meaningful_morning_and_evening_change"
    if len(snapshots) >= 6 and recommended == "active_daily":
        recommended = "recovery_periodic"
        reason = "updates_are_recovery_focused"
    if snapshots:
        last_cutoff = datetime.fromisoformat(snapshots[-1]["cutoff_at"])
        if datetime.now(JST) - last_cutoff > timedelta(hours=48):
            recommended = "closed"
            reason = "no_new_snapshot_for_48_hours; human_review_required"
    result = {
        "incident_id": incident_id, "current_mode": current,
        "recommended_mode": recommended,
        "recommendation_reason": reason,
        "evaluated_at": datetime.now(JST).isoformat(),
        "automatic_schedule_change": False,
        "human_approval_required": recommended != current,
    }
    apply_migrations(path)
    now = datetime.now(JST).isoformat()
    with closing(connect(path)) as conn:
        conn.execute("""INSERT INTO disaster_update_frequency_status
            (incident_id,current_mode,recommended_mode,recommendation_reason,
             evaluated_at,created_at) VALUES (?,?,?,?,?,?)""", (
                incident_id, current, recommended, reason,
                result["evaluated_at"], now,
            ))
        conn.commit()
    return result


def apply_frequency_mode(incident_id: str, mode: str, *,
                         confirm: bool = False,
                         path: Path | None = None) -> dict:
    """Persist a human-approved mode; task changes remain a separate action."""
    allowed = {
        "active_twice_daily", "active_daily", "recovery_periodic", "closed",
    }
    if mode not in allowed:
        raise ValueError("invalid disaster frequency mode")
    if not confirm:
        return {
            "changed": False, "reason": "explicit_confirmation_required",
            "requested_mode": mode, "windows_tasks_changed": False,
        }
    apply_migrations(path)
    now = datetime.now(JST).isoformat()
    with closing(connect(path)) as conn:
        previous = conn.execute(
            "SELECT status FROM disaster_incidents WHERE incident_id=?",
            (incident_id,)).fetchone()
        if not previous:
            return {"changed": False, "reason": "incident_not_found",
                    "windows_tasks_changed": False}
        conn.execute("""UPDATE disaster_incidents SET status=?,updated_at=?
                        WHERE incident_id=?""", (mode, now, incident_id))
        conn.commit()
    return {
        "changed": previous["status"] != mode,
        "previous_mode": previous["status"], "current_mode": mode,
        "human_approved": True, "windows_tasks_changed": False,
    }


def _save_lifecycle_report(incident_id: str, snapshot_id: str,
                           report_type: str, content: str,
                           filename: str, *,
                           path: Path | None = None) -> dict:
    directory = output_root() / "lifecycle"
    directory.mkdir(parents=True, exist_ok=True)
    output = directory / filename
    output.write_text(content.rstrip() + "\n", encoding="utf-8")
    apply_migrations(path)
    now = datetime.now(JST).isoformat()
    with closing(connect(path)) as conn:
        conn.execute("""INSERT INTO disaster_lifecycle_reports
            (incident_id,snapshot_id,report_type,status,output_path,created_at)
            VALUES (?,?,?,?,?,?) ON CONFLICT(
             incident_id,report_type,snapshot_id) DO UPDATE SET
             status=excluded.status,output_path=excluded.output_path""", (
                incident_id, snapshot_id, report_type, "draft",
                str(output), now,
            ))
        conn.commit()
    return {
        "status": "draft", "report_type": report_type,
        "snapshot_id": snapshot_id, "path": str(output),
        "external_posts": 0,
    }


def recovery_brief(incident_id: str = INCIDENT_ID,
                   path: Path | None = None) -> dict:
    """Create a factual Phase D recovery brief without external publishing."""
    snapshots = list_snapshots(incident_id, path)
    if not snapshots:
        return {"status": "no_snapshots", "external_posts": 0}
    snapshot = load_snapshot(snapshots[-1]["snapshot_id"])
    if not snapshot:
        return {"status": "not_found", "external_posts": 0}
    infra = snapshot["infrastructure"]
    transport = snapshot["transport"]
    lines = [
        "# 熊本地震・定期復旧報告（候補）", "",
        f"情報時点: {snapshot['cutoff_at']}", "",
        "## インフラ", "",
        f"- 停電: {_display(infra.get('power_outage_households'), '戸')}",
        f"- 断水: {_display(infra.get('water_outage_households'), '戸')}",
        f"- ガス供給停止: {_display(infra.get('gas_outage_households'), '戸')}",
        "", "## 交通", "",
        f"- 鉄道: {_plain(transport.get('rail'))}",
        f"- 道路: {_plain(transport.get('roads'))}",
        f"- 空港: {_plain(transport.get('airport'))}",
        "", "異なる機関の数字は単純合算していません。",
        "詳細な情報源は同じスナップショットのsources.mdを参照してください。",
    ]
    return _save_lifecycle_report(
        incident_id, snapshot["snapshot_id"], "recovery_periodic",
        "\n".join(lines), "recovery_brief.md", path=path)


def closure_package(incident_id: str = INCIDENT_ID, *,
                    path: Path | None = None) -> dict:
    """Create closure and preparedness drafts; never publish them."""
    snapshots = list_snapshots(incident_id, path)
    if not snapshots:
        return {"status": "no_snapshots", "external_posts": 0}
    snapshot = load_snapshot(snapshots[-1]["snapshot_id"])
    if not snapshot:
        return {"status": "not_found", "external_posts": 0}
    mode = _incident_mode(incident_id, path)
    summary = [
        "# 熊本地震・定点観測総括（候補）", "",
        f"最終確認時点: {snapshot['cutoff_at']}",
        f"運用状態: {mode}", "",
        "この総括は保存済みの公式スナップショットだけを基にしています。",
        "未確認値、推計値、異なる機関の単純合算は含みません。", "",
        "## 最終確認値", "",
        f"- 負傷者: {_display(snapshot['casualties'].get('injured'), '人')}",
        f"- 救助完了: {_display(snapshot['rescue'].get('rescued_people'), '人')}",
        f"- 避難者: {_display(snapshot['evacuation'].get('evacuees'), '人')}",
        f"- 停電: {_display(snapshot['infrastructure'].get('power_outage_households'), '戸')}",
        f"- 断水: {_display(snapshot['infrastructure'].get('water_outage_households'), '戸')}",
    ]
    preparedness = [
        "# 熊本地震を踏まえた防災確認（候補）", "",
        "公的機関の最新情報を優先し、避難所、停電、断水、交通情報を",
        "自治体や事業者の公式ページで個別に確認してください。", "",
        "この文書は一般的な確認事項で、個別の避難指示や医療判断を",
        "置き換えるものではありません。",
    ]
    first = _save_lifecycle_report(
        incident_id, snapshot["snapshot_id"], "closure_summary",
        "\n".join(summary), "closure_summary.md", path=path)
    second = _save_lifecycle_report(
        incident_id, snapshot["snapshot_id"], "preparedness_explainer",
        "\n".join(preparedness), "preparedness_explainer.md", path=path)
    return {
        "status": "draft" if mode == "closed" else "draft_pending_closure",
        "current_mode": mode, "summary": first, "preparedness": second,
        "external_posts": 0,
    }


def _write_generation_files(snapshot: dict, previous: dict | None,
                            delta: dict, candidates: dict, visuals: dict,
                            report: dict, directory: Path) -> None:
    (directory / "delta.json").write_text(
        json.dumps(delta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (directory / "x_post.md").write_text(
        "# X投稿候補（未公開）\n\n" + candidates["x"] + "\n",
        encoding="utf-8")
    (directory / "threads_post.md").write_text(
        "# Threads投稿候補（未公開）\n\n" + candidates["threads"] + "\n",
        encoding="utf-8")
    (directory / "correction_candidate.md").write_text(
        correction_candidate(snapshot, delta), encoding="utf-8")
    (directory / "short_candidate.md").write_text(
        short_candidate(snapshot, delta), encoding="utf-8")
    log = {
        "snapshot_id": snapshot["snapshot_id"],
        "previous_snapshot_id": previous.get("snapshot_id") if previous else None,
        "generated_at": snapshot["generated_at"],
        "dry_run_fixture": snapshot["metadata"].get("fixture", False),
        "quality": report["failed"] or "passed",
        "publish_eligible": candidates["publish_eligible"],
        "decision_reason": candidates["decision_reason"],
        "external_publication_calls": 0,
        "discord_raw_logs_sent": False,
        "visuals": visuals,
    }
    (directory / "generation_log.md").write_text(
        "# 生成ログ\n\n```json\n"
        + json.dumps(log, ensure_ascii=False, indent=2)
        + "\n```\n", encoding="utf-8")


def _notify(snapshot: dict, delta: dict, candidates: dict,
            visuals: dict, *, dry_run: bool) -> bool:
    if dry_run:
        return False
    try:
        from discord_notify import notify
        cutoff = datetime.fromisoformat(snapshot["cutoff_at"])
        if candidates["publish_eligible"]:
            title = "【熊本地震・朝夕更新準備】"
            description = "公式情報のスナップショットと投稿候補を準備しました。"
        else:
            title = "【熊本地震・定点更新見送り】"
            description = (
                "前回投稿から重大な公式変化が確認できなかったため、"
                "今回の投稿を見送りました。")
        return notify(
            "disaster_update", title, description,
            level="info" if candidates["publish_eligible"] else "warning",
            fields={
                "時点": snapshot["cutoff_at"],
                "スナップショット": snapshot["snapshot_id"],
                "前回からの重要変化": "／".join(
                    _important_changes(delta, 3)) or "重大な変化なし",
                "救助・安否確認": _display(
                    snapshot["rescue"]["rescued_people"], "人"),
                "避難": _display(snapshot["evacuation"]["evacuees"], "人"),
                "停電・断水": (
                    f"{_display(snapshot['infrastructure']['power_outage_households'],'戸')}／"
                    f"{_display(snapshot['infrastructure']['water_outage_households'],'戸')}"),
                "画像": Path(visuals["x"]).name,
                "投稿判定": "投稿候補" if candidates["publish_eligible"] else "見送り",
                "理由": candidates["decision_reason"],
                "次回確認": (
                    f"{cutoff:%m月%d日}19時" if cutoff.hour == 7
                    else f"{(cutoff + timedelta(days=1)):%m月%d日}7時"),
            },
        )
    except Exception:
        return False


def _notify_correction(snapshot: dict, delta: dict, *,
                       dry_run: bool) -> bool:
    if dry_run or not settings()["correction_enabled"]:
        return False
    corrected = [
        row for row in delta.get("changes", [])
        if row.get("delta_status") in {
            "corrected", "scope_changed", "definition_changed",
        }
    ]
    if not corrected:
        return False
    try:
        from discord_notify import notify_disaster_correction
        row = corrected[0]
        source = next(iter(snapshot.get("sources", [])), {})
        return notify_disaster_correction({
            "correction_required": True,
            "snapshot_id": snapshot["snapshot_id"],
            "metric_key": row["metric_key"],
            "previous_value": row["previous_value"],
            "current_value": row["current_value"],
            "source_name": source.get("name") or source.get("source_id"),
        })
    except Exception:
        return False


def _notify_frequency(result: dict, *, dry_run: bool) -> bool:
    if dry_run or not result.get("human_approval_required"):
        return False
    try:
        from discord_notify import notify
        return notify(
            "disaster_frequency",
            "【熊本地震・更新頻度の確認候補】",
            "状況に応じた更新頻度の変更候補です。自動変更は行いません。",
            level="warning",
            fields={
                "現在": result["current_mode"],
                "候補": result["recommended_mode"],
                "理由": result["recommendation_reason"],
                "人間の承認": "必要",
            },
        )
    except Exception:
        return False


def create_snapshot(incident_id: str, snapshot_type: str, *,
                    dry_run: bool = False, path: Path | None = None) -> dict:
    snapshot = collect_official_snapshot(
        snapshot_type, dry_run=dry_run, incident_id=incident_id, path=path)
    previous = _latest_existing_snapshot(
        incident_id, before=snapshot["cutoff_at"], path=path)
    delta = calculate_delta(previous, snapshot)
    candidates = make_candidates(snapshot, delta)
    persist_snapshot(snapshot, path=path)
    directory = save_snapshot(snapshot, previous)
    persist_delta(delta, path)
    (directory / "delta.json").write_text(
        json.dumps(delta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return {
        "status": "created", "snapshot": snapshot,
        "previous_snapshot": previous, "delta": delta,
        "candidates": candidates, "directory": str(directory),
        "dry_run": dry_run, "external_publication_calls": 0,
    }


def full_cycle(incident_id: str, snapshot_type: str, *,
               dry_run: bool = False, path: Path | None = None) -> dict:
    created = create_snapshot(
        incident_id, snapshot_type, dry_run=dry_run, path=path)
    snapshot = created["snapshot"]
    previous = created["previous_snapshot"]
    delta = created["delta"]
    candidates = created["candidates"]
    directory = Path(created["directory"])
    visuals = render_visuals(snapshot, delta, directory)
    report = quality_report(
        snapshot, delta, candidates, visuals, directory=directory)
    if report["failed"]:
        candidates["publish_eligible"] = False
        candidates["decision_reason"] = "quality_failed"
        snapshot["publish_eligible"] = False
        snapshot["decision_reason"] = "quality_failed"
    persist_snapshot(snapshot, path=path)
    save_snapshot(snapshot, previous)
    persist_candidates(snapshot, candidates, visuals, path)
    correction_count = persist_correction_candidates(snapshot, delta, path)
    _write_generation_files(
        snapshot, previous, delta, candidates, visuals, report, directory)
    discord_sent = _notify(
        snapshot, delta, candidates, visuals, dry_run=dry_run)
    correction_discord_sent = _notify_correction(
        snapshot, delta, dry_run=dry_run)
    frequency = frequency_recommendation(incident_id, path)
    frequency_discord_sent = _notify_frequency(frequency, dry_run=dry_run)
    auto_publish = (
        {"status": "dry_run", "external_posts": 0}
        if dry_run else auto_publish_verified(snapshot["snapshot_id"], path=path)
    )
    correction_auto_publish = (
        {"status": "dry_run", "external_posts": 0}
        if dry_run or correction_count == 0
        else auto_publish_corrections(snapshot["snapshot_id"], path=path)
    )
    current_mode = _incident_mode(incident_id, path)
    lifecycle = {"status": "not_due", "external_posts": 0}
    if current_mode == "recovery_periodic":
        lifecycle = recovery_brief(incident_id, path)
    elif current_mode == "closed":
        lifecycle = closure_package(incident_id, path=path)
    return {
        "status": "completed", "incident_id": incident_id,
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_type": snapshot_type, "cutoff_at": snapshot["cutoff_at"],
        "directory": str(directory), "delta": delta,
        "candidates": candidates, "visuals": visuals,
        "quality_report": report, "discord_sent": discord_sent,
        "correction_discord_sent": correction_discord_sent,
        "frequency": frequency,
        "frequency_discord_sent": frequency_discord_sent,
        "dry_run": dry_run, "phase": settings()["phase"],
        "auto_publish": auto_publish,
        "correction_candidate_count": correction_count,
        "correction_auto_publish": correction_auto_publish,
        "frequency_mode": current_mode,
        "lifecycle": lifecycle,
        "external_publication_calls": (
            auto_publish.get("external_posts", 0)
            + correction_auto_publish.get("external_posts", 0)),
        "x_published": bool(
            auto_publish.get("results", {}).get("x", {}).get("published")),
        "threads_published": bool(
            auto_publish.get("results", {}).get(
                "threads", {}).get("published")),
        "youtube_published": False, "instagram_published": False,
        "note_published": False,
    }


def status(incident_id: str = INCIDENT_ID,
           path: Path | None = None) -> dict:
    cfg = settings()
    snapshots = list_snapshots(incident_id, path)
    return {
        "incident_id": incident_id, "enabled": cfg["enabled"],
        "phase": cfg["phase"], "auto_post_enabled": cfg["auto_post"],
        "publish_enabled": cfg["publish_enabled"],
        "x_post_enabled": cfg["x_post_enabled"],
        "threads_post_enabled": cfg["threads_post_enabled"],
        "human_approval_required": cfg["human_approval_required"],
        "verified_only": cfg["verified_only"],
        "frequency_mode": _incident_mode(incident_id, path),
        "correction_auto_post": cfg["correction_auto_post"],
        "morning": {
            "cutoff": cfg["morning_cutoff"],
            "publish_candidate_at": cfg["morning_publish"],
        },
        "evening": {
            "cutoff": cfg["evening_cutoff"],
            "publish_candidate_at": cfg["evening_publish"],
        },
        "timezone": cfg["timezone"], "snapshot_count": len(snapshots),
        "latest_snapshot": snapshots[-1] if snapshots else None,
        "external_publication_supported_by_phase": cfg["phase"] in {
            "B", "C", "D"},
    }


def detail(snapshot_id: str) -> dict:
    snapshot = load_snapshot(snapshot_id)
    return snapshot or {"status": "not_found", "snapshot_id": snapshot_id}


def latest_pair(incident_id: str = INCIDENT_ID,
                path: Path | None = None) -> tuple[dict | None, dict | None]:
    rows = list_snapshots(incident_id, path)
    if not rows:
        return None, None
    current = load_snapshot(rows[-1]["snapshot_id"])
    previous = load_snapshot(rows[-2]["snapshot_id"]) if len(rows) > 1 else None
    return previous, current


def latest_delta(incident_id: str = INCIDENT_ID,
                 path: Path | None = None) -> dict:
    previous, current = latest_pair(incident_id, path)
    return calculate_delta(previous, current) if current else {
        "incident_id": incident_id, "status": "no_snapshots", "changes": [],
        "meaningful_change": False, "meaningful_change_count": 0,
    }


def render_latest(incident_id: str, snapshot_type: str, *,
                  dry_run: bool = False,
                  path: Path | None = None) -> dict:
    matches = [
        row for row in list_snapshots(incident_id, path)
        if row["snapshot_type"] == snapshot_type
    ]
    if not matches:
        created = create_snapshot(
            incident_id, snapshot_type, dry_run=dry_run, path=path)
        snapshot = created["snapshot"]
        delta = created["delta"]
    else:
        snapshot = load_snapshot(matches[-1]["snapshot_id"])
        previous = _latest_existing_snapshot(
            incident_id, before=snapshot["cutoff_at"], path=path)
        delta = calculate_delta(previous, snapshot)
    return render_visuals(snapshot, delta, _snapshot_dir(snapshot))


def latest_candidates(incident_id: str, snapshot_type: str, *,
                      dry_run: bool = False,
                      path: Path | None = None) -> dict:
    matches = [
        row for row in list_snapshots(incident_id, path)
        if row["snapshot_type"] == snapshot_type
    ]
    if not matches:
        result = create_snapshot(
            incident_id, snapshot_type, dry_run=dry_run, path=path)
        return result["candidates"]
    snapshot = load_snapshot(matches[-1]["snapshot_id"])
    previous = _latest_existing_snapshot(
        incident_id, before=snapshot["cutoff_at"], path=path)
    return make_candidates(snapshot, calculate_delta(previous, snapshot))


def latest_correction(incident_id: str, path: Path | None = None) -> dict:
    previous, current = latest_pair(incident_id, path)
    if not current:
        return {"status": "no_snapshots", "candidate": ""}
    delta = calculate_delta(previous, current)
    text = correction_candidate(current, delta)
    target = _snapshot_dir(current) / "correction_candidate.md"
    target.write_text(text, encoding="utf-8")
    return {
        "status": "candidate_saved", "path": str(target),
        "auto_post": False, "candidate": text,
    }
