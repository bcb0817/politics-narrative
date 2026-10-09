import copy
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch,MagicMock
from src.article_api import Ledger,Client,BudgetExceeded,AmbiguousCall,AuthenticationError,measured_usage,process_lock
from src.article_generation import run_article,main,ROOT
from src.article_sources import fetch,rank,candidates,canonical
from src.article_review import semantic_guards,check,blocking
from src.article_schema import obj,S
from src.bounded_http import read_html

CFG=json.loads((ROOT/'config/article_generation.json').read_text())
NOW=datetime(2026,10,1,tzinfo=timezone.utc)
def source():
    return dict(id=1,url='https://fiction.example/policy',title='テスト専用架空資料',publisher='架空局',published_at=None,
                updated_at=None,event_date=None,kind='primary',retrieved_at=NOW.isoformat(),error=None,
                text='架空の委員会が資料を公開した。対象範囲は未定である。',limitations=[],confirmed_scope='fictional test',evidence=[])
def claim(text='架空の委員会が資料を公開した。'):
    return dict(id='c1',text=text,classification='supported',severity='major',criterion=None,limitations=[],
                evidence=[dict(source_id=1,quote='架空の委員会が資料を公開した。')])
def article():
    return dict(titles=['架空資料を読む','架空制度の課題','架空委員会の説明'],recommended_title='架空資料を読む',
                body='架空の委員会が資料を公開した。[1]\n対象範囲は未定である。[1]',promo_posts=['架空資料の説明[1]','架空制度の背景[1]'],claims=[claim()])
def review(): return dict(claims=[claim()],issues=[],coverage_complete=True,summary='架空資料との一致を確認')
def raw(value):
    return dict(status='completed',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(value,ensure_ascii=False))])],
                usage=dict(input_tokens=100,output_tokens=100,output_tokens_details={'reasoning_tokens':40},input_tokens_details={'cached_tokens':10},cost_in_usd_ticks=10000000))

class ReviewTests(unittest.TestCase):
    def test_six_political_confusions(self):
        cases=json.loads((ROOT/'tests/fixtures/article_errors.json').read_text(encoding='utf-8'))['cases']
        for case in cases:
            with self.subTest(case=case['name']):
                c=claim(case['claim']); c['evidence'][0]['quote']=case['source']
                self.assertIn(case['expected'],{i['code'] for i in semantic_guards(c,[])})
    def test_title_stronger_than_body(self):
        a=article(); a['titles'][0]='必ず支援される'; a['recommended_title']=a['titles'][0]
        self.assertIn('headline_overclaim',{i['code'] for i in check(a,[source()],review(),1,4000)})
    def test_grounded_critical_opinion_allowed(self):
        a=article(); c=claim('評価：公開だけでは説明責任を果たしたとは考えられない。')
        c.update(classification='analysis',criterion='行政の説明責任')
        a['body']+='\n'+c['text']+'[1]'; a['claims']=[c]; r=review(); r['claims']=[c]
        self.assertFalse(blocking(check(a,[source()],r,1,4000)))
    def test_bad_quote_and_reference(self):
        a=article(); a['claims'][0]['evidence'][0]['quote']='存在しない'; a['body']+='[99]'
        codes={x['code'] for x in check(a,[source()],review(),1,4000)}
        self.assertTrue({'invalid_evidence','source_numbers'}<=codes)
    def test_missing_review_coverage(self):
        r=review(); r['claims']=[]
        self.assertIn('review_incomplete',{x['code'] for x in check(article(),[source()],r,1,4000)})
    def test_reviewer_cannot_replace_claim_or_hide_severity(self):
        r=review(); r['claims'][0].update(text='別の主張',classification='safe',severity='none')
        codes={x['code'] for x in check(article(),[source()],r,1,4000)}
        self.assertTrue({'review_claim_mismatch','review_classification'}<=codes)
    def test_failed_source(self):
        s=fetch('https://fiction.example',1,reader=lambda _: (_ for _ in ()).throw(OSError('private secret')))
        self.assertEqual(s['error'],'OSError'); self.assertNotIn('secret',json.dumps(s))
        self.assertTrue(blocking(check(article(),[s],review(),1,4000)))

class SafetyTests(unittest.TestCase):
    def test_ssrf(self):
        for url in ['file:///etc/passwd','http://user:pass@example.com','http://example.com:9000']:
            with self.assertRaises(ValueError): read_html(url)
        for address in ['127.0.0.1','10.0.0.1','::1','169.254.169.254']:
            with self.assertRaises(ValueError): read_html('https://example.com',resolver=lambda *a,**k:[(0,0,0,'',(address,443))])
    def test_mixed_dns_blocks(self):
        with self.assertRaises(ValueError): read_html('https://example.com',resolver=lambda *a,**k:[(0,0,0,'',('8.8.8.8',443)),(0,0,0,'',('127.0.0.1',443))])
    def test_redirect_revalidated_and_connection_closed(self):
        conn=MagicMock(); response=conn.getresponse.return_value
        response.status=302; response.getheader.return_value='http://127.0.0.1/secret'
        def resolve(host,*a,**k): return [(0,0,0,'',('127.0.0.1' if host=='127.0.0.1' else '8.8.8.8',443))]
        with patch('src.bounded_http.http.client.HTTPSConnection',return_value=conn):
            with self.assertRaises(ValueError): read_html('https://example.com',resolver=resolve)
        conn.close.assert_called_once()
    def test_size_cap_without_content_length(self):
        conn=MagicMock(); response=conn.getresponse.return_value
        response.status=200; response.read.return_value=b'x'*11
        response.getheader.side_effect=lambda k,*a: {'Content-Type':'text/html','Content-Encoding':'identity'}.get(k)
        with patch('src.bounded_http.http.client.HTTPSConnection',return_value=conn):
            with self.assertRaises(ValueError): read_html('https://example.com',max_bytes=10,resolver=lambda *a,**k:[(0,0,0,'',('8.8.8.8',443))])
        response.read.assert_called_once_with(11); conn.close.assert_called_once()
    def test_credential_url(self):
        with self.assertRaises(ValueError): canonical('https://example.com?token=secret')
    def test_reasoning_not_double_counted(self):
        v=raw({}); del v['usage']['cost_in_usd_ticks']
        self.assertAlmostEqual(measured_usage(v,CFG['pricing'])['estimated_token_cost_usd'],.000785)
        self.assertIsNone(measured_usage({},CFG['pricing'])['actual_cost_usd'])
    def test_duplicate_candidate(self):
        item=dict(title='架空政策',url='https://fiction.example/policy',published_at='2026-10-01T00:00:00Z')
        self.assertFalse(rank([item],[{'urls':[item['url']]}],NOW)[0]['eligible'])
        item['substantive_update']=dict(description='架空の新決定',source_url='https://fiction.example/new',event_date='2026-10-01',evidence_quote='架空の新決定を公表。')
        self.assertTrue(rank([item],[{'urls':[item['url']]}],NOW)[0]['eligible'])
    def test_unknown_axes_not_fabricated(self):
        self.assertIsNone(rank([dict(title='架空',url='https://fiction.example')],[],NOW)[0]['axes']['impact'])
    def test_expired_inventory_not_refreshed(self):
        item=dict(title='架空',url='https://fiction.example',expires_at='2026-09-30T00:00:00Z',published_at=NOW.isoformat())
        self.assertFalse(rank([item],[],NOW)[0]['eligible'])
    def test_existing_sqlite_inventory_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'candidates.db'
            c=sqlite3.connect(path)
            c.execute('CREATE TABLE reach_inventory (payload_json TEXT,status TEXT,first_seen_at TEXT,expires_at TEXT)')
            c.execute('INSERT INTO reach_inventory VALUES(?,?,?,?)',(json.dumps({'title':'架空','url':'https://fiction.example'}),'ready',NOW.isoformat(),'2026-09-30T00:00:00Z'))
            c.commit(); c.close()
            before=path.read_bytes(); items=candidates(path)
            self.assertFalse(rank(items,[],NOW)[0]['eligible']); self.assertEqual(before,path.read_bytes())
    def test_lock_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            with process_lock(Path(tmp)/'lock'):
                with self.assertRaises(RuntimeError):
                    with process_lock(Path(tmp)/'lock'): pass

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.ledger=Ledger(Path(self.tmp.name)/'ledger.db',CFG,{},lambda:NOW)
    def client(self,send): return Client(CFG,self.ledger,{'XAI_API_KEY':'TEST_KEY'},send,lambda _:None)
    def test_reservation_competition(self):
        self.ledger.reserve('run','draft',0,.3,.5)
        other=Ledger(self.ledger.path,CFG,{},lambda:NOW)
        with self.assertRaises(AmbiguousCall): other.reserve('run','review',0,.3,.5)
    def test_budget_and_resume_cached_result(self):
        count=[]; c=self.client(lambda *a:count.append(1) or raw({'ok':'yes'}))
        with self.assertRaises(BudgetExceeded): c.call('r','plan','x',obj(ok=S),.0001,'system')
        self.assertEqual(count,[])
        c.call('r','plan','x',obj(ok=S),.5,'system'); c.call('r','plan','x',obj(ok=S),.5,'system')
        self.assertEqual(len(count),1)
    def test_reported_over_cap_cannot_replay(self):
        count=[]
        def send(*a):
            count.append(1); r=raw({'ok':'yes'}); r['usage']['cost_in_usd_ticks']=6000000000; return r
        c=self.client(send)
        for _ in range(2):
            with self.assertRaises(BudgetExceeded): c.call('r','plan','x',obj(ok=S),.5,'system')
        self.assertEqual(len(count),1)
    def test_monthly_reservations_shared_between_runs(self):
        env={'XAI_MONTHLY_BUDGET_USD':'1.8','XAI_BUDGET_RESERVE_USD':'1.5'}
        one=Ledger(self.ledger.path,CFG,env,lambda:NOW); two=Ledger(self.ledger.path,CFG,env,lambda:NOW)
        one.reserve('r1','plan',0,.2,.5)
        with self.assertRaises(BudgetExceeded): two.reserve('r2','plan',0,.2,.5)
    def test_ambiguous_not_retried(self):
        count=[]
        def send(*a): count.append(1); raise TimeoutError()
        c=self.client(send)
        for _ in range(2):
            with self.assertRaises(AmbiguousCall): c.call('r','plan','x',obj(ok=S),.5,'system')
        self.assertEqual(len(count),1); self.assertGreater(self.ledger.summary('r')['accounted_usd'],0)
    def test_429_bounded_retry(self):
        count=[]
        def send(*a):
            count.append(1)
            if len(count)==1: raise urllib.error.HTTPError('https://api.x.ai',429,'',{},None)
            return raw({'ok':'yes'})
        self.client(send).call('r','plan','x',obj(ok=S),.5,'system')
        self.assertEqual(len(count),2)
    def test_auth_no_retry(self):
        count=[]
        def send(*a): count.append(1); raise urllib.error.HTTPError('https://api.x.ai',401,'',{},None)
        with self.assertRaises(AuthenticationError): self.client(send).call('r','plan','x',obj(ok=S),.5,'system')
        self.assertEqual(len(count),1)

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.clock=patch('src.article_generation.now',return_value=NOW.isoformat()); self.clock.start(); self.addCleanup(self.clock.stop)
    def data(self):
        return dict(theme='架空のテスト政策',urls=['https://fiction.example/policy'],region='架空国',audience='テスト',format='政策解説',editorial='架空資料',research_mode='provided',budget_usd=.5,min_chars=1,max_chars=4000)
    def test_end_to_end_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp); ledger=Ledger(path/'ledger.db',CFG,{},lambda:NOW); calls=[]
            plan=dict(reader_question='架空の疑問',conclusion='架空の結論',facts=[dict(statement=claim()['text'],evidence=claim()['evidence'])],analysis=[],counterarguments=[],unknowns=['対象範囲'],exclusions=[],source_metadata=[dict(source_id=1,publisher='架空局',published_at=None,updated_at=None,event_date=None,kind='primary',confirmed_scope='架空資料',limitations=[])])
            def send(payload,key):
                calls.append(payload['text']['format']['name'])
                return raw(plan if calls[-1].endswith('_plan') else review() if 'review_' in calls[-1] else article())
            c=Client(CFG,ledger,{'XAI_API_KEY':'TEST'},send)
            result=run_article(path/'run',self.data(),CFG,c,source_fetch=lambda *a,**k:source())
            self.assertEqual(result['status'],'ready_for_review'); self.assertEqual(len(calls),3)
            run_article(path/'run',self.data(),CFG,c,source_fetch=lambda *a,**k:self.fail('refetched'))
            self.assertEqual(len(calls),3)
            for name in ['article.md','article.txt','promo_posts.txt','sources.json','claims.json','review.json','usage.json','run.json']: self.assertTrue((path/'run'/name).exists())
    def test_budget_checkpoint_can_resume_without_repeating_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp); env={'XAI_MONTHLY_BUDGET_USD':'1.7','XAI_BUDGET_RESERVE_USD':'1.5'}
            ledger=Ledger(path/'ledger.db',CFG,env,lambda:NOW)
            plan=dict(reader_question='架空の疑問',conclusion='架空の結論',facts=[dict(statement=claim()['text'],evidence=claim()['evidence'])],analysis=[],counterarguments=[],unknowns=[],exclusions=[],source_metadata=[dict(source_id=1,publisher='架空局',published_at=None,updated_at=None,event_date=None,kind='primary',confirmed_scope='架空資料',limitations=[])])
            calls=[]
            def send(payload,key):
                name=payload['text']['format']['name']; calls.append(name)
                if name.endswith('_plan'):
                    r=raw(plan); r['usage']['cost_in_usd_ticks']=1500000000; return r
                return raw(review() if 'review_' in name else article())
            client=Client(CFG,ledger,{'XAI_API_KEY':'TEST'},send)
            first=run_article(path/'run',self.data(),CFG,client,source_fetch=lambda *a,**k:source())
            self.assertEqual(first['status'],'budget_exceeded'); self.assertTrue((path/'run/plan.json').exists())
            # Another month with unchanged monthly limits: pending stages may proceed.
            ledger.clock=lambda:datetime(2026,11,1,tzinfo=timezone.utc)
            second=run_article(path/'run',self.data(),CFG,client,source_fetch=lambda *a,**k:self.fail('refetched'))
            self.assertEqual(second['status'],'ready_for_review'); self.assertEqual(calls.count('article_plan'),1)
    def test_dry_run_no_network_or_key(self):
        with tempfile.TemporaryDirectory() as tmp,patch('socket.socket',side_effect=AssertionError('network')):
            result=run_article(Path(tmp),self.data(),CFG,dry_run=True,source_fetch=lambda *a,**k:self.fail('fetch'))
            self.assertEqual(result['status'],'needs_research')
            self.assertEqual(json.loads((Path(tmp)/'usage.json').read_text())['actual_cost_usd'],0)
    def test_all_source_failure_no_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'ledger.db',CFG,{},lambda:NOW)
            client=Client(CFG,ledger,{'XAI_API_KEY':'TEST'},lambda *a:self.fail('API'))
            failed=source(); failed['error']='HTTPError'
            result=run_article(Path(tmp)/'run',self.data(),CFG,client,source_fetch=lambda *a,**k:failed)
            self.assertEqual(result['status'],'needs_research')

if __name__=='__main__': unittest.main()
