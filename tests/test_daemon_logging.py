import tempfile
import ast
import inspect
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
import local_bot


class DaemonLoggingTests(unittest.TestCase):
    def test_review_summary_resolves_path_before_late_persistence(self):
        tree = ast.parse(inspect.getsource(local_bot.cmd_report))
        calls = [n for n in ast.walk(tree) if isinstance(n,ast.Call)
                 and isinstance(n.func,ast.Name) and n.func.id=='integrated_daily_review_summary']
        self.assertEqual(len(calls),1)
        path = next(k.value for k in calls[0].keywords if k.arg=='path')
        self.assertIsInstance(path,ast.Call)
        self.assertEqual(path.func.id,'review_summary_db_path')
    def test_log_is_persisted_without_state_write(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(local_bot,'resolve_dir',return_value=Path(tmp)), patch.object(local_bot,'datetime') as clock:
            clock.now.return_value=datetime(2026,9,8,15,0,tzinfo=ZoneInfo('Asia/Tokyo'))
            local_bot.log('daemon regression')
            self.assertEqual((Path(tmp)/'bot.log').read_text(encoding='utf-8'),'2026-09-08 15:00:00 daemon regression\n')

    def test_atomic_write_does_not_log_undefined_line(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(local_bot,'_append_log_line') as append:
            target=Path(tmp)/'state.json'
            local_bot.atomic_write_text(target,'{}')
            self.assertEqual(target.read_text(encoding='utf-8'),'{}')
            append.assert_not_called()
