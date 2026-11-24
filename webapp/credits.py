"""封面搭子账号接入：授权码换账号标识、查积分、扣分和退款。

只走封面搭子约定的服务器对服务器接口，拿不到也不保存手机号。接入密钥只放在请求头里，
不进日志、不进报错。真实研究开始前按研究编号扣分；研究最终没有可用报告就退回，
退款没成功的记录留在数据库里，由后台循环按退避时间重试，不会丢。
"""
import logging
import re
import threading
import time
from urllib.parse import urlencode, urlsplit
import httpx

log = logging.getLogger(__name__)
CLIENT_ID = 'breakout'
SUBJECT = re.compile(r'[0-9a-fA-F]{64}')
STATE = re.compile(r'[A-Za-z0-9_-]{16,200}')
CODE = re.compile(r'[\x21-\x7e]{1,512}')
NOTE = '小号爆款·真实研究'
TIMEOUT = 10          # 换码、扣分、退款的超时秒数
BALANCE_TIMEOUT = 3   # 查余额会挡住首页加载，超时设短一些
BALANCE_TTL = 30      # 余额按用户缓存的秒数（查不到也缓存，免得封面搭子出问题时每次打开页面都等）
UNSURE_DELAY = 60     # 扣分结果不确定时，等这么久再去退，免得退款比迟到的扣分先到
REFUND_LEASE = 60     # 领到一次退款尝试后，这段时间内别处不再重复去退
SETTLE_RETRY = 300    # 研究结束后核对报告出错时，隔多久再核对


class FmUnavailable(Exception):
    """连不上封面搭子、超时，或回应看不懂。sent=False 表示请求肯定没送到对方。"""
    def __init__(self, sent=True):
        super().__init__('封面搭子暂时连不上。')
        self.sent = sent


class FmRejected(Exception):
    """封面搭子明确拒绝了请求，例如授权码无效、接入密钥不对、账号不存在。"""
    def __init__(self, status, code):
        super().__init__('封面搭子拒绝了这次请求。')
        self.status, self.code = status, code


class InsufficientCredits(Exception):
    def __init__(self, balance, recharge_url):
        super().__init__('积分不够。')
        self.balance, self.recharge_url = balance, recharge_url


def whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def backoff(attempts):
    """第 1 次失败后隔 30 秒，之后每次翻倍，最长 1 小时。"""
    return min(3600, 30 * 2 ** min(attempts, 7))


class FmClient:
    def __init__(self, settings, transport=None):
        self.base = settings.fm_base
        self.secret = settings.fm_client_secret
        self.redirect_uri = settings.public_url + '/auth/callback'
        self.transport = transport

    def authorize_url(self, state):
        return self.base + '/connect/authorize?' + urlencode(
            {'client_id': CLIENT_ID, 'redirect_uri': self.redirect_uri, 'state': state})

    def link(self, value):
        """充值链接只接受和 BREAKOUT_FM_BASE 同源的地址（正式环境即 https），其余一律不用，由调用方换成默认的 /#/me。"""
        if not isinstance(value, str) or not 0 < len(value) <= 500 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            return None
        try:
            parts, base = urlsplit(value), urlsplit(self.base)
            same = (parts.scheme, parts.hostname, parts.port) == (base.scheme, base.hostname, base.port)
        except ValueError:
            return None
        return value if same and not parts.username and not parts.password else None

    def call(self, operation, payload, timeout=TIMEOUT):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                response = client.post(self.base + '/api/connect/' + operation, json=payload,
                                       headers={'Authorization': 'Bearer ' + CLIENT_ID + ':' + self.secret,
                                                'Accept': 'application/json'})
        except httpx.HTTPError as error:
            # 日志只记接口名和出错类型，不记请求内容、subject 和密钥。
            log.warning('封面搭子接口 %s 没有回应：%s', operation, type(error).__name__)
            raise FmUnavailable(sent=not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout))) from None
        try:
            data = response.json()
        except ValueError:
            data = None
        return response.status_code, data if isinstance(data, dict) else {}

    @staticmethod
    def failure(operation, status, data):
        # 4xx 说明对方收到并拒绝了，什么都没改；5xx、跳转或看不懂的回应都算“结果不确定”。
        # 日志记接口名、状态码和对方的错误码，方便排查密钥填错、回调地址没登记等问题。
        code = re.sub(r'[^A-Za-z0-9_.-]', '', str(data.get('error', '')))[:40]
        log.warning('封面搭子接口 %s 未成功：HTTP %s %s', operation, status, code)
        if 400 <= status < 500:
            return FmRejected(status, code)
        return FmUnavailable()

    def exchange(self, code):
        status, data = self.call('token', {'code': code, 'redirect_uri': self.redirect_uri})
        subject = data.get('subject')
        if status == 200 and isinstance(subject, str) and SUBJECT.fullmatch(subject):
            return subject
        raise self.failure('token', status, data)

    def balance(self, subject):
        status, data = self.call('balance', {'subject': subject}, timeout=BALANCE_TIMEOUT)
        if status == 200 and whole(data.get('balance')):
            return data['balance']
        raise self.failure('balance', status, data)

    def charge(self, subject, amount, reference):
        status, data = self.call('charge', {'subject': subject, 'amount': amount, 'reference': reference, 'note': NOTE})
        if status == 200 and data.get('charged') is True and whole(data.get('balance')):
            return data['balance']
        if status == 402:
            balance = data.get('balance')
            raise InsufficientCredits(balance if whole(balance) else None, self.link(data.get('recharge_url')))
        raise self.failure('charge', status, data)

    def refund(self, subject, reference):
        status, data = self.call('refund', {'subject': subject, 'reference': reference})
        if status == 200 and isinstance(data.get('refunded'), bool) and whole(data.get('balance')):
            return data['refunded'], data['balance']
        raise self.failure('refund', status, data)


class Credits:
    """真实研究的积分记账。delivered(job) 判断研究是否已有可用报告，由网页入口按 public_job 的结论提供。"""

    def __init__(self, settings, db, client, delivered):
        self.db, self.client, self.delivered = db, client, delivered
        self.cost = settings.research_credits
        self.recharge_url = settings.fm_base + '/#/me'
        self.cache = {}
        self.lock = threading.Lock()
        self.clock = time.monotonic

    def remember(self, subject, balance):
        now = self.clock()
        with self.lock:
            if len(self.cache) > 1000:
                self.cache = {key: value for key, value in self.cache.items() if value[0] > now}
            self.cache[subject] = (now + BALANCE_TTL, balance)

    def balance(self, subject):
        """当前积分；查不到时返回 None，不影响页面。"""
        with self.lock:
            cached = self.cache.get(subject)
        if cached and cached[0] > self.clock():
            return cached[1]
        try:
            value = self.client.balance(subject)
        except (FmUnavailable, FmRejected):
            value = None
        self.remember(subject, value)
        return value

    def charge(self, subject, job_id):
        """真实研究入队前扣分（编号就是研究编号），扣成功才返回新余额；其他情况抛错，由调用方给人话提示。"""
        self.db.begin_charge(job_id, subject, self.cost)
        return self.collect(subject, job_id)

    def recharge(self, subject, job_id):
        """退过分的研究再接续前重新扣一次，返回这次的编号 <研究编号>-rN。当前那笔还没退完时抛 Conflict。"""
        reference = self.db.begin_resume_charge(job_id, subject, self.cost)
        self.collect(subject, reference)
        return reference

    def collect(self, subject, reference):
        """向封面搭子扣已记成“扣分中”的这一笔，并按结果更新记录。"""
        try:
            balance = self.client.charge(subject, self.cost, reference)
        except InsufficientCredits as error:
            self.db.decline_charge(reference)
            if error.balance is not None:
                self.remember(subject, error.balance)
            raise
        except FmRejected:
            # 包括 409 reference_closed（这个编号已经退过或关闭了）：按没扣成处理。
            self.db.decline_charge(reference)
            raise
        except FmUnavailable as error:
            if error.sent:
                # 请求可能已经扣成、只是回应丢了：记为待退，稍后去退（对没扣成的编号，退款不会多给）。
                self.db.queue_refund(reference, ('charging',), delay=UNSURE_DELAY)
            else:
                self.db.decline_charge(reference)
            raise
        self.remember(subject, balance)
        return balance

    def resume_cost(self, job_id):
        """接续这份研究要用几分：当前那笔还扣着（或根本没扣过分，比如合成示例）就是 0，否则要重新扣一次。"""
        current = self.db.credit_charge(job_id)
        return 0 if current is None or current['state'] == 'charged' else self.cost

    def refund_now(self, reference, states):
        """把处在 states 里的那笔改成待退并马上试一次。返回 True 已确认退回，False 这次没退成（留给后台重试），
        None 没有要退的、别处正在退，或封面搭子账号已注销退不了。"""
        if not self.db.queue_refund(reference, states):
            return None
        return self.attempt(reference)

    def settle(self, job_id):
        """研究结束（含排队时被取消、被重启打断）后调用：当前那笔还扣着、却没有可用报告，就退回。返回值同 refund_now。"""
        job = self.db.job(job_id)
        if not job or job['status'] in ('queued', 'running') or job['finished_at'] is None:
            return None
        row = self.db.credit_charge(job_id)
        if not row or row['state'] != 'charged':
            return None
        try:
            delivered = self.delivered(job)
        except Exception:
            self.db.defer_charge(row['reference'], SETTLE_RETRY)
            raise
        if delivered:
            self.db.mark_settled(row['reference'])
            return None
        # 改待退时同一条 SQL 再确认研究没被接续；用户刚点了接续的话，这笔继续留给接续那次用。
        if not self.db.queue_settle_refund(row['reference'], job['finished_at']):
            return None
        return self.attempt(row['reference'])

    def attempt(self, reference):
        """去退一笔到期的待退记录：True 已确认退回，False 封面搭子没退成，None 不到期、别处正在退或账号已注销。"""
        row = self.db.claim_refund(reference, REFUND_LEASE)
        if not row:
            return None
        try:
            _, balance = self.client.refund(row['subject'], reference)
        except FmRejected as error:
            if error.status == 404 and error.code == 'unknown_subject':
                # 用户在封面搭子注销了账号：这笔退不了，记为终态，不再重试。
                self.db.void_charge(reference)
                log.warning('封面搭子账号已注销，这笔积分退不了，已停止重试：%s', reference)
                return None
            self.db.defer_charge(reference, backoff(row['attempts']))
            log.warning('积分退回暂未成功，稍后重试：%s', reference)
            return False
        except FmUnavailable:
            self.db.defer_charge(reference, backoff(row['attempts']))
            log.warning('积分退回暂未成功，稍后重试：%s', reference)
            return False
        self.db.finish_refund(reference)
        self.remember(row['subject'], balance)
        return True

    def retry_due(self):
        """后台循环调用：补核对结束后没来得及结算的研究，再补退之前没退成的积分。
        封面搭子一次没退成就先停这一轮，免得连续等超时、拖住后面排队的研究。"""
        for job_id in self.db.unsettled_charges():
            try:
                if self.settle(job_id) is False:
                    return
            except Exception:
                log.error('研究结束后的积分核对出错，稍后重试：%s', job_id)
        for reference in self.db.due_refunds():
            if self.attempt(reference) is False:
                return
