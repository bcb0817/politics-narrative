import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from production.clear_legacy_state import clean
import topical_editor
import reach_policy
from reach_storage import ensure
from metrics_db import connect

CUTOFF = '2026-09-08T00:00:00+09:00'


class LegacyResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'test.db'
        ensure(self.path)

    def sql(self, query, args=()):
        with closing(connect(self.path)) as conn:
            result = conn.execute(query,args).fetchall()
            conn.commit()
            return result

    def test_cleanup_preserves_published_unknown_today_and_budgets(self):
        from x_delivery import SCHEMA
        self.sql(SCHEMA)
        for ident, text, stamp in [(1,'published','2026-09-07T00:00:00+09:00'),
                                    (2,'unused','2026-09-07T00:00:00+09:00'),
                                    (3,'today',CUTOFF), (4,'uncertain','2026-09-07T00:00:00+09:00')]:
            self.sql("INSERT INTO generated_posts(id,text,threads_text,model,created_at) VALUES(?,?,?,'old',?)",(ident,text,text,stamp))
        self.sql("INSERT INTO published_posts(tweet_id,generated_post_id,text) VALUES('posted',1,'published')")
        digest=hashlib.sha256(json.dumps({'text':'uncertain'},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        self.sql("INSERT INTO x_delivery(delivery_key,payload_hash,status,created_at,updated_at) VALUES('u',?,'ambiguous',?,?)",(digest,CUTOFF,CUTOFF))
        self.sql("INSERT INTO api_usage_events(provider,estimated_cost_usd) VALUES('openai',12.5)")
        self.assertEqual(clean(self.path,CUTOFF)['unpublished_generated_payloads'],1)
        self.assertEqual(self.sql('SELECT text FROM generated_posts WHERE id=2')[0][0],'unused')
        result=clean(self.path,CUTOFF,apply=True)
        self.assertEqual(result['integrity'],'ok')
        self.assertEqual([r[0] for r in self.sql('SELECT text FROM generated_posts ORDER BY id')],['published','','today','uncertain'])
        self.assertEqual(self.sql('SELECT SUM(estimated_cost_usd) FROM api_usage_events')[0][0],12.5)
        self.assertEqual(self.sql('SELECT status FROM x_delivery')[0][0],'ambiguous')
        self.assertEqual(clean(self.path,CUTOFF,apply=True)['unpublished_generated_payloads'],0)

    def test_old_history_only_blocks_duplicates_not_ranking(self):
        now=datetime(2026,9,8,1,tzinfo=timezone.utc)
        news={'title':'通信料金の変更','summary':'通信サービスの料金が変更',
              'url':'https://news.web.nhk/a','pub_date':now.isoformat()}
        history=[{'title':'旧ニュース','genre':'技術・科学','openai_model':'old','topic_key':news['title']}]*5
        a=topical_editor.select([news],[],now=now)[0]['final_news_score']
        b=topical_editor.select([news],history,now=now)[0]['final_news_score']
        self.assertEqual(a,b)
        self.assertEqual(topical_editor.select([news],[{'source_url':news['url']}],now=now),[])

    def test_old_assignment_cannot_supply_new_prompt(self):
        now=datetime(2026,9,8,1,tzinfo=timezone.utc)
        item={'url':'https://news.web.nhk/a','title':'制度変更','genre':'暮らし・制度','summary':'制度変更'}
        self.sql(reach_policy.SCHEMA)
        key=hashlib.sha256(item['url'].encode()).hexdigest()
        self.sql("INSERT INTO reach_assignments VALUES(?,'old','treatment','old',?,'{}',NULL,NULL)",(key,CUTOFF))
        with patch.dict(os.environ,{'REACH_POLICY_ENABLED':'true'}):
            result=reach_policy.assign(item,now=now,path=self.path,config=topical_editor.experiment_settings())
        self.assertIsNotNone(result)
        self.assertNotEqual(result['experiment'],'old')
        self.assertEqual(json.loads(result['config_json'])['generation_version'],'topical-astra-v1')


if __name__=='__main__': unittest.main()
