import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from core import atomic_json
from demo import run_demo
from verification import audit_run
from check_delivery import check_delivery
from wizard import make_server


class SavedEvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store,_=run_demo(Path(self.temp.name)/'run',selection=True)

    def tearDown(self):self.temp.cleanup()

    def test_audit_accounts_for_pending_without_claiming_exhaustive_search(self):
        result=audit_run(self.store.root)
        self.assertEqual(result['status'],'PASS',result['checks'])
        self.assertEqual(result['coverage']['candidate_count'],len(self.store.state['candidates']))
        self.assertFalse(result['coverage']['search_exhaustive'])
        self.assertGreater(result['coverage']['outcomes']['pending'],0)

    def test_forged_completion_summary_is_not_evidence(self):
        path=self.store.root/'summary.json';data=json.loads(path.read_text(encoding='utf-8'))
        data['qualified_count']=900;atomic_json(path,data)
        failures=[r['id'] for r in audit_run(self.store.root)['checks'] if r['status']=='FAIL']
        self.assertIn('summary-counts',failures)

    def test_missing_candidate_is_found_from_saved_search_inputs(self):
        vid=next(iter(self.store.state['candidates']))
        del self.store.state['candidates'][vid];self.store.state['results'].pop(vid,None)
        for q in self.store.state['queries'].values():q['ids']=[i for i in q['ids'] if i!=vid]
        self.store.save()
        result=audit_run(self.store.root,require_report=False)
        entry=next(r for r in result['checks'] if r['id']=='candidate-accounting')
        self.assertEqual(entry['status'],'FAIL');self.assertIn(vid,entry['observed']['missing_candidate_ids'])

    def test_wrong_play_ratio_fails_independent_recalculation(self):
        target=next(r for r in self.store.state['results'].values() if r['status']=='qualified')
        target['ratio']=999;self.store.save()
        failures=[r['id'] for r in audit_run(self.store.root,require_report=False)['checks'] if r['status']=='FAIL']
        self.assertTrue(any(name.startswith('statistics:') for name in failures))

    def test_dropped_html_card_is_detected(self):
        path=self.store.root/'report.html';text=path.read_text(encoding='utf-8')
        path.write_text(text.replace('class="selection-card"','class="removed-card"',1),encoding='utf-8')
        failures=[r['id'] for r in audit_run(self.store.root)['checks'] if r['status']=='FAIL']
        self.assertIn('report-cards',failures)

    def test_visible_text_cannot_change_while_retaining_aria_labels(self):
        path=self.store.root/'report.html';original=path.read_text(encoding='utf-8')
        for changed in (original.replace('<dd>','<dd>9999万 ',1),
                        original.replace(self.store.state['research']['intro'][0],'被改写的正文')):
            path.write_text(changed,encoding='utf-8')
            failures=[r['id'] for r in audit_run(self.store.root)['checks'] if r['status']=='FAIL']
            self.assertIn('report-content',failures)

    def test_mislabelled_qualified_work_is_still_recomputed(self):
        target=next(r for r in self.store.state['results'].values() if r['status']=='qualified')
        target['status']='below_threshold';self.store.save()
        failures=[r['id'] for r in audit_run(self.store.root,require_report=False)['checks'] if r['status']=='FAIL']
        self.assertTrue(any(name.startswith('statistics:') for name in failures))

    def test_new_semantic_review_does_not_revalidate_old_statistics(self):
        from selection_report import eligible_ids
        from workflow import apply_review
        vid=next(iter(eligible_ids(self.store)))
        self.store.state['candidates'][vid]['followers']=50000
        apply_review(self.store,{vid:{'decision':'include','reason':'主题仍然相关','priority':1}})
        self.assertNotIn(vid,eligible_ids(self.store))

    def test_snapshot_withdraws_cards_when_their_evidence_has_changed(self):
        from wizard import Session
        session=Session(Path(self.temp.name)/'session',assistant_mode='host')
        session.active_store=self.store;session.publish_usable(self.store)
        self.assertTrue(session.snapshot()['usable_results'])
        vid=session.usable_results[0]['target']['id']
        self.store.state['candidates'][vid]['title']='依据已经变化'
        self.assertFalse(session.snapshot()['usable_results'])

    def test_genuine_follower_exclusion_with_old_history_is_not_a_false_failure(self):
        from selection_report import eligible_ids
        from workflow import apply_review, verify, CachedClient
        vid=next(iter(eligible_ids(self.store)))
        self.store.state['candidates'][vid]['followers']=50000
        # A real refreshed follower count must be present in the source response.
        for path in (self.store.root/'cache').glob('*.json'):
            record=json.loads(path.read_text())
            if record['operation']=='search':
                for video in record['data']['videos']:
                    if video['id']==vid:video['followers']=50000
                atomic_json(path,record)
        apply_review(self.store,{vid:{'decision':'include','reason':'仍与主题相关','priority':1}})
        verify(self.store,CachedClient(self.store))
        checked=audit_run(self.store.root,require_report=False)
        self.assertEqual(checked['status'],'PASS',checked['checks'])


class LiveDeliveryChecks(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store,summary=run_demo(Path(self.temp.name)/'run',selection=True)
        self.server=make_server(Path(self.temp.name)/'private-data',assistant_mode='host')
        session=self.server.session
        session.run_dir=self.store.root;session.summary=summary;session.status='done'
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.ready={'url':'http://127.0.0.1:%s/#%s'%(self.server.server_port,session.token),
                    'source_dir':str(ROOT),'source_at_start':session.source_at_start}

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.temp.cleanup()

    def test_live_saved_and_served_report_are_bound_and_token_is_not_exported(self):
        result=check_delivery(ROOT,ROOT,self.ready)
        self.assertEqual(result['status'],'PASS',result['checks'])
        self.assertNotIn(self.server.session.token,json.dumps(result))

    def test_an_old_process_cannot_pass_after_its_disk_was_updated(self):
        self.server.session.source_at_start=dict(self.server.session.source_at_start,sha256='old-process')
        result=check_delivery(ROOT,ROOT,self.ready)
        failures=[r['id'] for r in result['checks'] if r['status']=='FAIL']
        self.assertIn('live-startup',failures)

    def test_a_running_task_with_an_old_report_cannot_be_declared_done(self):
        self.server.session.status='running'
        result=check_delivery(ROOT,ROOT,self.ready)
        self.assertEqual(result['status'],'FAIL')
        self.assertIsNone(result['run_audit'])


if __name__=='__main__':unittest.main()
