import contextlib
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from webapp.settings import Settings
from webapp.app import create_app
from webapp.db import Conflict, FM_ONLY_PASSWORD, password_matches
from webapp.engine import run_dir
from webapp.remote_analyst import RemoteAnalyst, AssistanceNeeded
from fake_fm import FakeFm

ORIGIN='https://testserver'
PASSWORD='synthetic-test-password-only'
FM_SECRET='synthetic-fm-client-secret-only'
CALLBACK=ORIGIN+'/auth/callback'
STATE_COOKIE='__Host-breakout_auth'

def subject_for(name):return hashlib.sha256(name.encode()).hexdigest()

class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=Settings(Path(self.temp.name),ORIGIN,Fernet.generate_key().decode(),worker_enabled=False,fm_client_secret=FM_SECRET)
        self.fm=FakeFm(FM_SECRET)
        self.app=create_app(self.settings,self.fm.transport());self.db=self.app.state.db;self.credits=self.app.state.credits
        self.client=TestClient(self.app,base_url=ORIGIN);self.addCleanup(self.client.close)
    def start_login(self,client):
        """点“用手机号登录 / 注册”：核对跳转地址，返回带去封面搭子的 state。"""
        response=client.get('/auth/start',follow_redirects=False)
        self.assertEqual(response.status_code,302)
        target=urlsplit(response.headers['location']);query=parse_qs(target.query)
        self.assertEqual(target.scheme+'://'+target.netloc+target.path,'https://fm.zmtgzt.com/connect/authorize')
        self.assertEqual(query['client_id'],['breakout']);self.assertEqual(query['redirect_uri'],[CALLBACK])
        self.assertRegex(query['state'][0],r'^[A-Za-z0-9_-]{16,200}$')
        return query['state'][0]
    def callback(self,client,code,state,cookie=None):
        headers={'Cookie':STATE_COOKIE+'='+cookie} if cookie is not None else {}
        return client.get('/auth/callback',params={'code':code,'state':state},headers=headers,follow_redirects=False)
    def fm_login(self,client,name):
        """模拟一次完整的封面搭子登录：本站跳转 → 用户在封面搭子确认 → 带授权码回到本站。"""
        state=self.start_login(client);code=self.fm.issue_code(subject_for(name),CALLBACK)
        response=self.callback(client,code,state)
        self.assertEqual(response.status_code,302,response.text);self.assertEqual(response.headers['location'],'/')
        data=client.get('/api/bootstrap').json()
        client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':data['csrf']})
        return data
    def client_for(self,name,role='member'):
        client=TestClient(self.app,base_url=ORIGIN);self.addCleanup(client.close)
        user=self.fm_login(client,name)['user']
        if role=='admin':
            # 登录永远只建普通成员；管理员只能在服务器上指定，测试里直接改库。
            with self.db.connection(True) as c:c.execute("UPDATE users SET role='admin' WHERE id=?",(user['id'],))
            user=client.get('/api/bootstrap').json()['user']
        return client,user
    def live_ready(self):return patch.object(self.app.state.vault,'status',return_value={'tikhub_key':True,'deepseek_key':True})
    def charges(self):
        with self.db.connection() as c:return [dict(r) for r in c.execute('SELECT * FROM credit_charges ORDER BY created_at')]
    def test_phone_login_sets_secure_cookies_and_logout_stays_local(self):
        response=self.client.get('/auth/start',follow_redirects=False)
        state_cookie=response.headers['set-cookie']
        for part in (STATE_COOKIE+'=','HttpOnly','Secure','SameSite=lax','Max-Age=600','Path=/'):self.assertIn(part,state_cookie)
        state=parse_qs(urlsplit(response.headers['location']).query)['state'][0]
        r=self.callback(self.client,self.fm.issue_code(subject_for('alice'),CALLBACK),state)
        self.assertEqual(r.status_code,302)
        cookies=r.headers.get_list('set-cookie')
        session=[c for c in cookies if c.startswith('__Host-breakout_session=')][0]
        for part in ('HttpOnly','Secure','SameSite=strict','Path=/'):self.assertIn(part,session)
        self.assertTrue(any(c.startswith(STATE_COOKIE+'=') and 'Max-Age=0' in c for c in cookies))
        data=self.client.get('/api/bootstrap').json();self.assertEqual(data['user']['role'],'member')
        self.client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':data['csrf']})
        self.assertEqual(self.client.post('/api/logout',json={}).status_code,200)
        self.assertEqual(self.client.get('/api/jobs').status_code,401)
        self.assertEqual({op for op,_ in self.fm.calls},{'token','balance'})
    def test_dev_mode_state_cookie_is_not_secure(self):
        settings=Settings(Path(self.temp.name)/'dev','http://testserver',Fernet.generate_key().decode(),dev=True,worker_enabled=False)
        self.assertTrue(settings.fm_client_secret)
        with TestClient(create_app(settings,self.fm.transport()),base_url='http://testserver') as client:
            header=client.get('/auth/start',follow_redirects=False).headers['set-cookie']
        self.assertTrue(header.startswith('breakout_auth='));self.assertNotIn('Secure',header);self.assertIn('SameSite=lax',header)
    def test_callback_rejects_missing_wrong_expired_or_replayed_state(self):
        code=lambda:self.fm.issue_code(subject_for('alice'),CALLBACK)
        # 没有从本站发起（缺 cookie）
        r=self.callback(self.client,code(),'A'*43);self.assertEqual(r.status_code,400);self.assertIn('登录没有完成',r.text)
        # 缺 state、state 对不上
        state=self.start_login(self.client)
        self.assertEqual(self.client.get('/auth/callback',params={'code':code()},follow_redirects=False).status_code,400)
        state=self.start_login(self.client)
        self.assertEqual(self.callback(self.client,code(),state[:-1]+('B' if state[-1]=='A' else 'A')).status_code,400)
        # 超过 10 分钟
        state=self.start_login(self.client);later=time.time()+601
        with patch('webapp.app.time',types.SimpleNamespace(time=lambda:later)):
            r=self.callback(self.client,code(),state)
        self.assertEqual(r.status_code,400);self.assertIn('过期',r.text)
        # 重放：同一个 state 和 cookie 成功用过一次后再来，即使授权码有效也拒绝
        state=self.start_login(self.client);saved=self.client.cookies.get(STATE_COOKIE)
        self.assertEqual(self.callback(self.client,code(),state).status_code,302)
        fresh=TestClient(self.app,base_url=ORIGIN);self.addCleanup(fresh.close)
        r=self.callback(fresh,code(),state,cookie=saved)
        self.assertEqual(r.status_code,400);self.assertIn('已经用过',r.text)
        # 伪造 cookie：格式、时间都对，签名不对
        forged='F'*43
        r=self.callback(fresh,code(),forged,cookie=forged+'.'+str(int(time.time()))+'.'+'0'*64)
        self.assertEqual(r.status_code,400);self.assertIn('过期或不是从本站发起',r.text)
        self.assertEqual(self.fm.count('token'),1)
    def test_callback_handles_fm_rejection_and_outage_without_details(self):
        state=self.start_login(self.client)
        r=self.callback(self.client,'no-such-code-0000',state)
        self.assertEqual(r.status_code,400);self.assertIn('授权已过期或已经用过',r.text);self.assertNotIn('invalid_grant',r.text)
        for failure in (self.fm.unreachable,self.fm.lost_reply):
            failure.add('token');state=self.start_login(self.client)
            r=self.callback(self.client,self.fm.issue_code(subject_for('alice'),CALLBACK),state)
            self.assertEqual(r.status_code,503);self.assertIn('暂时连不上封面搭子',r.text);self.assertNotIn(FM_SECRET,r.text)
            failure.clear()
        # 接入密钥不对时封面搭子回 401：同样只给人话提示
        self.fm.secret='another-secret-on-fm-side';state=self.start_login(self.client)
        with self.assertLogs('webapp.credits','WARNING') as logs:
            r=self.callback(self.client,self.fm.issue_code(subject_for('alice'),CALLBACK),state)
        self.assertEqual(r.status_code,503);self.assertNotIn('invalid_client',r.text)
        # 日志里能看出是密钥不对，但不带密钥和 subject
        output='\n'.join(logs.output);self.assertIn('token',output);self.assertIn('401 invalid_client',output)
        self.assertNotIn(FM_SECRET,output);self.assertNotIn(subject_for('alice'),output)
        self.assertIsNone(self.client.get('/api/bootstrap').json()['user'])
        with self.db.connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM users').fetchone()[0],0)
    def test_new_subject_creates_member_and_same_subject_reuses_user(self):
        alice,a=self.client_for('alice')
        self.assertEqual(a['role'],'member');self.assertEqual(a['username'],'fm:'+a['id'][:16])
        self.assertNotIn(subject_for('alice')[:8],a['username'])
        job=alice.post('/api/jobs',json={'topic':'FDE','mode':'demo'}).json()
        again,a2=self.client_for('alice')
        self.assertEqual(a2['id'],a['id']);self.assertEqual([j['id'] for j in again.get('/api/jobs').json()['jobs']],[job['id']])
        bob,b=self.client_for('bob');self.assertNotEqual(b['id'],a['id']);self.assertEqual(bob.get('/api/jobs').json()['jobs'],[])
        with self.db.connection() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM users').fetchone()[0],2)
            self.assertEqual(sorted(r[0] for r in c.execute('SELECT subject FROM external_accounts')),sorted([subject_for('alice'),subject_for('bob')]))
            self.assertEqual({r[0] for r in c.execute('SELECT password_hash FROM users')},{FM_ONLY_PASSWORD})
        self.assertFalse(password_matches(PASSWORD,FM_ONLY_PASSWORD))
    def test_phone_login_is_always_member_without_admin_permissions(self):
        client,user=self.client_for('first-phone-user')
        self.assertEqual(user['role'],'member')
        self.assertEqual(client.get('/api/jobs').status_code,200)
        self.assertEqual(client.get('/api/admin').status_code,403)
        self.assertEqual(client.post('/api/admin/invites',json={'label':'no-access'}).status_code,403)
        self.assertEqual(client.post('/api/admin/providers',json={'tikhub_key':'not-a-real-key'}).status_code,403)
        admin,owner=self.client_for('setup-owner','admin')
        self.assertEqual(owner['role'],'admin');self.assertEqual(admin.get('/api/admin').status_code,200)
        r=admin.post('/api/admin/invites',json={'label':'旧入口'})
        self.assertEqual(r.status_code,410);self.assertIn('grant-admin',r.json()['detail'])
        with self.db.connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM invites').fetchone()[0],0)
    def test_legacy_password_accounts_are_disabled_but_kept(self):
        legacy=self.db.register_public('legacy-owner',PASSWORD);token,_=self.db.create_session(legacy['id'])
        job=self.db.enqueue(legacy['id'],'旧研究','all','demo')
        folder=run_dir(self.settings,job['id']);folder.mkdir(parents=True);(folder/'report.html').write_text('<body>LEGACY REPORT</body>')
        cookie={'Cookie':'__Host-breakout_session='+token}
        self.assertIsNone(self.client.get('/api/bootstrap',headers=cookie).json()['user'])
        self.assertEqual(self.client.get('/api/jobs',headers=cookie).status_code,401)
        self.assertEqual(self.client.get('/reports/'+job['id']+'/report.html',headers=cookie).status_code,401)
        data=self.client.get('/api/bootstrap').json();self.client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':data['csrf']})
        for path in ('/api/register','/api/login'):
            r=self.client.post(path,json={'username':'legacy-owner','password':PASSWORD})
            self.assertEqual(r.status_code,410);self.assertIn('已改为手机号登录',r.json()['detail'])
        self.assertIsNotNone(self.db.user(legacy['id']));self.assertIsNotNone(self.db.job(job['id']))
    def test_register_and_login_still_require_csrf_then_return_gone(self):
        body={'username':'public-csrf','password':PASSWORD}
        self.assertEqual(self.client.post('/api/register',json=body).status_code,403)
        data=self.client.get('/api/bootstrap').json()
        self.client.headers.update({'Origin':ORIGIN,'X-CSRF-Token':data['csrf']})
        self.assertEqual(self.client.post('/api/register',json={**body,'invitation':'invalid-secret-token-with-no-record'}).status_code,410)
        self.assertEqual(self.client.post('/api/register',json=body).status_code,410)
    def test_public_member_can_create_own_research_without_admin_permissions(self):
        alice,a=self.client_for('public-alice');bob,b=self.client_for('public-bob')
        with self.live_ready():
            response=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
        self.assertEqual(response.status_code,202)
        ident=response.json()['id'];self.assertEqual(self.db.job(ident)['user_id'],a['id'])
        folder=run_dir(self.settings,ident);folder.mkdir(parents=True);(folder/'report.html').write_text('<body>SYNTHETIC PRIVATE REPORT</body>')
        self.assertEqual(alice.get('/reports/'+ident+'/report.html').status_code,200)
        self.assertEqual(bob.get('/api/jobs').json()['jobs'],[])
        self.assertEqual(bob.get('/reports/'+ident+'/report.html').status_code,404)
        self.assertEqual(bob.post('/api/jobs/'+ident+'/cancel',json={}).status_code,404)
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
    def test_public_sample_needs_no_login_and_never_reads_private_runs(self):
        anonymous=self.client
        self.assertEqual(anonymous.get('/sample').status_code,404)
        client,user=self.client_for('owner');job=self.db.enqueue(user['id'],'FDE','all','demo')
        folder=run_dir(self.settings,job['id']);folder.mkdir(parents=True);(folder/'report.html').write_text('<body>PRIVATE REPORT</body>')
        self.assertEqual(anonymous.get('/sample').status_code,404)
        public=self.settings.data_dir/'public';public.mkdir()
        (public/'sample-report.html').write_text('<html><body><main>RAW REPORT 本轮预算</main></body></html>',encoding='utf-8')
        self.assertEqual(anonymous.get('/sample').status_code,404)
        (public/'sample-report.html').write_text('<html><body class="report-editorial"><main>SAMPLE REPORT</main></body></html>',encoding='utf-8')
        response=anonymous.get('/sample')
        self.assertEqual(response.status_code,200);self.assertIn('SAMPLE REPORT',response.text);self.assertNotIn('PRIVATE REPORT',response.text)
        self.assertIn('href="/"',response.text);self.assertIn("script-src 'none'",response.headers['content-security-policy'])
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
        self.assertEqual(self.client.post('/api/login',json={'username':'no-one','password':PASSWORD}).status_code,410)
        # 登录改走封面搭子后，限流放在授权回调上：同一来源 15 分钟内最多 20 次。
        for _ in range(20):self.assertEqual(self.callback(self.client,'code','s'*20).status_code,400)
        r=self.callback(self.client,'code','s'*20);self.assertEqual(r.status_code,429);self.assertIn('登录太频繁',r.text)
        self.assertEqual(self.fm.count('token'),0)
    def test_live_research_charges_once_with_job_id_as_reference(self):
        alice,a=self.client_for('alice')
        with self.live_ready():r=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
        self.assertEqual(r.status_code,202,r.text);job=r.json()
        calls=[payload for op,payload in self.fm.calls if op=='charge'];self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['reference'],job['id']);self.assertEqual(calls[0]['amount'],5);self.assertEqual(calls[0]['subject'],subject_for('alice'))
        row=self.db.credit_charge(job['id'])
        self.assertEqual((row['state'],row['amount'],row['reference']),('charged',5,job['id']))
        self.assertEqual(self.fm.balances[subject_for('alice')],5)
        self.assertEqual(alice.get('/api/bootstrap').json()['credits'],5)
    def test_insufficient_credits_returns_402_and_never_enqueues(self):
        alice,a=self.client_for('alice');self.fm.balances[subject_for('alice')]=3
        with self.live_ready():r=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
        self.assertEqual(r.status_code,402);body=r.json()
        self.assertEqual((body['balance'],body['cost'],body['recharge_url']),(3,5,'https://fm.zmtgzt.com/#/me'))
        self.assertIn('你现在有 3 分',body['detail']);self.assertIn('充值',body['detail'])
        self.assertEqual(self.db.jobs(a['id']),[]);self.assertEqual([c['state'] for c in self.charges()],['declined'])
        self.assertEqual(self.fm.balances[subject_for('alice')],3)
        # 封面搭子给的充值链接只接受普通网址，不能把 javascript: 之类的地址放进页面。
        # 只认和 BREAKOUT_FM_BASE 同源的地址，其余换成默认的 /#/me。
        for given,shown in (('javascript:alert(1)','https://fm.zmtgzt.com/#/me'),('https://pay.example.com/fm','https://fm.zmtgzt.com/#/me'),
                            ('http://fm.zmtgzt.com/#/me','https://fm.zmtgzt.com/#/me'),('https://fm.zmtgzt.com/#/me?from=breakout','https://fm.zmtgzt.com/#/me?from=breakout')):
            self.fm.recharge_url=given
            with self.live_ready():r=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
            self.assertEqual((r.status_code,r.json()['recharge_url']),(402,shown))
    def test_demo_research_never_charges(self):
        alice,a=self.client_for('alice')
        self.assertEqual(alice.post('/api/jobs',json={'topic':'FDE','mode':'demo'}).status_code,202)
        self.assertEqual(self.fm.count('charge'),0);self.assertEqual(self.charges(),[])
    def test_daily_quota_and_running_job_are_checked_before_charging(self):
        _,bob=self.client_for('bob');_,carol=self.client_for('carol');alice,a=self.client_for('alice')
        demo=self.db.enqueue(a['id'],'进行中的示例','all','demo')
        with self.live_ready():r=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
        self.assertEqual(r.status_code,409);self.assertIn('进行中',r.json()['detail'])
        self.db.cancel(demo['id'],a['id'])
        self.db.enqueue(bob['id'],'one','all','live');self.db.enqueue(carol['id'],'two','all','live')
        with self.live_ready():r=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
        self.assertEqual(r.status_code,409);self.assertIn('额度已用完',r.json()['detail'])
        self.assertEqual(self.fm.count('charge'),0);self.assertEqual(self.charges(),[])
        self.assertEqual(self.fm.balances[subject_for('alice')],10)
    def test_enqueue_failure_after_charge_refunds_immediately(self):
        alice,a=self.client_for('alice')
        # 模拟并发：扣分的同时，另一个请求抢先给 alice 排上了一项研究，入队时规则不放宽。
        self.fm.before['charge']=lambda payload:self.db.enqueue(a['id'],'抢先排上的示例','all','demo')
        with self.live_ready():r=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
        self.assertEqual(r.status_code,409);self.assertIn('刚才扣的积分已经退回',r.json()['detail'])
        self.assertEqual([j['mode'] for j in self.db.jobs(a['id'])],['demo'])
        self.assertEqual([c['state'] for c in self.charges()],['refunded']);self.assertEqual(self.fm.balances[subject_for('alice')],10)
    def test_unreachable_fm_returns_503_without_enqueue(self):
        alice,a=self.client_for('alice');self.fm.unreachable.add('charge')
        with self.live_ready():r=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
        self.assertEqual(r.status_code,503);self.assertIn('研究没有开始',r.json()['detail'])
        self.assertEqual(self.db.jobs(a['id']),[]);self.assertEqual([c['state'] for c in self.charges()],['declined'])
        self.assertEqual(self.fm.balances[subject_for('alice')],10)
    def test_unsure_charge_is_never_enqueued_and_is_refunded_later(self):
        # 扣分请求送到了、回应丢了：不入队；过一会儿后台去退，防止白扣。
        alice,a=self.client_for('alice');self.fm.lost_reply.add('charge')
        with self.live_ready():r=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
        self.assertEqual(r.status_code,503);self.assertIn('自动退回',r.json()['detail'])
        self.assertEqual(self.db.jobs(a['id']),[]);self.assertEqual(self.fm.balances[subject_for('alice')],5)
        row=self.charges()[0];self.assertEqual(row['state'],'refund_pending');self.assertGreater(row['next_attempt_at'],time.time()+30)
        self.fm.lost_reply.clear();self.credits.retry_due();self.assertEqual(self.fm.count('refund'),0)
        with self.db.connection(True) as c:c.execute('UPDATE credit_charges SET next_attempt_at=0')
        self.credits.retry_due()
        self.assertEqual(self.charges()[0]['state'],'refunded');self.assertEqual(self.fm.balances[subject_for('alice')],10)
    def test_interrupted_charge_request_is_refunded_by_background(self):
        # 扣分请求发出后进程中断：记录停在“扣分中”，两分钟后后台按待退处理。
        subject=subject_for('alice');self.fm.issue_code(subject,CALLBACK);job_id='a'*32
        self.db.begin_charge(job_id,subject,5);self.credits.client.charge(subject,5,job_id)
        self.credits.retry_due();self.assertEqual(self.charges()[0]['state'],'charging')
        with self.db.connection(True) as c:c.execute('UPDATE credit_charges SET created_at=created_at-121')
        self.credits.retry_due()
        self.assertEqual(self.charges()[0]['state'],'refunded');self.assertEqual(self.fm.balances[subject],10)
    def live_job(self,name='alice'):
        client,user=self.client_for(name)
        with self.live_ready():job=client.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'}).json()
        self.assertEqual(self.fm.balances[subject_for(name)],5)
        return client,user,job
    def test_failed_research_refunds_when_worker_finishes(self):
        client,user,job=self.live_job()
        claimed=self.db.claim();self.assertEqual(claimed['id'],job['id'])
        self.app.state.worker.execute(claimed)  # 没有真实服务配置：研究以 error 结束，没有报告
        self.assertEqual(client.get('/api/jobs/'+job['id']).json()['status'],'error')
        self.assertEqual(self.db.credit_charge(job['id'])['state'],'refunded');self.assertEqual(self.fm.balances[subject_for('alice')],10)
        self.credits.settle(job['id']);self.credits.retry_due();self.assertEqual(self.fm.count('refund'),1)
    def test_research_with_readable_report_keeps_the_charge(self):
        from demo import run_demo
        client,user,job=self.live_job()
        run_demo(run_dir(self.settings,job['id']),selection=True);self.db.finish(job['id'],'done','研究已完成',{})
        self.assertTrue(client.get('/api/jobs/'+job['id']).json()['report_ready'])
        self.credits.retry_due()  # 后台循环补核对：有可用报告，不退
        row=self.db.credit_charge(job['id']);self.assertEqual(row['state'],'charged');self.assertIsNotNone(row['settled_at'])
        self.credits.retry_due();self.assertEqual(self.fm.count('refund'),0);self.assertEqual(self.fm.balances[subject_for('alice')],5)
    def test_refund_failure_stays_pending_and_worker_loop_retries(self):
        client,user,job=self.live_job();self.fm.unreachable.add('refund')
        self.app.state.worker.execute(self.db.claim())
        row=self.db.credit_charge(job['id'])
        self.assertEqual((row['state'],row['attempts']),('refund_pending',1));self.assertGreater(row['next_attempt_at'],time.time())
        self.assertEqual(self.fm.balances[subject_for('alice')],5)
        self.fm.unreachable.clear()
        with self.db.connection(True) as c:c.execute('UPDATE credit_charges SET next_attempt_at=0')
        worker=self.app.state.worker;worker.start();self.addCleanup(worker.close)
        deadline=time.time()+10
        while time.time()<deadline and self.db.credit_charge(job['id'])['state']!='refunded':time.sleep(.05)
        self.assertEqual(self.db.credit_charge(job['id'])['state'],'refunded');self.assertEqual(self.fm.balances[subject_for('alice')],10)
    def test_cancelling_a_queued_live_research_refunds(self):
        client,user,job=self.live_job()
        self.assertEqual(client.post('/api/jobs/'+job['id']+'/cancel',json={}).json()['status'],'cancelled')
        self.assertEqual(self.db.credit_charge(job['id'])['state'],'refunded');self.assertEqual(self.fm.balances[subject_for('alice')],10)
    def test_interrupted_research_is_settled_and_resume_while_charged_is_free(self):
        client,user,job=self.live_job()
        # 服务重启打断了正在跑的研究，而且还没存下研究数据：网页上接续不了，积分不能就此卡住。
        self.db.claim();self.db.recover_interrupted()
        self.assertFalse(client.get('/api/jobs/'+job['id']).json()['can_resume'])
        self.credits.retry_due()
        self.assertEqual(self.db.credit_charge(job['id'])['state'],'refunded');self.assertEqual(self.fm.balances[subject_for('alice')],10)
        # 有研究数据、在后台结算前就接续的：这笔还扣着，接续不另扣
        _,_,other=self.live_job('bob')
        self.db.claim();self.db.recover_interrupted()
        folder=run_dir(self.settings,other['id']);folder.mkdir(parents=True);(folder/'state.json').write_text('{}')
        bob=self.client_for('bob')[0]
        self.assertEqual(bob.post('/api/jobs/'+other['id']+'/resume',json={}).status_code,202)
        self.assertEqual(self.fm.count('charge'),2);self.assertEqual(self.db.job(other['id'])['status'],'queued')
        self.credits.retry_due()  # 接续排着队时不结算
        self.assertEqual(self.db.credit_charge(other['id'])['state'],'charged');self.assertEqual(self.fm.balances[subject_for('bob')],5)
    def finished_without_report(self,job,status='error'):
        """研究跑完了但没有可用报告，留下研究数据（state.json），网页上可以接续。"""
        self.db.claim();self.db.finish(job['id'],status,'本轮研究暂时中断。',{})
        folder=run_dir(self.settings,job['id']);folder.mkdir(parents=True,exist_ok=True);(folder/'state.json').write_text('{}')
    def test_stopped_then_resumed_research_pays_exactly_once_per_report(self):
        client,user,job=self.live_job();subject=subject_for('alice');resume='/api/jobs/'+job['id']+'/resume'
        # 取完数据后用户自己停止：没有报告，退分
        self.finished_without_report(job,'cancelled');self.credits.settle(job['id'])
        self.assertEqual(self.fm.balances[subject],10)
        view=client.get('/api/jobs/'+job['id']).json();self.assertEqual((view['can_resume'],view['resume_credits']),(True,5))
        # 再接续要重新扣一次，扣成了才排队
        r=client.post(resume,json={'credits':5});self.assertEqual(r.status_code,202,r.text)
        self.assertEqual(self.db.job(job['id'])['status'],'queued');self.assertEqual(self.fm.balances[subject],5)
        self.assertEqual([(c['reference'],c['state']) for c in self.db.credit_charges(job['id'])],[(job['id'],'refunded'),(job['id']+'-r1','charged')])
        self.assertEqual(self.fm.calls[-1][1]['reference'],job['id']+'-r1')
        # 这次接续结束后还没有可用报告：退这一笔
        self.app.state.worker.execute(self.db.claim())
        self.assertEqual(self.db.credit_charge(job['id'])['state'],'refunded');self.assertEqual(self.fm.balances[subject],10)
        # 再接续，这次拿到报告：这笔留着，一份报告正好 5 分
        r=client.post(resume,json={'credits':5});self.assertEqual(r.status_code,202,r.text);self.assertEqual(self.fm.balances[subject],5)
        from demo import run_demo
        self.db.claim();shutil.rmtree(run_dir(self.settings,job['id']));run_demo(run_dir(self.settings,job['id']),selection=True)
        self.db.finish(job['id'],'done','研究已完成',{});self.credits.settle(job['id']);self.credits.retry_due()
        self.assertEqual([c['state'] for c in self.db.credit_charges(job['id'])],['refunded','refunded','charged'])
        self.assertEqual(self.fm.balances[subject],5);self.assertTrue(client.get('/api/jobs/'+job['id']).json()['report_ready'])
    def test_resume_recharge_failures_do_not_queue(self):
        client,user,job=self.live_job();subject=subject_for('alice');resume='/api/jobs/'+job['id']+'/resume'
        self.finished_without_report(job);self.credits.settle(job['id'])
        self.fm.balances[subject]=3
        r=client.post(resume,json={'credits':5});self.assertEqual(r.status_code,402)
        self.assertEqual(r.json()['detail'],'积分不够：跑一次真实研究要 5 积分，你现在有 3 分。请先去封面搭子充值。')
        self.fm.balances[subject]=10;self.fm.unreachable.add('charge')
        r=client.post(resume,json={'credits':5});self.assertEqual(r.status_code,503);self.assertIn('接续没有开始',r.json()['detail'])
        self.assertEqual(self.db.job(job['id'])['status'],'error')
        self.assertEqual([c['state'] for c in self.db.credit_charges(job['id'])],['refunded','declined','declined'])
        self.fm.unreachable.clear()
        self.assertEqual(client.post(resume,json={'credits':5}).status_code,202)
        self.assertEqual((self.db.credit_charge(job['id'])['reference'],self.fm.balances[subject]),(job['id']+'-r3',5))
    def test_two_resume_clicks_charge_only_once(self):
        # 同时点两次接续：第一次正在向封面搭子扣 -r1 时，第二次要记新的一笔会被拒绝，不会扣出两笔。
        client,user,job=self.live_job();self.finished_without_report(job);self.credits.settle(job['id'])
        outcomes=[]
        def second_click(payload):
            if payload['reference']!=job['id']+'-r1':return
            try:self.db.begin_resume_charge(job['id'],subject_for('alice'),5);outcomes.append('charged')
            except Conflict as error:outcomes.append(str(error))
        self.fm.before['charge']=second_click
        self.assertEqual(client.post('/api/jobs/'+job['id']+'/resume',json={'credits':5}).status_code,202)
        self.assertEqual(outcomes,['上一笔积分还在结算，请过一两分钟再接续。'])
        self.assertEqual([(c['reference'],c['state']) for c in self.db.credit_charges(job['id'])],[(job['id'],'refunded'),(job['id']+'-r1','charged')])
        self.assertEqual(self.fm.balances[subject_for('alice')],5)
        self.assertEqual(client.post('/api/jobs/'+job['id']+'/resume',json={}).status_code,409)
    def test_resume_waits_while_previous_charge_is_settling(self):
        client,user,job=self.live_job();resume='/api/jobs/'+job['id']+'/resume'
        self.fm.unreachable.add('refund');self.finished_without_report(job);self.credits.settle(job['id'])
        self.assertEqual(self.db.credit_charge(job['id'])['state'],'refund_pending')
        r=client.post(resume,json={});self.assertEqual(r.status_code,409);self.assertIn('上一笔积分还在结算',r.json()['detail'])
        self.assertEqual(self.db.job(job['id'])['status'],'error');self.assertEqual(self.fm.count('charge'),1)
        self.fm.unreachable.clear()
        with self.db.connection(True) as c:c.execute('UPDATE credit_charges SET next_attempt_at=0')
        self.credits.retry_due();self.assertEqual(client.post(resume,json={'credits':5}).status_code,202)
        self.assertEqual([c['state'] for c in self.db.credit_charges(job['id'])],['refunded','charged'])
    def test_resume_before_settlement_keeps_the_charge_for_the_resumed_run(self):
        # 并发顺序一：用户先点了接续，后台才结算。研究已在排队，这笔不退，留给接续那次用。
        client,user,job=self.live_job();self.finished_without_report(job)
        self.assertEqual(client.get('/api/jobs/'+job['id']).json()['resume_credits'],0)
        self.assertEqual(client.post('/api/jobs/'+job['id']+'/resume',json={}).status_code,202)
        self.credits.retry_due()
        self.assertEqual(self.db.credit_charge(job['id'])['state'],'charged');self.assertEqual(self.fm.sent.count('refund'),0)
        # 更窄的交错：后台已判定“没有报告”，正要改成待退时，用户的接续先提交了。改待退那条 SQL 自己会让开。
        _,_,other=self.live_job('bob');self.finished_without_report(other);bob=self.client_for('bob')[0]
        def resume_first(job_row):
            self.assertEqual(bob.post('/api/jobs/'+other['id']+'/resume',json={}).status_code,202);return False
        with patch.object(self.credits,'delivered',resume_first):self.credits.settle(other['id'])
        self.assertEqual(self.db.credit_charge(other['id'])['state'],'charged');self.assertEqual(self.fm.sent.count('refund'),0)
        self.assertEqual(self.fm.count('charge'),2)
    def test_settlement_before_resume_makes_the_resume_pay_again(self):
        # 并发顺序二：接续请求读到“还扣着”之后、真正排队之前，后台把这笔退了。排队那一步会发现并拒绝，用户再点会重新扣。
        client,user,job=self.live_job();self.finished_without_report(job);resume='/api/jobs/'+job['id']+'/resume'
        original=self.db.resume
        def settle_in_between(*args,**kwargs):
            self.credits.settle(job['id']);return original(*args,**kwargs)
        with patch.object(self.db,'resume',settle_in_between):r=client.post(resume,json={})
        self.assertEqual(r.status_code,409);self.assertIn('上一笔积分还在结算',r.json()['detail'])
        self.assertEqual((self.db.job(job['id'])['status'],self.db.credit_charge(job['id'])['state']),('error','refunded'))
        self.assertEqual(client.post(resume,json={'credits':5}).status_code,202)
        self.assertEqual([c['state'] for c in self.db.credit_charges(job['id'])],['refunded','charged'])
        self.assertEqual(self.fm.balances[subject_for('alice')],5)
    def test_resume_charges_only_the_price_the_page_showed(self):
        # 页面在后台结算前读到“接续不另扣”，随后后台把这笔退了：用户按着旧提示点接续，不能就这么扣 5 分。
        client,user,job=self.live_job();self.finished_without_report(job);resume='/api/jobs/'+job['id']+'/resume'
        self.assertEqual(client.get('/api/jobs/'+job['id']).json()['resume_credits'],0)
        self.credits.settle(job['id'])
        for body in ({'credits':0},{}):
            r=client.post(resume,json=body);self.assertEqual(r.status_code,409);self.assertIn('需要用 5 积分',r.json()['detail'])
        self.assertEqual((self.fm.count('charge'),self.fm.balances[subject_for('alice')]),(1,10))
        self.assertEqual(self.db.job(job['id'])['status'],'error')
        self.assertEqual(client.get('/api/jobs/'+job['id']).json()['resume_credits'],5)
        self.assertEqual(client.post(resume,json={'credits':5}).status_code,202);self.assertEqual(self.fm.balances[subject_for('alice')],5)
    def test_resume_recharge_that_cannot_queue_is_refunded_at_once(self):
        client,user,job=self.live_job();self.finished_without_report(job);self.credits.settle(job['id'])
        # 重新扣分的同时，同一用户抢先排上了别的研究：这次接续排不上，刚扣的 -r1 马上退
        self.fm.before['charge']=lambda payload:payload['reference'].endswith('-r1') and self.db.enqueue(user['id'],'抢先排上的示例','all','demo')
        r=client.post('/api/jobs/'+job['id']+'/resume',json={'credits':5})
        self.assertEqual(r.status_code,409);self.assertIn('刚才扣的积分已经退回',r.json()['detail'])
        self.assertEqual([(c['reference'],c['state']) for c in self.db.credit_charges(job['id'])],[(job['id'],'refunded'),(job['id']+'-r1','refunded')])
        self.assertEqual((self.db.job(job['id'])['status'],self.fm.balances[subject_for('alice')]),('error',10))
    def test_resume_recharge_reply_lost_is_refunded_later(self):
        client,user,job=self.live_job();self.finished_without_report(job);self.credits.settle(job['id']);resume='/api/jobs/'+job['id']+'/resume'
        self.fm.lost_reply.add('charge')  # 封面搭子扣成了，回应在路上丢了
        r=client.post(resume,json={'credits':5});self.assertEqual(r.status_code,503);self.assertIn('接续没有开始',r.json()['detail'])
        self.assertEqual((self.db.job(job['id'])['status'],self.db.credit_charge(job['id'])['state'],self.fm.balances[subject_for('alice')]),('error','refund_pending',5))
        self.fm.lost_reply.clear()
        r=client.post(resume,json={'credits':5});self.assertEqual(r.status_code,409);self.assertIn('上一笔积分还在结算',r.json()['detail'])
        with self.db.connection(True) as c:c.execute('UPDATE credit_charges SET next_attempt_at=0')
        self.credits.retry_due();self.assertEqual(self.fm.balances[subject_for('alice')],10)
        self.assertEqual(client.post(resume,json={'credits':5}).status_code,202)
        self.assertEqual([c['state'] for c in self.db.credit_charges(job['id'])],['refunded','refunded','charged'])
        self.assertEqual(self.fm.balances[subject_for('alice')],5)
    def test_settlement_skips_a_charge_used_by_a_newer_finished_run(self):
        # 结算判定“没有报告”之后、改待退之前，用户接续了而且这次接续已经带着报告跑完：这笔不能退。
        from demo import run_demo
        client,user,job=self.live_job();self.finished_without_report(job)
        def resumed_and_finished_meanwhile(job_row):
            self.assertEqual(client.post('/api/jobs/'+job['id']+'/resume',json={}).status_code,202)
            self.db.claim();shutil.rmtree(run_dir(self.settings,job['id']));run_demo(run_dir(self.settings,job['id']),selection=True)
            self.db.finish(job['id'],'done','研究已完成',{});return False
        with patch.object(self.credits,'delivered',resumed_and_finished_meanwhile):self.credits.settle(job['id'])
        self.assertEqual(self.db.credit_charge(job['id'])['state'],'charged');self.assertEqual(self.fm.sent.count('refund'),0)
        self.credits.retry_due()  # 新的那次结束按新的结束时间核对：有报告，这笔留着
        row=self.db.credit_charge(job['id']);self.assertEqual(row['state'],'charged');self.assertIsNotNone(row['settled_at'])
        self.assertEqual(self.fm.balances[subject_for('alice')],5)
    def test_charge_for_deleted_fm_account_asks_to_log_in_again(self):
        alice,a=self.client_for('alice');del self.fm.balances[subject_for('alice')]
        with self.live_ready():r=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
        self.assertEqual(r.status_code,409);self.assertIn('封面搭子账号已不可用',r.json()['detail'])
        self.assertEqual(self.db.jobs(a['id']),[]);self.assertEqual([c['state'] for c in self.charges()],['declined'])
    def test_refund_for_deleted_fm_account_becomes_void(self):
        client,user,job=self.live_job();self.finished_without_report(job)
        del self.fm.balances[subject_for('alice')]  # 用户在封面搭子注销了账号
        with self.assertLogs('webapp.credits','WARNING') as logs:self.credits.settle(job['id'])
        self.assertEqual(self.db.credit_charge(job['id'])['state'],'void');self.assertIn('已注销',''.join(logs.output))
        with self.db.connection(True) as c:c.execute('UPDATE credit_charges SET next_attempt_at=0')
        self.credits.retry_due();self.assertEqual(self.fm.sent.count('refund'),1)
    def test_charge_on_closed_reference_is_treated_as_not_charged(self):
        alice,a=self.client_for('alice')
        self.fm.before['charge']=lambda payload:self.fm.closed.add(payload['reference'])  # 封面搭子说这个编号已经关了
        with self.live_ready():r=alice.post('/api/jobs',json={'topic':'AI自媒体','mode':'live'})
        self.assertEqual(r.status_code,503);self.assertEqual(self.db.jobs(a['id']),[])
        self.assertEqual([c['state'] for c in self.charges()],['declined']);self.assertEqual(self.fm.balances[subject_for('alice')],10)
    def test_manage_commands_list_accounts_and_grant_admin(self):
        from webapp import manage
        alice,a=self.client_for('alice');self.db.enqueue(a['id'],'示例','all','demo')
        legacy=self.db.register_public('legacy-owner',PASSWORD)
        def run(*argv):
            out,err=io.StringIO(),io.StringIO()
            with patch('webapp.manage.Settings.from_env',return_value=self.settings),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                code=manage.main(list(argv))
            return code,out.getvalue(),err.getvalue()
        code,out,_=run('accounts');rows=[line.split('\t') for line in out.strip().splitlines()]
        self.assertEqual(code,0);self.assertEqual(rows[0],['用户编号','建号时间','最近登录','角色','研究数'])
        self.assertEqual(len(rows),2);self.assertEqual((rows[1][0],rows[1][3],rows[1][4]),(a['id'],'member','1'));self.assertNotEqual(rows[1][2],'—')
        self.assertNotIn(subject_for('alice'),out);self.assertNotIn(legacy['id'],out)
        code,out,_=run('grant-admin',a['id']);self.assertEqual(code,0);self.assertIn('已设为管理员',out)
        self.assertEqual(alice.get('/api/admin').status_code,200)
        for user_id,reason in ((legacy['id'],'只能给用封面搭子'),('0'*32,'没有找到'),('not-a-user-id','32 位')):
            code,_,err=run('grant-admin',user_id);self.assertEqual(code,2);self.assertIn(reason,err)
        code,out,_=run('revoke-admin',a['id']);self.assertEqual(code,0);self.assertIn('已撤销管理员',out)
        self.assertEqual(alice.get('/api/admin').status_code,403)
        with self.db.connection() as c:
            self.assertEqual([r[0] for r in c.execute("SELECT action FROM audit WHERE action IN ('grant_admin','revoke_admin') ORDER BY id")],['grant_admin','revoke_admin'])
        for old in ('bootstrap','invite'):
            code,out,err=run(old);self.assertEqual((code,out),(2,''));self.assertIn('已改为手机号登录',err);self.assertIn('grant-admin',err)
        with self.db.connection() as c:self.assertEqual(c.execute('SELECT count(*) FROM invites').fetchone()[0],0)
    def test_two_refund_attempts_for_one_charge_send_one_request(self):
        client,user,job=self.live_job()
        self.assertTrue(self.db.queue_refund(job['id'],('charged',)))
        started,release=threading.Event(),threading.Event()
        self.fm.before['refund']=lambda payload:(started.set(),release.wait(5))
        results=[]
        worker=threading.Thread(target=lambda:results.append(self.credits.attempt(job['id'])));worker.start()
        self.assertTrue(started.wait(5))
        self.assertIsNone(self.credits.attempt(job['id']))  # 另一处正在退：这次不再发请求
        release.set();worker.join(5)
        self.assertEqual(results,[True]);self.assertEqual(self.fm.count('refund'),1)
        self.assertEqual(self.fm.balances[subject_for('alice')],10)
    def test_retry_round_stops_at_first_fm_failure(self):
        for name in ('alice','bob'):
            _,_,job=self.live_job(name);self.db.queue_refund(job['id'],('charged',))
        self.fm.unreachable.add('refund')
        with self.assertLogs('webapp.credits','WARNING'):self.credits.retry_due()
        self.assertEqual(self.fm.sent.count('refund'),1)
        self.assertEqual(sorted(c['state'] for c in self.charges()),['refund_pending','refund_pending'])
    def test_bootstrap_returns_cached_balance_or_null(self):
        anonymous=self.client.get('/api/bootstrap').json()
        self.assertIsNone(anonymous['credits']);self.assertEqual(anonymous['research_credits'],5);self.assertEqual(self.fm.count('balance'),0)
        alice,a=self.client_for('alice');data=alice.get('/api/bootstrap').json()
        self.assertEqual((data['credits'],data['research_credits'],data['recharge_url']),(10,5,'https://fm.zmtgzt.com/#/me'))
        self.assertEqual(self.fm.count('balance'),1)  # 30 秒内用缓存
        now=time.monotonic();self.fm.balances[subject_for('alice')]=7;self.credits.clock=lambda:now+31
        self.assertEqual(alice.get('/api/bootstrap').json()['credits'],7);self.assertEqual(self.fm.count('balance'),2)
        self.fm.unreachable.add('balance');self.credits.clock=lambda:now+62
        data=alice.get('/api/bootstrap').json();self.assertIsNone(data['credits']);self.assertEqual(data['user']['id'],a['id'])
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
    def test_old_done_row_cannot_upgrade_a_report_below_the_target(self):
        from demo import run_demo
        client,user=self.client_for('alice');job=self.db.enqueue(user['id'],'Synthetic live record','all','live')
        run_demo(run_dir(self.settings,job['id']),selection=True)
        self.db.finish(job['id'],'done','研究已完成',{})
        result=client.get('/api/jobs/'+job['id']).json()
        self.assertEqual(result['status'],'report_ready')
        self.assertTrue(result['report_ready'])
        self.assertFalse(result['completion']['quantity']['met'])
        self.assertIn('尚未全部达成',result['message'])
        self.assertEqual(client.get(result['report_url']).status_code,200)

    def test_missing_data_disables_resume_and_returns_conflict_without_enqueue(self):
        from demo import run_demo
        from core import read_json,atomic_json
        client,user=self.client_for('alice');job=self.db.enqueue(user['id'],'Synthetic missing data','all','live')
        store,_=run_demo(run_dir(self.settings,job['id']),selection=True)
        for path in (store.root/'cache').glob('*.json'):
            rec=read_json(path)
            if rec['operation']=='stats':
                for data in rec['data']['statistics'].values():data['play_count']=1000
                atomic_json(path,rec)
        self.db.finish(job['id'],'error','写作失败',{})
        result=client.get('/api/jobs/'+job['id']).json()
        self.assertEqual(result['status'],'needs_data');self.assertFalse(result['can_resume'])
        self.assertEqual(client.post('/api/jobs/'+job['id']+'/resume',json={}).status_code,409)
        self.assertEqual(self.db.job(job['id'])['status'],'error')
        self.assertFalse((store.root.parent/'model-events.jsonl').exists())

    def test_completed_row_with_invalid_report_can_explicitly_resume_from_cache(self):
        from demo import run_demo
        client,user=self.client_for('alice');job=self.db.enqueue(user['id'],'Synthetic stale report','all','live')
        store,_=run_demo(run_dir(self.settings,job['id']),selection=True)
        path=store.root/'report.html';path.write_text(path.read_text().replace('<dd>','<dd>9999万 ',1))
        self.db.finish(job['id'],'done','旧完成状态',{})
        result=client.get('/api/jobs/'+job['id']).json()
        self.assertEqual(result['status'],'needs_analysis');self.assertTrue(result['can_resume'])
        response=client.post('/api/jobs/'+job['id']+'/resume',json={})
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(self.db.job(job['id'])['resume_only'],1)
        self.assertFalse((store.root.parent/'model-events.jsonl').exists())

    def test_unconfigured_real_research_does_not_enqueue(self):
        client,user=self.client_for('alice');r=client.post('/api/jobs',json={'topic':'AI自媒体'})
        self.assertEqual(r.status_code,409);self.assertEqual(self.db.jobs(user['id']),[])
        self.assertEqual(self.fm.count('charge'),0)

class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.key=Fernet.generate_key().decode()
    def test_production_requires_valid_fm_settings(self):
        folder=Path(self.temp.name)
        with self.assertRaises(ValueError) as error:Settings(folder,ORIGIN,self.key)
        self.assertIn('BREAKOUT_FM_CLIENT_SECRET',str(error.exception))
        for secret in ('too-short','has a space in it 123','x'*300,'中文密钥中文密钥中文密钥中文密钥'):
            with self.assertRaises(ValueError) as error:Settings(folder,ORIGIN,self.key,fm_client_secret=secret)
            self.assertNotIn(secret,str(error.exception))
        for base in ('http://fm.zmtgzt.com','https://fm.zmtgzt.com/api','https://user@fm.zmtgzt.com','fm.zmtgzt.com','https://fm.zmtgzt.com:99999'):
            with self.assertRaises(ValueError):Settings(folder,ORIGIN,self.key,fm_client_secret=FM_SECRET,fm_base=base)
        for credits in (0,51,True,'5'):
            with self.assertRaises(ValueError):Settings(folder,ORIGIN,self.key,fm_client_secret=FM_SECRET,research_credits=credits)
        self.assertEqual(Settings(folder,ORIGIN,self.key,fm_client_secret=FM_SECRET,research_credits=50).research_credits,50)
        settings=Settings(folder,ORIGIN,self.key,fm_client_secret=FM_SECRET,fm_base='https://fm.zmtgzt.com/')
        self.assertEqual((settings.fm_base,settings.research_credits),('https://fm.zmtgzt.com',5))
        self.assertNotIn(FM_SECRET,repr(settings))
    def test_dev_mode_allows_fake_secret_and_local_fm(self):
        settings=Settings(Path(self.temp.name),'http://127.0.0.1:8000',self.key,dev=True,fm_base='http://127.0.0.1:9000')
        self.assertTrue(settings.fm_client_secret);self.assertEqual(settings.fm_base,'http://127.0.0.1:9000')
        with self.assertRaises(ValueError):Settings(Path(self.temp.name),'http://127.0.0.1:8000',self.key,dev=True,fm_base='http://fm.example.com')
    def test_environment_variables(self):
        env={'BREAKOUT_MASTER_KEY':self.key,'BREAKOUT_DATA_DIR':self.temp.name,'BREAKOUT_PUBLIC_URL':'https://breakout.zmtgzt.com',
             'BREAKOUT_FM_CLIENT_SECRET':FM_SECRET,'BREAKOUT_RESEARCH_CREDITS':'3'}
        with patch.dict(os.environ,env):
            for name in ('BREAKOUT_DEV_MODE','BREAKOUT_FM_BASE'):os.environ.pop(name,None)
            settings=Settings.from_env()
            self.assertEqual((settings.fm_base,settings.fm_client_secret,settings.research_credits),('https://fm.zmtgzt.com',FM_SECRET,3))
            os.environ.pop('BREAKOUT_RESEARCH_CREDITS')
            self.assertEqual(Settings.from_env().research_credits,5)
            os.environ['BREAKOUT_FM_CLIENT_SECRET']=''
            with self.assertRaises(ValueError):Settings.from_env()

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
