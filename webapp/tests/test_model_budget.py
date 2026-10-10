import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from decimal import Decimal
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'webapp/tests')]
from quality_fixture import approval
from webapp.model_budget import effective_spent,proposal,fingerprint,append,SOURCE
from webapp.remote_analyst import RemoteAnalyst,AssistanceNeeded

class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.path=self.root/'model-events.jsonl'
        self.reserve={'kind':'reserve','id':1,'model':'deepseek-flash','price_source':SOURCE,'price_checked_at':'2026-10-10',
                      'at':1791610000,'usd':'.020','input_bound':20000,'max_tokens':10000}
        approval(self.root)
        self.finish={'kind':'finish','id':1,'at':1791610001,'outcome':'failed','provider_response_received':True,'usage':{'prompt_tokens':1000,'completion_tokens':500}}
    def test_failed_complete_usage_settles_and_original_bytes_unchanged(self):
        for row in (self.reserve,self.finish):append(self.path,row)
        before=self.path.read_bytes();rows=[self.reserve,self.finish]
        self.assertEqual(effective_spent(self.path,rows,True,True),Decimal('.0009'))
        once=Path(str(self.path)+'.settlements.jsonl').read_bytes()
        self.assertEqual(effective_spent(self.path,rows,True,True),Decimal('.0009'))
        self.assertEqual(once,Path(str(self.path)+'.settlements.jsonl').read_bytes());self.assertEqual(before,self.path.read_bytes())
        self.assertEqual(effective_spent(self.path,rows,False),Decimal('.02'))
    def test_ambiguous_missing_invalid_or_excess_usage_retains_hold(self):
        for value in (None,True,-1,0,1.5,100000):
            rows=copy.deepcopy([self.reserve,self.finish]);rows[1]['usage']['prompt_tokens']=value
            self.assertEqual(effective_spent(self.path,rows,True),Decimal('.02'))
        unknown=copy.deepcopy([self.reserve,self.finish]);unknown[1].pop('provider_response_received')
        self.assertEqual(proposal(unknown),{})
        for rows in ([self.reserve],[self.reserve,self.finish,self.finish],[self.reserve,self.reserve,self.finish]):
            self.assertEqual(proposal(rows),{})
    def test_legacy_requires_exact_approved_pair_hash(self):
        del self.reserve['input_bound'];del self.reserve['max_tokens'];rows=[self.reserve,self.finish]
        self.assertEqual(effective_spent(self.path,rows,True),Decimal('.02'))
        approval(self.root,[fingerprint(rows)])
        self.assertEqual(effective_spent(self.path,rows,True),Decimal('.0009'))
        self.finish['usage']['completion_tokens']=400
        self.assertEqual(effective_spent(self.path,rows,True),Decimal('.02'))
    def test_conflict_after_settlement_blocks_and_duplicate_cannot_release_twice(self):
        rows=[self.reserve,self.finish];effective_spent(self.path,rows,True,True)
        with self.assertRaises(AssistanceNeeded):effective_spent(self.path,rows+[self.finish],True)
        path=Path(str(self.path)+'.settlements.jsonl');path.write_bytes(path.read_bytes()*2)
        with self.assertRaises(AssistanceNeeded):effective_spent(self.path,rows,True)
    def test_settlement_write_failure_sends_nothing(self):
        for row in (self.reserve,self.finish):append(self.path,row)
        import httpx
        calls=[]
        model=RemoteAnalyst(threading.Event(),'SYNTHETIC',self.path,httpx.MockTransport(lambda r:calls.append(r)),settle_usage=True)
        with patch('webapp.model_budget.append',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):model.generate('prepare-research',{'topic':'x','candidates':[]},'')
        self.assertEqual(calls,[])
    def test_approval_cannot_be_copied_to_another_job_or_change_limits_or_prefixes(self):
        from webapp.approval import approved
        for key,value in (('job_id','wrong-job'),('model_limit_usd','999'),('max_requests',151)):
            document=approval(self.root);document[key]=value
            (self.root/'approved-continuation.json').write_text(json.dumps(document))
            with self.subTest(key=key):
                with self.assertRaises(AssistanceNeeded):effective_spent(self.path,[self.reserve,self.finish],True)
        append(self.path,self.reserve);approval(self.root)
        self.path.write_text('tampered')
        with self.assertRaises(AssistanceNeeded):approved(self.root)

    def test_two_processes_cannot_spend_the_same_remaining_hold(self):
        append(self.path,{'kind':'reserve','usd':'.148','id':1})
        append(self.path,{'kind':'finish','id':1,'outcome':'received'})
        script='''import sys,json,threading,time\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1]+'/scripts')\nimport httpx\nfrom webapp.remote_analyst import RemoteAnalyst,AssistanceNeeded\ndef send(r):\n with open(sys.argv[2]+'.sent','a') as f:f.write('sent\\n')\n time.sleep(.15)\n return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"ready":true,"search_keywords":["x"]}'}}]})\nm=RemoteAnalyst(threading.Event(),'SYNTHETIC',Path(sys.argv[2]),httpx.MockTransport(send))\ntry:m.generate('prepare-research',{'topic':'x','candidates':[]},'')\nexcept AssistanceNeeded:pass\n'''
        children=[subprocess.Popen([sys.executable,'-B','-c',script,str(ROOT),str(self.path)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        for child in children:
            out,err=child.communicate(timeout=15);self.assertEqual(child.returncode,0,err.decode())
        self.assertEqual(Path(str(self.path)+'.sent').read_text().count('sent'),1)
        rows=[json.loads(l) for l in self.path.read_text().splitlines()];ids=[r['id'] for r in rows if r['kind']=='reserve'];self.assertEqual(ids,[1,2])

if __name__=='__main__':unittest.main()
