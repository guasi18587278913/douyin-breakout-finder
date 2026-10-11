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
            ''')
            if c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] != '1':
                raise RuntimeError('Database schema requires an explicit migration.')
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

    def session(self, token):
        if not isinstance(token,str) or len(token)>100: return None
        with self.connection() as c:
            row = c.execute('SELECT s.csrf,s.user_id,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?',
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

    def enqueue(self, user_id, topic, focus, mode='live'):
        if mode not in ('live','demo'):raise InvalidInput('Unknown research mode.')
        now=time.time();job_id=uuid.uuid4().hex
        with self.connection(True) as c:
            if c.execute("SELECT 1 FROM jobs WHERE user_id=? AND status IN ('queued','running')",(user_id,)).fetchone():
                raise Conflict('你已经有一项研究进行中，可以先查看进度。')
            if mode=='live':
                total=c.execute("SELECT count(*) FROM jobs WHERE mode='live' AND created_at>?",(now-86400,)).fetchone()[0]
                own=c.execute("SELECT count(*) FROM jobs WHERE user_id=? AND mode='live' AND created_at>?",(user_id,now-86400)).fetchone()[0]
                if total>=self.settings.daily_live_jobs:raise Conflict('当前试用站的研究额度已用完，请稍后再来。')
                if own>=self.settings.daily_user_jobs:raise Conflict('你近24小时的研究额度已用完。')
            c.execute('INSERT INTO jobs(id,user_id,topic,focus,mode,status,message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                      (job_id,user_id,topic,focus,mode,'queued','已加入研究队列。',now,now))
            c.execute('INSERT INTO audit(at,user_id,action,job_id) VALUES (?,?,?,?)',(now,user_id,'create_job',job_id))
        return self.job(job_id,user_id)

    def recover_interrupted(self):
        with self.connection(True) as c:
            c.execute("UPDATE jobs SET status='interrupted',message='服务已恢复，已保存的数据仍在；可以继续整理报告。',updated_at=? WHERE status='running'",(time.time(),))

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

    def resume(self, job_id, user_id, allow_completed_repair=False):
        with self.connection(True) as c:
            row=c.execute('SELECT * FROM jobs WHERE id=? AND user_id=?',(job_id,user_id)).fetchone()
            if not row:return None
            if row['status'] not in ('interrupted','error','needs_analysis','cancelled') and not (allow_completed_repair and row['status'] in ('done','report_ready')):
                raise Conflict('这项研究当前不需要接续。')
            if c.execute("SELECT 1 FROM jobs WHERE user_id=? AND status IN ('queued','running')",(user_id,)).fetchone():
                raise Conflict('请先等待当前研究结束。')
            c.execute("UPDATE jobs SET status='queued',message='已排队接续，只使用现有数据。',cancel_requested=0,resume_only=1,updated_at=? WHERE id=?",(time.time(),job_id))
        return self.job(job_id,user_id)
