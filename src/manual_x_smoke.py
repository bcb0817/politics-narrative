"""Explicitly authorized, one-shot ordinary X post test; never scheduled.

No article publication or arbitrary text interface. A durable singleton prevents
repeated sends, including after a lost response. Costs are reserved, not measured.
"""
import argparse
import json
import math
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .article_api import Ledger, process_lock
from .article_generation import ROOT, load, save

TEXT = '動作確認のテスト投稿です。'
RESERVATION_USD = .025  # User read .010 + URL-free post .015, verified 2026-10-02.
SLOT = 'single-publication-smoke-v1'
KEYS = ('API_KEY', 'API_KEY_SECRET', 'ACCESS_TOKEN', 'ACCESS_TOKEN_SECRET')
SETTINGS = KEYS + ('POST_ENABLED', 'X_POST_ENABLED', 'X_MONTHLY_BUDGET_USD',
    'X_BUDGET_RESERVE_USD', 'TOTAL_MONTHLY_API_BUDGET_USD', 'TOTAL_BUDGET_RESERVE_USD',
    'X_POST_CREATE_MAX_PER_DAY', 'X_POST_CREATE_MAX_PER_MONTH',
    'X_OWNED_READ_MAX_PER_DAY', 'X_OWNED_READ_MAX_PER_MONTH')


def environment(path):
    values = {}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            if key.strip() in SETTINGS:
                values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({k: os.environ[k] for k in SETTINGS if k in os.environ})
    return values


def request(method, path, settings, payload=None):
    # Already installed locally; only the explicit live command imports it.
    import requests
    from requests_oauthlib import OAuth1
    if (method, path) not in (('GET', '/2/users/me'), ('POST', '/2/tweets')):
        raise ValueError('endpoint_not_allowed')
    with requests.Session() as session:
        session.trust_env = False
        session.auth = OAuth1(*(settings[k] for k in KEYS))
        with session.request(method, 'https://api.x.com' + path, json=payload,
                             timeout=(10, 40), allow_redirects=False, stream=True) as response:
            body = bytearray()
            for chunk in response.iter_content(4096):
                body.extend(chunk)
                if len(body) > 100000: raise ValueError('response_too_large')
            return response.status_code, json.loads(body)


def execute(db, settings, expected_user, send=request, clock=None):
    if not re.fullmatch(r'[A-Za-z0-9_]{1,15}', expected_user):
        raise ValueError('invalid_expected_user')
    if any(not settings.get(k) for k in KEYS): raise ValueError('missing_x_credentials')
    if any(settings.get(k, '').lower() != 'true' for k in ('POST_ENABLED', 'X_POST_ENABLED')):
        raise ValueError('publication_disabled_no_override')
    def amount(key):
        value = float(settings[key])
        if not math.isfinite(value) or value < 0: raise ValueError('invalid_limit')
        return value
    for key in ('X_POST_CREATE_MAX_PER_DAY', 'X_POST_CREATE_MAX_PER_MONTH',
                'X_OWNED_READ_MAX_PER_DAY', 'X_OWNED_READ_MAX_PER_MONTH'):
        if amount(key) < 1: raise ValueError('quota_disabled')
    at = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone(timedelta(hours=9)))
    month = at.strftime('%Y-%m')
    cfg = load(ROOT / 'config/article_generation.json')
    ledger = Ledger(db, cfg, {}, lambda: at)
    with closing(sqlite3.connect(db, timeout=20)) as c:
        c.execute('CREATE TABLE IF NOT EXISTS x_manual_smoke (id TEXT PRIMARY KEY, month TEXT, status TEXT, reserved_usd REAL, result_json TEXT)')
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        previous = c.execute('SELECT result_json FROM x_manual_smoke WHERE id=?', (SLOT,)).fetchone()
        if previous:
            result = json.loads(previous[0]); result['replayed_without_request'] = True
            return result
        # This one-off is intentionally not compatible with a restored auto-post DB.
        # Fail closed instead of bypassing unknown legacy quotas/delivery state.
        names = {r[0] for r in c.execute('SELECT name FROM sqlite_master')}
        if {'posts', 'api_usage_events', 'x_delivery_attempts'} & names:
            raise ValueError('legacy_runtime_present_requires_integrated_quota_review')
        total, _ = ledger.legacy_spend(c, month)
        article_cost = c.execute('SELECT COALESCE(SUM(charged),0) FROM article_calls WHERE month=?', (month,)).fetchone()[0]
        x_cost = c.execute('SELECT COALESCE(SUM(reserved_usd),0) FROM x_manual_smoke WHERE month=?', (month,)).fetchone()[0]
        if (x_cost + RESERVATION_USD > amount('X_MONTHLY_BUDGET_USD') - amount('X_BUDGET_RESERVE_USD') or
            total + article_cost + RESERVATION_USD > amount('TOTAL_MONTHLY_API_BUDGET_USD') - amount('TOTAL_BUDGET_RESERVE_USD')):
            raise ValueError('budget_exceeded')
        result = dict(status='checking_account', expected_user=expected_user, text=TEXT,
                      started_at=at.isoformat(), actual_cost_usd=None,
                      reserved_usd=RESERVATION_USD, auto_publish=False)
        c.execute('INSERT INTO x_manual_smoke VALUES(?,?,?,?,?)',
                  (SLOT, month, result['status'], RESERVATION_USD, json.dumps(result)))
        c.commit()
        def checkpoint(status, **fields):
            result.update(status=status, **fields)
            c.execute('UPDATE x_manual_smoke SET status=?,result_json=? WHERE id=?',
                      (status, json.dumps(result, ensure_ascii=False), SLOT)); c.commit()
        try:
            status, response = send('GET', '/2/users/me', settings)
            if status != 200:
                checkpoint('account_check_failed', http_status=status); return result
            account = response.get('data') or {}
            if account.get('username', '').casefold() != expected_user.casefold() or not re.fullmatch(r'\d+', account.get('id', '')):
                checkpoint('account_mismatch'); return result
            checkpoint('sending', account_id=account['id'], username=account['username'])
            status, response = send('POST', '/2/tweets', settings, {'text': TEXT})
            post = response.get('data') or {}
            if status == 201 and re.fullmatch(r'\d+', post.get('id', '')) and post.get('text') == TEXT:
                checkpoint('published', post_id=post['id'],
                           url=f'https://x.com/{account["username"]}/status/{post["id"]}')
            elif status in (400, 401, 403, 404, 422, 429):
                checkpoint('rejected', http_status=status)
            else:
                checkpoint('ambiguous', http_status=status)
        except Exception as exc:
            checkpoint('ambiguous' if result['status'] == 'sending' else 'account_check_failed',
                       error_type=type(exc).__name__)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--expected-user', required=True)
    args = parser.parse_args(argv)
    if not args.send:
        print(json.dumps(dict(status='dry_run', text=TEXT, account=args.expected_user), ensure_ascii=False)); return 0
    with process_lock(ROOT / 'outputs/articles/generation.lock'):
        result = execute(ROOT / 'data/bot_metrics.db', environment(ROOT / '.env'), args.expected_user)
        save(ROOT / 'outputs/manual_x_smoke/result.json', result)
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] == 'published' else 2


if __name__ == '__main__':
    raise SystemExit(main())
