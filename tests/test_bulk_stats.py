import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from core import Store, default_config, now, stamp, DAY, APIError
from tikhub import TikHubClient
from workflow import get_stats, verify, verification_fingerprint

class BulkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        c=default_config('synthetic',['synthetic'],'0.50');c['max_requests']=50;c['stats_strategy']='adaptive_fast'
        c['pricing']={'source_url':'https://tikhub.io/pricing','checked_at':now(),'usd_per_request':dict(search='0.001',posts='0.001',stats='0.001',stats_bulk='0.025',comments='0.001')}
        self.store=Store.create(Path(self.temp.name)/'run',c);self.calls=[];self.ids=[];self.targets=[]
        ts=int(stamp(self.store.state['as_of']))
        for a in range(3):
            aid='author_synthetic_'+str(a);videos={}
            for n in range(11):
                vid=str(1000000000000000000+a*100+n);self.ids.append(vid)
                row={'id':vid,'author_id':aid,'author_name':'合成作者','author_uid':'1','followers':500,'published_at':ts-(14+n)*DAY,'is_top':False,'title':'合成作品','likes':1}
                videos[vid]=row
                if n==0:self.targets.append(vid);self.store.state['candidates'][vid]={**row,'review':{'decision':'include','priority':1,'reason':'synthetic','method':'human-or-agent'}}
            self.store.state['authors'][aid+':normal']={'author_id':aid,'channel':'normal','pages':1,'complete':True,'cursor':0,'seen_cursors':[],'frontier':min(v['published_at'] for v in videos.values()),'videos':videos,'warning':None}
        self.store.save()
    def client(self,fail=False,omit=None,baseline=1000,retry=False):
        def send(op,params):
            self.calls.append((op,params['aweme_ids']))
            if fail:raise APIError('Synthetic failure')
            return {'code':200,'data':{'statistics_list':[{'aweme_id':vid,'play_count':30000 if vid in self.targets else baseline} for vid in params['aweme_ids'].split(',') if vid!=omit]}}
        return TikHubClient(self.store,True,retry_failed=retry,transport=send,api_key='SYNTHETIC-TEST-NOT-A-KEY',request_interval=0)
    def test_three_prepared_authors_use_one_33_id_request(self):
        verify(self.store,self.client());self.assertEqual(len(self.calls),1);self.assertEqual(self.calls[0][0],'stats_bulk');self.assertEqual(len(self.calls[0][1].split(',')),33)
        self.assertEqual([r['status'] for r in self.store.state['results'].values()],['qualified']*3)
        verify(self.store,self.client());self.assertEqual(len(self.calls),1)
    def test_bulk_failure_never_falls_back_even_with_retry_flag(self):
        verify(self.store,self.client(fail=True,retry=True));self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][0],'stats_bulk');self.assertTrue(all(r['status']=='pending' for r in self.store.state['results'].values()))
        verify(self.store,self.client(retry=True));self.assertEqual(len(self.calls),2);self.assertEqual(self.calls[0],self.calls[1])
    def test_missing_row_explicit_retry_refreshes_all_related_results(self):
        verify(self.store,self.client(omit=self.targets[0]));self.assertEqual([self.store.state['results'][i]['status'] for i in self.targets],['pending','qualified','qualified'])
        verify(self.store,self.client(baseline=4000,retry=True));self.assertEqual(self.calls[0],self.calls[1])
        for vid in self.targets:
            result=self.store.state['results'][vid];self.assertEqual(result['status'],'below_threshold');self.assertEqual(result['ratio'],7.5)
            self.assertEqual(result['verification_fingerprint'],verification_fingerprint(self.store,self.store.state['candidates'][vid],result))
    def test_small_batch_and_expensive_quote_keep_small_endpoint(self):
        get_stats(self.store,self.client(),self.ids[:2]);self.assertEqual([v[0] for v in self.calls],['stats'])
        self.store.config['pricing']['usd_per_request']['stats_bulk']='0.040'
        get_stats(self.store,self.client(),self.ids);self.assertTrue(all(v[0]=='stats' for v in self.calls))
    def test_legacy_default_never_uses_bulk(self):
        self.store.config.pop('stats_strategy');get_stats(self.store,self.client(),self.ids)
        self.assertEqual(len(self.calls),17);self.assertTrue(all(v[0]=='stats' for v in self.calls))
if __name__=='__main__':unittest.main()
