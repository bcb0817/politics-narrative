from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "special_research_kumamoto.py"
SPEC = importlib.util.spec_from_file_location("special_research_kumamoto", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_generate_complete_nonpublishing_packet(tmp_path):
    out = MODULE.generate(tmp_path)
    required = {
        "research_summary.md", "research_packet.json", "official_fact_ledger.json",
        "damage_ledger.json", "timeline.json", "sources.md", "source_matrix.csv",
        "aeon_mall_findings.md", "cogeneration_explainer.md",
        "misinformation_review.md", "unknowns.md", "x_posts.md", "x_thread.md",
        "threads_posts.md", "visual_brief.md", "short_script.md",
        "x_article_outline.md", "note_outline.md", "quality_report.json",
        "research_log.md",
    }
    assert required == {p.name for p in out.iterdir()}
    packet = json.loads((out / "research_packet.json").read_text(encoding="utf-8"))
    quality = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    assert packet["research_cutoff_at"] == "2026-07-29 03:10 JST"
    assert packet["publish"] is False
    assert packet["threads_analysis"]["status"] == "skipped_permission_missing"
    assert packet["x_search"]["api_requests"] == 1
    assert packet["x_search"]["retrieved_posts"] == 0
    assert quality["total"] >= 18
    assert quality["failed"] == []


def test_sensitive_claims_remain_unconfirmed():
    packet = MODULE.build_packet()
    assert packet["inferences"] == []
    assert any("事故原因" in x for x in packet["unverified_claims"])
    assert any("2016年" in x for x in packet["unverified_claims"])
    assert any(x["fact_id"] == "casualties" and x["status"] == "confirmed"
               and "確認中" in x["statement"] for x in MODULE.FACTS)
    assert any(x["fact_id"] == "fault" and x["status"] == "unknown"
               for x in MODULE.FACTS)


def test_disaster_copy_is_bounded_and_has_no_emoji():
    all_copy = MODULE.X_POSTS + MODULE.X_THREAD + MODULE.THREADS_POSTS
    assert all(len(text) <= 280 for text in MODULE.X_POSTS)
    assert not any(MODULE.re.compile(
        "[\U0001F300-\U0001FAFF\u2600-\u27BF]").search(text)
        for text in all_copy)
    assert all("時点" in text or "確認" in text for text in MODULE.X_POSTS)


def test_damage_ledger_has_source_time_and_separate_rows():
    assert all(row["as_of"] for row in MODULE.DAMAGE)
    assert all(row["source_url"] for row in MODULE.DAMAGE)
    keys = [(row["item_type"], row["location"], row["as_of"])
            for row in MODULE.DAMAGE]
    assert len(keys) == len(set(keys))


def test_script_has_no_publication_or_git_calls():
    text = SCRIPT.read_text(encoding="utf-8")
    forbidden = [
        "post_tweet(", "publish_threads(", "subprocess", "git commit",
        "git push", "Set-ScheduledTask", "Register-ScheduledTask",
    ]
    assert not any(token in text for token in forbidden)
    assert "--no-publish" in text
