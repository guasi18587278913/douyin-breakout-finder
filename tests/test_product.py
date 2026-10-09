import copy
import http.client
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from core import StopRun, read_json
from product import intake, contains, parse_price, evidence_notes, review_by_rules, simple_brief
from workflow import apply_review, write_inbox
from demo import run_demo
from wizard import make_server, Session, CancellableClient
from report import render

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class TestProduct(unittest.TestCase):
    def test_free_text_examples_extract_topic_without_configuration(self):
        for raw in ['我想了解一下AI自媒体现在有的一些小号爆款。','我想看看 AI 自媒体里，小号最近做出了哪些爆款。']:
            brief=simple_brief({'description':raw})
            self.assertEqual(brief['topic'],'AI自媒体')
            self.assertEqual(brief['keywords'],['AI自媒体','AI内容创作','AI短视频'])
        self.assertEqual(simple_brief({'description':'电商 AI 自动化','focus':'case'})['keywords'],['电商 AI 自动化 案例'])

    def test_native_quote_requires_router_params_and_endpoint(self):
        endpoint='/api/test'
        payload={'router':'/api/v1/tikhub/user/get_endpoint_info','params':{'endpoint':endpoint},'data':{'endpoint_uri':endpoint,'endpoint_cost':0.001}}
        self.assertEqual(parse_price(payload,endpoint),'0.001')
        payload['params']['endpoint']='/wrong'
        with self.assertRaises(StopRun):parse_price(payload,endpoint)

    def test_actual_curl_preserves_chinese_quotes_and_backslashes(self):
        import subprocess, shutil
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from transport import request
        if not shutil.which('curl'): self.skipTest('curl not available')
        captured={}
        class Echo(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                captured['body']=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured['auth']=self.headers['Authorization']
                self.send_response(200);self.end_headers();self.wfile.write(b'{}')
        server=ThreadingHTTPServer(('127.0.0.1',0),Echo)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        real_run=subprocess.run
        def local_only(command,**kwargs):
            # Only this isolated loopback fixture permits HTTP; production remains HTTPS-only.
            command=list(command);command[command.index('=https')]='=http'
            return real_run(command,**kwargs)
        payload={'keyword':'电商 AI 教程 "引号" 和路径 C:\\demo','other':'line1\nline2'}
        try:
            with patch('transport.subprocess.run',side_effect=local_only):
                status,_=request('http://127.0.0.1:%s/'%server.server_port,method='POST',payload=payload,api_key='SYNTHETIC-KEY')
            self.assertEqual(status,200);self.assertEqual(captured['body'],payload)
            self.assertEqual(captured['auth'],'Bearer SYNTHETIC-KEY')
        finally:server.shutdown();server.server_close()

    def test_live_branch_with_fixture_keeps_fixed_limits(self):
        from demo import ReplayClient
        from core import now
        fixture=read_json(ROOT/'assets/demo.json')
        fixture['queries']['FDE']=list(dict.fromkeys(vid for ids in fixture['queries'].values() for vid in ids))
        def fake_client(store,**kwargs):
            replay=ReplayClient.__new__(ReplayClient);replay.store=store;replay.fixture=fixture
            return replay
        quote={'usd_per_request':{op:'0.001' for op in ['search','posts','stats','comments']},'checked_at':now(),'source_url':'https://tikhub.io/pricing'}
        with tempfile.TemporaryDirectory() as folder:
            session=Session(folder);session.mode='live';session.key='SYNTHETIC-KEY'
            bridge_folder=Path(folder)/'bridge'
            class FixtureBridge:
                folder=bridge_folder
                last_request_id='fixture'
                def available(self):return True
                def ask(self,stage,payload,accept):
                    if stage=='prepare-research':return accept({'ready':True})
                    if stage=='semantic-review':return accept({'reviews':{v['id']:{'decision':'include','reason':'合成测试的同主题候选','priority':1} for v in payload['candidates']},'next_keywords':[]})
                    from report_fixture import selection_answer
                    return accept(selection_answer(payload))
            session.bridge=FixtureBridge()
            with patch('wizard.fetch_prices',return_value=quote),patch('wizard.TikHubClient',side_effect=fake_client):
                session.start({'description':'FDE','consent':True,'budget':'99999'})
                session.worker.join(10)
            self.assertEqual(session.status,'done')
            self.assertGreater(session.summary['qualified_count'],0)
            config=read_json(session.run_dir/'config.json')
            self.assertEqual(config['budget_usd'],'0.50');self.assertEqual(config['max_requests'],50)
            self.assertIsNone(session.key)

    def test_network_key_only_passes_in_stdin_and_redirect_stops(self):
        from transport import request
        from unittest.mock import Mock
        fake=Mock(returncode=0,stdout=b'{}\n200',stderr=b'')
        with patch('transport.shutil.which',return_value='/usr/bin/curl'),patch('transport.subprocess.run',return_value=fake) as runner:
            request('https://api.tikhub.io/test',api_key='SYNTHETIC-KEY')
            args,kwargs=runner.call_args
            self.assertNotIn('SYNTHETIC-KEY',str(args))
            self.assertIn(b'SYNTHETIC-KEY',kwargs['input'])
            self.assertIn('--disable',args[0]);self.assertNotIn('--location',args[0])
            fake.stdout=b'{}\n302'
            from core import APIError
            with self.assertRaises(APIError):request('https://api.tikhub.io/test',api_key='SYNTHETIC-KEY')

    def test_intake_deduplicates_and_preserves_boundaries(self):
        a = intake({'topic':'AI 自动化', 'keywords':'AI工作流,AI工作流，自动化', 'excludes':'卖课,招聘'})
        self.assertEqual(a['keywords'], ['AI工作流','自动化'])
        self.assertEqual(a['anchors'], a['keywords'])
        self.assertFalse(contains('abcFDExyz', 'FDE'))
        self.assertTrue(contains('什么是 FDE？', 'FDE'))

    def test_malformed_host_answers_fail_without_cost(self):
        for value in [None, '给我爆款', {'topic':12}, {'topic':'AI','goal':'invent'}, {'topic':'AI','keywords':['x']}, {'topic':'AI','keywords':','.join(str(i) for i in range(20))}]:
            with self.subTest(value=value), self.assertRaises(StopRun):
                intake(value)

    def test_quote_needs_exact_endpoint_and_explicit_dollars(self):
        endpoint = '/sample'
        for data in [{'price':'0.001'}, {'endpoint':endpoint,'price':'1','currency':'credits'}, {'endpoint':'/other','price_usd':'0.1'}, {'endpoint':endpoint,'price_usd':'NaN'}, {'endpoint':endpoint,'price_usd':'0'}, {'endpoint':endpoint,'total_price':'1','currency':'USD'}]:
            with self.subTest(data=data), self.assertRaises(StopRun):
                parse_price({'data':data}, endpoint)
        self.assertEqual(parse_price({'data':{'endpoint':endpoint,'price_per_request':'0.001','currency':'USD'}},endpoint),'0.001')

    def test_fixed_analysis_cannot_change_numbers(self):
        with tempfile.TemporaryDirectory() as folder:
            store, summary = run_demo(Path(folder)/'run')
            original = copy.deepcopy(store.state['results'])
            evidence_notes(store)
            self.assertEqual(store.state['results'], original)
            updated = render(store)
            self.assertEqual(summary['qualified_count'], updated['qualified_count'])
            self.assertTrue(store.state['analyses'])
            for a in store.state['analyses'].values():
                self.assertEqual(a['basis'], 'title-description')
                self.assertIn('未观看', a['evidence_note'])

    def test_review_inbox_preserves_keyword_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            store, _ = run_demo(Path(folder)/'run')
            for v in store.state['candidates'].values():v['review']={'decision':'pending','reason':''}
            review_by_rules(store, intake({'topic':'FDE'}))
            write_inbox(store)
            inbox=read_json(store.root/'review-inbox.json')
            apply_review(store,inbox)
            self.assertTrue(all(v['review']['method']=='keyword-rule' for v in store.state['candidates'].values()))
            render(store)
            self.assertIn('内容语义仍待复核',(store.root/'report.html').read_text())

    def test_cancel_prevents_following_paid_call(self):
        from unittest.mock import Mock
        client = Mock()
        stopped = threading.Event()
        wrapped = CancellableClient(client, stopped)
        stopped.set()
        with self.assertRaises(StopRun): wrapped.call('search', {})
        client.call.assert_not_called()

    def test_real_run_requires_consent_and_quote(self):
        with tempfile.TemporaryDirectory() as folder:
            session = Session(folder)
            session.mode, session.key = 'live', 'SYNTHETIC-KEY-FOR-TESTS'
            for data in [{'topic':'AI'}, {'topic':'AI','consent':True,'budget':'1'}]:
                with patch('wizard.fetch_prices', side_effect=StopRun('Quote unavailable')), self.assertRaises(StopRun):
                    session.start(data)
            self.assertEqual(list(Path(folder).iterdir()), [])


class TestInstaller(unittest.TestCase):
    def test_install_update_backup_and_data_survival(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'skills'/installer.NAME
            data = Path(folder)/'data'; data.mkdir(); (data/'keep.txt').write_text('my data')
            first = installer.install(ROOT, target, client='workbuddy')
            self.assertIsNone(first['backup'])
            self.assertEqual(read_json(target/'.installation.json')['client'], 'workbuddy')
            second = installer.install(ROOT, target, backup_root=Path(folder)/'backups')
            self.assertEqual(read_json(target/'.installation.json')['client'], 'workbuddy')
            self.assertTrue(Path(second['backup']).is_dir())
            self.assertNotEqual(Path(second['backup']).parent, target.parent)
            self.assertEqual((data/'keep.txt').read_text(),'my data')

    def test_reject_modified_unknown_and_symlink_install(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'managed'
            installer.install(ROOT,target)
            (target/'SKILL.md').write_text('local change')
            with self.assertRaises(ValueError): installer.install(ROOT,target)
            self.assertEqual((target/'SKILL.md').read_text(),'local change')
            unknown = Path(folder)/'unknown'; unknown.mkdir()
            with self.assertRaises(ValueError): installer.install(ROOT,unknown)

    @unittest.skipIf(sys.platform == 'win32', 'Windows symlink permissions vary')
    def test_ignored_source_symlinks_do_not_block(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'source'; source.mkdir()
            for name in ['SKILL.md','project.json']:
                (source/name).write_bytes((ROOT/name).read_bytes())
            ignored=source/'.venv'; ignored.mkdir()
            (ignored/'python').symlink_to(sys.executable)
            installer.install(source,Path(folder)/'destination')

    def test_reject_extra_user_data_on_update(self):
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)/'managed'; installer.install(ROOT,target)
            (target/'my-key.txt').write_text('SYNTHETIC-NOT-REAL')
            with self.assertRaises(ValueError): installer.install(ROOT,target)
            self.assertTrue((target/'my-key.txt').exists())


class TestWebBoundary(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=make_server(self.temp.name)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.host='127.0.0.1:%s' % self.server.server_port
        self.cookie='research_session='+self.server.session.token
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.temp.cleanup()
    def request(self,path,body=None,**headers):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        method='GET' if body is None else 'POST'
        heads={'Host':self.host,**headers}
        if body is not None: heads.setdefault('Content-Type','application/json')
        connection.request(method,path,body=None if body is None else json.dumps(body),headers=heads)
        response=connection.getresponse(); raw=response.read(); status=response.status; connection.close()
        return status,raw
    def test_private_data_needs_session(self):
        self.assertEqual(self.request('/api/state')[0],401)
        self.assertEqual(self.request('/api/state',Cookie=self.cookie)[0],200)
        self.assertEqual(self.request('/api/state',Cookie=self.cookie,Host='evil.example')[0],403)
    def test_cross_site_mutation_denied(self):
        data={'mode':'live','key':'SYNTHETIC-KEY-FOR-TESTS'}
        self.assertEqual(self.request('/api/setup',data,Cookie=self.cookie,Origin='https://evil.example',**{'X-Wizard':'1'})[0],403)
        self.assertEqual(self.request('/api/setup',data,Cookie=self.cookie,Origin='http://'+self.host)[0],403)
        self.assertIsNone(self.server.session.key)
    def test_key_not_in_state_disk_or_http_response(self):
        key='SYNTHETIC-PRIVATE-KEY-123456'
        status,raw=self.request('/api/setup',{'mode':'live','key':key},Cookie=self.cookie,Origin='http://'+self.host,**{'X-Wizard':'1'})
        self.assertEqual(status,200); self.assertNotIn(key.encode(),raw)
        self.assertNotIn(key.encode(),self.request('/api/state',Cookie=self.cookie)[1])
        self.assertEqual(list(Path(self.temp.name).rglob('*')),[])
    def test_report_traversal_is_not_served(self):
        self.assertEqual(self.request('/report/../state.json',Cookie=self.cookie)[0],404)


if __name__=='__main__': unittest.main()
