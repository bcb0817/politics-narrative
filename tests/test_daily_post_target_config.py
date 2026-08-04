import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import post
from publishing_policy import phase_daily_limit_reached


JST = ZoneInfo("Asia/Tokyo")


def _history(now, normal, breaking=0):
    kinds = ["strong_opinion"] * normal + ["breaking_news"] * breaking
    return [
        {
            "tweet_id": str(index),
            "post_type": kind,
            "posted_at_jst": (now - timedelta(minutes=index + 1)).isoformat(),
        }
        for index, kind in enumerate(kinds)
    ]


class DailyPostTargetConfigTests(unittest.TestCase):
    def test_twenty_is_reachable_without_breaking_posts(self):
        now = datetime(2026, 8, 2, 20, 0, tzinfo=JST)
        self.assertFalse(phase_daily_limit_reached(_history(now, 19), now, False))
        self.assertTrue(phase_daily_limit_reached(_history(now, 20), now, False))

    def test_breaking_posts_are_inside_total_twenty(self):
        now = datetime(2026, 8, 2, 20, 0, tzinfo=JST)
        rows = _history(now, 18, 2)
        self.assertTrue(phase_daily_limit_reached(rows, now, False))
        self.assertTrue(phase_daily_limit_reached(rows, now, True))

    def test_verified_evergreen_supply_is_bounded_at_two(self):
        now = datetime(2026, 8, 2, 20, 0, tzinfo=JST)
        rows = _history(now, 1)
        rows[0]["posted_at_jst"] = (now - timedelta(hours=4)).isoformat()
        rows[0]["post_type"] = "evergreen_explainer"
        rows[0]["topic_key"] = "used"
        with patch.dict(os.environ, {"EVERGREEN_MAX_PER_DAY": "2"}):
            self.assertIsNotNone(post._evergreen_candidate(rows, now))
            rows.append({
                "tweet_id": "second",
                "post_type": "evergreen_explainer",
                "posted_at_jst": (now - timedelta(hours=3, minutes=30)).isoformat(),
                "topic_key": "used-2",
            })
            self.assertIsNone(post._evergreen_candidate(rows, now))

    def test_stale_cache_retry_requires_active_remediation_and_is_bounded(self):
        enabled = {"retry_transient_slots": True}
        self.assertTrue(post._should_retry_stale_candidate_cache(
            cache_hit=True, usable_count=0,
            remediation_policy=enabled, retry_used=False))
        self.assertFalse(post._should_retry_stale_candidate_cache(
            cache_hit=True, usable_count=0,
            remediation_policy=enabled, retry_used=True))
        self.assertFalse(post._should_retry_stale_candidate_cache(
            cache_hit=True, usable_count=1,
            remediation_policy=enabled, retry_used=False))
        self.assertFalse(post._should_retry_stale_candidate_cache(
            cache_hit=True, usable_count=0,
            remediation_policy={}, retry_used=False))

    def test_apply_script_requires_explicit_apply(self):
        source = (ROOT / "production" / "set_daily_post_target.ps1").read_text(
            encoding="utf-8")
        self.assertIn("[switch]$Apply", source)
        self.assertIn("if (-not $Apply)", source)
        self.assertNotIn("Restart-Service", source)
        self.assertNotIn("Register-ScheduledTask", source)
        self.assertIn("[int]$Target = 20", source)
        self.assertIn("MONITOR_INTERVAL_MINUTES = 45", source)


if __name__ == "__main__":
    unittest.main()
