import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
import local_bot


class DaemonLoggingTests(unittest.TestCase):
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
