"""Read-only posting audit and NON-PUBLISHING comparison through the real generator.

python -m production.review_short_quality --run-id 20261005 --limit 10 [--api]
No X request is made by this entry point. The existing shared cost
ledger and lock apply to paid generation. Draft DB is isolated under outputs.
"""
import argparse
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from src.article_generation import ROOT, load, save
from src.article_api import Client, Ledger, process_lock, read_settings_env
from src.short_posts import Store, generate, REPAIRABLE, validate_post, repair_reason
from src.short_quality import audit, fetch_news, REVIEW, REVIEW_TASK, style_signals

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-id',required=True); p.add_argument('--limit',type=int,default=10); p.add_argument('--api',action='store_true')
    p.add_argument('--repair',action='store_true',help='At most one eligible revision, using the same per-candidate budget')
    p.add_argument('--recheck',action='store_true',help='One idempotent final-policy review of offline drafts; no publication')
    args=p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',args.run_id) or not 1<=args.limit<=10: p.error('invalid run/limit')
    destination=ROOT/'outputs/quality_review'/args.run_id
    with process_lock(ROOT/'outputs/articles/generation.lock'):
        with closing(sqlite3.connect((ROOT/'data/bot_metrics.db').as_uri()+'?mode=ro',uri=True)) as c:
            c.row_factory=sqlite3.Row
            posted=[dict(r) for r in c.execute("SELECT * FROM short_items WHERE status='published' ORDER BY created DESC LIMIT 100")]
        save(destination/'audit.json',audit(posted))
        cfg=load(ROOT/'config/short_posts.json')
        candidates=[i for i in load(ROOT/'data/rss_candidates.json')['candidates'] if '動静' not in i['title']][:args.limit]
        def retrieve(item):
            path=destination/'sources'/f"{item['id']}.json"
            if path.exists(): return load(path)
            source=fetch_news(item['url'],1,max_chars=cfg['source_max_chars']); save(path,source); return source
        with ThreadPoolExecutor(max_workers=3) as pool: sources=list(pool.map(retrieve,candidates))
        env=read_settings_env(ROOT/'.env') if args.api else {}
        model=load(ROOT/'config/article_generation.json'); model['max_output_tokens']=cfg['max_output_tokens']
        ledger=Ledger(ROOT/'data/bot_metrics.db',model,env) if args.api else None
        client=Client(model,ledger,env) if args.api else None
        store=Store(destination/'samples.db')
        prefix='quality_eval_'+args.run_id+'_'
        records=[]
        for item,source in zip(candidates,sources):
            row=next((r for r in store.items() if r['url']==item['url']),None)
            history=[dict(id=r['id'],url=r['url'],title=r['title'],text=r['body'],angle=None,form=None,facts=[]) for r in posted[:cfg['recent_post_count']] if r['url']!=item['url']]
            outcome=row['status'] if row else 'not_generated'
            if args.api and row is None and not source.get('error'):
                outcome=generate(store,client,cfg,item,datetime.now(timezone.utc),destination/'drafts',lambda *a,**k:source,prefix,history)
                print(json.dumps(dict(title=item['title'],outcome=outcome),ensure_ascii=False),flush=True)
            if args.api and args.repair:
                current=next((r for r in store.items() if r['url']==item['url']),None)
                if current and current['status']=='needs_research' and current['reason'] and set(current['reason'].split(','))<=REPAIRABLE:
                    outcome=generate(store,client,cfg,item,datetime.now(timezone.utc),destination/'drafts',lambda *a,**k:source,prefix,history)
                    print(json.dumps(dict(title=item['title'],revision_outcome=outcome),ensure_ascii=False),flush=True)
            artifact=destination/'drafts'/item['id']
            draft=load(artifact/'draft.json') if (artifact/'draft.json').exists() else None
            review=load(artifact/'review.json') if (artifact/'review.json').exists() else None
            if args.api and args.recheck and draft and review and not any(r['stage']=='short_revision' for r in ledger.rows(prefix+item['id'])):
                try:
                    packet=load(artifact/'context.json')
                    review=client.call(prefix+item['id'],'short_quality_check',json.dumps(dict(article=draft,source=packet,style_signals=style_signals(draft['text'],history),task=REVIEW_TASK),ensure_ascii=False),REVIEW,cfg['generation_budget_usd'],(ROOT/'config/short_editorial.md').read_text(encoding='utf-8'))
                    save(artifact/'review.json',review)
                except Exception as exc:
                    outcome=type(exc).__name__
                    # A missing final review cannot be represented as a pass.
                    review=None
                print(json.dumps(dict(title=item['title'],rechecked=review is not None),ensure_ascii=False),flush=True)
            errors=(['editorial_skip'] if draft and draft['decision']=='skip' else
                    validate_post(draft,review,source,history) if draft and review else ['not_reviewed'])
            if errors:
                outcome='needs_research'
                if review: store.update(item['id'],status='needs_research',reason=','.join(repair_reason(errors,review)))
            elif draft and review:
                outcome='ready'; store.update(item['id'],status='ready',body=draft['text'],reason=None)
            original=next((r for r in posted if r['url']==item['url']),None)
            baseline=original['body'] if original else ('NHKによると、'+draft['facts'][0]['text']+'今後の動向に注目です。' if draft and draft['facts'] else None)
            record=dict(title=item['title'],url=item['url'],retrieved_at=source.get('retrieved_at'),source_error=source.get('error'),
                source_scope=source.get('confirmed_scope'),before=baseline,before_type='published' if original else 'illustrative_reconstruction_not_old_model_output',
                after=draft['text'] if draft else None,angle=draft['angle'] if draft else None,outcome=outcome,validation_errors=errors,
                draft=draft,review=review,usage=ledger.summary(prefix+item['id']) if ledger else (load(artifact/'usage.json') if (artifact/'usage.json').exists() else None),
                published=False,comparison_note='Existing same-URL post excluded ONLY for offline rewriting comparison; production does not bypass duplicate checks.')
            if ledger and artifact.exists(): save(artifact/'usage.json',record['usage'])
            records.append(record); save(destination/'comparisons.json',records)
        print(json.dumps(dict(output=str(destination),samples=len(records),audit=audit(posted),published=False),ensure_ascii=False))

if __name__=='__main__': main()
