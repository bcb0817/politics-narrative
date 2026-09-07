import copy
import json
import math
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import metrics_db
import reach_audit
import reach_calendar
import reach_features
import reach_learning
import reach_maintenance
import reach_policy
import reach_report
from reach_storage import ensure, cost_scope, cost_metadata, incident

NOW=datetime.fromisoformat('2026-09-28T12:00:00+09:00')


class ReachCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'test.db'
        ensure(self.path)
        self.cfg=reach_policy.settings()
        self.env=patch.dict(os.environ,{'REACH_POLICY_ENABLED':'true'},clear=True)
        self.env.start(); self.addCleanup(self.env.stop)

    def sql(self,query,args=()):
        with closing(metrics_db.connect(self.path)) as conn:
            result=conn.execute(query,args).fetchall(); conn.commit(); return result

    def post(self,ident='1',at=None,text='制度が変更。\n\n対象は所得100万円以下。 https://example.org #政策',topic='tax'):
        at=at or NOW-timedelta(days=2)
        self.sql('INSERT INTO published_posts(tweet_id,text,posted_at,topic_key) VALUES(?,?,?,?)',(ident,text,at.isoformat(),topic))

    def calendar_env(self):
        return patch.dict(os.environ,{'X_ANALYTICS_ENTITLED':'true','X_ANALYTICS_OAUTH2_ACCESS_TOKEN':'test-only',
             'X_ANALYTICS_PRICE_PER_POST_USD':'.01','X_ANALYTICS_PRICE_VERIFIED':'true','X_OWNED_READ_MAX_PER_DAY':'24'})

    def test_six_formats_only_if_evidence_matches(self):
        text='現行から改正案へ変更。対象世帯への給付負担を調査資料で検証。財源配分の賛否と財政効果。'
        self.assertEqual(set(reach_features.eligible_formats({'summary':text},self.cfg)),set(self.cfg['formats']))
        self.assertEqual(reach_features.eligible_formats({'title':'こんにちは'},self.cfg),[])

    def test_rss_rfc_dates_are_observed_not_missing(self):
        self.assertEqual(reach_features.parse_time('Tue, 21 Jul 2026 19:33:15 +0900').isoformat(),'2026-07-21T19:33:15+09:00')
        self.assertIsNone(reach_features.parse_time('2026-07-21T19:33:15'))

    def test_format_allocation_stable_exploration_and_prompt(self):
        self.cfg['experiments']=self.cfg['experiments'][-1:]
        self.cfg['exploration_fraction']=1
        arms=[]; formats=set()
        for i in range(40):
            item={'url':f'https://example.org/{i}','summary':'現行から改正案へ変更。対象世帯の給付負担。調査資料で財政効果と財源配分の賛否を検証。'}
            row=reach_policy.assign(item,now=NOW,path=self.path,config=self.cfg)
            self.assertEqual(row,reach_policy.assign(item,now=NOW,path=self.path,config=self.cfg))
            arms.append(row['arm']); decision=json.loads(row['config_json'])['decision']
            if row['arm']=='treatment':
                self.assertTrue(decision['exploration'])
                self.assertAlmostEqual(decision['probability'],1/6)
                self.assertIn('説明形式',reach_policy.instruction(row))
                formats.add(decision['format'])
            else:
                self.assertIsNone(decision['format'])
                self.assertEqual(reach_policy.instruction(row),'')
        self.assertEqual(arms.count('control'),20)
        self.assertGreaterEqual(len(formats),4)

    def test_non_format_experiment_not_confounded(self):
        self.cfg['experiments']=self.cfg['experiments'][:1]
        row=reach_policy.assign({'url':'https://example.org/a','summary':'現行から改正案へ変更'},now=NOW,path=self.path,config=self.cfg)
        self.assertIsNone(json.loads(row['config_json'])['decision']['format'])

    def test_unresolved_incident_stops_experiment(self):
        incident('test',NOW.isoformat(),'correction','evidence','confirmed',path=self.path)
        self.assertIsNone(reach_policy.assign({'url':'https://example.org/a'},now=NOW,path=self.path,config=self.cfg))

    def learning_rows(self):
        rows=[]
        for i in range(100):
            posted=NOW-timedelta(days=25-i//4)
            x=i%4
            rows.append({'tweet_id':str(i),'posted_at':posted.isoformat(),'measured_at':(posted+timedelta(hours=25)).isoformat(),
                'impressions':round(math.exp(x+1)),'age_hours':25,'topic_key':str(i%6),'genre':'tax',
                'features':{'rank_features':{'reader_impact':x*3,'freshness':None,'observed_demand':None,'explanation_value':None,'difference':None},'format':'change','news_age_hours':1}})
        return rows

    def test_learning_holdout_and_weight_update_bounded(self):
        model=reach_learning.train(self.learning_rows(),self.cfg,NOW,self.path)
        self.assertEqual(model['status'],'active')
        self.assertLessEqual(max(abs(model['ranking_weights'][k]-self.cfg['ranking_weights'][k]) for k in self.cfg['ranking_weights']),.2)
        self.assertAlmostEqual(sum(model['ranking_weights'].values()),1)
        self.assertFalse(model['automatic_full_rollout'])
        self.assertEqual(model,reach_learning.train([],self.cfg,NOW,self.path))

    def test_learning_missing_and_buzz_do_not_activate(self):
        rows=self.learning_rows()
        for r in rows[:80]: r['age_hours']=100
        rows[-1]['impressions']=1000000
        self.assertEqual(reach_learning.train(rows,self.cfg,NOW,self.path)['status'],'insufficient_data')
        self.assertIsNone(reach_learning.active_model(self.path))

    def test_learning_safety_hold(self):
        self.assertEqual(reach_learning.train(self.learning_rows(),self.cfg,NOW,self.path,blocked=True)['status'],'safety_hold')

    def test_cost_context_nested_and_thread_isolated(self):
        self.assertEqual(cost_metadata(),{})
        with cost_scope(source_key='one'):
            with cost_scope(purpose='generate'): self.assertEqual(cost_metadata()['source_key'],'one')
            self.assertNotIn('purpose',cost_metadata())
            with ThreadPoolExecutor(1) as pool: self.assertEqual(pool.submit(cost_metadata).result(),{})
        self.assertEqual(cost_metadata(),{})

    def test_attribution_no_double_count_or_reserved_as_actual(self):
        self.sql("INSERT INTO api_usage_events(timestamp,provider,estimated_cost_usd,error_type,metadata_json) VALUES(?, 'openai',2,'',?)",(NOW.isoformat(),json.dumps({'tweet_ids':['a','b']})))
        self.sql("INSERT INTO api_usage_events(timestamp,provider,estimated_cost_usd,error_type) VALUES(?, 'xai',9,'reserved')",(NOW.isoformat(),))
        self.sql("INSERT INTO api_usage_events(timestamp,provider,estimated_cost_usd,error_type) VALUES(?, 'xai',1,'')",(NOW.isoformat(),))
        self.sql("INSERT INTO xai_usage_events(timestamp,cost_source,cost_verified,actual_cost_usd) VALUES(?,'actual',1,1)",(NOW.isoformat(),))
        with closing(metrics_db.connect(self.path)) as conn: r=reach_audit.expense_report(conn,NOW-timedelta(days=1),NOW+timedelta(seconds=1))
        self.assertEqual(r['completed_ledger_cost_usd'],3)
        self.assertEqual(r['in_flight_reserved_usd'],9)
        self.assertEqual(r['verified_actual_usd'],1)
        self.assertEqual(r['by_post_allocated_usd'],{'a':1,'b':1})
        self.assertEqual(r['unattributed_usd'],1)

    def test_calendar_missing_entitlement_is_null_without_request(self):
        self.post(); request=Mock()
        result=reach_calendar.collect_day(self.path,NOW,self.cfg,request)
        self.assertEqual(result['reason'],'analytics_entitlement_unconfirmed'); request.assert_not_called()

    def test_calendar_true_zero_and_missing_are_different(self):
        self.post('1'); self.post('2',text='another')
        self.cfg['calendar_analytics']['account_inventory_complete']=True
        with self.calendar_env(),patch.object(reach_calendar,'reserve',return_value=(1,'')),patch.object(reach_calendar,'finalize'):
            result=reach_calendar.collect_day(self.path,NOW,self.cfg,lambda *a:{'data':[{'id':'1','impressions':0},{'id':'2'}]})
        self.assertEqual(result['status'],'partial')
        end=NOW.replace(hour=0)
        with closing(metrics_db.connect(self.path)) as conn: day=reach_calendar.day_summary(conn,end-timedelta(days=1),end)['2026-09-27']
        self.assertEqual(day['known_posts_calendar_impressions'],0)
        self.assertIsNone(day['calendar_day_total_impressions'])
        self.assertEqual(day['missing_rate'],.5)

    def test_calendar_complete_interval_and_jst_boundary(self):
        self.post(); self.cfg['calendar_analytics']['account_inventory_complete']=True
        def request(ids,start,end):
            self.assertEqual(start.isoformat(),'2026-09-27T00:00:00+09:00')
            self.assertEqual(end-start,timedelta(days=1))
            return {'data':[{'id':'1','impressions':5}]}
        with self.calendar_env(),patch.object(reach_calendar,'reserve',return_value=(1,'')),patch.object(reach_calendar,'finalize'):
            self.assertEqual(reach_calendar.collect_day(self.path,NOW,self.cfg,request)['status'],'complete')
        end=NOW.replace(hour=0)
        with closing(metrics_db.connect(self.path)) as conn: day=reach_calendar.day_summary(conn,end-timedelta(days=1),end)['2026-09-27']
        self.assertEqual(day['calendar_day_total_impressions'],5)

    def test_calendar_read_budget_preserves_24h_reserve(self):
        self.post()
        self.sql("INSERT INTO api_usage_events(timestamp,provider,operation,resource_count) VALUES(?,'x','owned_read',5)",(NOW.isoformat(),))
        with self.calendar_env(),patch.object(reach_calendar,'reserve') as reserve:
            result=reach_calendar.collect_day(self.path,NOW,self.cfg,Mock())
        self.assertEqual(result['reason'],'24h_priority_budget_hold'); reserve.assert_not_called()

    def test_calendar_parallel_only_one_request(self):
        self.post(); entered=threading.Event(); release=threading.Event(); calls=[]
        def request(*a):
            calls.append(1); entered.set(); release.wait(5)
            return {'data':[{'id':'1','impressions':1}]}
        with self.calendar_env(),patch.object(reach_calendar,'reserve',return_value=(1,'')),patch.object(reach_calendar,'finalize'):
            with ThreadPoolExecutor(2) as pool:
                first=pool.submit(reach_calendar.collect_day,self.path,NOW,self.cfg,request)
                self.assertTrue(entered.wait(5))
                second=pool.submit(reach_calendar.collect_day,self.path,NOW,self.cfg,request).result()
                release.set(); first.result()
        self.assertEqual(len(calls),1); self.assertEqual(second['status'],'in_progress')

    def test_calendar_403_stops_retry_and_retains_reservation(self):
        self.post(); request=Mock(side_effect=RuntimeError('analytics_http_403'))
        with self.calendar_env(),patch.object(reach_calendar,'reserve',return_value=(1,'')),patch.object(reach_calendar,'finalize') as finish:
            self.assertEqual(reach_calendar.collect_day(self.path,NOW,self.cfg,request)['status'],'permission_denied')
            reach_calendar.collect_day(self.path,NOW,self.cfg,request)
            finish.assert_not_called(); self.assertEqual(request.call_count,1)

    def test_incident_audit_idempotent_separates_quality_and_loss(self):
        self.post('1',text='identical'); self.post('2',text='identical')
        logfile=Path(self.tmp.name)/'post_attempts.jsonl'
        logfile.write_text('\n'.join(json.dumps({'ts_jst':NOW.isoformat(),'slot_key':'slot','decision':'skip','reason':reason}) for reason in ['candidate_generation_failed','effective_score_below_threshold','openai_budget_guard']),encoding='utf-8')
        reach_audit.audit(self.path,NOW,logfile); reach_audit.audit(self.path,NOW,logfile)
        with closing(metrics_db.connect(self.path)) as conn: r=reach_audit.incident_report(conn,NOW-timedelta(days=28),NOW+timedelta(seconds=1))
        self.assertEqual(r['counts']['duplicate'],1)
        self.assertEqual(r['counts']['quality_hold'],1)
        self.assertEqual(r['counts']['technical_loss'],1)
        self.assertEqual(r['counts']['budget_stop'],1)

    def test_publication_features_and_full_report(self):
        row={'tweet_id':'p','tweet_text':'変更点100円。\n\n対象条件。 https://example.org #政策','posted_at_jst':(NOW-timedelta(days=2)).isoformat(),'post_format':'text_only'}
        metrics_db.insert_published(None,row,self.path)
        r=reach_report.report(self.path,NOW.replace(hour=0))
        feature=r['post_observations'][0]['features']
        self.assertEqual(feature['paragraphs'],2); self.assertEqual(feature['url_count'],1)
        self.assertEqual(feature['hashtag_count'],1); self.assertEqual(feature['media_type'],'text')
        self.assertIsNone(feature['news_age_hours'])
        self.assertIn('matched_comparisons',r['feature_analysis'])
        self.assertEqual(r['summary']['posts'],1)

    def test_follower_observation_window_not_exact_daily(self):
        end=NOW.replace(hour=0)
        for at,count in [(end-timedelta(days=1)+timedelta(minutes=5),50),(end-timedelta(minutes=5),52)]:
            self.sql('INSERT INTO follower_snapshots(captured_at,followers_count,estimated) VALUES(?,?,0)',(at.isoformat(),count))
        with closing(metrics_db.connect(self.path)) as conn: r=reach_calendar.day_summary(conn,end-timedelta(days=1),end)['2026-09-27']
        self.assertEqual(r['observed_follower_change'],2)
        self.assertIsNone(r['daily_net_followers'])

    def test_production_maintenance_without_external_credentials(self):
        result=reach_maintenance.run(self.path,NOW)
        self.assertEqual(result['latest_calendar_collection']['reason'],'analytics_entitlement_unconfirmed')
        self.assertEqual(result['learning']['status'],'insufficient_data')

    def test_budget_overrun_is_incident_not_quality_relaxation(self):
        self.sql("INSERT INTO api_usage_events(timestamp,provider,estimated_cost_usd,error_type) VALUES(?,'openai',100,'')",((NOW-timedelta(hours=1)).isoformat(),))
        reach_audit.audit(self.path,NOW)
        with closing(metrics_db.connect(self.path)) as conn:
            result=reach_audit.incident_report(conn,NOW-timedelta(days=1),NOW+timedelta(seconds=1))
        self.assertEqual(result['counts']['budget_overrun'],2)
        self.assertIsNone(reach_policy.assign({'url':'https://example.org/test'},now=NOW,path=self.path,config=self.cfg))

    def test_follower_missing_is_not_zero(self):
        import growth_tracking
        from types import SimpleNamespace
        client=Mock(); client.get_me.return_value=SimpleNamespace(data=SimpleNamespace(public_metrics={}))
        with patch.object(growth_tracking,'reserve',return_value=(1,'')),patch.object(growth_tracking,'finalize'),patch.object(growth_tracking,'estimate_x',return_value=.01):
            result=growth_tracking.capture_follower_snapshot(lambda **k:client,path=self.path,now=NOW)
        self.assertIsNone(result['followers_count'])

    def test_learning_disabled_rolls_ranking_back(self):
        self.cfg['learning']['enabled']=False
        with patch.object(reach_policy,'settings',return_value=self.cfg),patch.object(reach_learning,'active_model') as model:
            reach_policy.rank_eligible([{'title':'a','summary':'条件1','news_relevance_score':5}],[],path=self.path)
        model.assert_not_called()

    def test_confirmed_video_and_disaster_ids_join_existing_metrics(self):
        self.sql('CREATE TABLE short_video_publications(video_id TEXT,platform TEXT,status TEXT,external_post_id TEXT,published_at TEXT)')
        self.sql('CREATE TABLE disaster_update_publications(platform TEXT,status TEXT,external_post_id TEXT,published_at TEXT,candidate_text TEXT)')
        at=(NOW-timedelta(hours=25)).isoformat()
        self.sql("INSERT INTO short_video_publications VALUES('v','x','published','video-1',?)",(at,))
        self.sql("INSERT INTO short_video_publications VALUES('unknown','x','ambiguous','not-confirmed',?)",(at,))
        self.sql("INSERT INTO disaster_update_publications VALUES('x','published','disaster-1',?,'確認済み情報')",(at,))
        self.assertEqual(reach_audit.sync_publications(self.path,NOW),2)
        self.assertEqual(reach_audit.sync_publications(self.path,NOW),0)
        saved={r['tweet_id']:json.loads(r['features_json']) for r in self.sql('SELECT * FROM reach_features')}
        self.assertEqual(saved['video-1']['media_type'],'video')
        self.assertNotIn('not-confirmed',saved)

    def test_video_generation_cost_links_to_existing_publication(self):
        self.sql('CREATE TABLE short_video_publications(video_id TEXT,platform TEXT,status TEXT,external_post_id TEXT)')
        self.sql("INSERT INTO short_video_publications VALUES('v','x','published','x-video')")
        self.sql("INSERT INTO api_usage_events(timestamp,provider,estimated_cost_usd,error_type,metadata_json) VALUES(?,'openai',2,'',?)",(NOW.isoformat(),json.dumps({'video_id':'v'})))
        with closing(metrics_db.connect(self.path)) as conn: r=reach_audit.expense_report(conn,NOW-timedelta(days=1),NOW+timedelta(seconds=1))
        self.assertEqual(r['by_post_allocated_usd'],{'x-video':2})


if __name__=='__main__': unittest.main()
