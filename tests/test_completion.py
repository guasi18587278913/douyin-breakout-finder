"""Counterexamples: a readable deliverable is not proof of a finished research task."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import Store, atomic_json, digest
from demo import run_demo, demo_selection
from completion import outcome, run_identity, target_policy, target_scope
from report import render
from selection_report import eligible_ids
from verification import audit_run


class CompletionEvidence(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store,_=run_demo(self.root/'run',selection=True)

    def config(self, **changes):
        self.store.config.update(changes)
        self.store.state['config_hash']=digest(self.store.config)
        atomic_json(self.store.root/'config.json',self.store.config);self.store.save()
        demo_selection(self.store)
        render(self.store)

    def test_readable_report_and_record_integrity_do_not_finish_pending_candidates(self):
        result=audit_run(self.store.root);c=result['completion']
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['scope'],'saved_evidence_integrity')
        self.assertTrue(c['report']['ready'])
        self.assertTrue(c['retrieval']['unresolved_ids'])
        self.assertFalse(c['overall_goal_met'])
        self.assertEqual(outcome(c)[0],'report_ready')

    def test_search_failure_is_not_hidden_by_a_readable_report(self):
        params={'keyword':'a failed supplementary query','cursor':0}
        key=digest({'op':'search','params':params})
        self.store.reserve('search',key)
        self.store.event({'event':'request','key':key,'operation':'search','params':params})
        self.store.event({'event':'failed','key':key,'operation':'search','error':'HTTP 400'})
        render(self.store)
        c=audit_run(self.store.root)['completion']
        self.assertTrue(c['report']['available'])
        self.assertFalse(c['retrieval']['legal_stop'])
        self.assertEqual(c['retrieval']['stop_reason'],'search_failed')
        self.assertEqual(c['retrieval']['failed_search_request_keys'],[key])
        self.assertFalse(c['overall_goal_met'])

    def test_budget_stop_is_lawful_without_being_quantity_success(self):
        self.config(max_requests=len(self.store.reservations()))
        c=audit_run(self.store.root)['completion']
        self.assertTrue(c['report']['ready'])
        self.assertTrue(c['retrieval']['legal_stop'])
        self.assertEqual(c['retrieval']['stop_reason'],'request_limit')
        self.assertFalse(c['quantity']['met']);self.assertFalse(c['overall_goal_met'])

    def test_reserved_but_unsent_search_is_not_a_completed_round(self):
        self.config(search_policy='bounded-rounds-v1')
        original=[e['key'] for e in self.store.reservations() if e['operation']=='search']
        keys=[]
        for i in range(2):
            key=digest({'op':'search','params':{'keyword':'synthetic-unsent-'+str(i),'cursor':0}})
            self.store.reserve('search',key);keys.append(key)
        self.store.state['search_batches']=[original,[keys[0]],[keys[1]]];self.store.save()
        c=audit_run(self.store.root,require_report=False)['completion']
        self.assertEqual(c['retrieval']['failed_search_request_keys'],sorted(keys))
        self.assertIsNone(c['retrieval']['search_rounds_proven'])
        self.assertEqual(c['retrieval']['stop_kind'],'request_fault')
        self.assertTrue(c['retrieval']['stopping_allowed'])
        self.assertFalse(c['retrieval']['legal_stop']);self.assertFalse(c['overall_goal_met'])

    def test_reserving_verification_space_is_not_called_budget_exhaustion(self):
        self.config(max_requests=len(self.store.reservations())+1)
        c=audit_run(self.store.root)['completion']
        self.assertEqual(c['retrieval']['stop_reason'],'verification_reserve')
        self.assertEqual(c['retrieval']['remaining_requests'],1)
        self.assertEqual(c['retrieval']['followup_reserve_requests'],9)

    def test_missing_candidate_and_missing_delivered_card_are_detected_by_id(self):
        vid=next(iter(eligible_ids(self.store)))
        path=self.store.root/'report.html'
        path.write_text(path.read_text().replace('id="case-'+vid+'"','id="missing-case"'))
        report=audit_run(self.store.root)
        check=next(x for x in report['checks'] if x['id']=='report-cards')
        self.assertEqual(check['status'],'FAIL');self.assertIn(vid,check['observed']['expected_ids'])
        self.assertFalse(report['completion']['report']['ready'])
        self.store.state['candidates'].pop(vid);self.store.state['results'].pop(vid)
        for q in self.store.state['queries'].values():q['ids']=[x for x in q['ids'] if x!=vid]
        self.store.save()
        report=audit_run(self.store.root,require_report=False)
        check=next(x for x in report['checks'] if x['id']=='candidate-accounting')
        self.assertEqual(check['observed']['missing_candidate_ids'],[vid])
        self.assertEqual(next(r['status'] for r in report['coverage']['items'] if r['id']==vid),'missing')
        self.assertFalse(report['completion']['quantity']['verified'])

    def test_request_parameters_must_match_the_cached_request_key(self):
        record=next(json.loads(p.read_text()) for p in (self.store.root/'cache').glob('*.json')
                    if json.loads(p.read_text())['operation']=='search')
        key=digest({'op':'search','params':record['params']})
        self.store.event({'event':'request','operation':'search','params':{'keyword':'forged'},'key':key})
        report=audit_run(self.store.root,require_report=False)
        self.assertEqual(next(c['status'] for c in report['checks'] if c['id']=='request:'+key),'FAIL')

    def test_a_consistent_forged_statistics_projection_still_fails_raw_cache_check(self):
        from workflow import verification_fingerprint
        vid=next(iter(eligible_ids(self.store)));r=self.store.state['results'][vid]
        self.store.state['stats'][vid]['play_count']*=2
        r['play_count']*=2;r['ratio']*=2
        r['verification_fingerprint']=verification_fingerprint(self.store,self.store.state['candidates'][vid],r)
        self.store.save();render(self.store)
        result=audit_run(self.store.root,require_report=False)
        self.assertEqual(next(c['status'] for c in result['checks'] if c['id']=='statistics:'+vid),'FAIL')

    def test_provider_pagination_is_read_from_cache_not_state(self):
        for path in (self.store.root/'cache').glob('*.json'):
            record=json.loads(path.read_text())
            if record['operation']=='search':
                record['data'].update(has_more=True,cursor=123)
                atomic_json(path,record)
        self.assertTrue(all(q['complete'] for q in self.store.state['queries'].values()))
        c=audit_run(self.store.root,require_report=False)['completion']
        self.assertTrue(all(q['can_paginate'] and not q['provider_end'] for q in c['retrieval']['queries']))
        self.assertFalse(c['retrieval']['legal_stop'])

    def test_one_run_target_exception_cannot_be_copied_to_another_run(self):
        self.config(delivery_target_count=14)
        self.assertEqual(target_policy(self.store)['effective_min'],20)
        approval={'schema':'delivery-target-approval-v1','run_identity':run_identity(self.store),
                  'scope_sha256':target_scope(self.store),
                  'target':14,'authorization_reference':'SYNTHETIC-TEST-APPROVAL-ONLY'}
        atomic_json(self.store.root/'delivery-target-approval.json',approval)
        self.assertEqual(target_policy(self.store)['effective_min'],14)
        other=Store.create(self.root/'another',copy.deepcopy(self.store.config),as_of=self.store.state['as_of'])
        atomic_json(other.root/'delivery-target-approval.json',approval)
        self.assertEqual(target_policy(other)['effective_min'],20)
        self.assertFalse(target_policy(other)['exception_applied'])
        self.store.config.update(baseline_count=6,min_age_days=1)
        self.assertEqual(target_policy(self.store)['effective_min'],20)

    def test_legacy_topic_and_time_digest_cannot_authorize_an_exception(self):
        self.config(delivery_target_count=14)
        self.store.state.pop('run_id')
        approval={'schema':'delivery-target-approval-v1','run_identity':run_identity(self.store),
                  'scope_sha256':target_scope(self.store),'target':14,
                  'authorization_reference':'SYNTHETIC-TEST-ONLY'}
        atomic_json(self.store.root/'delivery-target-approval.json',approval)
        self.assertEqual(target_policy(self.store)['effective_min'],20)


if __name__=='__main__':unittest.main()
