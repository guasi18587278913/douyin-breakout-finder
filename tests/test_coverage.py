import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import Store, default_config, now, stamp, DAY
from tikhub import TikHubClient
from workflow import search_extensions, search, verify, verification_batch_size


class CoverageContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.c = default_config('Skill', ['Skill'], '0.50')
        self.c.update(max_requests=50, max_search_pages=3)
        self.c['pricing'] = {'source_url':'https://tikhub.io/pricing','checked_at':now(),
                            'usd_per_request':dict(search='0.01',posts='0.001',stats='0.001',comments='0.001')}
        self.store = Store.create(Path(self.temp.name)/'run', self.c)
        self.ts = int(stamp(self.store.state['as_of']))

    def tearDown(self): self.temp.cleanup()

    def video(self, i, days=20, followers=100, author=None):
        return {'id':str(1234567890123000000+i),'title':'Skill合成案例','author_id':author or 'synthetic_author_'+str(i),
                'author_name':'合成作者','followers':followers,'published_at':self.ts-days*DAY,'likes':1000-i,'is_top':False,
                'review':{'decision':'include','reason':'主题相关','priority':3,'method':'human-or-agent'}}

    def query(self, word, ids, complete=False):
        self.store.state['queries'][word]={'ids':ids,'last_new_ids':ids[:],'last_page_ids':ids[:],'new_to_run':ids[:],
             'complete':complete,'pages':1,'cursor':1,'seen_cursors':[0],'search_id':'','backtrace':'','reason':'test'}

    def fixture(self, count, cached_baselines=False):
        targets=[self.video(i) for i in range(count)]
        self.store.state['candidates']={v['id']:v for v in targets}
        histories={};all_targets={v['id'] for v in targets};calls=[]
        for i,t in enumerate(targets):
            baseline=[self.video(1000+i*20+j,21+j,author=t['author_id']) for j in range(10)]
            histories[t['author_id']]=[t]+baseline
            if cached_baselines:
                for b in baseline:self.store.state['stats'][b['id']]={'play_count':1000}
        def raw(v):
            return {'aweme_id':v['id'],'desc':v['title'],'create_time':v['published_at'],
                    'author':{'sec_uid':v['author_id'],'nickname':v['author_name'],'follower_count':v['followers']},'statistics':{'digg_count':v['likes']}}
        def transport(op, params):
            calls.append((op,dict(params)))
            if op=='posts':data={'aweme_list':[raw(v) for v in histories[params['sec_user_id']]],'has_more':False,'max_cursor':0}
            elif op=='stats':data={'statistics_list':[{'aweme_id':vid,'play_count':30000 if vid in all_targets else 1000} for vid in params['aweme_ids'].split(',')]}
            else:raise AssertionError(op)
            return {'code':200,'data':data}
        client=TikHubClient(self.store,True,transport=transport,api_key='SYNTHETIC-COVERAGE-KEY',request_interval=0)
        return targets,histories,calls,client

    def test_ninth_and_tenth_candidate_can_be_verified_within_original_limits(self):
        targets,_,calls,client=self.fixture(10,cached_baselines=True)
        verify(self.store,client)
        self.assertEqual(sum(r['status']=='qualified' for r in self.store.state['results'].values()),10)
        self.assertEqual(len(self.store.reservations()),20)
        self.assertLessEqual(self.store.reserved_cost(),0.50)
        before=len(calls);verify(self.store,client);self.assertEqual(len(calls),before)

    def test_many_authors_do_not_consume_all_requests_before_statistics(self):
        _,_,calls,client=self.fixture(30)
        verify(self.store,client)
        self.assertEqual(len(self.store.reservations()),50)
        self.assertLessEqual(sum(op=='posts' for op,_ in calls[:4]),3)
        self.assertEqual(sum(r['status']=='qualified' for r in self.store.state['results'].values()),7)
        self.assertTrue(all(r['status'] in ('qualified','pending') for r in self.store.state['results'].values()))

    def test_short_remaining_budget_finishes_one_before_opening_more_authors(self):
        self.store.config['budget_usd']='0.007'
        _,_,calls,client=self.fixture(4)
        self.assertEqual(verification_batch_size(self.store),1)
        verify(self.store,client)
        self.assertEqual(len(calls),7)
        self.assertEqual(sum(r['status']=='qualified' for r in self.store.state['results'].values()),1)

    def test_too_new_candidate_does_not_take_verification_slot(self):
        self.store.config['max_candidates']=1
        targets,_,calls,client=self.fixture(2,cached_baselines=True)
        targets[0]['published_at']=self.ts-DAY
        verify(self.store,client)
        self.assertEqual(self.store.state['results'][targets[1]['id']]['status'],'qualified')
        self.assertFalse(any(op=='posts' and p['sec_user_id']==targets[0]['author_id'] for op,p in calls))

    def test_changed_author_invalidates_aliased_completed_result(self):
        targets,histories,calls,client=self.fixture(1,cached_baselines=True)
        target=targets[0];verify(self.store,client)
        old=self.store.state['results'][target['id']]
        self.assertIs(old['target'],target)
        new_author='synthetic_new_author'
        histories[new_author]=[dict(v,author_id=new_author) for v in histories[target['author_id']]]
        target['author_id']=new_author
        verify(self.store,client)
        result=self.store.state['results'][target['id']]
        self.assertEqual(result['history_key'],new_author+':normal')
        self.assertTrue(all(v['author_id']==new_author for v in result['baseline']))

    def test_changed_channel_does_not_reuse_normal_history(self):
        targets,_,calls,client=self.fixture(1,cached_baselines=True)
        verify(self.store,client)
        verify(self.store,client,channel='lite')
        self.assertEqual([p['channel'] for op,p in calls if op=='posts'],['normal','lite'])
        self.assertEqual(self.store.state['results'][targets[0]['id']]['history_key'],targets[0]['author_id']+':lite')

    def test_changed_statistics_recomputes_previously_completed_ratio(self):
        targets,_,calls,client=self.fixture(1,cached_baselines=True)
        verify(self.store,client);target=targets[0]
        for path in (self.store.root/'cache').glob('*.json'):
            saved=json.loads(path.read_text(encoding='utf-8'))
            if saved['operation']=='stats' and target['id'] in saved['data']['statistics']:
                saved['data']['statistics'][target['id']]['play_count']=2000
                path.write_text(json.dumps(saved))
        verify(self.store,client)
        self.assertEqual(self.store.state['results'][target['id']]['status'],'below_threshold')
        self.assertEqual(self.store.state['results'][target['id']]['ratio'],2)

    def test_wizard_searches_again_after_three_and_preserves_original_limits(self):
        from wizard import Session
        from report_fixture import selection_answer
        targets,_,calls,fixture_client=self.fixture(4)
        transport=fixture_client.transport
        def raw(v):
            return {'aweme_id':v['id'],'desc':v['title'],'create_time':v['published_at'],
                    'author':{'sec_uid':v['author_id'],'nickname':v['author_name'],'follower_count':v['followers']},'statistics':{'digg_count':v['likes']}}
        def replay(op,params):
            if op!='search':return transport(op,params)
            calls.append((op,dict(params)))
            found=targets[:3] if params['keyword']=='Skill' else targets[3:] if params['keyword']=='Skill安装' else []
            return {'code':200,'data':{'business_data':[{'data':{'aweme_info':raw(v)}} for v in found],
                                      'business_config':{'has_more':False}}}
        class Analyst:
            def ask(self,stage,packet,accept):
                if stage=='prepare-research':answer={'ready':True,'search_keywords':['Skill']}
                elif stage=='semantic-review':
                    answer={'reviews':{v['id']:{'decision':'include','reason':'主题相关','priority':3} for v in packet['candidates']},
                            'next_keywords':[['Skill安装'],['Skill使用'],[]][packet['round']],'search_reason':'同主题用途'}
                else:answer=selection_answer(packet)
                return accept(answer)
        s=Session(Path(self.temp.name)/'wizard',assistant_mode='codex')
        s.mode,s.key='live','SYNTHETIC-COVERAGE-KEY'
        def client(store,**kwargs):return TikHubClient(store,True,transport=replay,api_key='SYNTHETIC-COVERAGE-KEY',request_interval=0)
        with patch.object(s,'select_assistant',return_value=Analyst()),patch('wizard.fetch_prices',return_value=self.c['pricing']),patch('wizard.TikHubClient',side_effect=client):
            s.start({'description':'Skill','consent':True});s.worker.join(10)
        self.assertFalse(s.worker.is_alive())
        self.assertEqual(s.status,'report_ready',s.message)
        self.assertEqual(s.summary['qualified_count'],4)
        self.assertEqual(s.summary['featured_count'],2)  # The writing fixture selects two; all four remain available.
        self.assertEqual([p['keyword'] for op,p in calls if op=='search'],['Skill','Skill安装','Skill使用'])
        stored=Store(s.run_dir)
        self.assertEqual(stored.config['max_requests'],50)
        self.assertEqual(stored.config['budget_usd'],'0.50')
        from verification import audit_run
        completion=audit_run(stored.root)['completion']
        self.assertEqual(completion['retrieval']['search_rounds_proven'],3)
        self.assertTrue(completion['retrieval']['legal_stop'])
        self.assertFalse(completion['quantity']['met'])
        self.assertFalse(completion['overall_goal_met'])
        self.assertLessEqual(len(stored.reservations()),50)
        self.assertLessEqual(stored.reserved_cost(),0.50)

    def test_good_page_and_new_keyword_can_continue_after_three_qualified_results(self):
        vs=[self.video(i) for i in range(3)]
        self.store.state['candidates']={v['id']:v for v in vs}
        self.query('Skill',[v['id'] for v in vs])
        self.store.state['results']={v['id']:{'status':'qualified'} for v in vs}
        self.assertEqual(search_extensions(self.store,['Skill安装']),['Skill','Skill安装'])

    def test_big_accounts_duplicate_page_and_error_are_not_paged_again(self):
        big=self.video(1,followers=300000);self.store.state['candidates'][big['id']]=big
        self.query('Skill',[big['id']]);self.assertEqual(search_extensions(self.store,['Skill','Skill安装']),['Skill安装'])
        big['followers']=100;self.store.state['queries']['Skill']['last_new_ids']=[]
        self.assertEqual(search_extensions(self.store,['Skill','Skill安装']),['Skill安装'])
        self.store.state['queries']['Skill']['last_new_ids']=[big['id']];self.store.state['queries']['Skill']['error']='failed once'
        self.assertEqual(search_extensions(self.store,['Skill','Skill安装']),['Skill安装'])

    def test_supplement_preserves_both_request_and_usd_headroom(self):
        self.store.config['max_requests']=9
        self.assertEqual(search_extensions(self.store,['Skill安装']),[])
        self.store.config['max_requests']=10
        self.assertEqual(search_extensions(self.store,['Skill安装','Skill科研']),['Skill安装'])
        self.store.config['budget_usd']='0.018'
        self.assertEqual(search_extensions(self.store,['Skill安装']),[])

    def test_page_novelty_tracks_duplicates_across_keywords(self):
        v=self.video(1)
        class Client:
            def call(self,*args):return {'videos':[v],'has_more':True,'cursor':2}
        search(self.store,Client(),page_limit=1)
        self.assertEqual(self.store.state['queries']['Skill']['last_new_ids'],[v['id']])
        search(self.store,Client(),['Skill安装'],'同主题',page_limit=1)
        self.assertEqual(self.store.state['queries']['Skill安装']['last_new_ids'],[])
        self.assertEqual(self.store.state['queries']['Skill安装']['last_page_ids'],[v['id']])


if __name__=='__main__':unittest.main()
