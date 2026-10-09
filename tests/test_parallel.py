import copy
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import Store, default_config, now, stamp, APIError, BudgetStop, StopRun, DAY
from tikhub import TikHubClient, RateLimitError
from workflow import search, prefetch_histories, get_stats, verify
from parallel_requests import call_many


class ParallelContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        c = default_config('合成', ['甲', '乙', '丙'], '0.005')
        c['max_requests'] = 5
        c['pricing'] = {'source_url':'https://tikhub.io/pricing', 'checked_at':now(),
                        'usd_per_request':{op:'0.001' for op in ('search','posts','stats','comments')}}
        self.store = Store.create(Path(self.temp.name) / 'run', c)

    def tearDown(self): self.temp.cleanup()

    def payload(self, op, params):
        return {'code':200,'data':{'statistics_list':[{'aweme_id':v,'play_count':100} for v in params['aweme_ids'].split(',')]}}

    def test_parallel_reservations_cannot_exceed_budget_or_request_limit(self):
        gate = threading.Lock(); counts = {'calls':0,'active':0,'peak':0}
        def transport(op, params):
            with gate:
                counts['calls']+=1;counts['active']+=1;counts['peak']=max(counts['peak'],counts['active'])
            time.sleep(.025)
            with gate:counts['active']-=1
            return self.payload(op, params)
        client = TikHubClient(self.store, True, transport=transport, api_key='SYNTHETIC-PARALLEL-KEY', request_interval=0)
        calls=[('stats',{'aweme_ids':str(1234567890123456700+i)},{}) for i in range(12)]
        results=call_many(client,calls)
        self.assertEqual(counts['calls'],5)
        self.assertEqual(sum(error is None for _,error in results),5)
        self.assertLessEqual(counts['peak'],3);self.assertGreater(counts['peak'],1)
        self.assertEqual(str(self.store.reserved_cost()),'0.005')
        self.assertEqual(len(self.store.reservations()),5)
        self.assertEqual(sum(isinstance(error,BudgetStop) for _,error in results),7)
        lines=(self.store.root/'requests.jsonl').read_text(encoding='utf-8').splitlines()
        self.assertEqual(len(lines),15)
        self.assertTrue(all(isinstance(json.loads(line),dict) for line in lines))

    def test_same_inflight_request_uses_one_reservation(self):
        count=[0]
        def transport(op,params):
            count[0]+=1;time.sleep(.04);return self.payload(op,params)
        client=TikHubClient(self.store,True,transport=transport,api_key='SYNTHETIC-PARALLEL-KEY',request_interval=0)
        with ThreadPoolExecutor(max_workers=8) as pool:
            values=list(pool.map(lambda _:client.call('stats',{'aweme_ids':'1234567890123456789'}),range(8)))
        self.assertEqual(count[0],1);self.assertEqual(len(self.store.reservations()),1)
        self.assertTrue(all(value==values[0] for value in values))
        client.call('stats',{'aweme_ids':'1234567890123456789'})
        self.assertEqual(count[0],1)

    def test_save_failure_stops_unsent_queue(self):
        calls=[]
        def transport(op, params):
            calls.append(params);time.sleep(.015);return self.payload(op,params)
        client=TikHubClient(self.store,True,transport=transport,api_key='SYNTHETIC-PARALLEL-KEY',request_interval=0)
        requests=[('stats',{'aweme_ids':str(1234567890123456700+i)},{}) for i in range(12)]
        with patch('tikhub.atomic_json',side_effect=OSError('disk full')):
            results=call_many(client,requests)
        self.assertLessEqual(len(calls),3)
        self.assertLessEqual(len(self.store.reservations()),3)
        self.assertTrue(all(isinstance(error,StopRun) for _,error in results))
        before=len(calls)
        with self.assertRaises(StopRun):client.call('stats',{'aweme_ids':'1234567890123456799'})
        self.assertEqual(len(calls),before)

    def test_later_author_page_failure_does_not_discard_sufficient_recent_history(self):
        ts=int(stamp(self.store.state['as_of']))
        def video(i,days,likes=1):
            return {'id':str(1234567890123456700+i),'title':'合成作品','author_id':'author_shared','author_name':'合成作者',
                    'followers':500,'published_at':ts-days*DAY,'likes':likes,'is_top':False,
                    'review':{'decision':'include','reason':'同一主题','priority':1,'method':'human-or-agent'}}
        recent=video(0,10,100);old=video(1,120,90)
        baseline=[video(i+2,11+i) for i in range(10)]
        self.store.state['candidates']={v['id']:v for v in (recent,old)}
        history_calls=[]
        class Client:
            parallel_safe=True
            def call(self,op,params):
                if op=='posts':
                    history_calls.append(params['max_cursor'])
                    if params['max_cursor']:raise APIError('later page failed')
                    return {'videos':[recent]+baseline,'has_more':True,'cursor':1}
                return {'statistics':{vid:{'play_count':20000 if vid==recent['id'] else 1000} for vid in params['aweme_ids'].split(',')}}
        verify(self.store,Client())
        self.assertEqual(history_calls,[0,1])
        self.assertEqual(self.store.state['results'][recent['id']]['status'],'qualified')
        self.assertEqual(self.store.state['results'][old['id']]['status'],'pending')

    def test_start_rate_is_limited_and_cancel_stops_queued_work(self):
        starts=[]
        def transport(op,params):starts.append(time.monotonic());return self.payload(op,params)
        client=TikHubClient(self.store,True,transport=transport,api_key='SYNTHETIC-PARALLEL-KEY',request_interval=.08)
        call_many(client,[('stats',{'aweme_ids':str(1234567890123456700+i)},{}) for i in range(3)])
        self.assertGreaterEqual(min(b-a for a,b in zip(sorted(starts),sorted(starts)[1:])),.06)
        client.cancel_event.set()
        with self.assertRaises(StopRun):client.call('stats',{'aweme_ids':'1234567890123456790'})
        self.assertEqual(len(starts),3)

    def test_rate_limit_does_not_retry_and_auth_failure_stops_new_calls(self):
        calls=[]
        def limited(op,params):calls.append(1);raise RateLimitError('rate limited')
        client=TikHubClient(self.store,True,transport=limited,api_key='SYNTHETIC-PARALLEL-KEY',request_interval=0)
        with self.assertRaises(RateLimitError):client.call('stats',{'aweme_ids':'1234567890123456789'})
        self.assertEqual(len(calls),1);self.assertGreater(client._next_start['stats'],time.monotonic()+1)
        with self.assertRaises(APIError):client.call('stats',{'aweme_ids':'1234567890123456789'})
        self.assertEqual(len(calls),1)
        other=TikHubClient(self.store,True,transport=lambda *args:(_ for _ in ()).throw(StopRun('authentication failed')),api_key='SYNTHETIC-PARALLEL-KEY',request_interval=0)
        with self.assertRaises(StopRun):other.call('stats',{'aweme_ids':'1234567890123456790'})
        reserved=len(self.store.reservations())
        with self.assertRaises(StopRun):other.call('stats',{'aweme_ids':'1234567890123456791'})
        self.assertEqual(len(self.store.reservations()),reserved)

    def test_keyword_results_merge_without_lost_duplicates(self):
        source=stamp(self.store.state['as_of'])
        class Client:
            parallel_safe=True
            def call(self,op,params):
                time.sleep(.02)
                return {'videos':[{'id':'1234567890123456789','title':'合成主题','published_at':int(source)-10*DAY,'author_id':'synthetic_author','is_top':False,'likes':3}],
                        'has_more':False,'cursor':0}
        owner=threading.get_ident();writes=[];save=self.store.save
        def record_save():writes.append(threading.get_ident());save()
        with patch.object(self.store,'save',side_effect=record_save):search(self.store,Client(),page_limit=1)
        self.assertEqual(len(self.store.state['candidates']),1)
        self.assertEqual(self.store.state['candidates']['1234567890123456789']['queries'],['甲','乙','丙'])
        self.assertEqual(set(writes),{owner})
        self.assertTrue(all(q['pages']==1 for q in self.store.state['queries'].values()))

    def test_author_prefetch_reuses_shared_author_and_keeps_state_writes_serial(self):
        ts=int(stamp(self.store.state['as_of']))-10*DAY
        targets=[{'id':str(1234567890123456700+i),'author_id':'author_A' if i<2 else 'author_B','published_at':ts,'is_top':False} for i in range(3)]
        calls=[];owner=threading.get_ident();writes=[];save=self.store.save
        class Client:
            parallel_safe=True
            def call(self,op,params):
                calls.append(params['sec_user_id']);time.sleep(.02)
                return {'videos':[t for t in targets if t['author_id']==params['sec_user_id']], 'has_more':False,'cursor':0}
        def record_save():writes.append(threading.get_ident());save()
        with patch.object(self.store,'save',side_effect=record_save):result=prefetch_histories(self.store,Client(),targets,'normal')
        self.assertEqual(result,{})
        self.assertCountEqual(calls,['author_A','author_B'])
        self.assertEqual(set(writes),{owner})

    def test_statistics_preserve_successes_when_another_batch_fails(self):
        ids=[str(1234567890123456700+i) for i in range(6)]
        class Client:
            parallel_safe=True
            def call(self,op,params):
                values=params['aweme_ids'].split(',');time.sleep(.015)
                if values[0]==ids[0]:raise APIError('one batch failed')
                return {'statistics':{v:{'play_count':100} for v in values}}
        with self.assertRaises(APIError):get_stats(self.store,Client(),ids)
        self.assertEqual(set(self.store.state['stats']),set(ids[2:]))


if __name__=='__main__':unittest.main()
