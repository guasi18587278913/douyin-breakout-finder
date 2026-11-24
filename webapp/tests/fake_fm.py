"""测试用的假封面搭子：按接口约定实现授权码、余额、扣分和退款，不连任何真实服务。

网站单元测试用 transport() 当 httpx 的假网络；浏览器测试用 serve() 在本机起一个 HTTP 服务。
"""
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit
import httpx

CLIENT_ID = 'breakout'
RECHARGE_URL = 'https://fm.zmtgzt.com/#/me'


class FakeFm:
    def __init__(self, secret, start_balance=10):
        self.secret, self.start_balance = secret, start_balance
        self.recharge_url = RECHARGE_URL
        self.balances = {}      # subject -> 积分
        self.codes = {}         # 授权码 -> 授权信息
        self.ledger = {}        # reference -> 扣分记录
        self.closed = set()     # 退过（或退一个没扣过的）编号会被关掉，之后不能再扣
        self.calls = []         # 封面搭子收到的 (接口, 请求内容)，按顺序记下
        self.sent = []          # 网站发出的请求（含连不上的），只记接口名
        self.unreachable = set()  # 这些接口连不上（请求没送到）
        self.lost_reply = set()   # 这些接口照常处理，但回应在路上丢了（客户端只看到超时）
        self.before = {}        # 接口名 -> 处理请求前顺带执行的函数，用来模拟并发
        self.lock = threading.RLock()

    def issue_code(self, subject, redirect_uri, ttl=120):
        """模拟用户在封面搭子点了确认：发一个一次性的授权码。"""
        with self.lock:
            self.balances.setdefault(subject, self.start_balance)
            code = secrets.token_urlsafe(24)
            self.codes[code] = {'subject': subject, 'redirect_uri': redirect_uri, 'expires': time.time() + ttl}
        return code

    def count(self, operation):
        return sum(1 for name, _ in self.calls if name == operation)

    def respond(self, operation, authorization, payload):
        """按接口约定处理一次服务器对服务器请求，返回 (状态码, JSON)。"""
        with self.lock:
            self.calls.append((operation, payload))
        if authorization != 'Bearer ' + CLIENT_ID + ':' + self.secret:
            return 401, {'error': 'invalid_client'}
        hook = self.before.get(operation)
        if hook:
            hook(payload)
        with self.lock:
            if operation == 'token':
                grant = self.codes.pop(payload.get('code'), None)
                if not grant or grant['expires'] < time.time() or grant['redirect_uri'] != payload.get('redirect_uri'):
                    return 400, {'error': 'invalid_grant'}
                return 200, {'subject': grant['subject']}
            subject = payload.get('subject')
            if subject not in self.balances:
                return 404, {'error': 'unknown_subject'}
            if operation == 'balance':
                return 200, {'balance': self.balances[subject]}
            if operation == 'charge':
                reference, amount = payload.get('reference'), payload.get('amount')
                assert isinstance(amount, int) and amount > 0 and re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', reference)
                assert len(payload.get('note', '')) <= 60
                if reference in self.closed:
                    return 409, {'error': 'reference_closed'}
                if reference in self.ledger:  # 同一 reference 重复调用不重复扣
                    return 200, {'charged': True, 'balance': self.balances[subject]}
                if self.balances[subject] < amount:
                    return 402, {'error': 'insufficient_credits', 'balance': self.balances[subject], 'recharge_url': self.recharge_url}
                self.balances[subject] -= amount
                self.ledger[reference] = {'subject': subject, 'amount': amount, 'refunded': False}
                return 200, {'charged': True, 'balance': self.balances[subject]}
            if operation == 'refund':
                entry = self.ledger.get(payload.get('reference'))
                self.closed.add(payload.get('reference'))
                if not entry or entry['refunded'] or entry['subject'] != subject:  # 同一 reference 最多退一次
                    return 200, {'refunded': False, 'balance': self.balances[subject]}
                entry['refunded'] = True
                self.balances[subject] += entry['amount']
                return 200, {'refunded': True, 'balance': self.balances[subject]}
        return 404, {'error': 'not_found'}

    def transport(self):
        def handle(request):
            operation = request.url.path.rsplit('/', 1)[-1]
            assert request.method == 'POST' and request.url.path == '/api/connect/' + operation
            self.sent.append(operation)
            if operation in self.unreachable:
                raise httpx.ConnectError('fake fm unreachable', request=request)
            status, body = self.respond(operation, request.headers.get('authorization'), json.loads(request.content or b'{}'))
            if operation in self.lost_reply:
                raise httpx.ReadTimeout('fake fm reply lost', request=request)
            return httpx.Response(status, json=body, headers={'Cache-Control': 'no-store'})
        return httpx.MockTransport(handle)

    def serve(self, port):
        """在 127.0.0.1:port 起假封面搭子。同一个浏览器环境算同一个封面搭子账号，新的浏览器环境是新用户。"""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlsplit(self.path)
                query = {key: values[0] for key, values in parse_qs(url.query).items()}
                if url.path != '/connect/authorize' or query.get('client_id') != CLIENT_ID or not query.get('state'):
                    return self.send_json(404, {'error': 'not_found'})
                cookies = dict(part.strip().split('=', 1) for part in self.headers.get('Cookie', '').split(';') if '=' in part)
                subject = cookies.get('fake_fm_subject') or secrets.token_hex(32)
                code = fake.issue_code(subject, query['redirect_uri'])
                self.send_response(302)
                self.send_header('Location', query['redirect_uri'] + '?' + urlencode({'code': code, 'state': query['state']}))
                self.send_header('Set-Cookie', 'fake_fm_subject=' + subject + '; Path=/; HttpOnly; SameSite=Lax')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                payload = json.loads(self.rfile.read(length) or b'{}')
                operation = urlsplit(self.path).path.rsplit('/', 1)[-1]
                status, body = fake.respond(operation, self.headers.get('Authorization'), payload)
                self.send_json(status, body)

        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        threading.Thread(target=server.serve_forever, name='fake-fm', daemon=True).start()
        return server
