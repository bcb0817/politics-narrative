"""Observable editorial features. Missing media/source data stays unknown."""
import json
import re
from contextlib import closing
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from metrics_db import connect
from reach_storage import ensure, source_key


def parse_time(value):
    try:
        value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return value if value.tzinfo else None
    except (ValueError, TypeError):
        try:
            value = parsedate_to_datetime(str(value))
            return value if value.tzinfo else None
        except (ValueError, TypeError, OverflowError):
            return None


def evidence_features(item, history=()):
    text = str(item.get('summary') or item.get('description') or '')
    # These are observable structure proxies, not LLM quality or truth scores.
    components = {'numbers': bool(re.search(r'\d', text)),
                  'conditions': bool(re.search(r'場合|対象|条件|以上|以下|未満|限り', text)),
                  'timing': bool(re.search(r'\d+月|\d+年|施行|開始|適用', text)),
                  'source_link': bool(item.get('url') or item.get('source_url'))}
    topic = item.get('topic_key') or item.get('title')
    repeat = sum((r.get('topic_key') or r.get('title')) == topic for r in history[-20:])
    demand = item.get('x_attention_score') if item.get('x_post_count', 0) > 0 and not item.get('xai_topic_match') else None
    return {'reader_impact': item.get('news_relevance_score'),
            'freshness': item.get('freshness_score'), 'observed_demand': demand,
            'explanation_value': sum(components.values()) * 2.5 if text else None,
            'difference': 10 / (1 + repeat)}


def eligible_formats(item, config):
    text = ' '.join(str(item.get(k) or '') for k in ('title', 'summary', 'description'))
    tests = {
        'change': r'改正|変更|施行|開始|決定|提案|導入|廃止|新設',
        'incidence': r'負担|給付|対象|税|料金|補助|所得|世帯',
        'comparison': r'現行.*(?:案|改正)|従来.*(?:新|変更)|前年|過去|比較',
        'overlooked_fact': r'資料|統計|調査|報告書|議事録',
        'tradeoff': r'賛否|争点|優先|配分|財源|両立',
        'evaluation': r'効果|費用|財政|検証|監査|説明責任',
    }
    return [name for name in config['formats'] if re.search(tests[name], text)]


def capture_publication(row, generated_id=None, path=None):
    ensure(path)
    text = row.get('tweet_text') or row.get('text') or ''
    posted = parse_time(row.get('posted_at_jst') or row.get('posted_at'))
    if not row.get('tweet_id') or not posted:
        return
    assignment = row.get('reach_assignment') or {}
    cfg = json.loads(assignment.get('config_json') or '{}')
    decision = cfg.get('decision', {})
    context = row.get('reach_feature_context') or {}
    with closing(connect(path)) as conn:
        news = conn.execute('''SELECT n.* FROM news_candidates n
            JOIN generated_posts g ON g.news_candidate_id=n.id WHERE g.id=?''', (generated_id,)).fetchone()
        news = dict(news) if news else {}
        recent = [dict(r) for r in conn.execute('''SELECT tweet_id,text,topic_key,posted_at FROM published_posts
            WHERE julianday(posted_at)<julianday(?) ORDER BY julianday(posted_at) DESC LIMIT 30''', (posted.isoformat(),))]
        source_time = parse_time(news.get('published_at')) or parse_time(context.get('news_published_at'))
        interval = (posted-parse_time(recent[0]['posted_at'])).total_seconds()/60 if recent and parse_time(recent[0]['posted_at']) else None
        opening = text.split('\n', 1)[0].split('。', 1)[0]
        topic = row.get('topic_key')
        same = [r for r in recent if topic and r['topic_key'] == topic]
        features = {
            'topic': topic or None, 'opening': opening,
            'length': len(text), 'paragraphs': len([p for p in re.split(r'\n\s*\n', text) if p.strip()]),
            'url_count': len(re.findall(r'https?://\S+', text)),
            'hashtag_count': len(re.findall(r'(?<!\w)[#＃]\w+', text)),
            'media_type': row.get('media_type') or ('text' if row.get('post_format') == 'text_only' else None),
            'angle': row.get('critique_axis') or row.get('post_type'),
            'format': decision.get('format'), 'format_exploration': decision.get('exploration'),
            'selection_probability': decision.get('probability'),
            'rank_features': decision.get('rank_features') or context.get('rank_features'),
            'policy_version': cfg.get('version') or context.get('policy_version'),
            'model_version': decision.get('model_version') or context.get('model_version'),
            'generation_version': row.get('prompt_version'),
            'interval_minutes': interval,
            'news_age_hours': (posted-source_time).total_seconds()/3600 if source_time and source_time <= posted else None,
            'same_topic_last_24h': sum(parse_time(r['posted_at']) >= posted-timedelta(days=1) for r in same),
            'opening_repeat_last_30': sum(r['text'].split('\n', 1)[0].split('。', 1)[0] == opening for r in recent),
            'followers_at_publish': row.get('followers_at_publish'),
        }
        conn.execute('INSERT OR REPLACE INTO reach_features VALUES(?,?,?,?)',
                     (str(row['tweet_id']), assignment.get('source_key') or context.get('source_key') or source_key(news),
                      posted.isoformat(), json.dumps(features, ensure_ascii=False, sort_keys=True)))
        conn.commit()


def enrich(rows, conn):
    saved = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='reach_features'").fetchone():
        saved = {r['tweet_id']: json.loads(r['features_json']) for r in conn.execute('SELECT * FROM reach_features')}
    recent = []
    for row in sorted(rows, key=lambda r: r['posted_at']):
        text = row.get('text') or ''
        posted = parse_time(row['posted_at'])
        news_time = parse_time(row.get('news_published_at'))
        base = {'topic': row.get('topic_key') or None, 'opening': text.split('\n')[0].split('。')[0],
                'length': len(text), 'paragraphs': len([p for p in re.split(r'\n\s*\n', text) if p.strip()]),
                'url_count': len(re.findall(r'https?://\S+', text)),
                'hashtag_count': len(re.findall(r'(?<!\w)[#＃]\w+', text)),
                'angle': row.get('critique_axis') or row.get('post_type'), 'media_type': None,
                'generation_version': row.get('prompt_version'),
                'followers_at_publish': row.get('followers_before_window'),
                'news_age_hours': (posted-news_time).total_seconds()/3600 if posted and news_time and posted >= news_time else None,
                'interval_minutes': (posted-parse_time(recent[-1]['posted_at'])).total_seconds()/60 if recent and posted else None,
                'same_topic_last_24h': sum(r.get('topic_key') == row.get('topic_key') and bool(row.get('topic_key')) and posted-parse_time(r['posted_at']) <= timedelta(days=1) for r in recent),
                'opening_repeat_last_30': sum(r['text'].split('\n')[0].split('。')[0] == text.split('\n')[0].split('。')[0] for r in recent[-30:])}
        row['features'] = {**base, **saved.get(row['tweet_id'], {})}
        recent.append(row)
    return rows
