"""Read-only source adapters. Retrieval time never becomes event/publication time."""
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from .bounded_http import read_html

def now(): return datetime.now(timezone.utc).isoformat()
def canonical(url):
    p = urlsplit(url)
    if p.scheme not in ('https','http') or not p.hostname or p.username or p.password:
        raise ValueError('invalid_public_url')
    if any(x in p.query.lower() for x in ('token=', 'key=', 'signature=', 'password=')):
        raise ValueError('credential_bearing_url')
    return urlunsplit((p.scheme,p.netloc.lower(),p.path or '/',p.query,''))

class Text(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0; self.title=[]; self.in_title=False; self.meta={}
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style','noscript'): self.hidden+=1
        if tag=='title': self.in_title=True
        if tag=='meta':
            a=dict(attrs); self.meta[a.get('property',a.get('name',''))]=a.get('content')
        if tag in ('p','div','li','h1','h2','h3','br'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script','style','noscript'): self.hidden=max(0,self.hidden-1)
        if tag=='title': self.in_title=False
    def handle_data(self, data):
        if self.in_title: self.title.append(data)
        if not self.hidden: self.parts.append(data)

def fetch(url, source_id, max_chars=10000, reader=read_html):
    record=dict(id=source_id,url=canonical(url),title=None,publisher=None,published_at=None,
                updated_at=None,retrieved_at=now(),event_date=None,kind='unknown',
                confirmed_scope='',limitations=[],error=None,text='',sha256=None,evidence=[])
    try:
        html=reader(record['url']); parser=Text(); parser.feed(html)
        text=re.sub(r'[ \t\r\f\v]+',' ',''.join(parser.parts))
        text=re.sub(r'\n\s*\n+','\n',text).strip()
        record.update(title=''.join(parser.title).strip() or record['url'],
                      published_at=parser.meta.get('article:published_time'),
                      updated_at=parser.meta.get('article:modified_time'),
                      publisher=parser.meta.get('og:site_name'),text=text[:max_chars],
                      sha256=hashlib.sha256(text.encode()).hexdigest())
        if len(text)>max_chars: record['limitations'].append('text_truncated_not_full_document')
        if len(text)<100: raise ValueError('insufficient_readable_text')
        record['confirmed_scope']='retrieved_excerpt_only; publisher claims not independently verified'
    except Exception as exc:
        record['error']=type(exc).__name__  # Never include URLs, keys or response bodies in exceptions.
        record['limitations'].append('retrieval_failed; no claim may rely on this source')
    return record

def candidates(path):
    path=Path(path)
    if path.suffix.lower() in ('.db','.sqlite','.sqlite3'):
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as c:
            names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'reach_inventory' in names:
                c.row_factory=sqlite3.Row
                result=[]
                for row in c.execute("SELECT * FROM reach_inventory WHERE status='ready' ORDER BY first_seen_at DESC LIMIT 100"):
                    item=json.loads(row['payload_json'])
                    if 'expires_at' in row.keys(): item['expires_at']=row['expires_at']
                    result.append(item)
                return result
            if 'news_candidates' in names:
                c.row_factory=sqlite3.Row
                return [dict(r) for r in c.execute('SELECT * FROM news_candidates ORDER BY id DESC LIMIT 100')]
            raise ValueError('unsupported_candidate_database')
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    return (data if isinstance(data,list) else data['candidates'])[:100]

def rank(items, history, at=None):
    at=at or datetime.now(timezone.utc); result=[]
    for i,item in enumerate(items):
        url=item.get('url') or item.get('source_url'); title=item.get('title','')
        reasons=[]; axes={k:None for k in ['impact','novelty','primary_availability','misunderstanding','added_value','duplicate']}
        if not url or not title: reasons.append('missing_url_or_title')
        try: url=canonical(url)
        except (ValueError,TypeError,AttributeError): reasons.append('invalid_url')
        previous=[h for h in history if url in h.get('urls',[]) or title==h.get('theme')]
        # A new date or re-fetch alone is not a substantive update.
        update=item.get('substantive_update',{})
        evidence_update=False
        try:
            canonical(update['source_url'])
            event=datetime.fromisoformat(update['event_date']).date()
            evidence_update=bool(update.get('description') and update.get('evidence_quote') and event<=at.date())
        except (KeyError,ValueError,TypeError): pass
        if previous and not evidence_update: reasons.append('duplicate_without_documented_update')
        axes['duplicate']=bool(previous)
        if item.get('expires_at'):
            try:
                expiry=datetime.fromisoformat(item['expires_at'].replace('Z','+00:00'))
                if not expiry.tzinfo or expiry<=at: reasons.append('expired_candidate')
            except (ValueError,TypeError): reasons.append('invalid_expiry')
        raw=item.get('published_at') or item.get('pub_date')
        try:
            dt=datetime.fromisoformat(raw.replace('Z','+00:00'))
            if not dt.tzinfo: raise ValueError()
            age=(at-dt).total_seconds()/3600
            axes['novelty']=2 if 0<=age<=48 else 1 if 0<=age<=168 else 0
            if age<0: reasons.append('future_publication')
        except (ValueError,TypeError,AttributeError): reasons.append('publication_date_unknown')
        host=urlsplit(url or '').hostname or ''
        axes['primary_availability']=2 if host.endswith('.go.jp') else (1 if item.get('primary_urls') else 0)
        annotations=item.get('article_assessment',{})
        for key in ('impact','misunderstanding','added_value'):
            entry=annotations.get(key,{})
            if isinstance(entry,dict) and entry.get('reason') and entry.get('score') in (0,1,2):
                axes[key]=entry['score']
        score=sum(v for k,v in axes.items() if k!='duplicate' and isinstance(v,int))
        result.append(dict(index=i,title=title,url=url,axes=axes,score=score,
                           reasons=reasons or ['eligible; unknown axes not invented'],
                           eligible=not any(x in reasons for x in ('missing_url_or_title','invalid_url','duplicate_without_documented_update','future_publication','expired_candidate','invalid_expiry')),
                           assessment=annotations,substantive_update=update,primary_urls=item.get('primary_urls',[])))
    return sorted(result,key=lambda r:(not r['eligible'],-r['score'],r['index']))
