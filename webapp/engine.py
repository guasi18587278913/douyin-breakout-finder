"""One durable worker. Reuses the same verification and report engine as the Skill."""
import fcntl
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT/'scripts') not in sys.path:sys.path.insert(0,str(ROOT/'scripts'))
from core import Store, StopRun, read_json
from product import simple_brief, config_for, fetch_prices
from wizard import Session
from webapp.remote_analyst import RemoteAnalyst

STAGES={'prepare':'正在确定贴近主题的搜索词','search':'正在搜索相关作品','review':'正在判断哪些作品值得继续核验',
        'verify':'正在核对账号作品和播放量','supplement':'正在补充同主题的候选作品','write':'正在整理案例与创作方法',
        'covers':'正在整理作品封面','render':'正在排版选题解读','verify_delivery':'正在逐项核对报告',
        'review_changed_evidence':'正在更新作品判断'}


def run_dir(settings,job_id):return settings.data_dir/'runs'/job_id/'research'


def public_progress(session):
    store=session.active_store
    return {'stage':getattr(session,'stage','prepare'),'candidates':len(store.state['candidates']) if store else 0,
            'qualified':len(session.usable_results),'usable_results':session.usable_results,
            'elapsed_seconds':round(time.monotonic()-session.run_started) if session.run_started else 0,
            'first_result_seconds':session.first_result_seconds}


class HostedSession(Session):
    def __init__(self,folder,db,job):
        super().__init__(folder,assistant_mode='host')
        self.db,self.job,self.stage=db,job,'prepare'
    def timed(self,stage,action):
        self.stage=stage
        self.db.progress(self.job['id'],STAGES.get(stage,'正在整理研究结果'),public_progress(self))
        return super().timed(stage,action)
    def publish_usable(self,store):
        super().publish_usable(store)
        self.db.progress(self.job['id'],STAGES.get(self.stage,'正在整理研究结果'),public_progress(self))


class Worker:
    def __init__(self,settings,db,vault):
        self.settings,self.db,self.vault=settings,db,vault
        self.stop=threading.Event();self.current={};self.lock=threading.Lock();self.thread=None;self.lease=None
    def start(self):
        self.lease=(self.settings.data_dir/'worker.lock').open('a')
        try:fcntl.flock(self.lease.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.lease.close();raise RuntimeError('Another worker owns this database.') from None
        from webapp.cached_analysis import recover_publications
        recover_publications(self.settings.data_dir)
        self.db.recover_interrupted()
        self.thread=threading.Thread(target=self.loop,name='research-worker',daemon=True);self.thread.start()
    def close(self):
        self.stop.set()
        with self.lock:
            for event in self.current.values():event.set()
        if self.thread:self.thread.join(timeout=3)
        # Keep the process lease while a network call is still unwinding.
        if self.lease and (not self.thread or not self.thread.is_alive()):self.lease.close()
    def cancel(self,job_id):
        with self.lock:
            event=self.current.get(job_id)
            if event:event.set()
    def healthy(self):
        return bool(self.thread and self.thread.is_alive() and not self.stop.is_set())
    def loop(self):
        recover=False
        while not self.stop.is_set():
            try:
                if recover:
                    self.db.recover_interrupted();recover=False
                job=self.db.claim()
                if job:self.execute(job)
                else:self.stop.wait(.5)
            except Exception:
                # Never include provider bodies or credentials in service logs.
                logging.getLogger(__name__).error('Worker interrupted; preserving jobs for cached recovery.')
                recover=True
                self.stop.wait(3)
    def execute(self,job):
        session=None
        try:
            folder=run_dir(self.settings,job['id']).parent
            folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            session=HostedSession(folder,self.db,job)
            session.mode=job['mode'];session.run_dir=folder/'research'
            session.run_started=time.monotonic();session.status='running'
            session.answers=simple_brief({'description':job['topic'],'focus':job['focus']})
            with self.lock:self.current[job['id']]=session.cancelled
            current=self.db.job(job['id'])
            if current['cancel_requested'] or self.stop.is_set():session.cancelled.set()
            if session.cancelled.is_set():raise StopRun('已停止研究，保留已经取得的数据。')
            config=None
            if job['mode']=='live':
                keys=self.vault.get()
                if not keys.get('deepseek_key') or (not job['resume_only'] and not keys.get('tikhub_key')):
                    raise StopRun('研究服务尚未配置完成，请联系管理员。')
                session.automatic=RemoteAnalyst(session.cancelled,keys['deepseek_key'],folder/'model-events.jsonl')
                session.assistant=session.automatic;session.key=keys.get('tikhub_key')
                if job['resume_only']:
                    lock=session.run_dir/'.run.lock'
                    if lock.exists():
                        try:pid=int(lock.read_text().strip())
                        except ValueError:raise StopRun('研究锁记录需要管理员检查。') from None
                        try:os.kill(pid,0)
                        except ProcessLookupError:lock.unlink()
                        else:raise StopRun('研究仍被其他进程占用，请稍后接续。')
                else:
                    prices=fetch_prices(include_bulk=True)
                    config=config_for(session.answers,prices,'0.50');config['max_requests']=50
                    config['stats_strategy']='adaptive_fast'
                    check=type('PriceCheck',(),{'config':config})()
                    Store.pricing_ready(check,['search','posts','stats','stats_bulk','comments'])
            if job['mode']=='live' and job['resume_only']:
                from webapp.cached_analysis import run_cached
                run_cached(session,Store(session.run_dir))
            else:
                session.run(config,resume=bool(job['resume_only']))
            status=session.status
            message=session.message
            if session.cancelled.is_set() and status!='done':status='cancelled';message='已停止研究，已经取得的数据已保存。'
            self.db.finish(job['id'],status,message,public_progress(session))
        except Exception as error:
            message=str(error) if isinstance(error,StopRun) else '本轮研究暂时中断，已保存的数据仍可查看，请联系管理员。'
            status='cancelled' if session and session.cancelled.is_set() else 'error'
            self.db.finish(job['id'],status,message,public_progress(session) if session else {})
        finally:
            with self.lock:self.current.pop(job['id'],None)
