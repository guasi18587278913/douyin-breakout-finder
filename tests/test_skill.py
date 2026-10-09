import copy
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from core import (Store, StopRun, APIError, BudgetStop, atomic_json, default_config,
                  score, select_baseline, history_sufficient, stamp, DAY)
from tikhub import TikHubClient, NoRedirect, normalize
from workflow import search, apply_review, verify, annotate, CachedClient, get_stats
from demo import run_demo
from report import render, csv_safe

class TestScoring(unittest.TestCase):
    def setUp(self):
        self.c=default_config('FDE',['FDE'],'1')
        self.asof=stamp('2026-10-08T12:00:00+00:00')
        self.t={'id':'1111111111111111111','author_id':'demo_author','followers':1000,
                'published_at':int(self.asof-30*DAY)}
        self.bs=[dict(self.t,id=str(2222222222222222200+i),published_at=int(self.asof-(29+i)*DAY)) for i in range(10)]
        self.stats={v['id']:{'play_count':100} for v in self.bs}
        self.stats[self.t['id']]={'play_count':1000}
    def test_threshold_inclusive_and_high_outlier_does_not_dominate(self):
        self.stats[self.bs[0]['id']]['play_count']=900000
        r=score(self.t,self.bs,self.stats,self.c,as_of=self.asof)
        self.assertEqual((r['status'],r['median_plays'],r['ratio']),('qualified',100,10))
    def test_missing_not_zero_or_silently_dropped(self):
        self.stats.pop(self.bs[0]['id'])
        r=score(self.t,self.bs,self.stats,self.c)
        self.assertEqual(r['status'],'pending');self.assertIsNone(r['ratio'])
    def test_zero_is_valid_but_zero_median_has_no_infinite_score(self):
        for b in self.bs:self.stats[b['id']]['play_count']=0
        r=score(self.t,self.bs,self.stats,self.c)
        self.assertEqual(r['median_plays'],0);self.assertEqual(r['status'],'pending')
        self.assertIsNone(r['ratio'])
    def test_fewer_samples_are_labelled(self):
        self.assertEqual(score(self.t,self.bs[:8],self.stats,self.c)['status'],'limited')
        self.assertEqual(score(self.t,self.bs[:3],self.stats,self.c)['status'],'pending')
    def test_account_and_membership_gates(self):
        self.assertEqual(score(self.t,self.bs,self.stats,self.c,listed=False)['status'],'pending')
        t=dict(self.t,followers=None)
        self.assertEqual(score(t,self.bs,self.stats,self.c)['status'],'pending')
        t=dict(self.t,followers=10001)
        self.assertEqual(score(t,self.bs,self.stats,self.c)['status'],'excluded')
    def test_time_selection_deduplicates_and_keeps_nearby_pinned(self):
        pinned=dict(self.bs[0],is_top=True)
        recent=dict(self.t,id='3333333333333333333',published_at=int(self.asof-DAY))
        far=dict(self.t,id='4444444444444444444',published_at=int(self.asof-200*DAY))
        foreign=dict(self.bs[1],id='5555555555555555555',author_id='different_author')
        chosen=select_baseline(self.t,[self.t,pinned,pinned,recent,far,foreign]+self.bs[1:],self.c,self.asof)
        self.assertEqual(len(chosen),10)
        self.assertTrue(next(v for v in chosen if v['id']==pinned['id'])['is_top'])
        self.assertNotIn(recent['id'],[x['id'] for x in chosen])
    def test_partial_history_does_not_claim_complete_neighbors(self):
        h={'videos':{self.t['id']:self.t},'complete':False,'frontier':self.t['published_at']+DAY}
        self.assertFalse(history_sufficient(self.t,self.bs,h,self.c))
        h['frontier']=self.t['published_at']-20*DAY
        self.assertTrue(history_sufficient(self.t,self.bs,h,self.c))

class TestParser(unittest.TestCase):
    def test_nested_business_error_in_success_envelope(self):
        with self.assertRaises(APIError):normalize('stats',{'code':200,'data':{'status_code':4}}, {'aweme_ids':'1234567890123456789'})
    def test_stats_preserves_zero_and_missing(self):
        p={'code':200,'data':{'statistics_list':[{'aweme_id':'1234567890123456789','play_count':0},
                                              {'aweme_id':'1234567890123456790'}]}}
        r=normalize('stats',p,{'aweme_ids':'1234567890123456789,1234567890123456790'})['statistics']
        self.assertEqual(r['1234567890123456789']['play_count'],0)
        self.assertIsNone(r['1234567890123456790']['play_count'])
    def test_search_views_never_used(self):
        a={'aweme_id':'1234567890123456789','desc':'FDE','create_time':1,
           'author':{'sec_uid':'MS4wLjABAAAAdemo123','nickname':'demo'},'statistics':{'play_count':900000}}
        p={'code':200,'data':{'business_data':[{'data':{'aweme_info':a}}],
                            'business_config':{'has_more':1,'next_page':{'cursor':8,'search_id':'sid'},'backtrace':'back'}}}
        r=normalize('search',p,{})
        self.assertNotIn('play_count',r['videos'][0]);self.assertEqual(r['cursor'],8)
    def test_posts_reject_different_author(self):
        with self.assertRaisesRegex(APIError,'different author'):
            normalize('posts',{'code':200,'data':{'sec_uid':'wrong','aweme_list':[],'has_more':0}}, {'sec_user_id':'expected'})
    def test_missing_or_invalid_pagination_is_not_complete_history(self):
        for value in [None,2,'unknown']:
            d={'aweme_list':[],'max_cursor':0}
            if value is not None:d['has_more']=value
            with self.assertRaisesRegex(APIError,'has_more'):
                normalize('posts',{'code':200,'data':d},{'sec_user_id':'expected'})
        with self.assertRaisesRegex(APIError,'has_more'):
            normalize('search',{'code':200,'data':{'business_data':[]}}, {})
    def test_no_cross_host_redirect_with_secret(self):
        with self.assertRaises(APIError):NoRedirect().redirect_request(None,None,302,'',{},'https://example.org/collect')

class TestBudgetAndCache(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.c=default_config('FDE',['FDE'],'0.003')
        self.c['pricing']={'checked_at':datetime.now(timezone.utc).isoformat(),
            'source_url':'https://tikhub.io/pricing','usd_per_request':{k:'0.001' for k in ['search','posts','stats','comments']}}
        self.s=Store.create(Path(self.tmp.name)/'run',self.c)
        self.env=patch.dict(os.environ,{'TIKHUB_API_KEY':'TEST_SECRET_NOT_A_REAL_KEY'});self.env.start();self.addCleanup(self.env.stop)
    def payload(self,op,p):
        return {'code':200,'data':{'statistics_list':[{'aweme_id':i,'play_count':200} for i in p['aweme_ids'].split(',')]}}
    def test_budget_stops_before_fourth_request(self):
        sent=[]
        def tr(op,p):sent.append(p);return self.payload(op,p)
        cl=TikHubClient(self.s,True,transport=tr)
        for i in range(3):cl.call('stats',{'aweme_ids':str(1234567890123456780+i)})
        with self.assertRaises(BudgetStop):cl.call('stats',{'aweme_ids':'1234567890123456789'})
        self.assertEqual(len(sent),3);self.assertEqual(str(self.s.reserved_cost()),'0.003')
    def test_cache_resume_does_not_pay_again(self):
        p={'aweme_ids':'1234567890123456789'};calls=[]
        cl=TikHubClient(self.s,True,transport=lambda op,p:(calls.append(1) or self.payload(op,p)))
        cl.call('stats',p)
        reopened=Store(self.s.root)
        cl2=TikHubClient(reopened,True,transport=lambda *a:self.fail('network on cached request'))
        cl2.call('stats',p)
        self.assertEqual(len(calls),1);self.assertEqual(len(reopened.reservations()),1)
    def test_failed_write_keeps_reservation_and_explicit_retry_is_bounded(self):
        p={'aweme_ids':'1234567890123456789'}
        cl=TikHubClient(self.s,True,transport=self.payload)
        with patch('tikhub.atomic_json',side_effect=OSError('disk full')):
            with self.assertRaises(StopRun):cl.call('stats',p)
        self.assertEqual(len(self.s.reservations()),1)
        # Storage failure is fatal to this client; explicit retry creates a fresh one.
        with self.assertRaises(StopRun):cl.call('stats',p)
        self.assertEqual(len(self.s.reservations()),1)
        retry=TikHubClient(self.s,True,True,transport=self.payload)
        retry.call('stats',p);self.assertEqual(len(self.s.reservations()),2)
    def test_auth_flag_and_environment_required(self):
        with self.assertRaises(StopRun):TikHubClient(self.s,False)
        with patch.dict(os.environ,{'TIKHUB_API_KEY':''}):
            with self.assertRaises(StopRun):TikHubClient(self.s,True)
    def test_key_redacted_from_response_projection(self):
        def tr(op,p):
            return {'code':200,'data':{'comments':[{'text':os.environ['TIKHUB_API_KEY'],'digg_count':0}]}}
        cl=TikHubClient(self.s,True,transport=tr);cl.call('comments',{'aweme_id':'1234567890123456789','cursor':0,'count':20})
        for p in self.s.root.rglob('*.json'):
            self.assertNotIn('TEST_SECRET_NOT_A_REAL_KEY',p.read_text(encoding='utf-8'))
        self.assertNotIn('TEST_SECRET_NOT_A_REAL_KEY',(self.s.root/'requests.jsonl').read_text(encoding='utf-8'))
    def test_config_cannot_reset_budget(self):
        c=copy.deepcopy(self.c);c['budget_usd']='100';atomic_json(self.s.root/'config.json',c)
        with self.assertRaises(StopRun):Store(self.s.root)
    def test_lock_refreshes_stale_budget_snapshot(self):
        stale=Store(self.s.root)
        self.s.reserve('stats','one')
        with stale.lock():self.assertEqual(len(stale.reservations()),1)
    def test_credential_in_config_rejected(self):
        c=copy.deepcopy(self.c);c['api_key']='not-allowed'
        with self.assertRaises(StopRun):Store.create(Path(self.tmp.name)/'bad',c)
    def test_damaged_journal_stops(self):
        (self.s.root/'requests.jsonl').write_text('{broken')
        with self.assertRaises(StopRun):Store(self.s.root)
    def test_partial_stats_never_regroup_into_implicit_paid_retry(self):
        a,b='1234567890123456788','1234567890123456789';sent=[]
        def tr(op,p):
            sent.append(p['aweme_ids'])
            return {'code':200,'data':{'statistics_list':[{'aweme_id':a,'play_count':100}]}}
        cl=TikHubClient(self.s,True,transport=tr)
        get_stats(self.s,cl,[a,b]);get_stats(self.s,cl,[a,b])
        reopened=Store(self.s.root)
        get_stats(reopened,TikHubClient(reopened,True,transport=tr),[a,b])
        self.assertEqual(sent,[a+','+b]);self.assertEqual(len(reopened.reservations()),1)
        self.assertIsNone(reopened.state['stats'][b]['play_count'])
        retry=TikHubClient(reopened,True,True,transport=lambda op,p:(sent.append(p['aweme_ids']) or
             {'code':200,'data':{'statistics_list':[{'aweme_id':b,'play_count':200}]}}))
        get_stats(reopened,retry,[a,b]);self.assertEqual(sent,[a+','+b,b])
        self.assertEqual(reopened.state['stats'][b]['play_count'],200)
    def test_empty_stats_explicit_retry_refreshes_same_request_once(self):
        ids=['1234567890123456788','1234567890123456789'];sent=[]
        def tr(op,p):
            sent.append(p['aweme_ids']);return {'code':200,'data':{'statistics_list':[]}}
        get_stats(self.s,TikHubClient(self.s,True,transport=tr),ids)
        retry=TikHubClient(self.s,True,True,transport=tr)
        get_stats(self.s,retry,ids);get_stats(self.s,retry,ids)
        self.assertEqual(len(sent),2);self.assertEqual(len(self.s.reservations()),2)
    def test_saved_stats_recover_without_recharging_after_state_loss(self):
        p={'aweme_ids':'1234567890123456789'}
        cl=TikHubClient(self.s,True,transport=self.payload);cl.call('stats',p)
        reopened=Store(self.s.root)
        get_stats(reopened,CachedClient(reopened),[p['aweme_ids']])
        self.assertEqual(reopened.state['stats'][p['aweme_ids']]['play_count'],200)
        self.assertEqual(len(reopened.reservations()),1)

class TestWorkflow(unittest.TestCase):
    def test_unprocessed_included_candidates_remain_in_report(self):
        with tempfile.TemporaryDirectory() as td:
            c=default_config('FDE',['FDE'],'0');c['max_candidates']=1
            s=Store.create(Path(td)/'run',c,as_of='2026-10-08T00:00:00+00:00')
            for i in range(3):
                vid=str(1234567890123456780+i)
                s.state['candidates'][vid]={'id':vid,'title':'FDE demo','author_name':'demo',
                    'author_id':'demo_author_'+str(i),'followers':100,'likes':100-i,
                    'published_at':int(stamp(s.state['as_of'])-20*DAY),
                    'review':{'decision':'include','reason':'FDE','priority':1}}
            verify(s,CachedClient(s))
            self.assertEqual(len(s.state['results']),3)
            self.assertEqual(render(s)['pending_count'],3)
    def test_budget_stop_keeps_the_remaining_queue(self):
        with tempfile.TemporaryDirectory() as td:
            c=default_config('FDE',['FDE'],'0');s=Store.create(Path(td)/'run',c,as_of='2026-10-08T00:00:00+00:00')
            for i in range(2):
                vid=str(1234567890123456780+i)
                s.state['candidates'][vid]={'id':vid,'title':'FDE demo','author_name':'demo',
                    'author_id':'demo_author_'+str(i),'followers':100,'likes':100-i,
                    'published_at':int(stamp(s.state['as_of'])-20*DAY),
                    'review':{'decision':'include','reason':'FDE','priority':1}}
            class Client:
                def call(self,*args,**kwargs):raise BudgetStop('budget')
            verify(s,Client())
            self.assertEqual(len(s.state['results']),2)
            self.assertTrue(all('预算' in r['reason'] for r in s.state['results'].values()))
    def test_offline_demo_covers_positive_negative_and_insufficient(self):
        with tempfile.TemporaryDirectory() as td:
            with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('network forbidden')):
                s,result=run_demo(Path(td)/'demo')
            self.assertEqual(result['candidate_count'],3);self.assertEqual(result['selected_count'],1)
            self.assertEqual({r['status'] for r in s.state['results'].values()}, {'qualified','below_threshold','pending'})
            self.assertEqual(str(s.reserved_cost()),'0')
            self.assertEqual(s.state['results']['9900000000000000100']['ratio'],25)
            self.assertNotIn('https://www.douyin.com/video/',(s.root/'report.html').read_text(encoding='utf-8'))
            with self.assertRaises(StopRun):TikHubClient(s,True)
    def test_report_escapes_text_and_csv_formulas(self):
        with tempfile.TemporaryDirectory() as td:
            s,_=run_demo(Path(td)/'demo');vid='9900000000000000100'
            annotate(s,{vid:{'basis':'title-description','evidence_note':'title only','summary':'<script>alert(1)</script>'}})
            render(s);h=(s.root/'report.html').read_text(encoding='utf-8')
            self.assertNotIn('<script>alert(1)</script>',h);self.assertIn('&lt;script&gt;',h)
            self.assertTrue(csv_safe(' =1+1').startswith("'"))
    def test_semantic_review_required_and_validated(self):
        with tempfile.TemporaryDirectory() as td:
            c=default_config('FDE',['FDE'],'0');s=Store.create(Path(td)/'run',c)
            with self.assertRaises(StopRun):verify(s,CachedClient(s))
            s.state['candidates']['1']={'id':'1','review':{'decision':'pending','reason':''}}
            with self.assertRaises(StopRun):apply_review(s,{'1':{'decision':'include','reason':''}})
            with self.assertRaises(StopRun):apply_review(s,{'999':{'decision':'exclude','reason':'wrong'}})
    def test_repeated_search_cursor_stops_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as td:
            c=default_config('FDE',['FDE'],'0');c['max_search_pages']=3
            s=Store.create(Path(td)/'run',c,as_of='2026-10-08T00:00:00+00:00')
            class Client:
                n=0
                def call(self,op,params):
                    self.n+=1
                    return {'videos':[{'id':'1234567890123456789','title':'FDE','likes':1,
                        'published_at':int(stamp(s.state['as_of'])-20*DAY),'followers':100,'author_id':'abc'}],
                        'cursor':0,'has_more':True}
            cl=Client();search(s,cl)
            self.assertEqual(cl.n,1);self.assertEqual(len(s.state['candidates']),1)
            self.assertIn('warning',s.state['queries']['FDE'])
    def test_review_exclusion_removes_previously_selected_case(self):
        with tempfile.TemporaryDirectory() as td:
            s,_=run_demo(Path(td)/'demo')
            apply_review(s,{'9900000000000000100':{'decision':'exclude','reason':'复核不相关'}})
            self.assertEqual(render(s)['selected_count'],0)

if __name__=='__main__':unittest.main()
