import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests'),str(ROOT/'webapp/tests')]
from core import Store, StopRun, ValidationIssue, atomic_json, digest, read_json
from demo import run_demo
from research import analysis_packet, accept_research
from report_fixture import selection_answer
from quality_fixture import verdict,approval
from webapp.content_quality import packet_for, evidence_for, validate, reviewed_generation, verify_saved
from webapp.remote_analyst import RemoteAnalyst, AssistanceNeeded

class QualityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store,_=run_demo(self.root/'research')
        self.packet=analysis_packet(self.store);self.answer=selection_answer(self.packet)
        self.audit=packet_for(self.answer,evidence_for(self.store,self.answer),self.store.config['scope'])
        self.calls=[];approval(self.root)
    def test_every_case_and_application_required_and_real_quotes(self):
        good=verdict(self.audit);self.assertEqual(validate(self.audit,good),[])
        for change in ('missing','path','quote','evidence','hash'):
            bad=copy.deepcopy(good)
            if change=='missing':bad['checks'].pop()
            if change=='path':bad['checks'][0]['path']='$.invented'
            if change=='quote':bad['checks'][0]['quote']='完全不存在于文章中的文字'
            if change=='hash':bad['input_sha256']='0'*64
            if change=='evidence':
                row=next(r for r in bad['checks'] if r['evidence_refs']);row['evidence_refs'][0]['quote']='不存在的原始事实'
            with self.subTest(change=change):
                with self.assertRaises(StopRun):validate(self.audit,bad)
    def test_exact_target_echo_is_redundant_and_cannot_replace_review_fields(self):
        good=verdict(self.audit)
        for row,target in zip(good['checks'],self.audit['targets']):
            row.update(text=target['text'],evidence_ids=target['evidence_ids'])
        self.assertEqual(validate(self.audit,good),[])
        good['checks'][0]['pass']=False
        self.assertEqual(len(validate(self.audit,good)),1)
        for change in ('text','evidence_ids','pass','reason','evidence_refs','extra'):
            bad=copy.deepcopy(good)
            if change in ('text','evidence_ids'):bad['checks'][0][change]='altered'
            elif change=='extra':bad['checks'][0]['invented']='unexpected'
            else:bad['checks'][0].pop(change)
            with self.subTest(change=change),self.assertRaises(StopRun):validate(self.audit,bad)
    def model(self, always_fail=False, swap_case=False, invalid_review=False):
        drafts=[]
        def send(request):
            payload=json.loads(request.content);self.calls.append(payload)
            if '独立内容编辑' in payload['messages'][0]['content']:
                audit=json.loads(payload['messages'][1]['content'])
                fail=always_fail or len(drafts)==1
                answer={} if invalid_review else verdict(audit,fail_path='$.intro' if fail else None)
                if getattr(self,'echo_targets',False):
                    for row,target in zip(answer['checks'],audit['targets']):
                        row.update(text=target['text'],evidence_ids=target['evidence_ids'])
            else:
                answer=copy.deepcopy(self.answer)
                if drafts:answer['intro']=['修正后，这个案例把原本分散的执行任务整理到一个具体问题里。']
                if drafts and swap_case:answer['lessons'][0]['cases'][0]['evidence_ids'].append('invalid')
                drafts.append(answer)
            if getattr(self,'provider_tag',False):answer={'type':'json_object',**answer}
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(answer,ensure_ascii=False)}}],
                'usage':{'prompt_tokens':100,'completion_tokens':200}})
        return RemoteAnalyst(threading.Event(),'SYNTHETIC-KEY-ONLY',self.root/'model-events.jsonl',httpx.MockTransport(send),settle_usage=True)
    def run_it(self,model):
        return reviewed_generation(model,'content-analysis',self.packet,self.store,lambda a:accept_research(self.store,a,expected_format='selection-report-v1'))
    def test_one_targeted_revision_then_new_independent_review(self):
        self.run_it(self.model());self.assertEqual(len(self.calls),4)
        self.assertTrue(verify_saved(self.store))
        third=self.calls[2]['messages'][1]['content'];self.assertIn('previous_answer',third);self.assertIn('$.intro',third)
        self.store.state['research']['intro'][0]+='后来修改';self.store.save();self.assertFalse(verify_saved(self.store))
    def test_echoed_review_retains_original_response_hash_and_trace(self):
        self.echo_targets=True;self.run_it(self.model())
        self.assertTrue(verify_saved(self.store));self.assertEqual(len(self.calls),4)
        receipt=read_json(self.store.root/'content-review.json')
        self.assertIn('text',receipt['verdict']['checks'][0])
    def test_second_failure_no_publication_no_third_writer_even_after_restart(self):
        model=self.model(always_fail=True);before=copy.deepcopy(self.store.state)
        with self.assertRaises(AssistanceNeeded):self.run_it(model)
        self.assertEqual(len(self.calls),4);self.assertEqual(before,self.store.state)
        self.assertFalse((self.store.root/'content-review.json').exists())
        with self.assertRaises(AssistanceNeeded):self.run_it(model)
        self.assertEqual(len(self.calls),4)
    def test_invalid_review_stops_without_writer_repair(self):
        with self.assertRaises(StopRun):self.run_it(self.model(invalid_review=True))
        self.assertEqual(len(self.calls),2)
    def test_whitespace_is_normalized_before_review_and_fake_receipt_is_rejected(self):
        self.provider_tag=True
        self.answer['intro'][0]='  '+self.answer['intro'][0]+'  '
        self.run_it(self.model());self.assertTrue(verify_saved(self.store))
        saved=read_json(self.store.root/'content-review.json');saved['draft_attempt']=998;saved['review_attempt']=999
        atomic_json(self.store.root/'content-review.json',saved);self.assertFalse(verify_saved(self.store))

    def test_budget_reserves_room_for_audit_before_writing(self):
        model=self.model();model.append({'kind':'reserve','usd':'0.13'})
        with self.assertRaises(AssistanceNeeded):self.run_it(model)
        self.assertEqual(self.calls,[])
    def test_unknown_result_never_restarts_with_changed_feedback(self):
        def fail(request):self.calls.append(1);raise httpx.ReadTimeout('synthetic')
        model=RemoteAnalyst(threading.Event(),'SYNTHETIC-KEY-ONLY',self.root/'model-events.jsonl',httpx.MockTransport(fail))
        with self.assertRaises(AssistanceNeeded):self.run_it(model)
        with self.assertRaises(AssistanceNeeded):self.run_it(model)
        self.assertEqual(len(self.calls),1)
    def test_repair_cannot_change_cases_or_references(self):
        with self.assertRaises(AssistanceNeeded):self.run_it(self.model(swap_case=True))
        self.assertEqual(len(self.calls),3)

if __name__=='__main__':unittest.main()
