import hashlib,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests'),str(ROOT/'webapp/tests')]
from core import Store,StopRun,atomic_json,digest,read_json
from demo import run_demo
from webapp.approval import approved
from webapp.expanded_analysis import continue_expansion
from webapp.cached_analysis import rebuild
from quality_fixture import approval

class ExtendedAuthorizationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.source,_=run_demo(self.root/'research');self.base=approval(self.root);self.base['expand_data']=True
  atomic_json(self.root/'approved-continuation.json',self.base)
  self.extension={'job_id':self.root.name,'max_requests':250,'prior_approval_sha256':digest(self.base),
   'data_limit_usd':'.50','model_limit_usd':'.15','total_limit_usd':'.65'}
  for label,path in (('data',self.source.root/'requests.jsonl'),('model',self.root/'model-events.jsonl'),('settlements',self.root/'model-events.jsonl.settlements.jsonl')):
   raw=path.read_bytes() if path.exists() else b''
   self.extension['prior_'+label+'_bytes']=len(raw);self.extension['prior_'+label+'_sha256']=hashlib.sha256(raw).hexdigest()
 def save_extension(self):atomic_json(self.root/'approved-extension-250.json',self.extension)
 def test_no_amendment_keeps_150_and_bound_amendment_allows_250(self):
  original=(self.root/'approved-continuation.json').read_bytes()
  self.assertEqual(approved(self.root)['max_requests'],150)
  self.save_extension();self.assertEqual(approved(self.root)['max_requests'],250)
  self.assertEqual((self.root/'approved-continuation.json').read_bytes(),original)
 def test_amendment_cannot_raise_money_copy_job_or_replace_approval(self):
  for key,value in (('job_id','another'),('max_requests',251),('data_limit_usd','1'),('model_limit_usd','1'),('total_limit_usd','1'),('prior_approval_sha256','0'*64)):
   with self.subTest(key=key):
    old=self.extension[key];self.extension[key]=value;self.save_extension()
    with self.assertRaises(StopRun):approved(self.root)
    self.extension[key]=old
 def test_prior_ledgers_cannot_be_changed_under_new_amendment(self):
  self.save_extension();path=self.source.root/'requests.jsonl';path.write_bytes(b'changed')
  with self.assertRaises(StopRun):approved(self.root)
 def prepared(self):
  self.save_extension();self.source.config.update(max_requests=250,budget_usd='0.50')
  replay=Mock();replay.config=self.source.config.copy()
  auth=approved(self.root);marker=self.root/'expanded-once.json';prior={'phase':'continuation-ready','approval_sha256':digest(auth)}
  return replay,auth,marker,prior
 def test_one_continuation_consumes_marker_and_retains_same_source_and_goal(self):
  replay,auth,marker,prior=self.prepared();session=Mock()
  before=(self.source.root/'requests.jsonl').read_bytes()
  with patch('webapp.expanded_analysis.expand') as expand:
   continue_expansion(session,self.source,replay,auth,marker,prior)
   expand.assert_called_once_with(session,self.source,replay,qualified_goal=25)
   self.assertEqual(read_json(marker)['phase'],'writing')
   with self.assertRaises(StopRun):continue_expansion(session,self.source,replay,auth,marker,prior)
  self.assertEqual(before,(self.source.root/'requests.jsonl').read_bytes())
 def test_interrupted_expansion_cannot_silently_repeat_or_skip_unknown_model(self):
  replay,auth,marker,prior=self.prepared();session=Mock()
  session.assistant.require_known_outcomes.side_effect=StopRun('unknown outcome')
  with patch('webapp.expanded_analysis.expand') as expand:
   with self.assertRaises(StopRun):continue_expansion(session,self.source,replay,auth,marker,prior)
   expand.assert_not_called();self.assertFalse(marker.exists())
  session.assistant.require_known_outcomes.side_effect=None
  with patch('webapp.expanded_analysis.expand',side_effect=StopRun('interrupted')) as expand:
   with self.assertRaises(StopRun):continue_expansion(session,self.source,replay,auth,marker,prior)
   self.assertEqual(read_json(marker)['phase'],'continuation-research')
   with self.assertRaises(StopRun):continue_expansion(session,self.source,replay,auth,marker,prior)
   self.assertEqual(expand.call_count,1)
 def test_reached_qualified_goal_never_calls_external_client(self):
  from workflow import verify
  source,_=run_demo(self.root/'goal',selection=True);client=Mock()
  verify(source,client,qualified_goal=1);client.call.assert_not_called()

if __name__=='__main__':unittest.main()
