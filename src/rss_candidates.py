"""Bounded RSS/Atom discovery; never treat feed summaries as verified evidence."""
import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin
from .bounded_http import read_html
from .article_sources import canonical
from .article_api import process_lock

ROOT = Path(__file__).resolve().parent.parent

def date(value):
    try:
        try: result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError: result = parsedate_to_datetime(value)
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError): return None

def parse(text, feed, at, hours=48):
    if '\x00' in text or re.search(r'<!\s*(DOCTYPE|ENTITY)', text, re.I): raise ValueError('xml_entities_forbidden')
    root = ET.fromstring(text)
    if root.tag.split('}')[-1] not in ('rss', 'RDF', 'feed'): raise ValueError('not_a_feed')
    items = []; rejected = []
    for node in [n for n in root.iter() if n.tag.split('}')[-1] in ('item', 'entry')][:100]:
        values = {}; link = None
        for child in node:
            name = child.tag.split('}')[-1]
            values[name] = ''.join(child.itertext()).strip()
            if name == 'link' and child.attrib.get('rel', 'alternate') == 'alternate':
                link = child.attrib.get('href') or values[name]
        title = values.get('title', '').strip()[:500]
        published = date(values.get('pubDate') or values.get('published') or values.get('date'))
        # Atom updated is not original publication time: do not extend an old story.
        reason = None
        try: url = canonical(urljoin(feed, link)) if link else None
        except ValueError: url = None
        if not title or not url: reason = 'missing_title_or_public_url'
        elif published is None: reason = 'publication_date_unknown'
        elif published > at: reason = 'future_publication'
        elif at >= published + timedelta(hours=hours): reason = 'expired'
        if reason:
            rejected.append({'title': title, 'reason': reason}); continue
        items.append(dict(title=title, url=url, published_at=published.isoformat(),
            expires_at=(published+timedelta(hours=hours)).isoformat(),
            feed_url=feed, discovered_at=at.isoformat(), source_kind='reporting',
            id=hashlib.sha256(url.encode()).hexdigest(), primary_urls=[]))
    return items, rejected

def collect(feeds, destination, at=None, reader=None, hours=48):
    from .article_generation import save, load
    at = at or datetime.now(timezone.utc)
    reader = reader or (lambda u: read_html(u, xml=True))
    if not feeds or len(feeds)>5 or not 0<hours<=168: raise ValueError('rss_limits')
    destination = Path(destination)
    with process_lock(destination.with_suffix('.lock')):
        old = load(destination).get('candidates', []) if destination.exists() else []
        known = {x['url']:x for x in old}; found = {}; report = []
        for feed in dict.fromkeys(canonical(f) for f in feeds):
            try:
                items, rejected = parse(reader(feed), feed, at, hours)
                for item in items:
                    previous = known.get(item['url'])
                    if previous:
                        # Original deadline is immutable even if publisher changes RSS dates.
                        item['published_at'] = previous['published_at']
                        item['expires_at'] = previous['expires_at']
                        item['discovered_at'] = previous['discovered_at']
                    if date(item['expires_at']) <= at:
                        rejected.append({'title':item['title'],'reason':'original_expiry'}); continue
                    found.setdefault(item['url'], item)
                report.append(dict(feed=feed, status='ok', received=len(items), rejected=rejected))
            except Exception as exc:
                report.append(dict(feed=feed, status='failed', error=type(exc).__name__))
        # Keep identity history separately; stale candidates are never selected.
        history_path = destination.with_suffix('.history.json')
        history = load(history_path) if history_path.exists() else {}
        history.update(known)
        for url, item in list(found.items()):
            prior = history.get(url)
            if prior:
                for field in ('published_at','expires_at','discovered_at'): item[field]=prior[field]
                if date(item['expires_at'])<=at: del found[url]
            history.setdefault(url,item)
        save(history_path, history)
        result = dict(collected_at=at.isoformat(), candidates=sorted(found.values(),key=lambda i:i['published_at'],reverse=True)[:100], feeds=report, auto_publish=False)
        save(destination,result)
        return result

def main(argv=None):
    parser=argparse.ArgumentParser(description='RSS collection only: no generation or publication')
    parser.add_argument('--feed',action='append'); args=parser.parse_args(argv)
    cfg=json.loads((ROOT/'config/rss_sources.json').read_text())
    result=collect(args.feed or cfg['feeds'],ROOT/'data/rss_candidates.json',hours=cfg['max_age_hours'])
    print(json.dumps({'candidates':len(result['candidates']),'feeds':[{k:v for k,v in f.items() if k!='rejected'} | {'rejected_count':len(f.get('rejected',[]))} for f in result['feeds']],'auto_publish':False},ensure_ascii=False))
    return 0 if any(f['status']=='ok' for f in result['feeds']) else 2

if __name__=='__main__': raise SystemExit(main())
