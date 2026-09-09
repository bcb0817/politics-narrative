"""Regression checks for Astra runtime isolation; no network or live state."""
import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import post  # noqa: E402


class TopicalRuntimeIsolationTests(unittest.TestCase):
    def test_topical_mode_ignores_retired_daily_remediation(self):
        now = datetime(2026, 9, 9, 0, tzinfo=timezone.utc)
        with patch.dict(os.environ, {"TOPICAL_EDITOR_ENABLED": "true"}, clear=False), patch(
            "daily_post_goal.load_active_remediation",
            return_value={"shortfall": 32, "prefilter_top_n": 4},
        ) as remediation:
            with patch.object(post, "load_post_history", return_value=[]):
                # Exercise the policy read through the live route, stopping
                # before any news collection or publication can occur.
                self.assertTrue(post.topical_editor.enabled())
                self.assertEqual(
                    post._daily_goal_policy(now),
                    {},
                )
            remediation.assert_called_once()

    def test_main_supervisor_does_not_start_short_video_service(self):
        text = (ROOT / "production" / "run_bot.ps1").read_text(encoding="utf-8")
        self.assertNotIn("short_video_media_start.ps1", text)


if __name__ == "__main__":
    unittest.main()
