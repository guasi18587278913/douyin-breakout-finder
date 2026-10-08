"""Local-only onboarding. API keys live in memory and are never sent to a model."""
import argparse
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from core import Store, StopRun, atomic_json, now, read_json
from bridge import FileBridge, AssistanceNeeded
from research import review_packet, accept_review, analysis_packet, accept_research, report_state
from product import intake, config_for, review_by_rules, evidence_notes, fetch_prices, simple_brief
from tikhub import TikHubClient
from workflow import search, verify, write_inbox
from report import render
from demo import run_demo

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = {'report.html': 'text/html; charset=utf-8', 'results.csv': 'text/csv; charset=utf-8',
           'comparisons.csv': 'text/csv; charset=utf-8', 'results.json': 'application/json',
           'summary.json': 'application/json', 'candidates.json': 'application/json'}


class Session:
    def __init__(self, data_dir, bridge_dir=None, bridge_timeout=600):
        self.data_dir = Path(data_dir).resolve()
        self.token = secrets.token_urlsafe(32)
        self.key = None
        self.mode = None
        self.pricing = None
        self.answers = None
        self.run_dir = None
        self.status = 'ready'
        self.message = '准备好后开始。'
        self.summary = None
        self.lock = threading.RLock()
        self.last_active = time.monotonic()
        self.cancelled = threading.Event()
        self.worker = None
        self.bridge = FileBridge(bridge_dir,self.cancelled,bridge_timeout) if bridge_dir else None

    def snapshot(self):
        with self.lock:
            if self.status!='running' and self.run_dir and not (self.run_dir/'.run.lock').exists():
                try:
                    refreshed=read_json(self.run_dir/'summary.json',{})
                    if refreshed and refreshed.get('generated_at')!=(self.summary or {}).get('generated_at'):
                        stored=Store(self.run_dir)
                        readiness=report_state(stored)
                        if refreshed.get('research_status')==readiness['status']:
                            self.summary=refreshed
                            self.status='done' if readiness['status']=='complete' else 'needs_analysis'
                            self.message='智能体已更新研究报告。' if self.status=='done' else '阶段报告已更新，仍有证据待补。'
                except (StopRun,OSError,ValueError):
                    pass
            return dict(status=self.status, message=self.message, mode=self.mode,
                        key_ready=bool(self.key), assistant_connected=bool(self.bridge and self.bridge.available()), pricing=self.pricing, answers=self.answers,
                        summary=self.summary, report_url='/report/report.html' if self.summary else None,
                        output_dir=str(self.run_dir) if self.run_dir else None, version='0.2.0')

    def start(self, data):
        with self.lock:
            if self.status == 'running':
                raise StopRun('已有任务正在运行，请等待本轮结束。')
            answers = simple_brief(data) if data.get('description') else intake(data)
            config = None
            if self.mode == 'live':
                if not self.key:
                    raise StopRun('请先填入本次会话的 TikHub Key。')
                if not self.bridge or not self.bridge.available():
                    raise StopRun('分析助手尚未接入。请回到启动 Skill 的智能体会话，说“继续完成小号爆款研究”；接入前不会发起收费查询。')
                if data.get('consent') is not True:
                    raise StopRun('请确认本轮美元预算后再开始。')
                if not self.pricing:
                    self.pricing = fetch_prices()
                config = config_for(answers, self.pricing, '0.50')
                config['max_requests'] = 50
                # Check quote validity before committing to a run or making calls.
                temp = type('PriceCheck', (), {'config': config})()
                Store.pricing_ready(temp, ['search', 'posts', 'stats', 'comments'])
            elif self.mode != 'demo':
                raise StopRun('请先选择免费示例或连接 TikHub。')
            self.answers = answers
            self.run_dir = self.data_dir / ('run-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(4))
            self.summary = None
            if self.bridge: atomic_json(self.bridge.folder/'status.json',{'terminal':False})
            self.cancelled.clear()
            self.status, self.message = 'running', '正在准备本轮研究…'
            thread = threading.Thread(target=self.run, args=(config,), daemon=True)
            self.worker = thread
            thread.start()

    def run(self, config):
        store = None
        try:
            if self.mode == 'demo':
                self.message = '正在用合成的 FDE 数据走完整流程；不会联网。'
                store, _ = run_demo(self.run_dir)
                summary = render(store)
            else:
                self.message = '正在确认智能体已经接入本轮研究…'
                def ready(answer):
                    if not isinstance(answer,dict) or answer.get('ready') is not True:
                        raise StopRun('智能体需确认已接入当前研究。')
                    return True
                self.bridge.ask('prepare-research',{'run_dir':str(self.run_dir),'topic':config['topic'],'budget_usd':config['budget_usd'],'max_requests':config['max_requests']},ready)
                store = Store.create(self.run_dir, config)
                with store.lock():
                    client = TikHubClient(store, allow_paid=True, api_key=self.key)
                    client = CancellableClient(client, self.cancelled)
                    self.message = '正在搜索本轮候选…'
                    search(store, client, page_limit=1)
                    review_by_rules(store, self.answers)
                    write_inbox(store)
                    self.summary = render(store)
                    for round_number in range(3):
                        self.message = '智能体正在复核内容相关性，并判断是否需要补搜…'
                        response=self.bridge.ask('semantic-review',review_packet(store,round_number),lambda answer:accept_review(store,answer))
                        if not response['next_keywords'] or round_number==2:break
                        self.message = '按智能体的研究判断补充候选，仍使用本轮原有额度…'
                        search(store,client,response['next_keywords'],response['search_reason'],page_limit=1)
                        review_by_rules(store,self.answers)
                        write_inbox(store)
                        self.summary=render(store)
                    if any(v['review']['decision']=='include' for v in store.state['candidates'].values()):
                        self.message = '正在核对同作者邻近作品和播放量…'
                        verify(store, client)
                    write_inbox(store)
                    self.summary=render(store)
                    self.message = '智能体正在阅读内容证据、整理主题发现和逐条案例…'
                    self.bridge.ask('content-analysis',analysis_packet(store),lambda answer:accept_research(store,answer))
                    summary = render(store)
            with self.lock:
                self.summary = summary
                self.status = 'done' if summary.get('report_complete') else 'needs_analysis'
                self.message = '研究报告已生成。' if summary.get('report_complete') else '已保存阶段报告；仍有内容证据或相关性判断待补充，尚未完成完整研究。'
        except Exception as error:
            # Do not serialize exception bodies from providers or keys.
            message = str(error) if isinstance(error, StopRun) else '本轮未能完成，请保留运行目录并联系维护者。'
            if self.key:
                message = message.replace(self.key, '[REDACTED]')
            if store:
                try:
                    store.state['stops'].append({'at': now(), 'phase': 'wizard', 'message': message})
                    store.save()
                    write_inbox(store)
                    self.summary = render(store)
                except Exception:
                    pass
            with self.lock:
                self.status, self.message = ('needs_analysis' if isinstance(error,AssistanceNeeded) else 'error'), message
        finally:
            self.key = None
            if self.bridge:
                atomic_json(self.bridge.folder/'status.json',{'terminal':True,'at':time.time(),'request_id':self.bridge.last_request_id,'status':self.status,'run_dir':str(self.run_dir),'message':self.message})


class Handler(BaseHTTPRequestHandler):
    server_version = 'LocalResearch'

    def log_message(self, *_):
        pass

    @property
    def session(self):
        return self.server.session

    def reply(self, code, body, kind='application/json; charset=utf-8', cookie=False):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', 'research_session=%s; HttpOnly; SameSite=Strict; Path=/' % self.session.token)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def guard(self, auth=True, mutation=False):
        host = '127.0.0.1:%s' % self.server.server_port
        if self.headers.get('Host') != host:
            self.reply(403, {'error': '仅允许本机访问。'})
            return False
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + host:
            self.reply(403, {'error': '拒绝跨站访问。'})
            return False
        if mutation and (origin != 'http://' + host or self.headers.get('X-Wizard') != '1'):
            self.reply(403, {'error': '请求来源校验失败，请重新打开向导。'})
            return False
        if auth:
            cookies = SimpleCookie()
            try:
                cookies.load(self.headers.get('Cookie', ''))
                supplied = cookies['research_session'].value if 'research_session' in cookies else ''
            except Exception:
                supplied = ''
            if not hmac.compare_digest(supplied, self.session.token):
                self.reply(401, {'error': '会话已失效，请从智能体重新打开向导。'})
                return False
            self.session.last_active = time.monotonic()
        return True

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        public = {'/': ('wizard.html', 'text/html; charset=utf-8'),
                  '/wizard.css': ('wizard.css', 'text/css'), '/wizard.js': ('wizard.js', 'text/javascript')}
        if path in public:
            if self.guard(auth=False):
                name, kind = public[path]
                self.reply(200, (ROOT / 'assets' / name).read_bytes(), kind)
            return
        if not self.guard():
            return
        if path == '/api/state':
            self.reply(200, self.session.snapshot())
        elif path.startswith('/report/') and path[len('/report/'):] in EXPORTS and self.session.summary:
            name = path[len('/report/'):]
            self.reply(200, (self.session.run_dir / name).read_bytes(), EXPORTS[name])
        else:
            self.reply(404, {'error': '未找到页面。'})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.guard(auth=path != '/api/connect', mutation=True):
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16000 or self.headers.get_content_type() != 'application/json':
                raise StopRun('请求内容格式或大小不正确。')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise StopRun('请求需要对象格式。')
            if path == '/api/connect':
                token = data.get('token', '')
                if not isinstance(token, str) or not hmac.compare_digest(token, self.session.token):
                    self.reply(401, {'error': '会话验证失败，请重新启动。'})
                    return
                self.reply(200, self.session.snapshot(), cookie=True)
                return
            if path == '/api/quit':
                self.session.cancelled.set()
                self.reply(200, {'message': '已请求停止，不再发起新查询。正在进行的请求可能已计费；完成保存后会话将自动退出并清除 Key。'})
                def finish():
                    if self.session.worker:
                        self.session.worker.join()
                    self.session.key = None
                    self.server.shutdown()
                threading.Thread(target=finish, daemon=True).start()
                return
            with self.session.lock:
                if self.session.status == 'running':
                    raise StopRun('任务正在运行，本轮设置已锁定。')
                if path == '/api/setup':
                    if data.get('mode') not in ('live', 'demo'):
                        raise StopRun('请选择运行方式。')
                    key = data.get('key', '')
                    if not isinstance(key, str) or len(key) > 2048 or any(ord(c) < 32 or ord(c) > 126 for c in key):
                        raise StopRun('Key 格式不正确，请重新复制。')
                    if data['mode'] == 'live' and not key.strip():
                        raise StopRun('请输入 TikHub Key。')
                    self.session.mode = data['mode']
                    self.session.key = key.strip() if data['mode'] == 'live' else None
                elif path == '/api/brief':
                    self.session.answers = simple_brief(data)
                elif path == '/api/intake':
                    self.session.answers = intake(data)
                elif path == '/api/prices':
                    # Network operation is anonymous, never authenticated with the key.
                    self.session.pricing = fetch_prices()
                elif path == '/api/manual-prices':
                    if data.get('verified') is not True:
                        raise StopRun('请先核实后台报价与美元单位。')
                    self.session.pricing = {'source_url': data.get('source_url'), 'checked_at': now(),
                                            'usd_per_request': data.get('usd_per_request')}
                    check = config_for(intake({'topic': '报价核验'}), self.session.pricing, '1')
                    Store.pricing_ready(type('PriceCheck', (), {'config': check})(), ['search', 'posts', 'stats', 'comments'])
                elif path == '/api/start':
                    self.session.start(data)
                else:
                    self.reply(404, {'error': '未知操作。'})
                    return
            self.reply(200, self.session.snapshot())
        except (ValueError, TypeError, KeyError, StopRun) as error:
            message = str(error) if isinstance(error, StopRun) else '字段格式不正确，请检查输入。'
            if self.session.key:
                message = message.replace(self.session.key, '[REDACTED]')
            self.reply(400, {'error': message})
        except Exception:
            self.reply(500, {'error': '操作未完成，请重新打开向导；不要把 Key 发到聊天里。'})


class CancellableClient:
    def __init__(self, client, cancelled):
        self.client, self.cancelled = client, cancelled

    def call(self, *args, **kwargs):
        if self.cancelled.is_set():
            raise StopRun('用户已结束会话；停止新的查询，已发出的请求仍计入预算。')
        return self.client.call(*args, **kwargs)


def make_server(data_dir, bridge_dir=None, bridge_timeout=600):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server.session = Session(data_dir,bridge_dir,bridge_timeout)
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default=str(Path.home() / '.local' / 'share' / 'douyin-breakout-finder'))
    parser.add_argument('--ready-file')
    parser.add_argument('--bridge-dir')
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt':
        os.umask(0o077)
    server = make_server(args.data_dir,args.bridge_dir)
    url = 'http://127.0.0.1:%s/#%s' % (server.server_port, server.session.token)
    ready = {'url': url, 'pid': os.getpid(), 'data_dir': str(server.session.data_dir), 'bridge_dir':args.bridge_dir}
    if args.ready_file:
        atomic_json(args.ready_file, ready)
    print(json.dumps(ready, ensure_ascii=False), flush=True)
    if not args.no_open:
        webbrowser.open(url)
    def expire():
        while True:
            time.sleep(30)
            if server.session.status != 'running' and time.monotonic() - server.session.last_active > 1800:
                server.session.key = None
                server.shutdown()
                return
    threading.Thread(target=expire, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.session.key = None
        server.server_close()
        if args.ready_file:
            Path(args.ready_file).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
