"""Safe, resumable short-video workflow with official X/Threads publishers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import time
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from . import repository as repo

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[2]
PLATFORMS = ("x", "threads", "youtube", "instagram")


def _bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _now() -> datetime:
    return datetime.now(JST)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _binary(name: str) -> str | None:
    configured = os.environ.get(f"{name.upper()}_PATH", "").strip()
    if configured and Path(configured).is_file():
        return configured
    return shutil.which(name)


class ShortVideoFactory:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else None
        repo.migrate(self.path)
        self.output = ROOT / os.environ.get(
            "SHORT_VIDEO_OUTPUT_DIR", "outputs/short_video_factory")
        self.output.mkdir(parents=True, exist_ok=True)

    def settings(self) -> dict:
        def legacy_bool(primary: str, fallback: str) -> bool:
            if primary in os.environ:
                return _bool(primary)
            return _bool(fallback, "false")
        return {
            "enabled": _bool("SHORT_VIDEO_FACTORY_ENABLED", "true"),
            "phase": os.environ.get("SHORT_VIDEO_OPERATION_PHASE", "A").upper(),
            "auto_publish": _bool("SHORT_VIDEO_AUTO_PUBLISH_ENABLED", "false"),
            "platform_auto": {
                p: legacy_bool(
                    f"SHORT_VIDEO_{p.upper()}_AUTO_PUBLISH",
                    f"SHORT_VIDEO_{p.upper()}_AUTO_PUBLISH_ENABLED")
                for p in PLATFORMS
            },
            "quality_min": _float(
                "SHORT_VIDEO_MIN_QUALITY_SCORE",
                _float("SHORT_VIDEO_QUALITY_MIN", 8.0)),
            "safety_min": _float(
                "SHORT_VIDEO_MIN_SAFETY_SCORE",
                _float("SHORT_VIDEO_SAFETY_MIN", 9.0)),
            "candidate_min": _float(
                "SHORT_VIDEO_MIN_POTENTIAL_SCORE",
                _float("SHORT_VIDEO_CANDIDATE_MIN", 7.2)),
            "min_samples": int(_float("SHORT_VIDEO_PHASE_D_MIN_SAMPLES", 30)),
            "recent_safe_samples": int(
                _float("SHORT_VIDEO_PHASE_D_SAFE_SAMPLE_WINDOW", 10)),
            "duration": int(_float(
                "SHORT_VIDEO_TARGET_DURATION_SECONDS",
                _float("SHORT_VIDEO_DURATION_SECONDS", 55))),
        }

    def _project(self, video_id: str) -> dict:
        items = repo.rows(
            "SELECT * FROM short_video_projects WHERE video_id=?",
            (video_id,), self.path)
        if not items:
            raise KeyError(f"short_video_project_not_found:{video_id}")
        return items[0]

    def status(self) -> dict:
        cfg = self.settings()
        counts = {}
        for table in (
            "short_video_projects", "short_video_scripts",
            "short_video_render_jobs", "short_video_publication_queue",
            "short_video_publications", "short_video_metrics",
        ):
            counts[table] = repo.rows(
                f"SELECT COUNT(*) AS n FROM {table}", path=self.path)[0]["n"]
        state = repo.rows(
            """SELECT state_value FROM short_video_system_state
               WHERE state_key='emergency_stop'""", path=self.path)
        return {
            "settings": cfg,
            "emergency_stopped": bool(state and state[0]["state_value"] == "true"),
            "ffmpeg": _binary("ffmpeg"),
            "ffprobe": _binary("ffprobe"),
            "counts": counts,
            "external_writes": 0,
        }

    def candidates(self, limit: int = 20) -> dict:
        cfg = self.settings()
        topics = repo.rows(
            """SELECT t.*, COUNT(e.id) AS counted_evidence
               FROM integrated_research_topics t
               LEFT JOIN integrated_research_evidence e ON e.topic_id=t.id
                 AND COALESCE(e.is_deleted,0)=0
               GROUP BY t.id ORDER BY t.updated_at DESC LIMIT ?""",
            (max(1, limit * 4),), self.path)
        output = []
        for topic in topics:
            confidence = float(topic.get("confidence") or 0)
            families = int(topic.get("source_family_count") or 0)
            evidence = int(topic.get("counted_evidence") or 0)
            value = float(topic.get("posting_value_score") or 0)
            verified = confidence >= .7 and families >= 2 and evidence >= 2
            score = round(min(10, confidence * 3 + min(families, 3) +
                              min(evidence, 4) * .5 + value * .2), 2)
            reasons = []
            if not verified:
                reasons.append("insufficient_verified_evidence")
            if topic.get("correction_status") not in (None, "", "current"):
                reasons.append("correction_not_current")
            if score < cfg["candidate_min"]:
                reasons.append("candidate_score_below_threshold")
            output.append({
                "topic_id": topic["id"], "topic_key": topic["topic_key"],
                "title": topic["title"], "content_packet_id":
                    topic.get("content_packet_id"),
                "score": score, "eligible": not reasons,
                "reasons": reasons,
            })
        return {"candidates": output[:limit], "count": min(len(output), limit)}

    def project_create(self, topic_id: int, angle: str = "制度の変化を60秒で解説",
                       force: bool = False) -> dict:
        topics = repo.rows(
            "SELECT * FROM integrated_research_topics WHERE id=?",
            (topic_id,), self.path)
        if not topics:
            raise KeyError(f"integrated_topic_not_found:{topic_id}")
        candidate = next(
            (x for x in self.candidates(200)["candidates"]
             if x["topic_id"] == topic_id), None)
        if not candidate:
            raise RuntimeError("candidate_not_scored")
        if not candidate["eligible"] and not force:
            return {"status": "blocked", **candidate}
        topic = topics[0]
        digest = hashlib.sha256(
            f"{topic_id}:{topic['updated_at']}:{angle}".encode()).hexdigest()[:10]
        video_id = f"sv-{topic_id}-{digest}"
        now = _now().isoformat()
        repo.write(
            """INSERT OR IGNORE INTO short_video_projects
               (video_id,topic_id,content_packet_id,topic_key,title,angle,phase,
                status,candidate_score,quality_score,safety_score,publish_eligible,
                created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,0,0,0,?,?)""",
            (video_id, topic_id, topic.get("content_packet_id"),
             topic["topic_key"], topic["title"], angle,
             self.settings()["phase"], "selected", candidate["score"], now, now),
            self.path)
        repo.event(video_id, "candidate", "success", candidate, self.path)
        return {"video_id": video_id, "status": "selected", **candidate}

    def script_generate(self, video_id: str) -> dict:
        project = self._project(video_id)
        topic = repo.rows(
            "SELECT * FROM integrated_research_topics WHERE id=?",
            (project["topic_id"],), self.path)[0]
        evidence = repo.rows(
            """SELECT source_url,title,summary,reliability FROM
               integrated_research_evidence WHERE topic_id=?
               AND COALESCE(is_deleted,0)=0 ORDER BY reliability DESC LIMIT 4""",
            (project["topic_id"],), self.path)
        facts = [str(e.get("summary") or e.get("title") or "").strip()
                 for e in evidence if e.get("summary") or e.get("title")]
        hook = f"🚨 いま押さえたいのは「{project['title']}」です。"
        body = [
            hook,
            f"📌 一次情報を照合すると、{topic.get('fact_summary') or project['title']}。",
        ]
        if facts:
            body.append(f"🔎 確認できた要点は、{facts[0][:180]}。")
        body.extend([
            "⚖️ 重要なのは、誰が決め、誰にどんな負担や影響が出るのかです。",
            "🗣️ 感情だけで終わらせず、根拠と進展を追い続けます。",
        ])
        narration = "\n".join(body)
        scenes = [
            {"index": i + 1, "start": i * 11, "end": (i + 1) * 11,
             "text": text, "visual": "headline" if i == 0 else "fact_card"}
            for i, text in enumerate(body)
        ]
        version = len(repo.rows(
            "SELECT id FROM short_video_scripts WHERE video_id=?",
            (video_id,), self.path)) + 1
        repo.write(
            """INSERT INTO short_video_scripts
               (video_id,version,hook,narration,scenes_json,
                duration_target_seconds,source_topic_updated_at,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (video_id, version, hook, narration, _json(scenes),
             self.settings()["duration"], topic["updated_at"], "generated",
             _now().isoformat()), self.path)
        for claim in body[1:3]:
            repo.write(
                """INSERT INTO short_video_claims
                   (video_id,claim_text,claim_type,evidence_urls_json,verified,
                    verification_reason,created_at) VALUES (?,?,?,?,?,?,?)""",
                (video_id, claim, "fact",
                 _json([e["source_url"] for e in evidence if e["source_url"]]),
                 1 if len(evidence) >= 2 else 0,
                 "integrated_research_evidence", _now().isoformat()), self.path)
        repo.write(
            """UPDATE short_video_projects SET status='scripted',updated_at=?
               WHERE video_id=?""", (_now().isoformat(), video_id), self.path)
        result = {"video_id": video_id, "version": version, "hook": hook,
                  "narration": narration, "scenes": scenes}
        repo.event(video_id, "script", "success", result, self.path)
        repo.api_event("short_video_script", "deterministic_script", metadata={
            "video_id": video_id, "api_calls": 0}, path=self.path)
        return result

    def script_check(self, video_id: str) -> dict:
        scripts = repo.rows(
            """SELECT * FROM short_video_scripts WHERE video_id=?
               ORDER BY version DESC LIMIT 1""", (video_id,), self.path)
        if not scripts:
            return {"video_id": video_id, "passed": False,
                    "reasons": ["script_missing"]}
        claims = repo.rows(
            "SELECT * FROM short_video_claims WHERE video_id=?",
            (video_id,), self.path)
        text = scripts[0]["narration"]
        reasons = []
        if not all(int(c["verified"] or 0) for c in claims):
            reasons.append("unverified_claim")
        if re.search(r"\b\d+(?:\.\d+)?[%％円人件]\b", text) and not claims:
            reasons.append("number_without_evidence")
        banned = ("死ね", "殺せ", "襲撃")
        if any(word in text for word in banned):
            reasons.append("unsafe_expression")
        score = max(0.0, 10.0 - len(reasons) * 3.0)
        self._save_check(video_id, "script_safety", not reasons, score,
                         {"reasons": reasons})
        return {"video_id": video_id, "passed": not reasons,
                "score": score, "reasons": reasons}

    def audio_generate(self, video_id: str) -> dict:
        self._project(video_id)
        folder = self.output / video_id / "audio"
        folder.mkdir(parents=True, exist_ok=True)
        script = repo.rows(
            """SELECT narration FROM short_video_scripts WHERE video_id=?
               ORDER BY version DESC LIMIT 1""", (video_id,), self.path)
        if not script:
            raise RuntimeError("script_missing")
        provider = os.environ.get("SHORT_VIDEO_TTS_PROVIDER", "mock").lower()
        if provider == "mock":
            duration = self.settings()["duration"]
            target = folder / "narration_mock.wav"
            rate = 24000
            with wave.open(str(target), "wb") as wav:
                wav.setparams((1, 2, rate, duration * rate,
                               "NONE", "not compressed"))
                frames = bytearray()
                for i in range(duration * rate):
                    active = (i // rate) % 4 != 3
                    sample = int(
                        900 * math.sin(2 * math.pi * 220 * i / rate)
                    ) if active else 0
                    frames.extend(struct.pack("<h", sample))
                wav.writeframes(frames)
        elif provider == "sapi":
            target = folder / "narration_sapi.wav"
            text_path = folder / "narration.txt"
            text_path.write_text(script[0]["narration"], encoding="utf-8")
            speech_script = ROOT / "production" / "generate_short_video_speech.ps1"
            command = [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(speech_script), "-TextPath", str(text_path),
                "-OutputPath", str(target), "-Voice",
                os.environ.get(
                    "SHORT_VIDEO_TTS_VOICE", "Microsoft Haruka Desktop"),
                "-Rate", str(int(_float("SHORT_VIDEO_TTS_RATE", -1))),
                "-Volume", "100",
            ]
            subprocess.run(command, check=True, capture_output=True,
                           text=False, timeout=180)
            with wave.open(str(target), "rb") as wav:
                rate = wav.getframerate()
                duration = wav.getnframes() / max(1, rate)
        elif provider == "openai":
            target = folder / "narration_openai.wav"
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("openai_api_key_missing")
            model = os.environ.get(
                "SHORT_VIDEO_TTS_MODEL", "gpt-4o-mini-tts")
            voice = os.environ.get("SHORT_VIDEO_TTS_VOICE", "coral")
            payload = {
                "model": model,
                "voice": voice,
                "input": script[0]["narration"][:4096],
                "instructions": os.environ.get(
                    "SHORT_VIDEO_TTS_INSTRUCTIONS",
                    "日本語で、落ち着いたニュース解説者として、明瞭かつ自然に読む。"
                    "絵文字は読まず、煽らず、事実を強調する。"),
                "response_format": "wav",
                "speed": _float("SHORT_VIDEO_TTS_SPEED", 0.9),
            }
            from api_budget import reserve as reserve_budget
            from api_budget import finalize as finalize_budget
            maximum_cost = max(
                0.001, _float("SHORT_VIDEO_TTS_MAX_COST_USD", 0.05))
            reservation, budget_reason = reserve_budget(
                "openai", "short_video_tts", model, maximum_cost,
                metadata={"video_id": video_id, "provider": "openai"},
                path=self.path)
            if not reservation:
                raise RuntimeError(
                    budget_reason or "short_video_tts_budget_guard")
            try:
                response = requests.post(
                    "https://api.openai.com/v1/audio/speech",
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json"},
                    json=payload,
                    timeout=float(os.environ.get(
                        "OPENAI_TIMEOUT_SECONDS", "90")))
                response.raise_for_status()
            except Exception as exc:
                finalize_budget(
                    reservation, 0, success=False,
                    error_type=type(exc).__name__, path=self.path)
                raise
            target.write_bytes(response.content)
            with wave.open(str(target), "rb") as wav:
                rate = wav.getframerate()
                duration = wav.getnframes() / max(1, rate)
            estimated_cost = (
                len(payload["input"]) / 4 / 1_000_000 * 0.60
                + duration * 50 / 1_000_000 * 12.0)
            finalize_budget(
                reservation, estimated_cost, success=True, path=self.path)
        else:
            raise RuntimeError(f"unsupported_tts_provider:{provider}")
        repo.write(
            """INSERT OR REPLACE INTO short_video_audio_assets
               (video_id,provider,local_path,duration_seconds,sample_rate,status,
                created_at) VALUES (?,?,?,?,?,?,?)""",
            (video_id, provider, str(target), duration, rate, "ready",
             _now().isoformat()), self.path)
        result = {"video_id": video_id, "path": str(target),
                  "provider": provider,
                  "duration_seconds": round(duration, 3)}
        repo.event(video_id, "audio", "success", result, self.path)
        if provider != "openai":
            repo.api_event("short_video_tts", f"local_{provider}_tts", metadata={
                "video_id": video_id, "api_calls": 0}, path=self.path)
        return result

    def captions_generate(self, video_id: str) -> dict:
        script = repo.rows(
            """SELECT * FROM short_video_scripts WHERE video_id=?
               ORDER BY version DESC LIMIT 1""", (video_id,), self.path)
        if not script:
            raise RuntimeError("script_missing")
        scenes = json.loads(script[0]["scenes_json"])
        folder = self.output / video_id / "captions"
        folder.mkdir(parents=True, exist_ok=True)
        srt = folder / "captions.srt"
        vtt = folder / "captions.vtt"
        def stamp(seconds: int, comma: bool) -> str:
            sep = "," if comma else "."
            return f"00:{seconds // 60:02d}:{seconds % 60:02d}{sep}000"
        srt.write_text("\n\n".join(
            f"{i}\n{stamp(x['start'], True)} --> {stamp(x['end'], True)}\n{x['text']}"
            for i, x in enumerate(scenes, 1)), encoding="utf-8")
        vtt.write_text("WEBVTT\n\n" + "\n\n".join(
            f"{stamp(x['start'], False)} --> {stamp(x['end'], False)}\n{x['text']}"
            for x in scenes), encoding="utf-8")
        captions_json = folder / "captions.json"
        captions_json.write_text(
            json.dumps(scenes, ensure_ascii=False, indent=2),
            encoding="utf-8")
        result = {"video_id": video_id, "srt": str(srt), "vtt": str(vtt),
                  "json": str(captions_json),
                  "cue_count": len(scenes)}
        repo.event(video_id, "captions", "success", result, self.path)
        return result

    def _visual_plan_legacy(self, video_id: str) -> dict:
        from PIL import Image, ImageDraw, ImageFont
        project = self._project(video_id)
        script = repo.rows(
            """SELECT scenes_json FROM short_video_scripts WHERE video_id=?
               ORDER BY version DESC LIMIT 1""", (video_id,), self.path)
        if not script:
            raise RuntimeError("script_missing")
        scenes = json.loads(script[0]["scenes_json"])
        folder = self.output / video_id / "visuals"
        folder.mkdir(parents=True, exist_ok=True)
        assets = []
        font = ImageFont.load_default()
        for scene in scenes:
            target = folder / f"scene_{scene['index']:02d}.png"
            image = Image.new("RGB", (1080, 1920), "#101827")
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((80, 220, 1000, 1650), 40, fill="#17233a")
            draw.text((110, 275), f"SCENE {scene['index']}  📌",
                      fill="#55d6be", font=font)
            wrapped = "\n".join(
                scene["text"][i:i + 24] for i in range(0, len(scene["text"]), 24))
            draw.multiline_text((110, 420), wrapped, fill="white",
                                font=font, spacing=22)
            draw.text((110, 1570), "一次資料を確認して解説", fill="#b9c4d6", font=font)
            image.save(target)
            repo.write(
                """INSERT OR REPLACE INTO short_video_visual_assets
                   (video_id,asset_type,scene_index,local_path,plan_json,status,
                    created_at) VALUES (?,?,?,?,?,?,?)""",
                (video_id, "scene", scene["index"], str(target), _json(scene),
                 "ready", _now().isoformat()), self.path)
            assets.append(str(target))
        result = {"video_id": video_id, "title": project["title"],
                  "assets": assets, "safe_area": {"top": 180, "bottom": 260}}
        repo.event(video_id, "visual_plan", "success", result, self.path)
        repo.api_event("short_video_visual", "local_pillow", metadata={
            "video_id": video_id, "api_calls": 0}, path=self.path)
        return result

    def visual_plan(self, video_id: str) -> dict:
        """Create Japanese-safe 1080x1920 scene images without external APIs."""
        from PIL import Image, ImageDraw, ImageFont

        project = self._project(video_id)
        script = repo.rows(
            """SELECT scenes_json FROM short_video_scripts WHERE video_id=?
               ORDER BY version DESC LIMIT 1""", (video_id,), self.path)
        if not script:
            raise RuntimeError("script_missing")
        scenes = json.loads(script[0]["scenes_json"])
        folder = self.output / video_id / "visuals"
        folder.mkdir(parents=True, exist_ok=True)

        def japanese_font(size: int):
            configured = os.environ.get("SHORT_VIDEO_FONT_PATH", "").strip()
            candidates = [
                Path(configured) if configured else None,
                Path(r"C:\Windows\Fonts\YuGothM.ttc"),
                Path(r"C:\Windows\Fonts\meiryo.ttc"),
                Path(r"C:\Windows\Fonts\msgothic.ttc"),
            ]
            for candidate in candidates:
                if candidate and candidate.is_file():
                    return ImageFont.truetype(str(candidate), size=size)
            return ImageFont.load_default()

        label_font = japanese_font(38)
        body_font = japanese_font(64)
        footer_font = japanese_font(34)
        assets = []
        for scene in scenes:
            target = folder / f"scene_{scene['index']:02d}.png"
            image = Image.new("RGB", (1080, 1920), "#101827")
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle(
                (80, 220, 1000, 1650), 40, fill="#17233a")
            draw.text(
                (110, 275), f"SCENE {scene['index']}  NEWS",
                fill="#55d6be", font=label_font)
            display_text = re.sub(
                r"[^\w\s\u3000-\u303f\uff00-\uffef"
                r"\u3040-\u30ff\u3400-\u9fff。、！？「」（）％・ー]",
                "", scene["text"])
            wrapped = "\n".join(
                display_text[index:index + 14]
                for index in range(0, len(display_text), 14))
            draw.multiline_text(
                (110, 420), wrapped, fill="white",
                font=body_font, spacing=22)
            draw.text(
                (110, 1570), "一次資料を確認して解説",
                fill="#b9c4d6", font=footer_font)
            image.save(target)
            repo.write(
                """INSERT OR REPLACE INTO short_video_visual_assets
                   (video_id,asset_type,scene_index,local_path,plan_json,status,
                    created_at) VALUES (?,?,?,?,?,?,?)""",
                (video_id, "scene", scene["index"], str(target), _json(scene),
                 "ready", _now().isoformat()), self.path)
            assets.append(str(target))
        result = {
            "video_id": video_id,
            "title": project["title"],
            "assets": assets,
            "safe_area": {"top": 180, "bottom": 260},
        }
        repo.event(video_id, "visual_plan", "success", result, self.path)
        repo.api_event(
            "short_video_visual", "local_pillow",
            metadata={"video_id": video_id, "api_calls": 0},
            path=self.path)
        return result

    def render(self, video_id: str, dry_run: bool = False) -> dict:
        ffmpeg = _binary("ffmpeg")
        audio = repo.rows(
            """SELECT local_path FROM short_video_audio_assets
               WHERE video_id=? AND status='ready' ORDER BY id DESC LIMIT 1""",
            (video_id,), self.path)
        visuals = repo.rows(
            """SELECT local_path FROM short_video_visual_assets
               WHERE video_id=? AND asset_type='scene' ORDER BY scene_index""",
            (video_id,), self.path)
        render_dir = self.output / video_id / "renders"
        render_dir.mkdir(parents=True, exist_ok=True)
        target = render_dir / "master.mp4"
        command = []
        if ffmpeg and audio and visuals:
            concat = self.output / video_id / "visuals.ffconcat"
            lines = ["ffconcat version 1.0"]
            for item in visuals:
                lines.extend([f"file '{Path(item['local_path']).as_posix()}'",
                              "duration 11"])
            concat.write_text("\n".join(lines), encoding="utf-8")
            command = [
                ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                "-i", audio[0]["local_path"], "-t", str(self.settings()["duration"]),
                "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-af", "apad", "-c:a", "aac", "-ar", "48000",
                "-movflags", "+faststart",
                str(target),
            ]
        status, error = "planned", ""
        if not ffmpeg:
            status, error = "review_required", "ffmpeg_not_found"
        elif not audio or not visuals:
            status, error = "blocked", "assets_missing"
        elif dry_run:
            status = "dry_run"
        else:
            try:
                subprocess.run(command, check=True, capture_output=True,
                               text=True, timeout=600)
                status = "ready"
            except (subprocess.SubprocessError, OSError):
                status, error = "failed", "ffmpeg_render_failed"
        repo.write(
            """INSERT INTO short_video_render_jobs
               (video_id,master_path,command_json,ffmpeg_available,status,
                error_type,started_at,completed_at) VALUES (?,?,?,?,?,?,?,?)""",
            (video_id, str(target), _json(command), int(bool(ffmpeg)), status,
             error, _now().isoformat(), _now().isoformat()), self.path)
        if status == "ready":
            repo.write(
                """UPDATE short_video_projects SET master_video_path=?,
                   status='rendered',updated_at=? WHERE video_id=?""",
                (str(target), _now().isoformat(), video_id), self.path)
        result = {"video_id": video_id, "status": status, "error_type": error,
                  "master_path": str(target), "command": command}
        repo.event(video_id, "render", status, result, self.path)
        repo.api_event("short_video_render", "local_ffmpeg",
                       success=status in {"ready", "dry_run", "review_required"},
                       metadata={"video_id": video_id, "status": status,
                                 "api_cost_usd": 0}, path=self.path)
        return result

    def _save_check(self, video_id: str, check_type: str, passed: bool,
                    score: float, details: dict) -> None:
        repo.write(
            """INSERT OR REPLACE INTO short_video_quality_checks
               (video_id,check_type,passed,score,details_json,checked_at)
               VALUES (?,?,?,?,?,?)""",
            (video_id, check_type, int(passed), score, _json(details),
             _now().isoformat()), self.path)

    def quality_check(self, video_id: str) -> dict:
        project = self._project(video_id)
        script = self.script_check(video_id)
        master = Path(project.get("master_video_path") or "")
        probe = {}
        ffprobe = _binary("ffprobe")
        if master.is_file() and ffprobe:
            try:
                process = subprocess.run(
                    [ffprobe, "-v", "error", "-show_streams", "-show_format",
                     "-of", "json", str(master)],
                    check=True, capture_output=True, text=True, timeout=60)
                probe = json.loads(process.stdout)
            except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
                probe = {}
        streams = probe.get("streams") or []
        video_stream = next(
            (x for x in streams if x.get("codec_type") == "video"), {})
        audio_stream = next(
            (x for x in streams if x.get("codec_type") == "audio"), {})
        try:
            duration = float((probe.get("format") or {}).get("duration") or 0)
        except ValueError:
            duration = 0
        checks = {
            "script": script["passed"],
            "master_exists": master.is_file(),
            "verified_claims": not bool(repo.rows(
                """SELECT id FROM short_video_claims
                   WHERE video_id=? AND verified=0""", (video_id,), self.path)),
            "dimensions": (
                int(video_stream.get("width") or 0) == 1080
                and int(video_stream.get("height") or 0) == 1920),
            "duration": (
                _float("SHORT_VIDEO_MIN_DURATION_SECONDS", 45)
                <= duration
                <= _float("SHORT_VIDEO_MAX_DURATION_SECONDS", 60.5)),
            "audio": bool(audio_stream),
            "video_codec": video_stream.get("codec_name") == "h264",
            "audio_codec": audio_stream.get("codec_name") == "aac",
            "production_audio": bool(repo.rows(
                """SELECT id FROM short_video_audio_assets
                   WHERE video_id=? AND status='ready' AND provider!='mock'
                   ORDER BY id DESC LIMIT 1""", (video_id,), self.path)),
        }
        # Media checks remain false until an actual FFmpeg render exists.
        score = round(sum(checks.values()) / len(checks) * 10, 2)
        safety = float(script.get("score") or 0)
        passed = all(checks.values()) and score >= self.settings()["quality_min"]
        self._save_check(video_id, "final", passed, score, checks)
        repo.write(
            """UPDATE short_video_projects SET quality_score=?,safety_score=?,
               publish_eligible=?,status=?,updated_at=? WHERE video_id=?""",
            (score, safety, int(passed),
             "quality_passed" if passed else "review_required",
             _now().isoformat(), video_id), self.path)
        result = {"video_id": video_id, "passed": passed,
                  "quality_score": score, "safety_score": safety,
                  "checks": checks, "probe": {
                      "duration_seconds": round(duration, 3),
                      "width": video_stream.get("width"),
                      "height": video_stream.get("height"),
                      "video_codec": video_stream.get("codec_name"),
                      "audio_codec": audio_stream.get("codec_name"),
                  }}
        target = self.output / video_id / "quality_report.json"
        target.write_text(_json(result), encoding="utf-8")
        result["path"] = str(target)
        return result

    def platform_variants(self, video_id: str) -> dict:
        project = self._project(video_id)
        script = repo.rows(
            """SELECT narration FROM short_video_scripts WHERE video_id=?
               ORDER BY version DESC LIMIT 1""", (video_id,), self.path)
        if not script:
            raise RuntimeError("script_missing")
        base = script[0]["narration"]
        render_dir = self.output / video_id / "renders"
        render_dir.mkdir(parents=True, exist_ok=True)
        platform_paths = {
            platform: str(render_dir / f"{platform}.mp4")
            for platform in PLATFORMS
        }
        master = Path(project.get("master_video_path") or "")
        if master.is_file():
            for target in map(Path, platform_paths.values()):
                if not target.exists():
                    shutil.copy2(master, target)
        variants = {
            "x": {"text": base[:270], "video_path": platform_paths["x"]},
            "threads": {"text": base[:490],
                        "video_path": platform_paths["threads"],
                        "requires_public_https_url": True},
            "youtube": {"title": project["title"][:100],
                        "description": base, "privacy": "private",
                        "video_path": platform_paths["youtube"]},
            "instagram": {"caption": base[:2000],
                          "video_path": platform_paths["instagram"],
                          "requires_public_https_url": True},
        }
        folder = self.output / video_id
        path = folder / "platform_variants.json"
        path.write_text(_json(variants), encoding="utf-8")
        repo.event(video_id, "platform_variants", "success", variants, self.path)
        return {"video_id": video_id, "path": str(path), "variants": variants}

    def publish_plan(self, video_id: str) -> dict:
        project = self._project(video_id)
        cfg = self.settings()
        gates = self._publish_gates(video_id)
        result = {
            "video_id": video_id, "phase": cfg["phase"],
            "eligible": not gates,
            "blocking_reasons": gates,
            "platforms": {
                "x": "chunked INIT/APPEND/FINALIZE/STATUS + POST /2/tweets",
                "threads": "public HTTPS VIDEO container + status + publish",
                "youtube": "resumable videos.insert; unverified projects remain private",
                "instagram": "public HTTPS REELS container + status + media_publish",
            },
            "quality": project["quality_score"],
            "safety": project["safety_score"],
            "external_writes": 0,
        }
        target = self.output / video_id / "publication_plan.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_json(result), encoding="utf-8")
        result["path"] = str(target)
        return result

    def queue(self, video_id: str, platforms: tuple[str, ...] = ("x", "threads"),
              scheduled_at: str = "") -> dict:
        explicit_when = (
            datetime.fromisoformat(scheduled_at) if scheduled_at else None)
        added = []
        scheduled = {}
        for platform in platforms:
            if platform not in PLATFORMS:
                continue
            offset = int(_float(
                f"SHORT_VIDEO_{platform.upper()}_OFFSET_MINUTES", 0))
            when = explicit_when or (_now() + timedelta(minutes=offset))
            key = hashlib.sha256(f"{video_id}:{platform}".encode()).hexdigest()
            repo.write(
                """INSERT OR IGNORE INTO short_video_publication_queue
                   (video_id,platform,scheduled_at,idempotency_key,status,
                    created_at,updated_at) VALUES (?,?,?,?,?,?,?)""",
                (video_id, platform, when.isoformat(), key, "queued", _now().isoformat(),
                 _now().isoformat()), self.path)
            added.append(platform)
            scheduled[platform] = when.isoformat()
        return {"video_id": video_id, "queued": added,
                "scheduled_at": scheduled, "external_writes": 0}

    def _publish_gates(self, video_id: str, platform: str = "") -> list[str]:
        cfg = self.settings()
        project = self._project(video_id)
        reasons = []
        if self.status()["emergency_stopped"]:
            reasons.append("emergency_stopped")
        if not cfg["enabled"]:
            reasons.append("factory_disabled")
        phase_b_private_youtube = (
            platform == "youtube"
            and cfg["phase"] in {"B", "C"}
            and os.environ.get(
                "SHORT_VIDEO_YOUTUBE_PRIVACY_STATUS", "private") == "private"
            and _bool("SHORT_VIDEO_YOUTUBE_PHASE_B_UPLOAD_ENABLED", "false")
        )
        if cfg["phase"] != "D" and not phase_b_private_youtube:
            reasons.append("phase_d_required")
        if not cfg["auto_publish"]:
            reasons.append("global_auto_publish_disabled")
        if platform and not cfg["platform_auto"].get(platform, False):
            reasons.append(f"{platform}_auto_publish_disabled")
        credential_names = {
            "x": ("API_KEY", "API_KEY_SECRET", "ACCESS_TOKEN",
                  "ACCESS_TOKEN_SECRET"),
            "threads": ("THREADS_ACCESS_TOKEN", "THREADS_USER_ID"),
            "youtube": ("YOUTUBE_ACCESS_TOKEN",),
            "instagram": ("INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"),
        }
        if platform and not all(
                os.environ.get(name, "").strip()
                for name in credential_names.get(platform, ())):
            reasons.append(f"{platform}_authentication_missing")
        if float(project["quality_score"] or 0) < cfg["quality_min"]:
            reasons.append("quality_below_threshold")
        if float(project["safety_score"] or 0) < cfg["safety_min"]:
            reasons.append("safety_below_threshold")
        if not int(project.get("publish_eligible") or 0):
            reasons.append("project_quality_gate_not_passed")
        if not Path(project.get("master_video_path") or "").is_file():
            reasons.append("master_video_missing")
        sample_count = repo.rows(
            """SELECT COUNT(*) AS n FROM short_video_publications
               WHERE status='published'""", path=self.path)[0]["n"]
        if sample_count < cfg["min_samples"] and not phase_b_private_youtube:
            reasons.append("minimum_sample_count_not_met")
        recent = repo.rows(
            """SELECT passed FROM short_video_quality_checks
               WHERE check_type='final' ORDER BY checked_at DESC LIMIT ?""",
            (cfg["recent_safe_samples"],), self.path)
        if (not phase_b_private_youtube
                and (len(recent) < cfg["recent_safe_samples"] or not all(
                    int(x["passed"]) for x in recent))):
            reasons.append("recent_safe_sample_window_not_met")
        today = _now().date().isoformat()
        daily_count = repo.rows(
            """SELECT COUNT(*) AS n FROM short_video_publications
               WHERE status='published' AND substr(published_at,1,10)=?""",
            (today,), self.path)[0]["n"]
        if daily_count >= int(_float("SHORT_VIDEO_PUBLISH_LIMIT_DAILY", 2)):
            reasons.append("daily_publish_limit_reached")
        month_prefix = _now().strftime("%Y-%m")
        spent = repo.rows(
            """SELECT COALESCE(SUM(estimated_cost_usd),0) AS amount
               FROM api_usage_events WHERE substr(timestamp,1,7)=?""",
            (month_prefix,), self.path)[0]["amount"]
        budget = _float("TOTAL_MONTHLY_API_BUDGET_USD", 23.0)
        if float(spent or 0) >= budget:
            reasons.append("monthly_api_budget_reached")
        return reasons

    def publish(self, video_id: str, platform: str, confirm: bool = False,
                dry_run: bool = True, public_url: str = "") -> dict:
        if platform not in PLATFORMS:
            raise ValueError("supported_publish_platforms:" + ",".join(PLATFORMS))
        gates = self._publish_gates(video_id, platform)
        if not confirm:
            gates.append("explicit_confirmation_required")
        variants = self.platform_variants(video_id)["variants"]
        issued_media = False
        if platform in {"threads", "instagram"} and not public_url.startswith("https://"):
            base = os.environ.get(
                "SHORT_VIDEO_PUBLIC_MEDIA_BASE_URL", "").strip()
            preliminary = [x for x in gates if x]
            if (not dry_run and confirm and not preliminary
                    and base.startswith("https://")):
                from short_video_media_server import issue
                public_url = issue(
                    video_id, Path(self._project(video_id)["master_video_path"]),
                    int(_float("SHORT_VIDEO_PUBLIC_MEDIA_TTL_MINUTES", 180)),
                    self.path)["public_url"]
                issued_media = True
            else:
                gates.append(f"{platform}_public_https_video_url_required")
        if dry_run or gates:
            result = {"video_id": video_id, "platform": platform,
                      "status": "dry_run" if dry_run else "blocked",
                      "blocking_reasons": sorted(set(gates)),
                      "external_writes": 0}
            repo.event(video_id, f"publish_{platform}", result["status"],
                       result, self.path)
            return result
        project = self._project(video_id)
        try:
            if platform == "x":
                from crosspost import XVideoClient
                client = XVideoClient()
                container_id = client.upload(Path(project["master_video_path"]))
                post_id = client.publish(container_id, variants["x"]["text"])
            elif platform == "threads":
                from threads_api import ThreadsClient
                client = ThreadsClient()
                created = client.create_container(
                    variants["threads"]["text"], media_type="VIDEO",
                    video_url=public_url)
                container_id = str(created.get("id") or "")
                status = {}
                for _ in range(max(
                        1, int(_float("SHORT_VIDEO_THREADS_STATUS_POLLS", 20)))):
                    status = client.container_status(container_id)
                    state = str(status.get("status") or "").upper()
                    if state in {"FINISHED", "PUBLISHED"}:
                        break
                    if state in {"ERROR", "FAILED", "EXPIRED"}:
                        raise RuntimeError("threads_video_container_failed")
                    time.sleep(3)
                else:
                    raise RuntimeError("threads_video_container_not_ready")
                published = client.publish_container(container_id)
                post_id = str(published.get("id") or "")
            elif platform == "youtube":
                from crosspost import YouTubeClient
                client = YouTubeClient()
                container_id = ""
                post_id = client.upload(
                    Path(project["master_video_path"]), {
                        **variants["youtube"],
                        "privacy": os.environ.get(
                            "SHORT_VIDEO_YOUTUBE_PRIVACY_STATUS", "private"),
                        "contains_synthetic_media": True,
                    })
            else:
                from crosspost import InstagramClient
                client = InstagramClient()
                container_id = client.create_reel(
                    public_url, variants["instagram"]["caption"],
                    share_to_feed=_bool(
                        "SHORT_VIDEO_INSTAGRAM_SHARE_TO_FEED", "true"))
                status = {}
                for _ in range(max(
                        1, int(_float("SHORT_VIDEO_INSTAGRAM_STATUS_POLLS", 20)))):
                    status = client.container_status(container_id)
                    state = str(
                        status.get("status_code")
                        or status.get("status") or "").upper()
                    if state in {"FINISHED", "PUBLISHED"}:
                        break
                    if state in {"ERROR", "FAILED", "EXPIRED"}:
                        raise RuntimeError("instagram_reel_container_failed")
                    time.sleep(3)
                else:
                    raise RuntimeError("instagram_reel_container_not_ready")
                post_id = client.publish(container_id)
            if not post_id:
                raise RuntimeError("external_post_id_missing")
            now = _now().isoformat()
            repo.write(
                """INSERT OR REPLACE INTO short_video_publications
                   (video_id,platform,external_container_id,external_post_id,
                    status,published_at,metrics_due_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (video_id, platform, container_id, post_id, "published", now,
                 (_now() + timedelta(hours=24)).isoformat(), now, now), self.path)
            repo.write(
                """UPDATE short_video_publication_queue SET status='published',
                   updated_at=? WHERE video_id=? AND platform=?""",
                (now, video_id, platform), self.path)
            if issued_media:
                from short_video_media_server import revoke
                revoke(video_id, self.path)
            return {"video_id": video_id, "platform": platform,
                    "status": "published", "external_post_id": post_id,
                    "external_writes": 1 if platform == "youtube" else 2}
        except Exception as exc:
            error = type(exc).__name__
            repo.write(
                """UPDATE short_video_publication_queue SET status='failed',
                   retry_count=retry_count+1,last_error_type=?,updated_at=?
                   WHERE video_id=? AND platform=?""",
                (error, _now().isoformat(), video_id, platform), self.path)
            repo.event(video_id, f"publish_{platform}", "failed",
                       {"error_type": error}, self.path)
            if issued_media:
                try:
                    from short_video_media_server import revoke
                    revoke(video_id, self.path)
                except Exception:
                    pass
            return {"video_id": video_id, "platform": platform,
                    "status": "failed", "error_type": error,
                    "external_writes": "ambiguous"}

    @staticmethod
    def _metric_value(payload: dict, name: str) -> float | None:
        direct = payload.get(name)
        if direct is not None:
            try:
                return float(direct)
            except (TypeError, ValueError):
                return None
        for row in payload.get("data") or []:
            if str(row.get("name") or "") != name:
                continue
            values = row.get("values") or []
            value = values[-1].get("value") if values else row.get("value")
            try:
                return float(value)
            except (TypeError, ValueError, AttributeError):
                return None
        return None

    def _fetch_platform_metrics(self, platform: str, post_id: str) -> dict:
        if platform == "x":
            from requests_oauthlib import OAuth1
            response = requests.get(
                f"https://api.x.com/2/tweets/{post_id}",
                params={"tweet.fields": "public_metrics,non_public_metrics"},
                auth=OAuth1(
                    os.environ["API_KEY"], os.environ["API_KEY_SECRET"],
                    os.environ["ACCESS_TOKEN"],
                    os.environ["ACCESS_TOKEN_SECRET"]),
                timeout=30)
            response.raise_for_status()
            return (response.json().get("data") or {}).get(
                "public_metrics") or {}
        if platform == "threads":
            from threads_api import ThreadsClient
            return ThreadsClient().insights(post_id)
        if platform == "youtube":
            from crosspost import YouTubeClient
            return YouTubeClient().metrics(post_id)
        from crosspost import InstagramClient
        return InstagramClient().insights(post_id)

    def metrics_sync(self, video_id: str = "", dry_run: bool = True) -> dict:
        publications = repo.rows(
            """SELECT * FROM short_video_publications WHERE status='published'
               AND (?='' OR video_id=?)""", (video_id, video_id), self.path)
        due, synced, failed = [], 0, 0
        windows = (("15m", 15), ("2h", 120), ("24h", 1440))
        for publication in publications:
            try:
                published_at = datetime.fromisoformat(publication["published_at"])
            except (TypeError, ValueError):
                continue
            existing = {
                row["measurement_window"] for row in repo.rows(
                    """SELECT measurement_window FROM short_video_metrics
                       WHERE video_id=? AND platform=?""",
                    (publication["video_id"], publication["platform"]), self.path)
            }
            elapsed = (_now() - published_at).total_seconds() / 60
            for window, minutes in windows:
                if elapsed < minutes or window in existing:
                    continue
                due.append({
                    "video_id": publication["video_id"],
                    "platform": publication["platform"],
                    "window": window,
                })
                if dry_run:
                    continue
                try:
                    raw = self._fetch_platform_metrics(
                        publication["platform"],
                        publication["external_post_id"])
                    source = raw.get("statistics") or raw
                    views = self._metric_value(source, "view_count")
                    if views is None:
                        views = self._metric_value(source, "viewCount")
                    likes = self._metric_value(source, "like_count")
                    if likes is None:
                        likes = self._metric_value(source, "likeCount")
                    replies = self._metric_value(source, "reply_count")
                    if replies is None:
                        replies = self._metric_value(source, "commentCount")
                    reposts = self._metric_value(source, "retweet_count")
                    shares = self._metric_value(source, "shares")
                    repo.write(
                        """INSERT OR REPLACE INTO short_video_metrics
                           (video_id,platform,measurement_window,measured_at,
                            views,likes,replies,reposts,shares,raw_json)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (publication["video_id"], publication["platform"],
                         window, _now().isoformat(),
                         None if views is None else int(views),
                         None if likes is None else int(likes),
                         None if replies is None else int(replies),
                         None if reposts is None else int(reposts),
                         None if shares is None else int(shares),
                         _json(raw)), self.path)
                    repo.api_event(
                        "short_video_metrics",
                        f"{publication['platform']}_metrics_read",
                        provider=publication["platform"],
                        endpoint="official_api", metadata={
                            "video_id": publication["video_id"],
                            "measurement_window": window,
                        }, path=self.path)
                    synced += 1
                except Exception as exc:
                    failed += 1
                    repo.event(
                        publication["video_id"], "metrics_sync", "failed",
                        {"platform": publication["platform"],
                         "window": window,
                         "error_type": type(exc).__name__}, self.path)
        return {"publication_count": len(publications), "dry_run": dry_run,
                "due": due, "synced": synced, "failed": failed,
                "missing_metrics_remain_null": True}

    def run_queue(self, live: bool = False, limit: int = 10) -> dict:
        queued = repo.rows(
            """SELECT * FROM short_video_publication_queue
               WHERE status IN ('queued','retry')
                 AND scheduled_at<=?
               ORDER BY scheduled_at,id LIMIT ?""",
            (_now().isoformat(), max(1, limit)), self.path)
        results = []
        for item in queued:
            try:
                result = self.publish(
                    item["video_id"], item["platform"], confirm=live,
                    dry_run=not live)
            except Exception as exc:
                result = {
                    "video_id": item["video_id"],
                    "platform": item["platform"],
                    "status": "blocked",
                    "blocking_reasons": [
                        f"queue_precondition:{type(exc).__name__}"],
                    "external_writes": 0,
                }
            results.append(result)
            if live and result["status"] == "blocked":
                reasons = result.get("blocking_reasons") or []
                policy_holds = {
                    "phase_d_required", "global_auto_publish_disabled",
                    "minimum_sample_count_not_met",
                    "recent_safe_sample_window_not_met",
                    "project_quality_gate_not_passed",
                    "quality_below_threshold", "safety_below_threshold",
                    "master_video_missing", "emergency_stopped",
                }
                transient = (
                    any(reason in policy_holds for reason in reasons)
                    or any(
                        reason.endswith((
                            "_auto_publish_disabled",
                            "_authentication_missing",
                            "_public_https_video_url_required",
                        ))
                        for reason in reasons)
                )
                next_status = "queued" if transient else "retry"
                delay = int(_float(
                    "SHORT_VIDEO_POLICY_HOLD_RETRY_MINUTES",
                    60 if transient else 15))
                repo.write(
                    """UPDATE short_video_publication_queue
                       SET status=?,scheduled_at=?,last_error_type=?,updated_at=?
                       WHERE id=?""",
                    (next_status, (_now() + timedelta(minutes=delay)).isoformat(),
                     ",".join(reasons)[:300], _now().isoformat(), item["id"]),
                    self.path)
        metrics = self.metrics_sync(dry_run=not live)
        return {
            "due": len(queued), "results": results, "metrics": metrics,
            "live": live,
            "external_writes": sum(
                int(x.get("external_writes") or 0)
                for x in results
                if isinstance(x.get("external_writes"), int)),
        }

    def scheduled_run(self, live_publish: bool = False) -> dict:
        """Build at most the configured daily inventory, then process the queue."""
        if not self.settings()["enabled"]:
            return {"status": "disabled", "external_writes": 0}
        today = _now().date().isoformat()
        created_today = int(repo.rows(
            """SELECT COUNT(*) AS n FROM short_video_projects
               WHERE substr(created_at,1,10)=?""", (today,), self.path)[0]["n"])
        daily_limit = max(
            0, int(_float("SHORT_VIDEO_PUBLISH_READY_LIMIT_DAILY", 3)))
        cycle = None
        if created_today < daily_limit:
            existing = {
                int(row["topic_id"]) for row in repo.rows(
                    "SELECT DISTINCT topic_id FROM short_video_projects",
                    path=self.path)
                if row.get("topic_id") is not None
            }
            candidate = next(
                (row for row in self.candidates(50)["candidates"]
                 if row["eligible"] and int(row["topic_id"]) not in existing),
                None)
            if candidate:
                cycle = self.full_cycle(
                    int(candidate["topic_id"]), dry_run=False)
                video_id = cycle.get("video_id")
                quality = (cycle.get("stages") or {}).get("quality") or {}
                if video_id and quality.get("passed"):
                    enabled = tuple(
                        platform for platform in PLATFORMS
                        if _bool(
                            f"SHORT_VIDEO_{platform.upper()}_ENABLED",
                            "true" if platform in {"x", "threads", "youtube"}
                            else "false"))
                    cycle["queue"] = self.queue(video_id, enabled)
        worker = self.run_queue(live=live_publish)
        return {
            "status": "completed", "created_today_before": created_today,
            "daily_limit": daily_limit, "cycle": cycle, "worker": worker,
            "external_writes": worker["external_writes"],
        }

    def experiment_report(self) -> dict:
        return {"experiments": repo.rows(
            """SELECT experiment_id,platform,variant,status,result_json
               FROM short_video_experiments ORDER BY id DESC LIMIT 100""",
            path=self.path)}

    def report(self, weekly: bool = False) -> dict:
        projects = repo.rows(
            """SELECT status,COUNT(*) AS n,AVG(quality_score) AS quality,
               AVG(safety_score) AS safety FROM short_video_projects
               GROUP BY status""", path=self.path)
        metrics = repo.rows(
            """SELECT platform,COUNT(*) AS samples,AVG(views) AS views,
               AVG(completion_rate) AS completion_rate
               FROM short_video_metrics GROUP BY platform""", path=self.path)
        return {"period": "weekly" if weekly else "daily",
                "projects": projects, "metrics": metrics,
                "missing_metrics_remain_null": True}

    def full_cycle(self, topic_id: int, dry_run: bool = True) -> dict:
        created = self.project_create(topic_id)
        if created.get("status") == "blocked":
            return {"status": "blocked", "candidate": created}
        video_id = created["video_id"]
        stages = {
            "script": self.script_generate(video_id),
            "script_check": self.script_check(video_id),
            "audio": self.audio_generate(video_id),
            "captions": self.captions_generate(video_id),
            "visuals": self.visual_plan(video_id),
        }
        stages["render"] = self.render(video_id, dry_run=dry_run)
        stages["quality"] = self.quality_check(video_id)
        stages["variants"] = self.platform_variants(video_id)
        stages["plan"] = self.publish_plan(video_id)
        result = {"video_id": video_id, "status": "review_required"
                if stages["render"]["status"] != "ready" else "completed",
                "stages": stages, "external_writes": 0}
        if _bool("SHORT_VIDEO_DISCORD_ENABLED", "false"):
            try:
                from discord_notify import notify
                result["discord_sent"] = notify(
                    "short_video",
                    "🎬 Short動画生成結果",
                    "統合リサーチから公開計画までの処理が完了しました。",
                    fields={
                        "video_id": video_id,
                        "結果": result["status"],
                        "品質": stages["quality"]["quality_score"],
                        "安全性": stages["quality"]["safety_score"],
                        "外部投稿": "0件",
                    })
            except Exception:
                result["discord_sent"] = False
        log_path = self.output / video_id / "generation_log.md"
        log_path.write_text(
            "# Short Video Generation Log 🎬\n\n"
            f"- video_id: `{video_id}`\n"
            f"- status: `{result['status']}`\n"
            f"- external writes: `0`\n"
            f"- generated at: `{_now().isoformat()}`\n",
            encoding="utf-8")
        result["generation_log"] = str(log_path)
        return result

    def emergency_stop(self) -> dict:
        repo.write(
            """INSERT OR REPLACE INTO short_video_system_state
               (state_key,state_value,updated_at) VALUES ('emergency_stop','true',?)""",
            (_now().isoformat(),), self.path)
        return {"emergency_stopped": True}

    def emergency_resume(self, confirm: bool = False) -> dict:
        if not confirm:
            return {"emergency_stopped": True,
                    "reason": "explicit_confirmation_required"}
        repo.write(
            """INSERT OR REPLACE INTO short_video_system_state
               (state_key,state_value,updated_at) VALUES ('emergency_stop','false',?)""",
            (_now().isoformat(),), self.path)
        return {"emergency_stopped": False}
