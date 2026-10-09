"""Three daily research snapshots; no social publication or popularity estimates."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from .social_radar import ROOT, JST, Store, instant, url_key, validate_config
from .social_radar_api import RadarLedger, SearchClient
from .article_api import process_lock, read_settings_env, BudgetExceeded, AmbiguousCall
from .article_generation import load, save

HOURS = (8, 14, 20)
QUERY = '''日本で直近24時間にX上で批判・反発・苦情・賛否の論争が生じている具体的な出来事を最大5件探す。
企業、商品、政策、行政、社会問題、公開活動中の公人を対象とする。謝罪・撤回がない出来事も含める。
最初に複数分野を探索し、有力候補が少なければ同じ呼び出し内で分野を広げる。
確認済みの大規模炎上だけに限定せず、根拠リンクがある小規模な論争を調査候補に含める。
各候補に具体的な出来事、反応の根拠URL、原典（不明ならnull）、当事者の説明、有力な反論、未確認事項を示す。
原典未取得は除外理由ではなく確認待ちとして記録する。存在しないリンクや件数を作らない。
候補数を満たすための創作は禁止。一般私人や属性集団への攻撃一覧は作らない。
古い事件を新規と呼ばず、直近にどの新しい反応や情報があったかをreasonに記す。'''


def slot(at):
    local = at.astimezone(JST)
    hours = [h for h in HOURS if h <= local.hour]
    if not hours:
        local -= timedelta(days=1)
    return 'hunter-' + local.strftime('%Y%m%d') + '-' + str(max(hours) if hours else 20)


def rank_candidates(result, at):
    cited = set()
    for u in result.get('citations', []):
        try:
            cited.add(url_key(u))
        except (ValueError, AttributeError):
            pass
    rows, seen = [], set()
    for item in result.get('topics', []):
        if item.get('target_kind') not in ('organization', 'policy', 'public_figure'):
            continue
        urls = []
        for u in item.get('source_urls', []):
            try:
                u = url_key(u)
                if u in cited and u.startswith('https://'):
                    urls.append(u)
            except (ValueError, AttributeError):
                pass
        urls = sorted(set(urls))
        if not urls or not item.get('event') or not item.get('title'):
            continue
        key = (item.get('target'), item['event'])
        if key in seen:
            continue
        seen.add(key)
        try:
            original = url_key(item['original_url']) if item.get('original_url') else None
        except (ValueError, AttributeError):
            original = None
        original = original if original in cited else None
        # Structural evidence score, NOT sentiment, reach, or a verified fact score.
        parts = {'引用リンク': min(len(urls), 3) * 10,
                 '原典リンク': 25 if original else 0,
                 'X以外の資料リンク': 15 if any(urlsplit(u).hostname != 'x.com' for u in urls) else 0,
                 '反論の記録': 15 if item.get('counterarguments') else 0,
                 '未確認事項の記録': 15 if item.get('unknowns') else 0}
        rows.append(dict(title=item['title'], event=item['event'], categories=item.get('categories', []),
                         state='held', angle=item.get('angle', ''), source_urls=urls,
                         original_url=original, reason=item.get('reason', ''),
                         unknowns=item.get('unknowns', []) + ['元投稿本文・反応規模・厳密な24時間内の適格性は未検証'],
                         counterarguments=item.get('counterarguments', []),
                         score=sum(parts.values()), score_parts=parts, score_version='evidence-priority-v1',
                         score_label='調査優先度（炎上規模ではない）',
                         tier='原典リンクあり・要検証' if original else '調査候補・原典確認待ち',
                         reported_event_at=item.get('event_at'), verified_event_at=None,
                         observed_at=at.isoformat(), controversy_score=None))
    rows.sort(key=lambda r: (-r['score'], r['title']))
    return [dict(r, rank=i+1) for i, r in enumerate(rows)]


def execute(root=ROOT, at=None, sender=None):
    at = at or datetime.now(timezone.utc)
    key = slot(at)
    folder = root/'outputs/social_radar/hunter'
    checkpoint = folder/(key+'.json')
    with process_lock(root/'outputs/articles/generation.lock'):
        if checkpoint.exists():
            return load(checkpoint)
        cfg = load(root/'config/social_radar.json'); validate_config(cfg)
        if not cfg['enabled']:
            return {'status': 'disabled', 'run_id': key}
        settings = load(root/'config/article_generation.json')
        env = read_settings_env(root/'.env')
        ledger = RadarLedger(root/'data/bot_metrics.db', settings, env, cfg, lambda: at)
        client = SearchClient(cfg, ledger, env, **({'send': sender} if sender else {}))
        report = dict(run_id=key, started_at=at.isoformat(), window_start=(at-timedelta(hours=24)).isoformat(),
                      window_end=at.isoformat(), schedule_jst=list(HOURS), topics=[],
                      actual_cost_usd=None, auto_publish=False, ranking_kind='research_priority',
                      budget=dict(run=cfg['run_budget_usd'], day=cfg['daily_budget_usd'], month=cfg['monthly_budget_usd']))
        try:
            result = client.search(key, 'discovery', QUERY, at-timedelta(hours=24), at, 'discovery')
            report.update(topics=rank_candidates(result, at), limitations=result['limitations'],
                          actual_cost_usd=result['usage']['actual_cost_usd'], usage=result['usage'])
            store = Store(root/'data/bot_metrics.db')
            for item in result['topics']:
                store.ingest(item, result, key, at, cfg)
            report['status'] = 'completed' if report['topics'] else 'no_evidence'
        except (BudgetExceeded, AmbiguousCall) as exc:
            report.update(status='budget_exceeded' if isinstance(exc, BudgetExceeded) else 'ambiguous', error=str(exc))
        except Exception as exc:
            report.update(status='failed', error=type(exc).__name__)
        # Persist empty/failed runs too: no repeated paid requests to force a nonempty list.
        save(checkpoint, report)
        save(folder/'latest.json', report)
        return report


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = execute()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] in ('completed', 'no_evidence', 'disabled') else 2


if __name__ == '__main__':
    raise SystemExit(main())
