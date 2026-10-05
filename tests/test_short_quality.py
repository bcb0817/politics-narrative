import copy
import unittest
import sqlite3
from contextlib import closing
from unittest.mock import patch
import test_short_posts as fixtures
DRAFT,REVIEW,SOURCE,CFG,AT=(fixtures.DRAFT,fixtures.REVIEW,fixtures.SOURCE,fixtures.CFG,fixtures.AT)
from src.short_quality import fetch_news, quality_errors, style_signals
from src.short_posts import validate_post, generate, followup_item, item_identity, Store
from src.article_generation import save

class QualityTests(unittest.TestCase):
    def test_unfounded_number_even_if_model_approves(self):
        d=copy.deepcopy(DRAFT); d['text']+='100億円の負担増。'
        self.assertIn('unsupported_number',validate_post(d,REVIEW,SOURCE))
    def test_unverified_quote(self):
        d=copy.deepcopy(DRAFT); d['text']+='「明日施行する」'
        self.assertIn('unverified_quote',validate_post(d,REVIEW,SOURCE))
    def test_stage_errors_rejected_by_independent_review(self):
        # Explicit fictional review outputs: tests the gate, not LLM detection ability.
        for source,wrong in [('架空候補は出馬の意向を示した。','架空候補が正式に届け出た。'),
                             ('架空被告は起訴された。','架空被告の有罪が確定した。'),
                             ('架空予算は3年総額100億円の要求。','架空予算は年額100億円で成立。')]:
            d=copy.deepcopy(DRAFT); d['text']=wrong+'NHK報道。'; d['claims']=[dict(text=wrong,kind='fact',criterion='',segment_ids=[1])]
            r=copy.deepcopy(REVIEW); r['checks']=[dict(claim_index=0,supported=False,segment_ids=[1])]
            r['quality']['accuracy']={'pass_check':False,'reason':'段階または期間の不一致'}
            self.assertIn('quality_accuracy',validate_post(d,r,dict(SOURCE,text=source)))
    def test_explicit_stage_upgrade_blocks_even_with_model_approval(self):
        for original,wrong,code in [('架空被告は起訴された。','架空被告の有罪が確定した。','stage_not_in_evidence'),
            ('架空候補は出馬の意向。','架空候補が正式に届け出た。','stage_not_in_evidence'),
            ('架空事業は3年総額100億円。','架空事業は年額100億円。','period_mismatch')]:
            d=copy.deepcopy(DRAFT); d['text']=wrong; d['claims']=[dict(text=wrong,kind='fact',criterion='',segment_ids=[1])]
            r=copy.deepcopy(REVIEW); r['checks']=r['checks'][:1]
            self.assertIn(code,validate_post(d,r,dict(SOURCE,text=original)))
    def test_same_news_requires_new_evidence_not_rewording(self):
        r=copy.deepcopy(REVIEW); r['comparison']['same_news']=True
        self.assertIn('duplicate_without_new_fact',quality_errors(DRAFT,r,SOURCE))
        r['comparison'].update(new_fact=True,new_fact_text=DRAFT['claims'][0]['text'],segment_ids=[1])
        self.assertEqual(quality_errors(DRAFT,r,SOURCE),[])
        r['comparison']['segment_ids']=[99]
        self.assertIn('new_fact_evidence_missing',quality_errors(DRAFT,r,SOURCE))
    def test_history_coverage_and_exact_duplicate(self):
        h=[{'id':'old','text':DRAFT['text']}]
        self.assertIn('history_coverage',quality_errors(DRAFT,REVIEW,SOURCE,h))
        self.assertIn('exact_duplicate',quality_errors(DRAFT,REVIEW,SOURCE,h))
    def test_same_url_requires_new_fact_even_if_model_denies_duplicate(self):
        h=[{'id':'old','url':'https://news.web.nhk/fiction','text':'以前の提案。'}]
        r=copy.deepcopy(REVIEW); r['comparison']['compared_ids']=['old']
        source=dict(SOURCE,url=h[0]['url'])
        self.assertIn('duplicate_without_new_fact',quality_errors(DRAFT,r,source,h))
    def test_quality_failure_cannot_pass_via_overall_approval(self):
        for axis in REVIEW['quality']:
            r=copy.deepcopy(REVIEW); r['quality'][axis]['pass_check']=False
            self.assertIn('quality_'+axis,validate_post(DRAFT,r,SOURCE))
    def test_style_signals_not_word_ban(self):
        d=copy.deepcopy(DRAFT); d['text']+='ただし、対象は未定。'
        self.assertEqual(quality_errors(d,REVIEW,SOURCE),[])
        self.assertEqual(style_signals(d['text'],[])['caution_phrases'],['ただし'])
    def test_sidebar_removed_and_incomplete_tail_marked(self):
        raw=dict(SOURCE,url='https://news.web.nhk/fiction',text='メニュー\n架空記事\n2026年10月5日 12:00\nシェアする政治\n架空法案が提出された。\n担当者は今後…\n注目ワード\n関連記事では成立。',limitations=[])
        with patch('src.short_quality.fetch',return_value=raw):
            s=fetch_news('https://news.web.nhk/fiction',1)
        self.assertEqual(s['text'],'架空法案が提出された。\n担当者は今後')
        self.assertTrue(any('mid_sentence' in x for x in s['limitations']))
        self.assertNotIn('成立',s['text'])
    def test_missing_boundaries_fail_closed(self):
        with patch('src.short_quality.fetch',return_value=dict(SOURCE,url='https://news.web.nhk/fiction',limitations=[])):
            self.assertEqual(fetch_news('https://news.web.nhk/fiction',1)['error'],'article_boundary_unconfirmed')

class RevisionTests(unittest.TestCase):
    setUp=fixtures.ShortTests.setUp
    ready=fixtures.ShortTests.ready
    def test_source_failure_stops_before_model(self):
        class FakeClient:
            ledger=self.ledger
            def call(*args): raise AssertionError('source failure must not call API')
        item=dict(url='https://news.web.nhk/fiction/failure',title='架空',published_at=AT.isoformat(),expires_at=AT.isoformat())
        result=generate(self.store,FakeClient(),CFG,item,AT,self.root,lambda *a,**k:dict(SOURCE,error='retrieval_failed'))
        self.assertEqual(result,'needs_research')
        self.assertEqual(self.store.items()[0]['reason'],'source_failed')
    def test_single_revision_then_no_more_generation(self):
        calls=[]
        class FakeClient:
            ledger=self.ledger
            def call(inner,run,stage,*args):
                calls.append(stage)
                inner.ledger.reserve(run,stage,0,.001,1)
                inner.ledger.settle(run,stage,0,'completed',{'actual_cost_usd':.001})
                if stage=='short_draft':
                    bad=copy.deepcopy(DRAFT); bad['text']+='未定。'*80; return bad
                return DRAFT if stage=='short_revision' else REVIEW
        item=dict(url='https://news.web.nhk/fiction/repair',title='架空',published_at=AT.isoformat(),expires_at=AT.isoformat())
        self.assertEqual(generate(self.store,FakeClient(),CFG,item,AT,self.root,lambda *a,**k:SOURCE),'needs_research')
        self.assertEqual(generate(self.store,FakeClient(),CFG,item,AT,self.root,lambda *a,**k:SOURCE),'ready')
        self.assertEqual(generate(self.store,FakeClient(),CFG,item,AT,self.root),'not_repairable')
        self.assertEqual(calls,['short_draft','short_review','short_revision','short_revision_review'])
    def test_legacy_unique_url_migration_preserves_rows(self):
        path=self.root/'legacy.db'
        with closing(sqlite3.connect(path)) as c:
            c.execute('CREATE TABLE short_items(id TEXT PRIMARY KEY,url TEXT UNIQUE,title TEXT,created TEXT,expires TEXT,status TEXT,body TEXT,post_id TEXT,reason TEXT,config_hash TEXT)')
            c.execute('INSERT INTO short_items VALUES(?,?,?,?,?,?,?,?,?,?)',('old','https://news.web.nhk/fiction','架空',AT.isoformat(),AT.isoformat(),'published','旧投稿','987',None,'old'))
            c.commit()
        migrated=Store(path)
        self.assertEqual(migrated.items()[0]['post_id'],'987')
        self.assertEqual(migrated.items()[0]['body'],'旧投稿')
        self.assertEqual(Store(path).items(),migrated.items())
    def test_configurable_history_window(self):
        for i in range(4): self.ready(i)
        self.assertEqual(len(self.store.history(2)),2)
        self.assertEqual(len(self.store.history(40)),4)
    def test_same_url_followup_keeps_original_deadline_and_history(self):
        row=self.ready(); self.store.update(row['id'],status='published',post_id='123')
        folder=self.root/'sources'
        save(folder/row['id']/'source.json',dict(SOURCE,url=row['url'],confirmed_scope='visible_article_excerpt_only'))
        item=dict(url=row['url'],title=row['title'],published_at=row['created'],expires_at=row['expires'])
        self.assertIsNone(followup_item(self.store,item,SOURCE,folder))
        source=dict(SOURCE,text='架空委員会が対象を中小企業と決定した。')
        followup=followup_item(self.store,item,source,folder)
        self.assertEqual(followup['expires_at'],row['expires'])
        identity=self.store.put(followup,AT,CFG)
        self.assertNotEqual(identity,row['id'])
        self.assertEqual(len(self.store.items()),2)
        self.assertEqual(self.store.history()[0]['id'],row['id'])
    def test_revision_cap_and_old_policy(self):
        row=self.ready(); self.store.update(row['id'],status='needs_research',reason='length')
        item={'url':row['url']}
        class FakeClient:
            ledger=self.ledger
            def call(*args): raise AssertionError('must not generate')
        changed=dict(CFG,version='different')
        self.assertEqual(generate(self.store,FakeClient(),changed,item,AT,self.root),'previous_editorial_version')
        self.ledger.reserve('short_'+row['id'],'short_revision',0,.001,1)
        self.assertEqual(generate(self.store,FakeClient(),CFG,item,AT,self.root),'revision_limit')
