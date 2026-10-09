"""Local-only onboarding. API keys can be remembered privately and are never sent to a model."""
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
from credentials import CredentialStore, CredentialError, validate_key
from bridge import FileBridge, AssistanceNeeded
from analyst import LocalAnalyst, MODES
from research import review_packet, accept_review, analysis_packet, accept_research, report_state, pending_reviews, review_fingerprint
from selection_report import FORMAT
from cover_cache import cache_covers
from product import intake, config_for, review_by_rules, evidence_notes, fetch_prices, simple_brief
from tikhub import TikHubClient
from workflow import search, verify, write_inbox, CachedClient, search_extensions, MAX_SEARCH_ROUNDS
from report import render
from demo import run_demo
from provenance import source_identity
from verification import audit_run

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = {'report.html': 'text/html; charset=utf-8', 'results.csv': 'text/csv; charset=utf-8',
           'comparisons.csv': 'text/csv; charset=utf-8', 'results.json': 'application/json',
           'summary.json': 'application/json', 'candidates.json': 'application/json'}


class ReviewChanged(Exception):
    """Restart semantic validation if a host adds evidence while writing."""


class Session:
    def __init__(self, data_dir, bridge_dir=None, bridge_timeout=600, assistant_mode='host'):
        self.data_dir = Path(data_dir).resolve()
        self.token = secrets.token_urlsafe(32)
        self.key = None
        self.mode = None
        self.credentials = CredentialStore(self.data_dir / 'private', forbidden_roots=[ROOT])
        self.key_remembered = False
        self.key_notice = ''
        try:
            self.key_remembered = bool(self.credentials.load())
            if self.key_remembered: self.mode = 'live'
        except CredentialError as error:
            self.key_notice = str(error)
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
        self.timings = []
        self.bridge = FileBridge(bridge_dir,self.cancelled,bridge_timeout) if bridge_dir else None
        self.automatic = LocalAnalyst(self.cancelled, assistant_mode, bridge_timeout, ROOT) if assistant_mode != 'host' else None
        self.assistant = None
        self.analysis_interrupted = False
        self.source_at_start = source_identity(ROOT)
        self.usable_results = []
        self.run_started = None
        self.first_result_seconds = None
        self.active_store = None

    def select_assistant(self):
        if self.automatic:
            if not self.automatic.available():
                raise StopRun(self.automatic.unavailable_message())
            return self.automatic
        if not self.bridge or not self.bridge.available():
            raise StopRun('当前是宿主兼容模式，分析助手尚未接入。请使用自动分析模式启动，或让宿主助手持续接单；未接入前不会查询付费数据。')
        return self.bridge

    def snapshot(self):
        with self.lock:
            if self.status!='running' and self.run_dir and not (self.run_dir/'.run.lock').exists():
                try:
                    refreshed=read_json(self.run_dir/'summary.json',{})
                    if refreshed and refreshed.get('generated_at')!=(self.summary or {}).get('generated_at'):
                        stored=Store(self.run_dir)
                        readiness=report_state(stored)
                        if refreshed.get('research_status')==readiness['status']:
                            if refreshed.get('report_format') == FORMAT and readiness['status'] == 'complete':
                                checked = audit_run(self.run_dir)
                                atomic_json(self.run_dir / 'verification.json', checked)
                                if checked['status'] != 'PASS':
                                    self.status, self.message = 'error', '报告未通过独立核对，已保留文件，请检查本地验收记录。'
                                    return self._snapshot()
                            self.summary=refreshed
                            self.status='done' if readiness['status']=='complete' else 'needs_analysis'
                            self.message='智能体已更新研究报告。' if self.status=='done' else '阶段报告已更新，仍有证据待补。'
                except (StopRun,OSError,ValueError):
                    pass
            return self._snapshot()

    def _snapshot(self):
        usable = self.usable_results
        if self.active_store and usable:
            from selection_report import eligible_ids
            try:
                current = eligible_ids(self.active_store)
                usable = [row for row in usable if row['target']['id'] in current]
            except (RuntimeError, KeyError, OSError, StopRun):
                # A concurrent update cannot leave a stale qualified card visible.
                usable = []
        return dict(status=self.status, message=self.message, mode=self.mode,
                        key_ready=bool(self.key) or (self.key_remembered and self.credentials.exists()),
                        key_saved=self.credentials.exists(), key_storage_available=self.credentials.available, key_notice=self.key_notice,
                        assistant_connected=bool(self.automatic.available() if self.automatic else self.bridge and self.bridge.available()),
                        assistant_mode=self.automatic.name if self.automatic else 'host',
                        assistant_label=self.automatic.label if self.automatic else '当前智能体',
                        can_resume=bool(self.automatic and self.analysis_interrupted and self.mode=='live' and self.run_dir and self.summary and self.status in ('needs_analysis','error')),
                        pricing=self.pricing, answers=self.answers,
                        summary=self.summary, report_url='/report/report.html' if self.summary else None,
                        output_dir=str(self.run_dir) if self.run_dir else None, version='0.2.0',
                        usable_results=usable)

    def start(self, data):
        with self.lock:
            if self.status == 'running' or (self.worker and self.worker.is_alive()):
                raise StopRun('已有任务正在运行，请等待本轮结束。')
            answers = simple_brief(data) if data.get('description') else intake(data)
            config = None
            if self.mode == 'live':
                if not self.key and self.key_remembered:
                    self.key = self.credentials.load()
                    self.key_remembered = bool(self.key)
                if not self.key:
                    raise StopRun('请先填入本次会话的 TikHub Key。')
                self.assistant = self.select_assistant()
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
            self.analysis_interrupted = False
            self.timings = []
            self.usable_results = []
            self.run_started = time.monotonic()
            self.first_result_seconds = None
            self.active_store = None
            if self.bridge and not self.automatic: atomic_json(self.bridge.folder/'status.json',{'terminal':False})
            self.cancelled.clear()
            self.status, self.message = 'running', '正在准备本轮研究…'
            thread = threading.Thread(target=self.run, args=(config,), daemon=True)
            self.worker = thread
            thread.start()

    def resume_analysis(self):
        with self.lock:
            if self.status == 'running' or (self.worker and self.worker.is_alive()):
                raise StopRun('已有任务正在运行，请等待本轮结束。')
            if not self.automatic or not self.analysis_interrupted or self.mode != 'live' or not self.run_dir or not self.summary or self.status not in ('needs_analysis', 'error'):
                raise StopRun('当前没有可继续的分析任务。')
            self.assistant = self.select_assistant()
            self.cancelled.clear()
            self.analysis_interrupted = False
            self.status, self.message = 'running', '正在接续分析，复用已有数据，不重新查询 TikHub…'
            self.worker = threading.Thread(target=self.run, args=(None, True), daemon=True)
            self.worker.start()

    def timed(self, stage, action):
        start = time.monotonic()
        before_requests = len(self.active_store.reservations()) if self.active_store else 0
        before_models = len(getattr(self.assistant, 'metrics', []))
        before_cache = getattr(self.active_store, 'cache_hits', 0)
        outcome = 'failed'
        try:
            result = action()
            outcome = 'done'
            return result
        finally:
            attempts = getattr(self.assistant, 'metrics', [])[before_models:]
            self.timings.append({'stage':stage,'seconds':round(time.monotonic()-start,3),'status':outcome,'at':now(),
                                 'candidate_count':len(self.active_store.state['candidates']) if self.active_store else 0,
                                 'request_attempts':(len(self.active_store.reservations()) if self.active_store else 0) - before_requests,
                                 'cache_hits':getattr(self.active_store, 'cache_hits', 0) - before_cache,
                                 'model_generations':len(attempts),
                                 'model_retries':sum(row['attempt'] > 1 for row in attempts),
                                 'model_attempts':attempts})
            if self.run_dir and self.run_dir.is_dir():
                try:
                    atomic_json(self.run_dir/'timings.json', {'stages':self.timings,
                        'first_usable_result_seconds':self.first_result_seconds,
                        'elapsed_seconds':round(time.monotonic() - self.run_started, 3) if self.run_started else None})
                except OSError:
                    pass

    def publish_usable(self, store):
        from selection_report import eligible_ids
        rows = []
        for vid in sorted(eligible_ids(store), key=lambda key: -(store.state['results'][key].get('ratio') or 0)):
            result = store.state['results'][vid]
            rows.append({'status':'qualified', 'ratio':result['ratio'], 'play_count':result['play_count'],
                         'median_plays':result['median_plays'],
                         'demo':bool(store.state.get('demo')),
                         'target':{key: store.state['candidates'][vid].get(key) for key in
                                   ('id', 'title', 'author_name', 'followers', 'published_at')}})
        with self.lock:
            self.usable_results = rows
            if rows and self.first_result_seconds is None and self.run_started:
                self.first_result_seconds = round(time.monotonic() - self.run_started, 3)

    def refresh_review(self, store):
        packet = review_packet(store, MAX_SEARCH_ROUNDS - 1)
        packet['remaining_requests'] = 0
        def accept(answer):
            if isinstance(answer, dict) and answer.get('next_keywords'):
                raise StopRun('写作阶段更新证据只重审已有候选，不再补搜。')
            return accept_review(store, answer, expected=packet['review_fingerprints'])
        self.timed('review_changed_evidence', lambda:self.assistant.ask('semantic-review', packet, accept))
        self.publish_usable(store)

    def write_report(self, store):
        for attempt in range(2):
            packet = analysis_packet(store)
            stale = [vid for vid in pending_reviews(store)
                     if store.state['candidates'][vid]['review'].get('decision') in ('include', 'exclude')]
            if stale:
                self.refresh_review(store)
                packet = analysis_packet(store)
            expected = {vid:review_fingerprint(store, vid) for vid in store.state['candidates']}
            def accept(answer):
                if any(review_fingerprint(store, vid) != fingerprint for vid, fingerprint in expected.items()):
                    raise ReviewChanged()
                return accept_research(store, answer, expected_format=FORMAT)
            try:
                return self.timed('write', lambda:self.assistant.ask('content-analysis', packet, accept))
            except ReviewChanged:
                if attempt == 1:
                    raise AssistanceNeeded('写作期间证据连续变化，已保留数据；请稳定证据后继续分析。') from None
                self.refresh_review(store)

    def run(self, config, resume=False):
        store = None
        try:
            if self.mode == 'demo':
                self.message = '正在用合成的 FDE 数据走完整流程；不会联网。'
                store, _ = run_demo(self.run_dir, selection=True)
                self.active_store = store
                summary = render(store)
            else:
                if resume:
                    store = Store(self.run_dir)
                    config = store.config
                self.message = '正在启动自动分析程序…' if self.automatic else '正在确认智能体已经接入本轮研究…'
                def ready(answer):
                    if not isinstance(answer,dict) or answer.get('ready') is not True:
                        raise StopRun('智能体需确认已接入当前研究。')
                    words = answer.get('search_keywords', [] if resume else config['keywords'])
                    if not isinstance(words, list) or (not resume and not 1 <= len(words) <= 3) or any(not isinstance(w,str) or not w.strip() or len(w)>80 for w in words):
                        raise StopRun('首轮搜索词需要1至3个同主题短词。')
                    return list(dict.fromkeys(w.strip() for w in words))
                planned = self.timed('prepare', lambda:self.assistant.ask('prepare-research',{'run_dir':str(self.run_dir),'topic':config['topic'],'scope':config['scope'],'initial_keywords':config['keywords'],'resume':resume,'budget_usd':config['budget_usd'],'max_requests':config['max_requests']},ready))
                if not resume:
                    config['keywords'] = planned
                if not resume:
                    store = Store.create(self.run_dir, config)
                self.active_store = store
                store.on_verified = lambda:self.publish_usable(store)
                with store.lock():
                    if resume:
                        client = CachedClient(store)
                    else:
                        client = CancellableClient(TikHubClient(store, allow_paid=True, api_key=self.key), self.cancelled)
                        self.message = '正在搜索本轮候选…'
                        self.timed('search', lambda:search(store, client, page_limit=1))
                        review_by_rules(store, self.answers)
                        self.publish_usable(store)
                        write_inbox(store)
                        self.summary = render(store)
                    pending = bool(pending_reviews(store))
                    rounds = [MAX_SEARCH_ROUNDS - 1] if resume and pending else [] if resume else range(MAX_SEARCH_ROUNDS)
                    for round_number in rounds:
                        self.message = '智能体正在复核内容相关性，并判断是否需要补搜…'
                        packet = review_packet(store,round_number)
                        if resume:
                            packet['remaining_requests'] = 0
                        def accept(answer):
                            if resume and isinstance(answer,dict) and answer.get('next_keywords'):
                                raise StopRun('接续分析只使用已有数据，不能补搜。')
                            return accept_review(store,answer,expected=packet['review_fingerprints'])
                        response=self.timed('review', lambda:self.assistant.ask('semantic-review',packet,accept))
                        self.publish_usable(store)
                        if any(v['review']['decision']=='include' for v in store.state['candidates'].values()):
                            self.message = '正在同时核对几位作者的作品和播放量…'
                            self.timed('verify', lambda:verify(store, client))
                        if resume or round_number == MAX_SEARCH_ROUNDS - 1:break
                        next_words = search_extensions(store, response['next_keywords'])
                        if not next_words:break
                        self.message = '按智能体的研究判断补充候选，仍使用本轮原有额度…'
                        self.timed('supplement', lambda:search(store,client,next_words,response['search_reason'] or '继续查找产生相关小号的关键词，按剩余额度核验。',page_limit=1))
                        review_by_rules(store,self.answers)
                        self.publish_usable(store)
                        write_inbox(store)
                        self.summary=render(store)
                    if resume and not rounds and any(v['review']['decision']=='include' for v in store.state['candidates'].values()):
                        self.message = '正在用已有数据恢复报告…'
                        verify(store, client)
                    write_inbox(store)
                    self.summary=render(store)
                    self.message = '正在整理值得借鉴的做法和具体案例…'
                    self.write_report(store)
                    self.message = '正在整理原作品封面和图文报告…'
                    self.timed('covers', lambda:cache_covers(store))
                    summary = self.timed('render', lambda:render(store))
            self.publish_usable(store)
            if summary.get('report_complete') and summary.get('report_format') == FORMAT:
                checked = self.timed('verify_delivery', lambda:audit_run(self.run_dir))
                atomic_json(self.run_dir / 'verification.json', checked)
                if checked['status'] != 'PASS':
                    raise StopRun('报告未通过独立核对，已保留数据与验收记录，不能视为完成。')
            with self.lock:
                self.summary = summary
                self.status = 'done' if summary.get('report_complete') else 'needs_analysis'
                self.message = ('选题解读报告已生成。' if summary.get('report_format') == FORMAT else '研究报告已生成。') if summary.get('report_complete') else '已保存阶段报告；仍有内容证据或相关性判断待补充，尚未完成完整研究。'
        except Exception as error:
            # Do not serialize exception bodies from providers or keys.
            message = str(error) if isinstance(error, StopRun) else '本轮未能完成，请保留运行目录并联系维护者。'
            if self.key:
                message = message.replace(self.key, '[REDACTED]')
            if store:
                try:
                    with store.lock():
                        store.state['stops'].append({'at': now(), 'phase': 'wizard', 'message': message})
                        store.save()
                        write_inbox(store)
                        self.summary = render(store)
                except Exception:
                    pass
            with self.lock:
                self.analysis_interrupted = True
                self.status, self.message = ('needs_analysis' if isinstance(error,AssistanceNeeded) else 'error'), message
        finally:
            self.key = None
            if self.bridge and not self.automatic:
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
                  '/wizard.css': ('wizard.css', 'text/css'), '/wizard.js': ('wizard.js', 'text/javascript'),
                  '/journal.css': ('journal.css', 'text/css'),
                  '/journal-background.webp': ('journal-background.webp', 'image/webp'),
                  '/journal-title.woff2': ('journal-title.woff2', 'font/woff2')}
        if path in public:
            if self.guard(auth=False):
                name, kind = public[path]
                self.reply(200, (ROOT / 'assets' / name).read_bytes(), kind)
            return
        if not self.guard():
            return
        if path == '/api/state':
            self.reply(200, self.session.snapshot())
        elif path == '/api/provenance':
            self.reply(200, {'startup': self.session.source_at_start, 'disk': source_identity(ROOT),
                             'report_format': FORMAT,
                             'output_dir': str(self.session.run_dir) if self.session.run_dir else None})
        elif path.startswith('/report/') and path[len('/report/'):] in EXPORTS and self.session.summary:
            name = path[len('/report/'):]
            body = (self.session.run_dir / name).read_bytes()
            if name == 'report.html':
                # Navigation belongs to the live app; standalone reports remain portable.
                navigation = ('<!-- live-navigation:start --><style>.report-nav{display:flex;justify-content:flex-end;margin-bottom:20px}'
                              '.report-nav a{padding:8px 0;font-size:14px}'
                              '@media print{.report-nav{display:none}}</style>'
                              '<nav class="report-nav" aria-label="报告导航">'
                              '<a id="new-research-link" href="/?new=1">新研究 ↗</a></nav><!-- live-navigation:end -->')
                body = body.replace(b'<main>', b'<main>' + navigation.encode('utf-8'), 1)
            self.reply(200, body, EXPORTS[name])
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
                self.reply(200, {'message': '已请求停止，不再发起新查询。正在进行的请求可能已计费；完成保存后会话将自动退出并清除临时 Key；已记住的 Key 会保留，可在“新研究”页面点击“忘记 Key”。'})
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
                    remember = data.get('remember', False)
                    if type(remember) is not bool:
                        raise StopRun('请明确是否记住 Key。')
                    if data['mode'] == 'live':
                        key = validate_key(key)
                        if remember:
                            self.session.credentials.save(key)
                        else:
                            self.session.credentials.forget()
                        self.session.key_remembered = remember
                        self.session.key_notice = ''
                    self.session.mode = data['mode']
                    self.session.key = key if data['mode'] == 'live' else None
                elif path == '/api/forget-key':
                    self.session.credentials.forget()
                    self.session.key = None
                    self.session.key_remembered = False
                    self.session.mode = None
                    self.session.key_notice = '已忘记保存的 Key，下次查询时需要重新填写。'
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
                elif path == '/api/resume-analysis':
                    self.session.resume_analysis()
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
        self.parallel_safe = getattr(client, 'parallel_safe', False)
        if self.parallel_safe:
            client.cancel_event = cancelled

    def call(self, *args, **kwargs):
        if self.cancelled.is_set():
            raise StopRun('用户已结束会话；停止新的查询，已发出的请求仍计入预算。')
        return self.client.call(*args, **kwargs)


def make_server(data_dir, bridge_dir=None, bridge_timeout=600, assistant_mode='auto'):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server.session = Session(data_dir,bridge_dir,bridge_timeout,assistant_mode)
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', default=str(Path.home() / '.local' / 'share' / 'douyin-breakout-finder'))
    parser.add_argument('--ready-file')
    parser.add_argument('--bridge-dir')
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--assistant', choices=MODES, default='auto')
    args = parser.parse_args()
    if os.name != 'nt':
        os.umask(0o077)
    server = make_server(args.data_dir,args.bridge_dir,assistant_mode=args.assistant)
    url = 'http://127.0.0.1:%s/#%s' % (server.server_port, server.session.token)
    ready = {'url': url, 'pid': os.getpid(), 'data_dir': str(server.session.data_dir), 'bridge_dir':args.bridge_dir,
             'source_dir': str(ROOT), 'report_format': FORMAT,
             'source_at_start': server.session.source_at_start,
             'assistant_mode':server.session.automatic.name if server.session.automatic else 'host'}
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
