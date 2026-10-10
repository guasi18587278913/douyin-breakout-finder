import copy,json,sys,tempfile,threading,unittest
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests'),str(ROOT/'webapp/tests')]
from core import ValidationIssue,read_json,atomic_json,digest
from demo import run_demo
from research import analysis_packet,accept_research
from report_fixture import selection_answer
from webapp.structured_writer import packet_for,assemble,resolve,messages_for,replacement_keys
from webapp.content_quality import reviewed_generation,verify_saved
from webapp.remote_analyst import RemoteAnalyst,AssistanceNeeded
from quality_fixture import approval,verdict

class StructuredTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.store,_=run_demo(self.root/'research');self.packet=packet_for(self.store)
        answer=selection_answer(analysis_packet(self.store))
        self.body={'title':answer['title'],'intro':answer['intro'],'method_1':answer['lessons'][0],'closing':answer['closing']}
        approval(self.root);self.calls=[]
    def model(self, first_fail=False, missing=False):
        def send(request):
            payload=json.loads(request.content);self.calls.append(payload);data=json.loads(payload['messages'][1]['content'])
            if '独立内容编辑' in payload['messages'][0]['content']:
                answer=verdict(data,fail_path='$.intro' if first_fail and len(self.calls)==2 else None)
            elif data['feedback']:
                if missing:answer={'replace':{'closing':self.body['closing']}}
                else:answer={'replace':{'intro':['这个案例用一个明确的工作难题组织内容，让观众先看见自己要完成的事情。']}}
            else:
                answer=copy.deepcopy(self.body)
                if missing:answer.pop('closing')
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'type':'json_object',**answer},ensure_ascii=False)}}],
                                          'usage':{'prompt_tokens':100,'completion_tokens':300}})
        return RemoteAnalyst(threading.Event(),'SYNTHETIC-KEY',self.root/'model-events.jsonl',httpx.MockTransport(send),settle_usage=True)
    def run_it(self,model):
        return reviewed_generation(model,'report-sections',self.packet,self.store,
            lambda body:accept_research(self.store,assemble(self.packet,body),expected_format='selection-report-v1'),lambda body:assemble(self.packet,body))
    def test_compact_packet_has_no_baselines_stats_or_old_story(self):
        raw=json.dumps(self.packet,ensure_ascii=False)
        self.assertNotIn('baseline',raw);self.assertNotIn('play_count',raw);self.assertNotIn('research',raw)
        self.assertTrue(self.packet['materials']);self.assertIn('写作示例',messages_for(self.packet)[0]['content'])
    def test_program_metadata_and_all_body_from_real_mock_response(self):
        self.run_it(self.model());self.assertEqual(len(self.calls),2);self.assertTrue(verify_saved(self.store))
        story=self.store.state['research'];self.assertEqual(story['format'],'selection-report-v1')
        self.assertEqual(story['intro'],self.body['intro']);self.assertEqual(len(story['limitations']),1)
    def test_targeted_patch_preserves_other_sections_and_is_traceable(self):
        self.run_it(self.model(first_fail=True));self.assertEqual(len(self.calls),4);self.assertTrue(verify_saved(self.store))
        self.assertEqual(self.store.state['research']['closing'],self.body['closing'])
        repair=json.loads(self.calls[2]['messages'][1]['content']);self.assertEqual(replacement_keys(self.packet,repair['feedback']),['intro'])
        receipt=read_json(self.store.root/'content-review.json');self.assertEqual(len(receipt['draft_attempts']),2)
        raw=read_json(self.root/'model-diagnostics/attempt-003-response.json');raw['answer']['replace']['intro']=['篡改后的分析内容不能通过凭据检查。']
        atomic_json(self.root/'model-diagnostics/attempt-003-response.json',raw);self.assertFalse(verify_saved(self.store))
    def test_missing_section_is_repaired_without_rewriting_other_sections(self):
        self.run_it(self.model(missing=True));self.assertEqual(len(self.calls),3);self.assertTrue(verify_saved(self.store))
        self.assertEqual(self.store.state['research']['intro'],self.body['intro'])

    def test_complete_invalid_json_can_be_replaced_once_with_full_new_body(self):
        def send(request):
            payload=json.loads(request.content);self.calls.append(payload)
            if len(self.calls)==1:content='{"title":'
            elif len(self.calls)==2:content=json.dumps({'replace':self.body},ensure_ascii=False)
            else:content=json.dumps(verdict(json.loads(payload['messages'][1]['content'])),ensure_ascii=False)
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':content}}],
                'usage':{'prompt_tokens':100,'completion_tokens':300}})
        model=RemoteAnalyst(threading.Event(),'SYNTHETIC-KEY',self.root/'model-events.jsonl',httpx.MockTransport(send),settle_usage=True)
        self.run_it(model);self.assertEqual(len(self.calls),3);self.assertTrue(verify_saved(self.store))
        ledger=model.entries();ledger[1]['response_complete']=False
        (self.root/'model-events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in ledger))
        self.assertFalse(verify_saved(self.store))

    def test_unrequested_patch_or_manual_metadata_is_rejected(self):
        feedback={'error':{'path':'$.intro'},'previous_answer':self.body}
        with self.assertRaises(ValidationIssue):resolve(self.packet,{'replace':{'title':'不应修改'}},feedback)
        with self.assertRaises(ValidationIssue):assemble(self.packet,{**self.body,'limitations':['人工补齐']})
    def test_additional_method_cannot_change_fixed_structure(self):
        with self.assertRaises(ValidationIssue):assemble(self.packet,{**self.body,'method_2':self.body['method_1']})
    def test_approved_fourteen_is_distinct_from_long_term_twenty(self):
        from report import render
        from verification import audit_run
        self.store.config['delivery_target_count']=1;atomic_json(self.store.root/'config.json',self.store.config)
        self.store.state['config_hash']=digest(self.store.config);self.store.save();self.run_it(self.model());summary=render(self.store)
        self.assertTrue(summary['delivery_target_met']);self.assertFalse(summary['quantity_target_met'])
        # Real demo fixtures omit 'request' events; this check only targets the new independent count fields.
        count=next(c for c in audit_run(self.store.root)['checks'] if c['id']=='summary-counts');self.assertEqual(count['status'],'PASS')

if __name__=='__main__':unittest.main()
