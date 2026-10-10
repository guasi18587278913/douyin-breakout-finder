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

    def test_three_featured_cases_keep_two_methods_and_enforce_counts(self):
        packet={**self.packet,'method_count':2,'featured_count':3}
        body=copy.deepcopy(self.body);body['method_2']=copy.deepcopy(body['method_1'])
        body['method_1']['cases'].append(copy.deepcopy(body['method_1']['cases'][0]))
        answer=assemble(packet,body)
        self.assertEqual([len(lesson['cases']) for lesson in answer['lessons']],[2,1])
        self.assertIn('method_1选2个不同案例',messages_for(packet)[0]['content'])
        body['method_1']['cases'].pop()
        with self.assertRaises(ValidationIssue):assemble(packet,body)

    def test_legacy_writer_receipt_remains_verifiable(self):
        self.packet=packet_for(self.store,protocol='report-sections-v1')
        self.assertNotIn('featured_count',self.packet)
        self.assertIn('每个方法只选1个代表案例',messages_for(self.packet)[0]['content'])
        self.run_it(self.model());self.assertTrue(verify_saved(self.store))
    def test_v2_request_and_receipt_remain_compatible(self):
        self.packet=packet_for(self.store,protocol='report-sections-v2')
        self.assertIn('featured_count',self.packet)
        self.assertIn('系统会用真实数据生成开场统计',messages_for(self.packet)[0]['content'])
        self.run_it(self.model());self.assertTrue(verify_saved(self.store))
    def test_new_writer_uses_reader_rules_and_blocks_internal_copy(self):
        self.assertEqual(self.packet['protocol'],'report-sections-v3')
        prompt=messages_for(self.packet)[0]['content']
        self.assertNotIn('少于20篇须明确说明',prompt)
        self.assertNotIn('系统会用真实数据生成开场统计',prompt)
        for text in ('本轮共找到18篇合格作品。','尚未达到本轮20–30篇的结果池目标。',
                     '3篇精选解读','2个选题方法','预算与数据边界','本报告依据作品标题和简介分析选题。',
                     '本轮查询了250次，下面展示筛选结果。','本报告已通过模型审核。','本轮模型调用费用为0.1美元。'):
            body=copy.deepcopy(self.body);body['intro']=[text]
            with self.subTest(text=text),self.assertRaises(ValidationIssue) as raised:assemble(self.packet,body)
            self.assertEqual(raised.exception.feedback['path'],'$.intro[0]')
        body=copy.deepcopy(self.body);body['intro']=['如果拍摄预算有限，可以先用家里的台灯试一个具体画面。']
        assemble(self.packet,body)
        body['intro']=['本次预算只有100元，先用一盏台灯拍完这条产品演示。']
        assemble(self.packet,body)
    def test_internal_copy_is_repaired_before_audit_without_changing_other_prose(self):
        self.body['intro']=['本轮共找到18篇合格作品。']
        self.run_it(self.model());self.assertEqual(len(self.calls),3)
        self.assertTrue(verify_saved(self.store))
        self.assertEqual(self.store.state['research']['closing'],self.body['closing'])
    def test_fresh_website_writer_delivers_the_clean_default_template(self):
        from unittest.mock import Mock
        from webapp.engine import HostedSession
        from webapp.cached_analysis import packet_for as cached_packet,messages_for as cached_messages
        from report import render
        self.store.config['reader_copy_required']=True;self.store.state['config_hash']=digest(self.store.config)
        atomic_json(self.store.root/'config.json',self.store.config);self.store.save()
        session=HostedSession(self.root,Mock(),{'id':'synthetic-reader-check'})
        session.assistant=self.model();session.active_store=self.store
        session.write_report(self.store);html=render(self.store,write=False)
        self.assertTrue(verify_saved(self.store))
        for token in ('class="selection-summary"','class="selection-index"','class="selection-opening"',
                      'class="data-appendix"','预算与数据边界','本报告依据'):
            self.assertNotIn(token,html)
        resumed=cached_packet(self.store)
        self.assertTrue(resumed['reader_copy_required'])
        self.assertIn('面向读者的正文不写内部流程说明',cached_messages(resumed)[0]['content'])
        self.assertNotIn('少于20篇须明确说明',cached_messages(resumed)[0]['content'])
    def test_unscoped_lower_target_does_not_replace_twenty(self):
        from report import render
        from verification import audit_run
        self.store.config['delivery_target_count']=1;atomic_json(self.store.root/'config.json',self.store.config)
        self.store.state['config_hash']=digest(self.store.config);self.store.save();self.run_it(self.model());summary=render(self.store)
        self.assertFalse(summary['delivery_target_met']);self.assertFalse(summary['quantity_target_met'])
        self.assertEqual(summary['delivery_target_count'],20)
        # Real demo fixtures omit 'request' events; this check only targets the new independent count fields.
        count=next(c for c in audit_run(self.store.root)['checks'] if c['id']=='summary-counts');self.assertEqual(count['status'],'PASS')

if __name__=='__main__':unittest.main()
