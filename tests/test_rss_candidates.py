import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from src.rss_candidates import parse, collect

AT=datetime(2026,10,5,tzinfo=timezone.utc)
FEED='https://fiction.example/rss'
def rss(day='Sun, 04 Oct 2026 00:00:00 GMT'):
    return f'<rss><channel><item><title>架空政策</title><link>https://fiction.example/news</link><pubDate>{day}</pubDate></item></channel></rss>'

class RSSTests(unittest.TestCase):
    def test_rss(self):
        rows, rejected=parse(rss(),FEED,AT)
        self.assertEqual(len(rows),1); self.assertEqual(rejected,[])
        self.assertEqual(rows[0]['expires_at'],'2026-10-06T00:00:00+00:00')
    def test_atom(self):
        text='<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>架空</title><link href="/news"/><published>2026-10-04T00:00:00Z</published></entry></feed>'
        self.assertEqual(parse(text,FEED,AT)[0][0]['url'],'https://fiction.example/news')
    def test_missing_old_and_future_dates(self):
        for day in ('', 'Mon, 28 Sep 2026 00:00:00 GMT','Tue, 06 Oct 2026 00:00:00 GMT'):
            self.assertEqual(parse(rss(day),FEED,AT)[0],[])
    def test_entities_and_invalid_xml(self):
        for text in ('<!DOCTYPE rss><rss/>','<!ENTITY evil "x"><rss/>','broken'):
            with self.assertRaises(Exception): parse(text,FEED,AT)
    def test_dedup_and_no_expiry_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rss.json'
            self.assertEqual(len(collect([FEED,FEED],path,AT,lambda _:rss())['candidates']),1)
            result=collect([FEED],path,AT+timedelta(days=3),lambda _:rss('Wed, 07 Oct 2026 00:00:00 GMT'))
            self.assertEqual(result['candidates'],[])
    def test_failed_feed_keeps_other_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            def reader(url):
                if url==FEED: raise OSError('SECRET')
                return rss()
            result=collect([FEED,'https://fiction.example/other'],Path(tmp)/'rss.json',AT,reader)
            self.assertEqual(len(result['candidates']),1); self.assertNotIn('SECRET',str(result))
    def test_existing_rank_adapter(self):
        from src.article_sources import candidates, rank
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'rss.json'; collect([FEED],path,AT,lambda _:rss())
            self.assertTrue(rank(candidates(path),[],AT)[0]['eligible'])
            self.assertFalse(rank(candidates(path),[{'urls':['https://fiction.example/news']}],AT)[0]['eligible'])
