import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
from core import Store,StopRun,BudgetStop,atomic_json,digest,read_json
from demo import run_demo
from history_discovery import discover,from_cache
from verification import audit_run
from webapp.cached_analysis import rebuild
from webapp.expanded_analysis import CanonicalClient

class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.source,_=run_demo(self.root/'source')
        self.source.config['discover_author_history']=True
        atomic_json(self.source.root/'config.json',self.source.config);self.source.state['config_hash']=digest(self.source.config);self.source.save()
    def test_history_discovery_has_exact_author_provenance_and_complete_accounting(self):
        work=rebuild(self.source,self.root/'replay');history=from_cache(work)
        self.assertGreater(len(history),0)
        self.assertEqual(set(history),{v for v,c in work.state['candidates'].items() if c.get('discovery')})
        audit=audit_run(work.root,require_report=False);self.assertEqual(audit['failed'],0,audit)
        vid=next(iter(history));work.state['candidates'][vid]['followers']+=1;work.save()
        self.assertGreater(audit_run(work.root,require_report=False)['failed'],0)
    def test_updated_author_followers_invalidates_old_small_account_snapshot(self):
        work=rebuild(self.source,self.root/'replay');before=set(from_cache(work));self.assertTrue(before)
        for path in (work.root/'cache').glob('*.json'):
            rec=read_json(path)
            if rec['operation']=='search':
                for v in rec['data']['videos']:v['followers']=20000
                atomic_json(path,rec)
        changed=discover(work);self.assertTrue(changed)
        for vid in before:
            self.assertEqual(work.state['candidates'][vid]['followers'],20000)
            self.assertEqual(work.state['candidates'][vid]['review']['decision'],'pending')
    def test_wrong_author_or_unlogged_page_cannot_be_used(self):
        work=rebuild(self.source,self.root/'replay')
        for path in (work.root/'cache').glob('*.json'):
            rec=read_json(path)
            if rec['operation']=='posts':
                for v in rec['data']['videos']:v['author_id']='another-author'
                atomic_json(path,rec)
        self.assertEqual(from_cache(work),{})
    def test_canonical_failed_reservation_is_mirrored_and_never_reset(self):
        work=rebuild(self.source,self.root/'replay');before=(self.source.root/'requests.jsonl').read_bytes()
        class Client:
            def __init__(self,store,**kwargs):self.store=store
            def call(self,op,params,**kwargs):
                key=digest({'op':op,'params':params});self.store.reserve(op,key)
                self.store.event({'event':'request','operation':op,'params':params,'key':key})
                raise StopRun('synthetic interrupted response')
        with patch('webapp.expanded_analysis.TikHubClient',Client):
            client=CanonicalClient(self.source,work,'SYNTHETIC',threading.Event())
            with self.assertRaises(StopRun):client.call('stats',{'aweme_ids':'1234567890123'})
        after=(self.source.root/'requests.jsonl').read_bytes();self.assertTrue(after.startswith(before));self.assertGreater(len(after),len(before))
        self.assertEqual(after,(work.root/'requests.jsonl').read_bytes());self.assertEqual(len(work.reservations()),len(self.source.reservations()))
    def test_mirror_failure_stops_other_queued_data_requests(self):
        work=rebuild(self.source,self.root/'replay');calls=[]
        class Client:
            def __init__(self,*args,**kwargs):pass
            def call(self,*args,**kwargs):calls.append(1);return {}
        with patch('webapp.expanded_analysis.TikHubClient',Client):
            client=CanonicalClient(self.source,work,'SYNTHETIC',threading.Event())
            with patch('webapp.expanded_analysis.shutil.copy2',side_effect=OSError('synthetic')):
                with self.assertRaises(OSError):client.call('stats',{'aweme_ids':'1234567890123'})
                with self.assertRaises(StopRun):client.call('stats',{'aweme_ids':'1234567890124'})
        self.assertEqual(len(calls),1)

    def test_quantity_claim_is_recomputed_in_independent_audit(self):
        from report import render
        self.source,_=run_demo(self.root/'complete',selection=True);render(self.source)
        summary=read_json(self.source.root/'summary.json');self.assertFalse(summary['quantity_target_met'])
        summary['quantity_target_met']=True;atomic_json(self.source.root/'summary.json',summary)
        failed=[r['id'] for r in audit_run(self.source.root)['checks'] if r['status']=='FAIL']
        self.assertIn('summary-counts',failed)

    def test_request_cap_and_money_cap_independently_stop_before_request(self):
        self.source.config['max_requests']=150
        while len(self.source.reservations())<150:self.source.reserve('stats','synthetic-'+str(len(self.source.reservations())))
        with self.assertRaises(BudgetStop):self.source.reserve('stats','151')
        self.source.config['max_requests']=1000;self.source.config['budget_usd']=str(self.source.reserved_cost())
        self.source.config['pricing']['usd_per_request']['stats']='0.001'
        with self.assertRaises(BudgetStop):self.source.reserve('stats','over-money')

if __name__=='__main__':unittest.main()
