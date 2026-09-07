import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import metrics_db
import post_metrics
import reach_policy
import reach_report
import x_delivery
import bounded_http
import candidate_inventory
import api_budget

NOW=datetime.fromisoformat('2026-09-08T12:00:00+09:00')


class ReachTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'test.db'
        metrics_db.init_db(self.path)

    def test_late_measurements_not_backfilled(self):
        history=[{'tweet_id':'old','posted_at_jst':(NOW-timedelta(days=9)).isoformat()},
                 {'tweet_id':'due','posted_at_jst':(NOW-timedelta(hours=25)).isoformat()}]
        self.assertEqual([(p['tweet_id'],w) for p,w in post_metrics.due_measurements(history,NOW,self.path)],[('due','24h')])

    def test_missing_and_zero_distinct(self):
        result=reach_report.summarize([{'age_hours':25,'impressions':0},{'age_hours':80,'impressions':500}])
        self.assertEqual(result['median_24h'],0)
        self.assertEqual(result['missing_rate'],.5)
        self.assertIsNone(result['calendar_day_total_impressions'])

    def test_response_lost_no_resend_and_reserved_budget_retained(self):
        with patch.object(x_delivery,'reserve',return_value=(1,'')),patch.object(x_delivery,'estimate_x',return_value=.01),patch.object(x_delivery,'finalize') as finish:
            def lost(): raise TimeoutError()
            with self.assertRaisesRegex(x_delivery.DeliveryBlocked,'ambiguous'):
                x_delivery.publish({'text':'hello'},lost,path=self.path,now=NOW)
            with self.assertRaises(x_delivery.DeliveryBlocked):
                x_delivery.publish({'text':'hello'},lambda:self.fail('resent'),path=self.path,now=NOW)
            finish.assert_not_called()

    def test_parallel_send_once_and_slot_blocks_changed_text(self):
        calls=[]
        def attempt(i):
            try:
                return x_delivery.publish({'text':str(i)},lambda:calls.append(i) or '123',key='slot:1',path=self.path,now=NOW)
            except x_delivery.DeliveryBlocked: return None
        with patch.object(x_delivery,'reserve',return_value=(1,'')),patch.object(x_delivery,'estimate_x',return_value=.01),patch.object(x_delivery,'finalize'):
            with ThreadPoolExecutor(2) as pool: list(pool.map(attempt,range(2)))
        self.assertEqual(len(calls),1)

    def test_reconcile_requires_owned_timestamp_not_old_identical_post(self):
        with patch.object(x_delivery,'reserve',return_value=(1,'')),patch.object(x_delivery,'estimate_x',return_value=.01):
            with self.assertRaises(x_delivery.DeliveryBlocked):
                x_delivery.publish({'text':'hello'},lambda:None,path=self.path,now=NOW)
        self.assertFalse(x_delivery.reconcile({'text':'hello'},'old',published_at=NOW-timedelta(days=1),path=self.path,now=NOW))
        self.assertTrue(x_delivery.reconcile({'text':'hello'},'new',published_at=NOW,path=self.path,now=NOW))

    def test_assignment_stable_and_control_balanced(self):
        cfg=reach_policy.settings(); cfg['experiments']=cfg['experiments'][:1]
        with patch.dict(os.environ,{'REACH_POLICY_ENABLED':'true'},clear=True):
            a=reach_policy.assign({'url':'https://example.com/a','genre':'tax'},path=self.path,now=NOW,config=cfg)
            again=reach_policy.assign({'url':'https://example.com/a','genre':'tax'},path=self.path,now=NOW+timedelta(hours=1),config=cfg)
            b=reach_policy.assign({'url':'https://example.com/b','genre':'tax'},path=self.path,now=NOW,config=cfg)
        self.assertEqual(a,again); self.assertNotEqual(a['arm'],b['arm'])

    def test_private_dns_rejected_before_connect(self):
        resolver=lambda *a,**k:[(None,None,None,None,('127.0.0.1',443))]
        with self.assertRaisesRegex(ValueError,'dns_not_public'):
            bounded_http.read_html('https://example.org',resolver=resolver)

    def test_expiry_never_extended_by_refetch(self):
        original={'url':'https://example.org/a','pub_date':NOW.isoformat()}
        candidate_inventory.refresh([original],path=self.path,now=NOW)
        later=NOW+timedelta(days=2)
        current=candidate_inventory.refresh([{**original,'pub_date':later.isoformat()}],path=self.path,now=later)
        self.assertEqual(current,[])

    def test_xai_concurrent_reservations_respect_limit(self):
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None): return NOW
        config={'providers':{'x':10,'openai':10},'provider_reserves':{},
                'effective_total_limit':20,'total_reserve':0}
        with patch.object(api_budget,'datetime',Clock),patch.object(api_budget,'budget_configuration',return_value=config),patch.object(api_budget,'effective_xai_limit',return_value=1),patch.object(api_budget,'forecast',return_value={'current_warning_stage':'normal','restriction_level':0}):
            api_budget.usage_totals(self.path,NOW)
            def attempt(_): return api_budget.reserve('xai','test','mock',.6,1,path=self.path)
            with ThreadPoolExecutor(2) as pool: results=list(pool.map(attempt,range(2)))
            self.assertEqual(sum(bool(r[0]) for r in results),1)
            self.assertAlmostEqual(api_budget.usage_totals(self.path,NOW)['xai'],.6)


if __name__=='__main__': unittest.main()
