import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from webapp.settings import Settings
from webapp.app import create_app
from webapp.db import Conflict
from webapp.engine import run_dir
from webapp.remote_analyst import RemoteAnalyst, AssistanceNeeded

ORIGIN='https://testserver'
PASSWORD='synthetic-test-password-only'

class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=Settings(Path(self.temp.name),ORIGIN,Fernet.generate_key().decode(),worker_enabled=False)
        self.app=create_app(self.settings);self.db=self.app.state.db
        self.client=TestClient(self.app,base_url=ORIGIN);self.addCleanup(self.client.close)
    def client_for(self,name,role='member'):
        client=TestClient(self.app,base_url=ORIGIN);self.addCleanup(client.close)
        data=client.get('/api/bootstrap').json();client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':data['csrf']})
        invitation=self.db.invite('synthetic',role)
        response=client.post('/api/register',json={'invitation':invitation,'username':name,'password':PASSWORD})
        self.assertEqual(response.status_code,200,response.text)
        client.headers['X-CSRF-Token']=response.json()['csrf'];return client,response.json()['user']
    def test_secure_session_cookie_logout_and_invite_single_use(self):
        token=self.db.invite('one-time')
        bootstrap=self.client.get('/api/bootstrap').json();self.client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':bootstrap['csrf']})
        body={'invitation':token,'username':'alice','password':PASSWORD}
        r=self.client.post('/api/register',json=body);self.assertEqual(r.status_code,200)
        cookie=r.headers['set-cookie'];self.assertIn('__Host-breakout_session',cookie);self.assertIn('HttpOnly',cookie);self.assertIn('Secure',cookie);self.assertIn('SameSite=strict',cookie)
        self.client.headers['X-CSRF-Token']=r.json()['csrf']
        body['username']='another';self.assertEqual(self.client.post('/api/register',json=body).status_code,400)
        self.assertEqual(self.client.post('/api/logout',json={}).status_code,200)
        self.assertEqual(self.client.get('/api/jobs').status_code,401)
    def test_csrf_origin_and_anonymous_registration(self):
        data=self.client.get('/api/bootstrap').json()
        body={'username':'unknown','password':PASSWORD}
        self.assertEqual(self.client.post('/api/login',json=body).status_code,403)
        self.client.headers['Origin']=ORIGIN
        self.assertEqual(self.client.post('/api/login',json=body).status_code,403)
        self.client.headers['X-CSRF-Token']=data['csrf'];self.client.headers['Origin']='https://evil.invalid'
        self.assertEqual(self.client.post('/api/login',json=body).status_code,403)
    def test_all_job_files_and_mutations_are_owner_filtered(self):
        alice,a=self.client_for('alice');bob,b=self.client_for('bob')
        job=alice.post('/api/jobs',json={'topic':'FDE','mode':'demo'}).json();id=job['id']
        folder=run_dir(self.settings,id);folder.mkdir(parents=True);(folder/'report.html').write_text('<body>PRIVATE REPORT</body>')
        self.assertEqual(bob.get('/api/jobs').json()['jobs'],[])
        for path in ['/api/jobs/'+id,'/reports/'+id+'/report.html','/reports/'+id+'/summary.json']:
            self.assertEqual(bob.get(path).status_code,404)
        for action in ['cancel','resume']:
            self.assertEqual(bob.post('/api/jobs/'+id+'/'+action,json={}).status_code,404)
        self.assertIn('PRIVATE REPORT',alice.get('/reports/'+id+'/report.html').text)
        self.assertEqual(alice.get('/reports/'+id+'/config.json').status_code,404)
        self.assertEqual(bob.get('/api/admin').status_code,403)
    def test_report_csp_and_new_research_link(self):
        client,user=self.client_for('reader');job=self.db.enqueue(user['id'],'FDE','all','demo')
        folder=run_dir(self.settings,job['id']);folder.mkdir(parents=True);(folder/'report.html').write_text('<html><body class="report-editorial"><main>report</main></body></html>')
        response=client.get('/reports/'+job['id']+'/report.html')
        self.assertEqual(response.status_code,200);self.assertIn('href="/?new=1"',response.text)
        self.assertIn("script-src 'none'",response.headers['content-security-policy'])
        self.assertEqual(response.headers['cache-control'],'no-store')
    def test_credentials_encrypted_never_returned_even_validation_error(self):
        client,user=self.client_for('owner','admin');key='SYNTHETIC-NOT-A-REAL-API-KEY-12345'
        r=client.post('/api/admin/providers',json={'tikhub_key':key,'deepseek_key':key})
        self.assertEqual(r.status_code,200);self.assertNotIn(key,r.text)
        self.assertNotIn(key,client.get('/api/admin').text);self.assertNotIn(key,client.get('/api/bootstrap').text)
        self.assertNotIn(key.encode(),self.app.state.vault.path.read_bytes())
        r=client.post('/api/admin/providers',json={'tikhub_key':key*30});self.assertEqual(r.status_code,422);self.assertNotIn(key,r.text)
        self.assertEqual(self.app.state.vault.get()['tikhub_key'],key)
    def test_request_size_and_login_limit(self):
        data=self.client.get('/api/bootstrap').json();self.client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':data['csrf']})
        self.assertEqual(self.client.post('/api/login',json={'password':'x'*20000}).status_code,413)
        for _ in range(10):self.assertEqual(self.client.post('/api/login',json={'username':'no-one','password':PASSWORD}).status_code,401)
        self.assertEqual(self.client.post('/api/login',json={'username':'no-one','password':PASSWORD}).status_code,429)
    def test_concurrent_user_and_global_limits_include_queue(self):
        client,user=self.client_for('alice')
        def enqueue(_):
            try:return self.db.enqueue(user['id'],'topic','all')['id']
            except Conflict:return None
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(enqueue,range(8)))
        self.assertEqual(len([v for v in results if v]),1)
        _,bob=self.client_for('bob');self.db.enqueue(bob['id'],'second','all')
        _,charlie=self.client_for('charlie')
        with self.assertRaises(Conflict):self.db.enqueue(charlie['id'],'third','all')
    def test_cancel_queue_and_restart_never_restarts_paid_search(self):
        client,user=self.client_for('alice');job=self.db.enqueue(user['id'],'topic','all')
        self.db.cancel(job['id'],user['id']);self.assertIsNone(self.db.claim())
        demo=self.db.enqueue(user['id'],'FDE','all','demo');self.db.claim();self.db.recover_interrupted()
        self.assertEqual(self.db.job(demo['id'])['status'],'interrupted');self.assertIsNone(self.db.claim())
        folder=run_dir(self.settings,job['id']);folder.mkdir(parents=True);(folder/'state.json').write_text('{}')
        r=client.post('/api/jobs/'+job['id']+'/resume',json={});self.assertEqual(r.status_code,202)
        self.assertEqual(self.db.job(job['id'])['resume_only'],1)
    def test_worker_has_single_process_lease(self):
        from webapp.engine import Worker
        first=self.app.state.worker;first.start();self.addCleanup(first.close)
        other=Worker(self.settings,self.db,self.app.state.vault)
        with self.assertRaises(RuntimeError):other.start()
    def test_full_demo_uses_real_audit_and_private_report(self):
        client,user=self.client_for('alice');job=self.db.enqueue(user['id'],'FDE（合成示例）','all','demo')
        self.db.claim();self.app.state.worker.execute(job)
        completed=client.get('/api/jobs/'+job['id']).json()
        self.assertEqual(completed['status'],'done',completed)
        folder=run_dir(self.settings,job['id']);audit=json.loads((folder/'verification.json').read_text())
        self.assertEqual(audit['status'],'PASS');self.assertEqual(completed['progress']['qualified'],1)
        html=client.get(completed['report_url']);self.assertEqual(html.status_code,200);self.assertIn('新研究',html.text)
    def test_unconfigured_real_research_does_not_enqueue(self):
        client,user=self.client_for('alice');r=client.post('/api/jobs',json={'topic':'AI自媒体'})
        self.assertEqual(r.status_code,409);self.assertEqual(self.db.jobs(user['id']),[])

class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.calls=0
        self.key='SYNTHETIC-HTTP-MODEL-KEY-ONLY'
        self.path=Path(self.temp.name)/'model-events.jsonl';self.cancel=threading.Event()
        self.packet={'topic':'FDE','scope':'test','initial_keywords':['FDE'],'resume':False,'budget_usd':'0.50','max_requests':50}
    def model(self,handler):return RemoteAnalyst(self.cancel,self.key,self.path,httpx.MockTransport(handler))
    def good(self,request):
        self.calls+=1;body=json.loads(request.content);self.assertEqual(body['thinking'],{'type':'disabled'})
        self.assertEqual(request.url.host,'api.deepseek.com');self.assertNotIn(self.key,request.content.decode())
        self.assertTrue(self.path.exists())
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"ready":true,"search_keywords":["FDE"]}'}}],'usage':{'prompt_tokens':40,'completion_tokens':30}})
    def test_request_reserved_before_send_and_resume_retains_ledger(self):
        first=self.model(self.good);self.assertTrue(first.generate('prepare-research',self.packet,'')['ready'])
        second=self.model(self.good);second.generate('prepare-research',self.packet,'')
        rows=second.entries();self.assertEqual(sum(r['kind']=='reserve' for r in rows),2);self.assertEqual(self.calls,2)
        self.assertNotIn(self.key,self.path.read_text())
    def test_failed_request_no_retry_and_ledger_kept(self):
        def fail(request):self.calls+=1;return httpx.Response(429,json={'secret':self.key})
        m=self.model(fail)
        with self.assertRaises(AssistanceNeeded) as error:m.ask('prepare-research',self.packet,lambda a:a)
        self.assertEqual(self.calls,1);self.assertNotIn(self.key,str(error.exception));self.assertEqual(len(m.entries()),2)
    def test_ceiling_and_corrupt_ledger_prevent_send(self):
        m=self.model(self.good);m.append({'kind':'reserve','usd':'0.15'})
        with self.assertRaises(AssistanceNeeded):m.generate('prepare-research',self.packet,'')
        self.assertEqual(self.calls,0);self.path.write_text('{broken')
        with self.assertRaises(AssistanceNeeded):m.generate('prepare-research',self.packet,'')
        self.assertEqual(self.calls,0)
    def test_cancellation_before_reservation(self):
        self.cancel.set();m=self.model(self.good)
        with self.assertRaises(AssistanceNeeded):m.generate('prepare-research',self.packet,'')
        self.assertFalse(self.path.exists());self.assertEqual(self.calls,0)

if __name__=='__main__':unittest.main()
