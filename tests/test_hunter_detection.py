"""Fictional data only; no network or real clock."""
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch
from datetime import timedelta
from test_social_radar import topic, result, raw, AT, CFG, ARTICLE
from src.hunter_detection import rank_candidates, slot, execute


class HunterTests(unittest.TestCase):
    def test_cited_topic_without_primary_is_candidate(self):
        item=topic(); item['original_url']=None
        rows=rank_candidates(result([item]), AT)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['score'],40)
        self.assertIsNone(rows[0]['controversy_score'])

    def test_no_invented_links_or_private_target(self):
        r=result(); r['citations']=[]
        self.assertEqual(rank_candidates(r, AT),[])
        item=topic(); item['target_kind']='private_person'
        self.assertEqual(rank_candidates(result([item]),AT),[])

    def test_score_breakdown_and_dedup(self):
        item=topic(); item['counterarguments']=['架空の反論']
        rows=rank_candidates(result([item,item]), AT)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['score'],80)
        self.assertEqual(rows[0]['score'],sum(rows[0]['score_parts'].values()))
        self.assertIsNone(rows[0]['verified_event_at'])

    def test_slots_jst(self):
        self.assertEqual(slot(AT),'hunter-20261007-8')
        self.assertEqual(slot(AT+timedelta(hours=5)),'hunter-20261007-14')
        self.assertEqual(slot(AT+timedelta(hours=11)),'hunter-20261007-20')
        self.assertEqual(slot(AT-timedelta(hours=2)),'hunter-20261006-20')

    def test_empty_not_fabricated(self):
        self.assertEqual(rank_candidates(result([]),AT),[])

    def test_checkpoint_prevents_repeated_send(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'config').mkdir()
            (root/'config/social_radar.json').write_text(json.dumps(CFG),encoding='utf-8')
            (root/'config/article_generation.json').write_text(json.dumps(ARTICLE),encoding='utf-8')
            sent=[]
            def send(*a,**kw):
                sent.append(1)
                return raw()
            with patch('src.hunter_detection.read_settings_env',return_value={'XAI_API_KEY':'fictional-test-key'}):
                first=execute(root,AT,send)
                second=execute(root,AT+timedelta(minutes=10),send)
            self.assertEqual(first['status'],'completed')
            self.assertEqual(first,second)
            self.assertEqual(len(sent),1)
