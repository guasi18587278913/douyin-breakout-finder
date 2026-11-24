"""Private users, sessions and durable jobs. Queue/limits use one SQLite transaction."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from contextlib import contextmanager


# 封面搭子登录建立的用户没有本站密码：这个值不是合法的盐和摘要格式，任何密码都比对不上。
FM_ONLY_PASSWORD = '!fm-login-only'
# 发出扣分请求后超过这么久还停在“扣分中”的记录，说明当时进程中断了，按待退处理。
STALE_CHARGE_SECONDS = 120
# 这份研究当前那笔扣分：最新的一笔，没扣成的尝试不算。
CURRENT_CHARGE = "SELECT * FROM credit_charges WHERE job_id=? AND state!='declined' ORDER BY rowid DESC LIMIT 1"
SETTLING = '上一笔积分还在结算，请过一两分钟再接续。'


class Conflict(Exception):
    pass


class InvalidInput(Exception):
    pass


def hashed(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 10 <= len(password) <= 128:
        raise InvalidInput('密码需要10–128个字符。')
    salt = salt or secrets.token_bytes(16)
    result = Scrypt(salt=salt,length=64,n=32768,r=8,p=3).derive(password.encode('utf-8'))
    return base64.b64encode(salt).decode() + ':' + base64.b64encode(result).decode()


def password_matches(password, encoded):
    try:
        salt = base64.b64decode(encoded.split(':')[0], validate=True)
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError, InvalidInput):
        return False


def username(value):
    if not isinstance(value, str): raise InvalidInput('请填写用户名。')
    value = unicodedata.normalize('NFKC', value).strip().casefold()
    if not re.fullmatch(r'[\w.-]{2,40}', value):
        raise InvalidInput('用户名使用2–40个文字、字母、数字或点、短横线。')
    return value


class Database:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_dir / 'site.sqlite3'
        with self.connection() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES ('schema_version', '1');
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
              role TEXT NOT NULL CHECK(role IN ('admin','member')), created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS invites (
              token_hash TEXT PRIMARY KEY, label TEXT NOT NULL, role TEXT NOT NULL,
              created_at REAL NOT NULL, expires_at REAL NOT NULL, consumed_by TEXT REFERENCES users(id));
            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
              csrf TEXT NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), topic TEXT NOT NULL,
              focus TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('live','demo')),
              status TEXT NOT NULL, message TEXT NOT NULL, progress_json TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL, updated_at REAL NOT NULL, finished_at REAL,
              cancel_requested INTEGER NOT NULL DEFAULT 0, resume_only INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS jobs_user_time ON jobs(user_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS rates (bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, user_id TEXT, action TEXT NOT NULL, job_id TEXT);
            -- 2026-11-24 起只用封面搭子账号登录：subject 是封面搭子给本站的账号标识，不含手机号。
            CREATE TABLE IF NOT EXISTS external_accounts (
              subject TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE REFERENCES users(id), created_at REAL NOT NULL);
            -- 真实研究扣的封面搭子积分。一份研究可以有多笔：开始研究扣一笔（reference 就是研究编号），
            -- 退过分之后再接续，用 <研究编号>-r1、-r2… 再扣一笔。“这份研究当前那笔”取最新一笔（没扣成的不算）。
            -- job_id 先生成再扣分，所以这里不设到 jobs 的外键。
            -- charging=已发出扣分请求还没落定；charged=已扣、研究已排队；declined=确认没扣成；
            -- refund_pending=要退、还没和封面搭子确认；refunded=封面搭子确认这笔不再扣着；
            -- void=封面搭子账号已注销，这笔退不了，不再重试。
            CREATE TABLE IF NOT EXISTS credit_charges (
              reference TEXT PRIMARY KEY, job_id TEXT NOT NULL, subject TEXT NOT NULL,
              amount INTEGER NOT NULL CHECK(amount>0),
              state TEXT NOT NULL CHECK(state IN ('charging','charged','declined','refund_pending','refunded','void')),
              attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0, settled_at REAL,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS credit_charges_job ON credit_charges(job_id,created_at);
            CREATE INDEX IF NOT EXISTS credit_charges_due ON credit_charges(state,next_attempt_at);
            ''')
            if c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] != '1':
                raise RuntimeError('Database schema requires an explicit migration.')
            # 0fee398 那版的扣分表以研究编号为主键（那版没有上线）。真遇到这种库就停下，免得按错的结构记账。
            if not any(row['name']=='reference' and row['pk'] for row in c.execute('PRAGMA table_info(credit_charges)')):
                raise RuntimeError('credit_charges 是未上线的旧结构，请先确认这张表为空并删除后再启动。')
        if os.name != 'nt': self.path.chmod(0o600)
        self.dummy_password = password_hash('not-a-real-account-password')

    @contextmanager
    def connection(self, write=False):
        c = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=15000')
        try:
            if write: c.execute('BEGIN IMMEDIATE')
            yield c
            if write: c.execute('COMMIT')
        except BaseException:
            if write and c.in_transaction: c.execute('ROLLBACK')
            raise
        finally:
            c.close()

    def allow(self, bucket, limit, seconds):
        now = time.time()
        with self.connection(True) as c:
            row = c.execute('SELECT * FROM rates WHERE bucket=?', (bucket,)).fetchone()
            if row and row['expires_at'] > now:
                if row['count'] >= limit: return False
                c.execute('UPDATE rates SET count=count+1 WHERE bucket=?', (bucket,))
            else:
                c.execute('INSERT OR REPLACE INTO rates VALUES (?,?,?)', (bucket,1,now+seconds))
            c.execute('DELETE FROM rates WHERE expires_at<?', (now-86400,))
        return True

    def invite(self, label, role='member', hours=48):
        if role not in ('member', 'admin'): raise InvalidInput('Unknown role.')
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.connection(True) as c:
            c.execute('INSERT INTO invites VALUES (?,?,?,?,?,NULL)',
                      (hashed(token), str(label)[:80], role, now, now+hours*3600))
        return token

    def register_public(self, name, password):
        """Open signup always creates a member, including the first public account."""
        name = username(name)
        encoded = password_hash(password)
        user_id = uuid.uuid4().hex
        with self.connection(True) as c:
            try:
                c.execute('INSERT INTO users VALUES (?,?,?,?,?)', (user_id,name,encoded,'member',time.time()))
            except sqlite3.IntegrityError:
                raise Conflict('这个用户名已被使用，请换一个。') from None
            c.execute('INSERT INTO audit(at,user_id,action) VALUES (?,?,?)', (time.time(),user_id,'register'))
        return self.user(user_id)

    def register(self, invitation, name, password):
        name = username(name)
        if not isinstance(invitation, str) or not 30 <= len(invitation) <= 100:
            raise InvalidInput('邀请码无效或已经使用。')
        with self.connection() as c:
            invite = c.execute('SELECT * FROM invites WHERE token_hash=?', (hashed(invitation),)).fetchone()
        if not invite or invite['consumed_by'] or invite['expires_at'] <= time.time():
            raise InvalidInput('邀请码无效或已经使用。')
        encoded = password_hash(password)
        user_id = uuid.uuid4().hex
        with self.connection(True) as c:
            invite = c.execute('SELECT * FROM invites WHERE token_hash=?', (hashed(invitation),)).fetchone()
            if not invite or invite['consumed_by'] or invite['expires_at'] <= time.time():
                raise InvalidInput('邀请码无效或已经使用。')
            try:
                c.execute('INSERT INTO users VALUES (?,?,?,?,?)', (user_id,name,encoded,invite['role'],time.time()))
            except sqlite3.IntegrityError:
                raise Conflict('这个用户名已被使用，请换一个。') from None
            c.execute('UPDATE invites SET consumed_by=? WHERE token_hash=?', (user_id,hashed(invitation)))
            c.execute('INSERT INTO audit(at,user_id,action) VALUES (?,?,?)', (time.time(),user_id,'register'))
        return self.user(user_id)

    def user(self, user_id):
        with self.connection() as c:
            row = c.execute('SELECT id,username,role FROM users WHERE id=?', (user_id,)).fetchone()
        return dict(row) if row else None

    def login(self, name, password):
        try: name = username(name)
        except InvalidInput: name = ''
        with self.connection() as c:
            row = c.execute('SELECT * FROM users WHERE username=?', (name,)).fetchone()
        okay = password_matches(password, row['password_hash'] if row else self.dummy_password)
        return self.user(row['id']) if row and okay else None

    def create_session(self, user_id):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection(True) as c:
            c.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                      (hashed(token),user_id,csrf,time.time()+self.settings.session_days*86400))
            c.execute('DELETE FROM sessions WHERE expires_at<?', (time.time(),))
        return token, csrf

    def external_user(self, subject):
        """用封面搭子的 subject 找到本站用户；第一次登录时新建一个普通成员。"""
        now = time.time()
        with self.connection(True) as c:
            row = c.execute('SELECT user_id FROM external_accounts WHERE subject=?', (subject,)).fetchone()
            if row:
                user_id = row['user_id']
            else:
                user_id = uuid.uuid4().hex
                # 用户名取随机的用户编号，不从 subject 推出来；旧账号的用户名只允许文字、字母、数字、点和短横线，
                # 带冒号的名字不会和它们撞上。
                name = 'fm:' + user_id[:16]
                if c.execute('SELECT 1 FROM users WHERE username=?', (name,)).fetchone(): name = 'fm:' + user_id
                c.execute('INSERT INTO users VALUES (?,?,?,?,?)', (user_id,name,FM_ONLY_PASSWORD,'member',now))
                c.execute('INSERT INTO external_accounts VALUES (?,?,?)', (subject,user_id,now))
                c.execute('INSERT INTO audit(at,user_id,action) VALUES (?,?,?)', (now,user_id,'fm_register'))
            c.execute('INSERT INTO audit(at,user_id,action) VALUES (?,?,?)', (now,user_id,'fm_login'))
        return self.user(user_id)

    def session(self, token):
        if not isinstance(token,str) or len(token)>100: return None
        # 只认绑定了封面搭子账号的用户：旧的用户名密码账号即使会话没过期也立即失效。
        with self.connection() as c:
            row = c.execute('SELECT s.csrf,s.user_id,u.username,u.role,e.subject FROM sessions s JOIN users u ON u.id=s.user_id '
                            'JOIN external_accounts e ON e.user_id=u.id WHERE s.token_hash=? AND s.expires_at>?',
                            (hashed(token),time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, token):
        with self.connection(True) as c: c.execute('DELETE FROM sessions WHERE token_hash=?', (hashed(token),))

    def jobs(self, user_id):
        with self.connection() as c:
            rows = c.execute('SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 100', (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def job(self, job_id, user_id=None):
        if not re.fullmatch(r'[a-f0-9]{32}',job_id): return None
        with self.connection() as c:
            if user_id is None: row=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            else: row=c.execute('SELECT * FROM jobs WHERE id=? AND user_id=?',(job_id,user_id)).fetchone()
        return dict(row) if row else None

    def remaining(self, user_id):
        with self.connection() as c:
            count=c.execute("SELECT count(*) FROM jobs WHERE user_id=? AND mode='live' AND created_at>?",(user_id,time.time()-86400)).fetchone()[0]
        return max(0,self.settings.daily_user_jobs-count)

    def _admit(self, c, user_id, mode, now):
        if c.execute("SELECT 1 FROM jobs WHERE user_id=? AND status IN ('queued','running')",(user_id,)).fetchone():
            raise Conflict('你已经有一项研究进行中，可以先查看进度。')
        if mode=='live':
            total=c.execute("SELECT count(*) FROM jobs WHERE mode='live' AND created_at>?",(now-86400,)).fetchone()[0]
            own=c.execute("SELECT count(*) FROM jobs WHERE user_id=? AND mode='live' AND created_at>?",(user_id,now-86400)).fetchone()[0]
            if total>=self.settings.daily_live_jobs:raise Conflict('当前试用站的研究额度已用完，请稍后再来。')
            if own>=self.settings.daily_user_jobs:raise Conflict('你近24小时的研究额度已用完。')

    def admit(self, user_id, mode='live'):
        """扣分前的预检，规则和入队完全相同；真正入队时还会在写事务里再查一遍。"""
        with self.connection() as c: self._admit(c,user_id,mode,time.time())

    def enqueue(self, user_id, topic, focus, mode='live', job_id=None, charged=None):
        if mode not in ('live','demo'):raise InvalidInput('Unknown research mode.')
        now=time.time();job_id=job_id or uuid.uuid4().hex
        with self.connection(True) as c:
            self._admit(c,user_id,mode,now)
            c.execute('INSERT INTO jobs(id,user_id,topic,focus,mode,status,message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                      (job_id,user_id,topic,focus,mode,'queued','已加入研究队列。',now,now))
            c.execute('INSERT INTO audit(at,user_id,action,job_id) VALUES (?,?,?,?)',(now,user_id,'create_job',job_id))
            # 已经扣过分的真实研究：入队和“已扣”记账在同一个事务里，要么都成、要么都不成。charged 是这次扣分的编号。
            if charged and c.execute("UPDATE credit_charges SET state='charged',updated_at=? WHERE reference=? AND job_id=? AND state='charging'",(now,charged,job_id)).rowcount!=1:
                raise Conflict('积分记录对不上，这次研究没有开始。')
        return self.job(job_id,user_id)

    def recover_interrupted(self):
        # 记下结束时间：被重启打断的研究和出错、停止一样结算积分，没有可用报告就退（接续照旧不扣分）。
        with self.connection(True) as c:
            c.execute("UPDATE jobs SET status='interrupted',message='服务已恢复，已保存的数据仍在；可以继续整理报告。',updated_at=?,finished_at=? WHERE status='running'",(time.time(),time.time()))

    def claim(self):
        with self.connection(True) as c:
            if c.execute("SELECT 1 FROM jobs WHERE status='running'").fetchone():return None
            row=c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:return None
            c.execute("UPDATE jobs SET status='running',updated_at=? WHERE id=? AND status='queued'",(time.time(),row['id']))
            return dict(row)

    def progress(self, job_id, message, public):
        with self.connection(True) as c:
            c.execute("UPDATE jobs SET message=?,progress_json=?,updated_at=? WHERE id=? AND status='running'",
                      (message,json.dumps(public,ensure_ascii=False),time.time(),job_id))

    def finish(self, job_id, status, message, public):
        with self.connection(True) as c:
            c.execute('UPDATE jobs SET status=?,message=?,progress_json=?,updated_at=?,finished_at=? WHERE id=?',
                      (status,message,json.dumps(public,ensure_ascii=False),time.time(),time.time(),job_id))

    def cancel(self, job_id, user_id):
        with self.connection(True) as c:
            row=c.execute('SELECT * FROM jobs WHERE id=? AND user_id=?',(job_id,user_id)).fetchone()
            if not row:return None
            if row['status']=='queued':
                c.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,message='已取消，不再开始新的查询。',updated_at=?,finished_at=? WHERE id=?",(time.time(),time.time(),job_id))
            elif row['status']=='running':
                c.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?',(job_id,))
        return self.job(job_id,user_id)

    def resume(self, job_id, user_id, allow_completed_repair=False, charged=None):
        now=time.time()
        with self.connection(True) as c:
            row=c.execute('SELECT * FROM jobs WHERE id=? AND user_id=?',(job_id,user_id)).fetchone()
            if not row:return None
            if row['status'] not in ('interrupted','error','needs_analysis','cancelled') and not (allow_completed_repair and row['status'] in ('done','report_ready')):
                raise Conflict('这项研究当前不需要接续。')
            if c.execute("SELECT 1 FROM jobs WHERE user_id=? AND status IN ('queued','running')",(user_id,)).fetchone():
                raise Conflict('请先等待当前研究结束。')
            if charged:
                # 这次接续刚扣的那笔：和排队放在同一个事务里记为已扣。
                if c.execute("UPDATE credit_charges SET state='charged',updated_at=? WHERE reference=? AND job_id=? AND state='charging'",(now,charged,job_id)).rowcount!=1:
                    raise Conflict('积分记录对不上，这次接续没有开始。')
            else:
                # 扣过分的研究不另扣分接续时，当前那笔必须还扣着；后台刚把它改成待退的话，这次不接续。
                current=c.execute(CURRENT_CHARGE,(job_id,)).fetchone()
                if current and current['state']!='charged':raise Conflict(SETTLING)
            c.execute("UPDATE jobs SET status='queued',message='已排队接续，只使用现有数据。',cancel_requested=0,resume_only=1,updated_at=? WHERE id=?",(time.time(),job_id))
        return self.job(job_id,user_id)

    # ---- 封面搭子积分记账（按扣分编号 reference 记，一份研究可以有多笔） ----

    def begin_charge(self, job_id, subject, amount, reference=None):
        """开始研究扣分前先记一笔“扣分中”（reference 就是研究编号），进程中途中断也能查到、能退。"""
        now=time.time()
        with self.connection(True) as c:
            try:
                c.execute('INSERT INTO credit_charges(reference,job_id,subject,amount,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                          (reference or job_id,job_id,subject,amount,'charging',now,now))
            except sqlite3.IntegrityError:
                raise Conflict(SETTLING) from None

    def credit_charge(self, job_id):
        """这份研究当前那笔：最新的一笔，没扣成的尝试不算。"""
        with self.connection() as c:
            row=c.execute(CURRENT_CHARGE,(job_id,)).fetchone()
        return dict(row) if row else None

    def credit_charges(self, job_id):
        with self.connection() as c:
            return [dict(row) for row in c.execute('SELECT * FROM credit_charges WHERE job_id=? ORDER BY rowid',(job_id,))]

    def begin_resume_charge(self, job_id, subject, amount):
        """退过分的研究再接续前，先记一笔新的“扣分中”，返回编号 <研究编号>-r1、-r2…（每次都不重复，符合封面搭子的
        [A-Za-z0-9_.:-]{1,80}）。同一个写事务里确认当前那笔确实已退（或作废）、没有别的扣分在进行，
        同时点两次接续也只会有一笔在扣。"""
        now=time.time()
        with self.connection(True) as c:
            current=c.execute(CURRENT_CHARGE,(job_id,)).fetchone()
            if not current or current['state'] not in ('refunded','void'):raise Conflict(SETTLING)
            count=c.execute('SELECT count(*) FROM credit_charges WHERE job_id=?',(job_id,)).fetchone()[0]
            reference=job_id+'-r'+str(count)
            c.execute('INSERT INTO credit_charges(reference,job_id,subject,amount,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                      (reference,job_id,subject,amount,'charging',now,now))
        return reference

    def decline_charge(self, reference):
        """封面搭子明确没扣（余额不足、编号已关闭、拒绝请求或根本没连上）。"""
        with self.connection(True) as c:
            c.execute("UPDATE credit_charges SET state='declined',updated_at=? WHERE reference=? AND state='charging'",(time.time(),reference))

    def queue_refund(self, reference, states, delay=0):
        """把处在 states 里的扣分记录改成待退；返回是否真的改到了。"""
        now=time.time();marks=','.join('?'*len(states))
        with self.connection(True) as c:
            return c.execute('UPDATE credit_charges SET state=\'refund_pending\',next_attempt_at=?,updated_at=? WHERE reference=? AND state IN ('+marks+')',
                             (now+delay,now,reference,*states)).rowcount==1

    def queue_settle_refund(self, reference):
        """研究结束后没有可用报告：把还扣着的这笔改成待退。研究要是已经被接续（排队或运行中）就不动，
        这个条件和改状态在同一条 SQL 里，后台结算和用户点接续同时发生也不会让接续白跑。"""
        now=time.time()
        with self.connection(True) as c:
            return c.execute("UPDATE credit_charges SET state='refund_pending',next_attempt_at=?,updated_at=? WHERE reference=? AND state='charged' "
                             "AND EXISTS (SELECT 1 FROM jobs j WHERE j.id=credit_charges.job_id AND j.status NOT IN ('queued','running'))",
                             (now,now,reference)).rowcount==1

    def claim_refund(self, reference, lease):
        """领取一次退款尝试：到期的待退记录先顺延 lease 秒，免得两处同时去退。"""
        now=time.time()
        with self.connection(True) as c:
            row=c.execute("SELECT * FROM credit_charges WHERE reference=? AND state='refund_pending' AND next_attempt_at<=?",(reference,now)).fetchone()
            if not row:return None
            c.execute('UPDATE credit_charges SET attempts=attempts+1,next_attempt_at=?,updated_at=? WHERE reference=?',(now+lease,now,reference))
        return dict(row)

    def finish_refund(self, reference):
        with self.connection(True) as c:
            c.execute("UPDATE credit_charges SET state='refunded',updated_at=? WHERE reference=? AND state='refund_pending'",(time.time(),reference))

    def void_charge(self, reference):
        """封面搭子账号已注销，这笔退不了：记为终态，不再重试。"""
        with self.connection(True) as c:
            c.execute("UPDATE credit_charges SET state='void',updated_at=? WHERE reference=? AND state='refund_pending'",(time.time(),reference))

    def defer_charge(self, reference, seconds):
        """这次没处理成，过 seconds 秒再试。"""
        now=time.time()
        with self.connection(True) as c:
            c.execute('UPDATE credit_charges SET next_attempt_at=?,updated_at=? WHERE reference=?',(now+seconds,now,reference))

    def mark_settled(self, reference):
        """研究已有可用报告，这次结束后不退分；以后接续再结束时会重新核对。"""
        now=time.time()
        with self.connection(True) as c:
            c.execute("UPDATE credit_charges SET settled_at=?,updated_at=? WHERE reference=? AND state='charged'",(now,now,reference))

    def unsettled_charges(self, limit=20):
        """已扣分、研究已结束，但结束后还没核对过要不要退的研究编号（含当时核对出错、到点重试的）。"""
        with self.connection() as c:
            rows=c.execute("SELECT c.job_id FROM credit_charges c JOIN jobs j ON j.id=c.job_id WHERE c.state='charged' "
                           "AND j.status NOT IN ('queued','running') AND j.finished_at IS NOT NULL "
                           "AND (c.settled_at IS NULL OR c.settled_at<j.finished_at) AND c.next_attempt_at<=? LIMIT ?",
                           (time.time(),limit)).fetchall()
        return [row['job_id'] for row in rows]

    def due_refunds(self, limit=20):
        """到点该退的扣分编号。"""
        now=time.time()
        with self.connection() as c:
            stale=c.execute("SELECT 1 FROM credit_charges WHERE state='charging' AND created_at<? LIMIT 1",(now-STALE_CHARGE_SECONDS,)).fetchone()
        if stale:
            # 扣分请求发出后进程中断：结果不明，一律去退（没扣成的编号退款时封面搭子会答“没有可退的”，并关掉这个编号）。
            with self.connection(True) as c:
                c.execute("UPDATE credit_charges SET state='refund_pending',next_attempt_at=?,updated_at=? WHERE state='charging' AND created_at<?",
                          (now,now,now-STALE_CHARGE_SECONDS))
        with self.connection() as c:
            rows=c.execute("SELECT reference FROM credit_charges WHERE state='refund_pending' AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?",
                           (now,limit)).fetchall()
        return [row['reference'] for row in rows]

    # ---- 服务器维护命令用 ----

    def fm_accounts(self):
        """封面搭子账号对应的本站用户；不取 subject。"""
        with self.connection() as c:
            rows=c.execute("SELECT u.id,u.created_at,u.role,"
                           "(SELECT max(a.at) FROM audit a WHERE a.user_id=u.id AND a.action='fm_login') AS last_login,"
                           "(SELECT count(*) FROM jobs j WHERE j.user_id=u.id) AS jobs "
                           "FROM external_accounts e JOIN users u ON u.id=e.user_id ORDER BY u.created_at").fetchall()
        return [dict(row) for row in rows]

    def set_role(self, user_id, role):
        """设或撤管理员并写审计；只能给绑定了封面搭子的账号设管理员。返回角色是否真的变了。"""
        if role not in ('admin','member'):raise InvalidInput('Unknown role.')
        with self.connection(True) as c:
            row=c.execute('SELECT u.role,e.user_id IS NOT NULL AS linked FROM users u LEFT JOIN external_accounts e ON e.user_id=u.id WHERE u.id=?',(user_id,)).fetchone()
            if not row:raise InvalidInput('没有找到这个用户编号。')
            if role=='admin' and not row['linked']:raise InvalidInput('只能给用封面搭子手机号登录过的账号设管理员；旧的用户名密码账号已停用。')
            if row['role']==role:return False
            c.execute('UPDATE users SET role=? WHERE id=?',(role,user_id))
            c.execute('INSERT INTO audit(at,user_id,action) VALUES (?,?,?)',(time.time(),user_id,'grant_admin' if role=='admin' else 'revoke_admin'))
        return True
