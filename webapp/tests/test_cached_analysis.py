import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import httpx

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
from core import Store, StopRun, ValidationIssue, atomic_json, read_json
from demo import run_demo
from research import analysis_packet, pending_reviews
from report_fixture import selection_answer
from selection_report import prose
from webapp.cached_analysis import (rebuild, packet_for, messages_for, accept_joint, run_cached,
                                   begin_publication, recover_publication)
from webapp.remote_analyst import RemoteAnalyst, AssistanceNeeded
from webapp.engine import HostedSession


class CachedTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source,_=run_demo(self.root/'original')
        records={p.stem:read_json(p) for p in (self.source.root/'cache').glob('*.json')}
        events=[]
        for event in self.source.events:
            events.append(event)
            if event['event']=='reserved':
                rec=records[event['key']]
                events.append({'event':'request','key':event['key'],'operation':rec['operation'],'params':rec['params'],'at':event['at']})
        (self.source.root/'requests.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
        self.source.events=self.source._events()
        self.source.state['research']={'sentinel':'MANUAL_STORY_MUST_NOT_BE_READ'}
        self.source.state['analyses']={'sentinel':'MANUAL_ANALYSIS_MUST_NOT_BE_READ'}
        for v in self.source.state['candidates'].values():v['review']={'decision':'include','reason':'MANUAL_REVIEW_MUST_NOT_BE_READ'}
        self.source.save()
        self.work=rebuild(self.source,self.root/'replay')
        self.packet=packet_for(self.work)
        self.ledger=self.root/'model-events.jsonl';self.cancel=threading.Event();self.calls=[]

    def response(self, packet):
        before=copy.deepcopy(self.work.state)
        try:
            from workflow import apply_review
            apply_review(self.work,{row[1]:{'decision':'include','reason':'合成主题相关','priority':1} for row in packet['candidates']})
            return {'reviews':[[row[0],'include','合成主题相关'] for row in packet['candidates']],
                    'report':selection_answer(analysis_packet(self.work))}
        finally:self.work.state=before;self.work.save()

    def model(self, answer=None, status=200):
        if answer is None:answer=self.response(self.packet)
        def send(request):
            payload=json.loads(request.content);self.calls.append(payload)
            actual=answer
            if '独立内容编辑' in payload['messages'][0]['content']:
                from quality_fixture import verdict
                actual=verdict(json.loads(payload['messages'][1]['content']))
            if status!=200:return httpx.Response(status,json={'error':'synthetic failure'})
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(actual)}}],
                                            'usage':{'prompt_tokens':100,'completion_tokens':200}})
        return RemoteAnalyst(self.cancel,'SYNTHETIC-MODEL-KEY-ONLY',self.ledger,httpx.MockTransport(send))

    def test_raw_rebuild_drops_all_manual_derivations_and_preserves_journal(self):
        self.assertEqual(set(self.work.state['candidates']),set(self.source.state['candidates']))
        self.assertEqual((self.work.root/'requests.jsonl').read_bytes(),(self.source.root/'requests.jsonl').read_bytes())
        prompt=json.dumps(messages_for(self.packet),ensure_ascii=False)
        self.assertNotIn('MANUAL_',prompt)
        self.assertEqual(self.work.state['analyses'],{})
        self.assertNotIn('research',self.work.state)
        self.assertIn((ROOT/'references/report-standard.md').read_text(),messages_for(self.packet)[0]['content'])
        self.assertIn((ROOT/'references/report-example.json').read_text(),messages_for(self.packet)[0]['content'])

    def test_pending_candidate_without_play_result_is_not_counted_as_qualified(self):
        vid=self.packet['candidates'][0][1]
        self.work.state['results'].pop(vid,None)
        packet=packet_for(self.work)
        index=next(r[0] for r in packet['candidates'] if r[1]==vid)
        self.assertTrue(packet['data_incomplete']);self.assertNotIn(index,packet['numerically_qualified'])

    def test_joint_answer_requires_every_candidate_and_valid_statistics(self):
        answer=self.response(self.packet)
        bad=copy.deepcopy(answer);bad['reviews']=[]
        with self.assertRaises(ValidationIssue):accept_joint(self.work,self.packet,bad)
        bad=copy.deepcopy(answer)
        featured=bad['report']['lessons'][0]['cases'][0]['video_id']
        for row in bad['reviews']:
            if self.packet['candidates'][row[0]][1]==featured:row[1]='exclude'
        before=copy.deepcopy(self.work.state)
        with self.assertRaises(ValidationIssue):accept_joint(self.work,self.packet,bad)
        self.assertEqual(before,self.work.state)
        self.assertEqual(accept_joint(self.work,self.packet,answer)['status'],'complete')

    def test_no_refund_budget_matches_exact_payload_and_token_limit(self):
        m=self.model();m.append({'kind':'reserve','usd':'0.14'})
        plan=m.plan('cached-report',self.packet,'')
        m.ask('cached-report',self.packet,lambda a:accept_joint(self.work,self.packet,a))
        self.assertEqual(len(self.calls),1)
        call=self.calls[0]
        self.assertEqual(call['max_tokens'],plan['maximum'])
        bound=sum(len(row['content'].encode()) for row in call['messages'])+2048
        actual=(Decimal(bound)*Decimal('.30')+Decimal(call['max_tokens'])*Decimal('1.20'))/1000000
        self.assertEqual(actual,Decimal(plan['reserved_usd']))
        self.assertLessEqual(sum(Decimal(r['usd']) for r in m.entries() if r['kind']=='reserve'),Decimal('.15'))

    def test_invalid_output_saved_but_not_retried_or_promoted(self):
        m=self.model({'reviews':[],'report':{}})
        with self.assertRaises(AssistanceNeeded):m.ask('cached-report',self.packet,lambda a:accept_joint(self.work,self.packet,a))
        self.assertEqual(len(self.calls),1)
        self.assertTrue((self.root/'model-diagnostics/attempt-001-response.json').is_file())
        detail=read_json(self.root/'model-diagnostics/attempt-001-validation.json')
        self.assertFalse(detail['accepted']);self.assertEqual(detail['detail']['path'],'$.reviews')
        self.assertNotIn('SYNTHETIC-MODEL-KEY-ONLY',json.dumps(detail))

    def test_received_response_reused_with_full_ledger_and_no_extra_request(self):
        m=self.model();expected=m.generate('cached-report',self.packet,'')
        spent=sum(Decimal(r['usd']) for r in m.entries() if r['kind']=='reserve')
        m.append({'kind':'reserve','usd':str(Decimal('.15')-spent)})
        before=self.ledger.read_bytes()
        plan=m.plan('cached-report',self.packet,'');self.assertEqual(plan['reserved_usd'],'0')
        self.assertEqual(m.generate('cached-report',self.packet,''),expected)
        self.assertEqual(len(self.calls),1);self.assertEqual(before,self.ledger.read_bytes())

    def test_failed_request_and_tampered_response_never_resubmit(self):
        m=self.model(status=429)
        with self.assertRaises(AssistanceNeeded):m.generate('cached-report',self.packet,'')
        with self.assertRaises(AssistanceNeeded):m.generate('cached-report',self.packet,'')
        self.assertEqual(len(self.calls),1)
        self.ledger.unlink();self.calls.clear()
        m=self.model();m.generate('cached-report',self.packet,'')
        path=self.root/'model-diagnostics/attempt-001-response.json'
        saved=read_json(path);saved['answer']['report']['title']='tampered';atomic_json(path,saved)
        with self.assertRaises(AssistanceNeeded):m.generate('cached-report',self.packet,'')
        self.assertEqual(len(self.calls),1)

    def test_forged_numeric_exclusion_does_not_skip_semantic_review(self):
        vid=next(row[1] for row in self.packet['candidates'])
        from research import review_fingerprint
        self.work.state['candidates'][vid]['review']={'decision':'exclude','method':'eligibility-rule',
            'input_hash':review_fingerprint(self.work,vid)}
        self.assertIn(vid,pending_reviews(self.work))

    def test_all_numeric_exclusions_can_finish_an_honest_empty_report(self):
        for path in (self.source.root/'cache').glob('*.json'):
            rec=read_json(path)
            if rec['operation']=='search':
                for video in rec['data']['videos']:video['followers']=999999
                atomic_json(path,rec)
        fresh=rebuild(self.source,self.root/'all-large')
        packet=packet_for(fresh)
        self.assertEqual(packet['candidates'],[])
        self.assertEqual(pending_reviews(fresh),{})
        self.assertEqual(accept_joint(fresh,packet,{'reviews':[],'report':selection_answer(analysis_packet(fresh))})['status'],'complete')

    def test_json_escaped_credentials_are_never_written_to_diagnostics(self):
        key='SYNTHETIC-MODEL-KEY-ONLY'
        escaped=''.join('\\u%04x'%ord(c) for c in key)
        def send(request):return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"note":"'+escaped+'"}'}}]})
        m=RemoteAnalyst(self.cancel,key,self.ledger,httpx.MockTransport(send))
        with self.assertRaises(AssistanceNeeded):m.generate('cached-report',self.packet,'')
        folder=self.root/'model-diagnostics'
        self.assertFalse((folder/'attempt-001-raw-output.json').exists())
        self.assertFalse((folder/'attempt-001-response.json').exists())
        for path in folder.glob('*.json'):
            text=path.read_text();self.assertNotIn(key,text);self.assertNotIn(escaped,text)

    def test_publication_interrupt_rolls_back_and_durable_marker_recovers(self):
        class DB:
            def progress(*args):pass
        before={name:(self.source.root/name).read_bytes() for name in ('state.json','report.html','requests.jsonl')}
        session=HostedSession(self.root/'session',DB(),{'id':'synthetic'});session.assistant=self.model();session.mode='live'
        original=os.replace;triggered=[]
        def interrupt(src,dst):
            if Path(dst)==self.source.root/'report.html' and not triggered:
                triggered.append(True);raise KeyboardInterrupt()
            return original(src,dst)
        with patch('os.replace',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):run_cached(session,self.source)
        for name,raw in before.items():self.assertEqual((self.source.root/name).read_bytes(),raw)
        self.assertFalse((self.source.root/'.automatic-publish.json').exists())
        replay=self.source.root.parent/'automatic-replay-recovery-test';replay.mkdir()
        begin_publication(self.source.root,replay/'previous')
        (self.source.root/'state.json').write_text('{"interrupted":true}')
        self.assertTrue(recover_publication(self.source.root))
        for name,raw in before.items():self.assertEqual((self.source.root/name).read_bytes(),raw)
        self.assertFalse(recover_publication(self.source.root))
        # A durable replay can publish again after rollback without overwriting the old backup.
        begin_publication(self.source.root,replay/'previous')
        with self.assertRaises(StopRun):begin_publication(self.source.root,replay/'previous')
        self.assertTrue(recover_publication(self.source.root))
        for name,raw in before.items():self.assertEqual((self.source.root/name).read_bytes(),raw)

    def test_complete_worker_path_needs_writer_and_review_but_no_data_client(self):
        class DB:
            def progress(*args):pass
        session=HostedSession(self.root/'session',DB(),{'id':'synthetic'})
        session.assistant=self.model();session.mode='live';session.run_dir=self.source.root
        journal=(self.source.root/'requests.jsonl').read_bytes()
        with patch('tikhub.TikHubClient',side_effect=AssertionError('No paid data client allowed')):
            run_cached(session,self.source)
        self.assertEqual(session.status,'done');self.assertEqual(len(self.calls),2)
        self.assertEqual(journal,(self.source.root/'requests.jsonl').read_bytes())
        receipt=read_json(self.source.root/'automatic-receipt.json')
        self.assertFalse(receipt['prior_reviews_used']);self.assertFalse(receipt['prior_story_used'])
        self.assertEqual(read_json(self.source.root/'verification.json')['status'],'PASS')

    def test_validation_failure_preserves_previous_report(self):
        class DB:
            def progress(*args):pass
        before={name:(self.source.root/name).read_bytes() for name in ('state.json','report.html','requests.jsonl')}
        session=HostedSession(self.root/'session',DB(),{'id':'synthetic'})
        session.assistant=self.model({'reviews':[],'report':{}});session.mode='live'
        with self.assertRaises(AssistanceNeeded):run_cached(session,self.source)
        for name,raw in before.items():self.assertEqual((self.source.root/name).read_bytes(),raw)
        self.assertNotEqual(session.status,'done')


class NumericProseTests(unittest.TestCase):
    def test_non_quantitative_language_is_not_a_statistic(self):
        for value in ('播放表现不能单独证明某一种标题有效。','播放数据只能提供一种观察角度。',
                      '粉丝的反馈不等于一种因果证据。','不能仅凭播放推断一般规律。'):
            self.assertEqual(prose(value,'依据说明'),value)
    def test_arabic_and_chinese_statistics_remain_rejected(self):
        for value in ('播放量达到了999万。','这一条有999万次播放。','作者粉丝不过三千，播放超过三十万。',
                      '播放量最终超过三十万。','粉丝大约一千四百。','相当于平常的十二倍。','播放量保守估计124248。',
                      '播放量保守估计三千。','作者粉丝总共两千。'):
            with self.subTest(value=value):
                with self.assertRaises(StopRun):prose(value,'正文')

if __name__=='__main__':unittest.main()
