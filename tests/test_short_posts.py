import copy
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
from src.short_posts import Store, gate, publish, generate, validate_post, weighted, report, run_cycle, JST
from src.article_generation import ROOT, load, digest, save
from src.article_api import Ledger, Client, BudgetExceeded

AT=datetime(2026,10,5,12,0,tzinfo=JST)
CFG=load(ROOT/'config/short_posts.json'); CFG['enabled']=True
MODEL=load(ROOT/'config/article_generation.json')
ENV={k:'TEST' for k in ('API_KEY','API_KEY_SECRET','ACCESS_TOKEN','ACCESS_TOKEN_SECRET')}
ENV.update(POST_ENABLED='true',X_POST_ENABLED='true',X_MONTHLY_BUDGET_USD='16',X_BUDGET_RESERVE_USD='.75',TOTAL_MONTHLY_API_BUDGET_USD='61',TOTAL_BUDGET_RESERVE_USD='3.25',X_POST_CREATE_MAX_PER_DAY='20',X_POST_CREATE_MAX_PER_MONTH='600',X_OWNED_READ_MAX_PER_DAY='36',X_OWNED_READ_MAX_PER_MONTH='1080')
FACT='架空の委員会が制度見直しを提案。対象や時期は未定。'
OPINION='負担軽減を基準に、対象と費用の説明を求めたい。'
BODY='NHKによると、'+FACT+'【論評】'+OPINION
SOURCE={'id':1,'text':'架空の委員会は制度見直しを提案した。対象や実施時期は未定。現行制度は変わらない。','error':None}
DRAFT={'text':BODY,'claims':[{'text':FACT,'kind':'fact','criterion':'','segment_ids':[1]},{'text':OPINION,'kind':'opinion','criterion':'国民負担の軽減','segment_ids':[1]}]}
REVIEW={'approved':True,'coverage_complete':True,'attribution_ok':True,'conditions_preserved':True,'opinion_separated':True,'no_group_attack':True,'checks':[{'claim_index':i,'supported':True,'segment_ids':[1]} for i in range(2)],'issues':[]}
DRAFT.update(decision='publish',skip_reason='',mode='comment',angle='対象が未定の制度見直し',form='短い指摘',
             facts=[{'text':FACT,'segment_ids':[1]}],context={'entities':['架空の委員会'],'dates':[],'quantities':[],'stage':'提案','unknowns':['対象と時期']})
REVIEW.update(quality={k:{'pass_check':True,'reason':'架空資料と整合'} for k in ('accuracy','specificity','readability','editorial_value','repetition')},
              comparison={'same_news':False,'same_angle':False,'repeated_style':False,'new_fact':False,'new_fact_text':'','segment_ids':[],'compared_ids':[],'reason':'履歴なし'})

class ShortTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.store=Store(self.root/'db.sqlite')
        self.ledger=Ledger(self.store.path,MODEL,ENV,lambda:AT); self.calls=[]
    def ready(self,i=0,at=AT):
        item={'url':f'https://news.web.nhk/fiction/{i}','title':f'架空{i}','published_at':(at-timedelta(hours=1)).isoformat(),'expires_at':(at+timedelta(hours=24)).isoformat()}
        identity=self.store.put(item,at,CFG); self.store.update(identity,status='ready',body=BODY)
        return next(r for r in self.store.items() if r['id']==identity)
    def send(self,method,path,env,payload=None):
        self.calls.append(method)
        return (200,{'data':{'id':CFG['expected_user_id'],'username':CFG['expected_username']}}) if method=='GET' else (201,{'data':{'id':'123456','text':payload['text']}})
    def test_valid_short_and_limits(self):
        self.assertEqual(validate_post(DRAFT,REVIEW,SOURCE),[]); self.assertLessEqual(weighted(BODY),280)
        bad=copy.deepcopy(DRAFT); bad['text']+='https://example.com'
        self.assertIn('style_or_url',validate_post(bad,REVIEW,SOURCE))
    def test_bad_evidence_or_coverage_blocks(self):
        bad=copy.deepcopy(DRAFT); bad['claims'][0]['segment_ids']=[999]
        self.assertIn('evidence_missing',validate_post(bad,REVIEW,SOURCE))
        rev=copy.deepcopy(REVIEW); rev['checks']=[]
        self.assertIn('review_coverage',validate_post(DRAFT,rev,SOURCE))
    def test_opinion_requires_criterion_not_fixed_label(self):
        bad=copy.deepcopy(DRAFT); bad['text']=BODY.replace('【論評】','')
        self.assertEqual(validate_post(bad,REVIEW,SOURCE),[])
        bad=copy.deepcopy(DRAFT); bad['claims'][1]['criterion']=''
        self.assertIn('missing_evaluation_criterion',validate_post(bad,REVIEW,SOURCE))
    def test_unsupported_opinion_and_group_attack_blocked(self):
        for flag in ('opinion_separated','no_group_attack'):
            rev=copy.deepcopy(REVIEW); rev[flag]=False
            self.assertIn('review_rejected',validate_post(DRAFT,rev,SOURCE))
        rev=copy.deepcopy(REVIEW); rev['checks'][1]['supported']=False
        self.assertIn('review_evidence_missing',validate_post(DRAFT,rev,SOURCE))
    def test_concise_fact_brief_allowed(self):
        brief=copy.deepcopy(DRAFT); brief.update(text=FACT+'（NHK報道）',mode='news_brief',claims=[DRAFT['claims'][0]])
        rev=copy.deepcopy(REVIEW); rev['checks']=rev['checks'][:1]
        self.assertEqual(validate_post(brief,rev,SOURCE),[])
    def test_long_verbatim_copy_blocked(self):
        source=dict(SOURCE,text=BODY)
        self.assertIn('source_copy_too_long',validate_post(DRAFT,REVIEW,source))
    def test_daily_target_and_interval(self):
        for i in range(10):
            r=self.ready(i,AT-timedelta(hours=10-i)); self.store.update(r['id'],status='published',post_id=str(i))
        self.assertEqual(gate(self.store,CFG,AT,ENV),'daily_target_reached')
        self.assertEqual(report(self.store,CFG,AT,'test')['today']['published'],10)
    def test_publish_and_no_repeat(self):
        r=self.ready(); self.assertEqual(publish(self.store,self.ledger,ENV,CFG,r,AT,self.send),'published')
        current=self.store.items()[0]
        self.assertEqual(publish(self.store,self.ledger,ENV,CFG,current,AT,self.send),'not_ready_or_expired')
        self.assertEqual(self.calls,['GET','POST'])
        self.assertEqual(gate(self.store,CFG,AT,ENV),'minimum_interval')
    def test_lost_response_halts_future_posts(self):
        def send(method,*args):
            if method=='POST': self.calls.append('POST'); raise TimeoutError()
            return self.send(method,*args)
        self.assertEqual(publish(self.store,self.ledger,ENV,CFG,self.ready(),AT,send),'ambiguous')
        self.assertEqual(gate(self.store,CFG,AT+timedelta(hours=2),ENV),'ambiguous_delivery_requires_reconciliation')
        self.assertEqual(self.calls,['GET','POST'])
    def test_wrong_account_halts(self):
        with self.assertRaises(ValueError): publish(self.store,self.ledger,ENV,CFG,self.ready(),AT,lambda *a:(200,{'data':{'id':'999','username':'other'}}))
        self.assertEqual(self.store.state('halt'),'account_mismatch')
    def test_budget_blocks_before_post(self):
        env=dict(ENV,X_MONTHLY_BUDGET_USD='.75')
        with self.assertRaises(BudgetExceeded): publish(self.store,self.ledger,env,CFG,self.ready(),AT,self.send)
        self.assertEqual(self.calls,[])
    def test_disabled_and_expired(self):
        r=self.ready()
        self.assertEqual(publish(self.store,self.ledger,ENV,dict(CFG,enabled=False),r,AT,self.send),'disabled')
        self.assertEqual(publish(self.store,self.ledger,ENV,CFG,r,AT+timedelta(days=2),self.send),'not_ready_or_expired')
        self.assertEqual(self.calls,[])
    def test_schedule_and_jst_day_boundary(self):
        self.assertEqual(gate(self.store,CFG,AT.replace(hour=7),ENV),'not_due')
        self.assertEqual(gate(self.store,CFG,AT.replace(hour=23),ENV),'outside_window')
        self.assertIsNone(gate(self.store,CFG,AT,ENV))
    def test_shared_budget_records_x_spend(self):
        publish(self.store,self.ledger,ENV,CFG,self.ready(),AT,self.send)
        with closing(self.ledger.connect()) as c: total,xai=self.ledger.legacy_spend(c,'2026-10')
        self.assertAlmostEqual(total,.025); self.assertEqual(xai,0)
    def test_generate_independent_review(self):
        calls=[]
        class FakeClient:
            ledger=self.ledger
            def call(inner,*args):
                calls.append(args[1]); return DRAFT if args[1]=='short_draft' else REVIEW
        item={'title':'架空ニュース','url':'https://news.web.nhk/fiction','published_at':AT.isoformat(),'expires_at':(AT+timedelta(hours=1)).isoformat()}
        self.assertEqual(generate(self.store,FakeClient(),CFG,item,AT,self.root,lambda *a,**k:SOURCE),'ready')
        self.assertEqual(calls,['short_draft','short_review'])
        self.assertEqual(self.store.items()[0]['body'],BODY)
    def test_full_cycle_mocked(self):
        root=self.root/'runtime'
        for name,data in [('short_posts',CFG),('article_generation',MODEL),('rss_sources',{'feeds':['https://fiction.example/rss'],'max_age_hours':48})]: save(root/f'config/{name}.json',data)
        item={'title':'架空ニュース','url':'https://news.web.nhk/fiction','published_at':AT.isoformat(),'expires_at':(AT+timedelta(hours=1)).isoformat()}
        class FakeClient:
            def __init__(inner,cfg,ledger,env): inner.ledger=ledger
            def call(inner,*args): return DRAFT if args[1]=='short_draft' else REVIEW
        with patch('src.short_posts.x_environment',return_value=ENV),patch('src.short_posts.read_settings_env',return_value={}):
            result=run_cycle(root,self.send,lambda:AT,lambda *a,**k:{'candidates':[item]},lambda *a,**k:SOURCE,FakeClient)
            again=run_cycle(root,self.send,lambda:AT,lambda *a,**k:self.fail('recollect'),lambda *a,**k:SOURCE,FakeClient)
        self.assertEqual(result['outcome'],'published'); self.assertEqual(again['outcome'],'minimum_interval')
        self.assertEqual(self.calls,['GET','POST'])
