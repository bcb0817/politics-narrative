import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import local_bot  # noqa: E402


class DaemonSingletonTests(unittest.TestCase):
    def tearDown(self):
        local_bot.release_daemon_lock()

    def test_second_process_cannot_acquire_daemon_lock(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            env = {**os.environ, "STATE_DIR": tmp}
            with patch.dict(os.environ, {"STATE_DIR": tmp}, clear=False):
                self.assertTrue(local_bot.acquire_daemon_lock())
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import local_bot;"
                            "print('acquired' if local_bot.acquire_daemon_lock() "
                            "else 'blocked')"
                        ),
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=True,
                )
                self.assertEqual(child.stdout.strip(), "blocked")

    def test_lock_is_released_for_next_process(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            env = {**os.environ, "STATE_DIR": tmp}
            with patch.dict(os.environ, {"STATE_DIR": tmp}, clear=False):
                self.assertTrue(local_bot.acquire_daemon_lock())
                local_bot.release_daemon_lock()
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import local_bot;"
                            "print('acquired' if local_bot.acquire_daemon_lock() "
                            "else 'blocked')"
                        ),
                    ],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=True,
                )
                self.assertEqual(child.stdout.strip(), "acquired")


if __name__ == "__main__":
    unittest.main()
