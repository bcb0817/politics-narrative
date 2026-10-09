"""Fictional account and mock responses only; no network or production env."""
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from src.manual_x_smoke import execute, TEXT, KEYS

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'ledger.db'
        self.env = {k: 'TEST_ONLY' for k in KEYS}
        self.env.update(POST_ENABLED='true', X_POST_ENABLED='true', X_MONTHLY_BUDGET_USD='16',
                        X_BUDGET_RESERVE_USD='.75', TOTAL_MONTHLY_API_BUDGET_USD='61',
                        TOTAL_BUDGET_RESERVE_USD='3.25', X_POST_CREATE_MAX_PER_DAY='20',
                        X_POST_CREATE_MAX_PER_MONTH='600', X_OWNED_READ_MAX_PER_DAY='36',
                        X_OWNED_READ_MAX_PER_MONTH='1080')
        self.calls = []
    def send(self, method, path, env, payload=None):
        self.calls.append(method)
        return (200, {'data': {'id': '123', 'username': 'fiction_test'}}) if method == 'GET' else (201, {'data': {'id': '456', 'text': TEXT}})
    def run_test(self, send=None):
        return execute(self.db, self.env, 'fiction_test', send or self.send, lambda: NOW)
    def test_publish_exactly_once(self):
        self.assertEqual(self.run_test()['status'], 'published')
        self.assertTrue(self.run_test()['replayed_without_request'])
        self.assertEqual(self.calls, ['GET', 'POST'])
    def test_wrong_account_never_posts(self):
        def send(*a): self.calls.append('GET'); return 200, {'data': {'id': '123', 'username': 'other'}}
        self.assertEqual(self.run_test(send)['status'], 'account_mismatch')
        self.assertEqual(self.calls, ['GET'])
    def test_lost_post_response_never_retries(self):
        def send(method, *args):
            if method == 'POST': self.calls.append('POST'); raise TimeoutError('DO_NOT_LOG_SECRET')
            return self.send(method, *args)
        result = self.run_test(send)
        self.assertEqual(result['status'], 'ambiguous'); self.assertNotIn('DO_NOT_LOG_SECRET', str(result))
        self.assertEqual(self.run_test()['status'], 'ambiguous'); self.assertEqual(self.calls, ['GET', 'POST'])
    def test_disabled_flag_blocks(self):
        self.env['POST_ENABLED'] = 'false'
        with self.assertRaises(ValueError): self.run_test()
        self.assertEqual(self.calls, [])
    def test_budget_blocks_before_request(self):
        self.env['X_MONTHLY_BUDGET_USD'] = '.75'
        with self.assertRaises(ValueError): self.run_test()
        self.assertEqual(self.calls, [])
    def test_auth_error_never_posts_or_retries(self):
        def send(*a): self.calls.append('GET'); return 401, {}
        self.assertEqual(self.run_test(send)['status'], 'account_check_failed')
        self.run_test(); self.assertEqual(self.calls, ['GET'])
    def test_article_budget_includes_smoke_reservation(self):
        self.run_test()
        from src.article_api import Ledger
        from src.article_generation import load, ROOT
        from contextlib import closing
        ledger = Ledger(self.db, load(ROOT / 'config/article_generation.json'), {})
        with closing(ledger.connect()) as c:
            total, xai = ledger.legacy_spend(c, '2026-10')
        self.assertEqual(total, .025); self.assertEqual(xai, 0)
