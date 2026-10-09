import copy,json,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Store,StopRun,atomic_json,read_json,default_config,now
from bridge import FileBridge,AssistanceNeeded,next_request,submit
from research import accept_research,validate_research,report_state,accept_review,add_evidence
from demo import run_demo
from report import render
from workflow import verify,CachedClient
from wizard import Session


def answer_from(story):
    return {key:copy.deepcopy(story[key]) for key in ['headline','lead','findings','categories','cases','next_topics','limitations']}


class ResearchContract(unittest.TestCase):
    def test_cases_require_categories_and_next_topics(self):
        with tempfile.TemporaryDirectory() as temp:
            store,_=run_demo(Path(temp)/'demo')
            for key in ['categories','next_topics']:
                answer=answer_from(store.state['research']);answer[key]=[]
                with self.assertRaises(StopRun):validate_research(store,answer)

    def test_parallel_evidence_registration_preserves_both_under_store_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            store,_=run_demo(Path(temp)/'demo');vids=list(store.state['candidates'])[:2]
            files=[]
            for i,vid in enumerate(vids):
                file=Path(temp)/(vid+'.txt');file.write_text('合成证据内容，只用于验证并发登记不丢失。这不是来自真实视频的转写。'+str(i),encoding='utf-8');files.append(file)
            errors=[]
            def add(vid,path):
                try:add_evidence(store,vid,path,'video-notes','合成测试观看记录')
                except Exception as e:errors.append(e)
            with store.lock():
                threads=[threading.Thread(target=add,args=(vid,path)) for vid,path in zip(vids,files)]
                for t in threads:t.start()
                for t in threads:t.join(4)
            index=read_json(store.root/'evidence-index.json')
            self.assertFalse(errors);self.assertTrue(all(vid in index for vid in vids))
            self.assertTrue(all(any(e['kind']=='video-notes' for e in index[vid]) for vid in vids))

    def test_offline_completed_analysis_refreshes_session_status(self):
        with tempfile.TemporaryDirectory() as temp:
            store,_=run_demo(Path(temp)/'demo');full=answer_from(store.state['research']);partial=copy.deepcopy(full)
            for vid,case in partial['cases'].items():case.update(evidence_ids=[vid+':title'],opening='',outline=[])
            accept_research(store,partial);summary=render(store)
            session=Session(Path(temp)/'unused');session.run_dir=store.root;session.status='needs_analysis';session.summary=summary
            accept_research(store,full);render(store)
            updated=session.snapshot()
            self.assertEqual(updated['status'],'done');self.assertTrue(updated['summary']['report_complete'])

    def test_title_only_cannot_claim_spoken_opening(self):
        with tempfile.TemporaryDirectory() as temp:
            store,_=run_demo(Path(temp)/'demo');answer=answer_from(store.state['research'])
            vid=next(iter(answer['cases']));answer['cases'][vid]['evidence_ids']=[vid+':title']
            with self.assertRaises(StopRun):validate_research(store,answer)
            answer['cases'][vid]['opening']='';answer['cases'][vid]['outline']=[]
            partial=accept_research(store,answer)
            self.assertEqual(partial['status'],'partial');self.assertFalse(render(store)['report_complete'])

    def test_unknown_citations_and_statistical_overrides_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            store,_=run_demo(Path(temp)/'demo');answer=answer_from(store.state['research'])
            answer['findings'][0]['video_ids']=['9999999999999999999']
            with self.assertRaises(StopRun):validate_research(store,answer)
            answer=answer_from(store.state['research']);answer['qualified_count']=99
            with self.assertRaises(StopRun):validate_research(store,answer)
            self.assertEqual(render(store)['qualified_count'],1)

    def test_changed_evidence_invalidates_report_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            store,_=run_demo(Path(temp)/'demo')
            self.assertTrue(render(store)['report_complete'])
            entry=next(iter(read_json(store.root/'evidence-index.json').values()))[0]
            (store.root/entry['file']).write_text('tampered',encoding='utf-8')
            self.assertEqual(report_state(store)['status'],'awaiting_analysis')

    def test_changed_source_does_not_keep_old_story(self):
        with tempfile.TemporaryDirectory() as temp:
            store,_=run_demo(Path(temp)/'demo');next(iter(store.state['candidates'].values()))['title']='different title'
            self.assertIsNone(report_state(store)['story'])

    def test_all_candidates_must_be_accounted_for(self):
        with tempfile.TemporaryDirectory() as temp:
            store,_=run_demo(Path(temp)/'demo');before=copy.deepcopy(store.state['candidates'])
            with self.assertRaises(StopRun):accept_review(store,{'reviews':{},'next_keywords':[]})
            self.assertEqual(before,store.state['candidates'])

    def test_21_candidates_three_prior_requests_are_not_zero_results(self):
        with tempfile.TemporaryDirectory() as temp:
            c=default_config('AI创作',['query-a','query-b','query-c'],'0.50')
            c['pricing']={'source_url':'https://tikhub.io/pricing','checked_at':now(),'usd_per_request':{k:'0.01' for k in ['search','posts','stats','comments']}}
            store=Store.create(Path(temp)/'run',c)
            for i in range(3):store.reserve('search','original-'+str(i))
            for i in range(21):
                vid=str(1111111111111111100+i)
                store.state['candidates'][vid]={'id':vid,'title':'AI做视频 '+str(i),'author_name':'synthetic','author_id':'synthetic_author_'+str(i),'followers':4000 if i<2 else 200000,'published_at':1700000000,'likes':1,'queries':[], 'is_top':False,'review':{'decision':'unsure','reason':'semantic review pending','priority':1,'method':'keyword-rule'}}
            store.save();journal=(store.root/'requests.jsonl').read_bytes()
            before=render(store);self.assertEqual(before['review_pending_count'],21);self.assertFalse(before['report_complete'])
            reviews={vid:{'decision':'include','reason':'具体AI视频制作主题','priority':1} for vid in store.state['candidates']}
            accept_review(store,{'reviews':reviews,'next_keywords':[]})
            verify(store,CachedClient(store));summary=render(store)
            self.assertEqual(summary['pending_count'],2)
            self.assertEqual(summary['qualified_count'],0)
            self.assertEqual(summary['reserved_usd_at_configured_prices'],'0.03')
            self.assertEqual(journal,(store.root/'requests.jsonl').read_bytes())
            self.assertIn('还没走完核验的线索',(store.root/'report.html').read_text(encoding='utf-8'))


class BridgeContract(unittest.TestCase):
    def test_host_loop_generates_full_report_without_provider_network(self):
        from demo import ReplayClient
        root=Path(__file__).resolve().parents[1]
        fixture=read_json(root/'assets/demo.json')
        fixture['queries']['FDE']=list(dict.fromkeys(vid for rows in fixture['queries'].values() for vid in rows))
        quote={'source_url':'https://tikhub.io/pricing','checked_at':now(),'usd_per_request':{k:'0.001' for k in ['search','posts','stats','comments']}}
        def fake_client(store,**kwargs):
            replay=ReplayClient.__new__(ReplayClient);replay.store=store;replay.fixture=fixture;return replay
        with tempfile.TemporaryDirectory() as temp:
            session=Session(Path(temp)/'data',Path(temp)/'bridge',bridge_timeout=10)
            session.mode='live';session.key='SYNTHETIC-NOT-REAL'
            stages=[];errors=[]
            def host():
                after=None
                try:
                    for _ in range(15):
                        req=next_request(session.bridge.folder,after,wait=1)
                        if req['stage']=='waiting_for_user':continue
                        if req['stage']=='finished':return
                        stages.append(req['stage']);packet=req['payload']
                        if req['stage']=='prepare-research':answer={'ready':True}
                        elif req['stage']=='semantic-review':answer={'reviews':{v['id']:{'decision':'include','reason':'合成的同主题研究候选','priority':1} for v in packet['candidates']},'next_keywords':[]}
                        elif req['stage']=='content-analysis':
                            store=Store(packet['run_dir']);cases={}
                            for vid,r in store.state['results'].items():
                                if r['status'] not in ('qualified','limited'):continue
                                source=Path(temp)/(vid+'.txt');source.write_text('合成逐字稿：开场提出重复录入问题，中段展示输入和操作，最后核对结果并保留人工判断。只用于离线测试。',encoding='utf-8')
                                entry=add_evidence(store,vid,source,'transcript','测试构造的逐字稿，无外部视频或用户数据。')
                                cases[vid]={'title':'合成的任务演示','category':'任务演示','summary':'用一个小任务解释流程。','title_angle':'以任务作为入口。','opening':'先提出重复录入问题。','outline':['展示输入','完成操作','检查结果'],'takeaway':'参考清晰的任务组织方式。','limitations':'全部为合成测试素材。','evidence_ids':[entry['id']]}
                            from report_fixture import selection_answer
                            from research import analysis_packet
                            answer=selection_answer(analysis_packet(store))
                        else:raise AssertionError(req)
                        submit(session.bridge.folder,req['request_id'],answer);after=req['request_id']
                except Exception as e:errors.append(e)
            thread=threading.Thread(target=host,daemon=True);thread.start()
            until=time.monotonic()+2
            while not session.bridge.available() and time.monotonic()<until:time.sleep(.01)
            with patch('wizard.fetch_prices',return_value=quote),patch('wizard.TikHubClient',side_effect=fake_client):
                session.start({'description':'FDE','consent':True})
                session.worker.join(10);thread.join(2)
            self.assertFalse(errors,errors);self.assertEqual(stages,['prepare-research','semantic-review','content-analysis'])
            self.assertEqual(session.status,'done');self.assertTrue(session.summary['report_complete']);self.assertIsNone(session.key)
            html=(session.run_dir/'report.html').read_text(encoding='utf-8')
            self.assertIn('自动生成的离线合成报告',html);self.assertIn('把一个具体任务讲清楚',html);self.assertIn('案例解读引用了本轮登记的内容证据',html)

    def test_missing_host_stops_before_quote_or_paid_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            s=Session(Path(temp)/'data',Path(temp)/'bridge');s.mode='live';s.key='SYNTHETIC'
            with patch('wizard.fetch_prices') as prices, self.assertRaises(StopRun):s.start({'description':'AI','consent':True})
            prices.assert_not_called();self.assertIsNone(s.run_dir)

    def test_timeout_removes_request_and_rejects_late_submit(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge=FileBridge(temp,threading.Event(),timeout=.05)
            with self.assertRaises(AssistanceNeeded):bridge.ask('content-analysis',{'run_dir':'/tmp/synthetic-run'},lambda x:x)
            self.assertFalse((Path(temp)/'request.json').exists())
            with self.assertRaises(StopRun):submit(temp,'late',{})

    def test_cancelled_request_does_not_apply_answer(self):
        with tempfile.TemporaryDirectory() as temp:
            cancel=threading.Event();cancel.set();bridge=FileBridge(temp,cancel,timeout=1)
            used=[]
            with self.assertRaises(AssistanceNeeded):bridge.ask('semantic-review',{'run_dir':'/tmp/synthetic-run'},lambda x:used.append(x))
            self.assertEqual(used,[])

    def test_stale_identity_rejected_then_valid_response_applied_once(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge=FileBridge(temp,threading.Event(),timeout=3);result=[];errors=[]
            def worker():
                try:result.append(bridge.ask('semantic-review',{'run_dir':'/tmp/synthetic-run'},lambda x:x['value']))
                except Exception as e:errors.append(e)
            thread=threading.Thread(target=worker);thread.start()
            request=next_request(temp,wait=1)
            bad={k:request[k] for k in ['session_id','request_id','run_id','stage','payload_hash']};bad['run_id']='wrong';bad['answer']={'value':9}
            atomic_json(Path(temp)/'response.json',bad)
            until=time.monotonic()+1
            while not (Path(temp)/'feedback.json').exists() and time.monotonic()<until:time.sleep(.02)
            self.assertFalse(read_json(Path(temp)/'feedback.json')['accepted'])
            submit(temp,request['request_id'],{'value':7});thread.join(3)
            self.assertFalse(errors);self.assertEqual(result,[7])
            with self.assertRaises(StopRun):submit(temp,request['request_id'],{'value':8})

    def test_host_lease_ends_when_wait_command_returns(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge=FileBridge(temp,threading.Event())
            result=next_request(temp,wait=0)
            self.assertEqual(result['stage'],'waiting_for_user');self.assertFalse(bridge.available())

    def test_previous_completion_does_not_block_new_wait(self):
        with tempfile.TemporaryDirectory() as temp:
            bridge=FileBridge(temp,threading.Event())
            atomic_json(Path(temp)/'status.json',{'terminal':True,'request_id':'previous','at':time.time()-10})
            self.assertEqual(next_request(temp,wait=0)['stage'],'waiting_for_user')
            self.assertEqual(next_request(temp,after='previous',wait=0)['stage'],'finished')


if __name__=='__main__':unittest.main()
