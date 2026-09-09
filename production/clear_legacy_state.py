"""Retire pre-cutoff generated state while preserving publication and cost evidence."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def clean(path, cutoff, *, apply=False):
    if datetime.fromisoformat(cutoff).tzinfo is None:
        raise ValueError('cutoff_requires_timezone')
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {}
        # No DELETE/UPDATE of publication, metrics, incidents, API ledger, batches,
        # or delivery tables. Missing dates and uncertain publication states stay.
        for table, date, extra in [
            ('daily_reviews','generated_at','1'), ('weekly_reviews','generated_at','1'),
            ('social_anger_weekly_reviews','created_at','1'), ('reach_models','trained_at','1'),
            ('reach_assignments','assigned_at',"tweet_id IS NULL"),
            ('reach_inventory','first_seen_at','1'),
        ]:
            if table not in tables:
                continue
            where = f'julianday({date}) < julianday(?) AND ({extra})'
            result[table] = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}', (cutoff,)).fetchone()[0]
            if apply:
                conn.execute(f'DELETE FROM {table} WHERE {where}', (cutoff,))
        if {'generated_posts','published_posts'} <= tables:
            deliveries = {r[0] for r in conn.execute('SELECT payload_hash FROM x_delivery')} if 'x_delivery' in tables else set()
            ids = []
            for ident, text in conn.execute('''SELECT id,text FROM generated_posts WHERE
                julianday(created_at)<julianday(?) AND COALESCE(model,'')<>'gpt-6-astra'
                AND COALESCE(decision,'')<>'retired_legacy'
                AND id NOT IN (SELECT generated_post_id FROM published_posts WHERE generated_post_id IS NOT NULL)''', (cutoff,)):
                digest = hashlib.sha256(json.dumps({'text':text},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                if digest not in deliveries:
                    ids.append(ident)
            result['unpublished_generated_payloads'] = len(ids)
            if apply:
                conn.executemany("UPDATE generated_posts SET text='',threads_text='',decision='retired_legacy',decision_reason='legacy_reset_20260908',quality_score=NULL WHERE id=?", ((i,) for i in ids))
        # Keep IDs so historical joins remain valid, erase only unused draft payloads.
        for table, payload in [
            ('engagement_queue','content_json'), ('content_visual_candidates','brief_json'),
            ('content_thread_candidates','posts_json'), ('content_short_candidates','short_script_outline_json'),
            ('content_longform_candidates','outline_json'), ('reply_candidates','body'),
            ('quote_candidates','body'), ('social_anger_candidates','candidate_text'),
            ('note_drafts','draft_path'),
        ]:
            if table not in tables:
                continue
            where = "julianday(created_at)<julianday(?) AND status IN ('pending','draft','generated','ready','revision_required','failed','visual_ready','thread_ready','short_ready','longform_ready','approval_required','rejected')"
            result[table] = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}',(cutoff,)).fetchone()[0]
            if apply:
                conn.execute(f"UPDATE {table} SET {payload}='',status='retired_legacy' WHERE {where}",(cutoff,))
        if apply:
            conn.commit()
        else:
            conn.rollback()
        result['integrity'] = conn.execute('PRAGMA integrity_check').fetchone()[0]
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cutoff', default='2026-09-08T00:00:00+09:00')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup', type=Path, required=True)
    args = parser.parse_args()
    backup = args.backup.resolve()
    if not backup.is_relative_to(ROOT/'backups') or not (backup/'data/bot_metrics.db').is_file():
        raise SystemExit('Existing repository backup with DB is required')
    print(json.dumps(clean(ROOT/'data/bot_metrics.db',args.cutoff,apply=args.apply),ensure_ascii=False))
