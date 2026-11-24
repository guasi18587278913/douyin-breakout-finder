"""Account-isolated browser entry for the research engine."""
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field
from webapp.settings import Settings
from webapp.db import Database, Conflict, InvalidInput, SETTLING
from webapp.vault import Secrets
from webapp.engine import Worker, ROOT, run_dir
from webapp.credits import Credits, FmClient, FmRejected, FmUnavailable, InsufficientCredits, CODE, STATE
from core import StopRun
from product import simple_brief

STATE_SECONDS=600  # 封面搭子登录跳转用的 state 有效期
PHONE_LOGIN='已改为手机号登录，请刷新页面后重新登录。'

class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')
class Research(Body):
    topic:str=Field(min_length=1,max_length=500)
    focus:str='all'
    mode:str='live'
class Configure(Body):
    tikhub_key:str=Field(default='',max_length=500)
    deepseek_key:str=Field(default='',max_length=500)


def create_app(settings=None,fm_transport=None):
    settings=settings or Settings.from_env()
    db=Database(settings);vault=Secrets(settings);fm=FmClient(settings,fm_transport)
    # delivered 用到下面定义的 public_job：研究是否已有可用报告，以网页显示的结论为准。
    credits=Credits(settings,db,fm,lambda job:delivered(job))
    worker=Worker(settings,db,vault,credits)
    cookie='breakout_session' if settings.dev else '__Host-breakout_session'
    anon='breakout_csrf' if settings.dev else '__Host-breakout_csrf'
    state_cookie='breakout_auth' if settings.dev else '__Host-breakout_auth'
    def sign(value):return hmac.new(settings.master_key.encode(),value.encode(),hashlib.sha256).hexdigest()
    def state_token(state):
        payload=state+'.'+str(int(time.time()))
        return payload+'.'+sign('fm-state:'+payload)
    def read_state(value):
        """核对签名和有效期，返回 cookie 里的 state；不对就返回 None。"""
        try:
            state,issued,sig=value.split('.')
            fresh=0<=time.time()-int(issued)<STATE_SECONDS
            return state if fresh and hmac.compare_digest(sign('fm-state:'+state+'.'+issued),sig) else None
        except (ValueError,TypeError,AttributeError):return None
    def anonymous_token():
        value=str(int(time.time()))+'.'+secrets.token_urlsafe(24)
        return value+'.'+sign(value)
    def valid_anonymous(value):
        try:
            prefix,sig=value.rsplit('.',1);timestamp=int(prefix.split('.')[0])
            return 0<=time.time()-timestamp<3600 and hmac.compare_digest(sign(prefix),sig)
        except (ValueError,AttributeError):return False
    def get_session(request):return db.session(request.cookies.get(cookie))
    def auth(request,admin=False):
        user=get_session(request)
        if not user:raise HTTPException(401,'请先登录。')
        if admin and user['role']!='admin':raise HTTPException(403,'此操作需要管理员账号。')
        return user
    def own(request,job_id):
        user=auth(request);job=db.job(job_id,user['user_id'])
        if not job:raise HTTPException(404,'没有找到这项研究。')
        return job
    def public_job(job):
        folder=run_dir(settings,job['id'])
        from webapp.job_outcome import assessment, cannot_recover_data
        from completion import outcome
        result = assessment(folder) if job['status'] not in ('queued','running') else None
        status,message = job['status'],job['message']
        if status in ('done','report_ready'):
            if result:status,message=outcome(result,job['mode']=='demo')
            else:status,message='needs_analysis','已有报告待核对，尚不能确认本轮研究完成。'
        blocked = cannot_recover_data(result)
        if blocked and status in ('error','interrupted','needs_analysis','needs_data'):
            status,message='needs_data','已有资料不足以核验出可解读案例，重复整理不能补齐数据。本轮尚未完成。'
        can_resume=not blocked and job['mode']=='live' and status in ('interrupted','error','needs_analysis','cancelled') and (folder/'state.json').is_file()
        return {**{k:job[k] for k in ('id','topic','focus','mode','created_at','updated_at','cancel_requested')},
                'status':status,'message':message,
                'completion':{**{k:result[k] for k in ('report','quantity')},
                              'retrieval':{k:result['retrieval'][k] for k in ('legal_stop','all_candidates_resolved')}} if result else None,
                'report_ready':bool(result and result['report']['ready']),
                'progress':json.loads(job['progress_json']),
                'report_url':'/reports/'+job['id']+'/report.html' if (folder/'report.html').is_file() else None,
                'can_resume':can_resume,
                # 接续要用几分：当前那笔还扣着就是 0；退过分了要重新扣
                'resume_credits':credits.resume_cost(job['id']) if can_resume else 0}
    def delivered(job):
        """研究是否已有可用报告：网页显示为 done/report_ready，或已有能读的报告。都不是就该退分。"""
        view=public_job(job)
        return view['status'] in ('done','report_ready') or view['report_ready']
    def shortage(error):
        """积分不够时的 402：开始研究和接续用同一套文案。"""
        balance='' if error.balance is None else f'，你现在有 {error.balance} 分'
        return JSONResponse({'detail':f'积分不够：跑一次真实研究要 {settings.research_credits} 积分{balance}。请先去封面搭子充值。',
                             'balance':error.balance,'cost':settings.research_credits,'recharge_url':error.recharge_url or credits.recharge_url},402)
    def fm_down(what):return f'暂时连不上封面搭子的积分服务，{what}没有开始；万一已经扣了分，会自动退回。请稍后再试。'
    def limit(request,bucket,count,seconds):
        address=request.client.host if request.client else 'unknown'
        if not db.allow(bucket+':'+hashlib.sha256(address.encode()).hexdigest(),count,seconds):
            raise HTTPException(429,'操作太频繁，请稍后再试。')
    def start_session(response,user_id):
        token,_=db.create_session(user_id)
        response.set_cookie(cookie,token,max_age=settings.session_days*86400,secure=not settings.dev,httponly=True,samesite='strict',path='/')
        response.delete_cookie(anon,path='/',secure=not settings.dev,httponly=True,samesite='strict')
        return response
    def forget_state(response):
        response.delete_cookie(state_cookie,path='/',secure=not settings.dev,httponly=True,samesite='lax')
        return response
    def login_failed(reason,status):
        # reason 只用下面写死的几句话，不带出封面搭子的回应细节。
        page=('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
              '<title>登录没有完成 · 小号爆款</title><link rel="stylesheet" href="/static/site.css"></head><body><main>'
              '<section class="paper auth-card login-failed"><p class="eyebrow">封面搭子账号登录</p><h2>登录没有完成</h2>'
              '<p class="muted">'+reason+'</p><div class="actions"><a class="primary" href="/auth/start">重新登录 →</a>'
              '<a class="text-button" href="/">返回首页</a></div></section></main></body></html>')
        return forget_state(HTMLResponse(page,status))
    @asynccontextmanager
    async def lifespan(app):
        if settings.worker_enabled:worker.start()
        yield
        if settings.worker_enabled:worker.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.db=db;app.state.vault=vault;app.state.worker=worker;app.state.settings=settings;app.state.credits=credits
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=[urlsplit(settings.public_url).hostname])
    @app.middleware('http')
    async def protect(request,call_next):
        if request.method in ('POST','PUT','PATCH','DELETE'):
            if request.headers.get('origin')!=settings.public_url:
                return JSONResponse({'detail':'请求来源不正确，请刷新页面后重试。'},status_code=403)
            if request.headers.get('content-type','').split(';')[0]!='application/json':
                return JSONResponse({'detail':'请使用网页内的操作。'},status_code=415)
            try:
                length=int(request.headers.get('content-length','0'))
            except ValueError:
                return JSONResponse({'detail':'请求长度无效。'},status_code=400)
            if length<0 or length>16384:return JSONResponse({'detail':'提交内容过长。'},status_code=413)
            chunks=[];size=0
            async for chunk in request.stream():
                size+=len(chunk)
                if size>16384:return JSONResponse({'detail':'提交内容过长。'},status_code=413)
                chunks.append(chunk)
            request._body=b''.join(chunks)
            token=request.headers.get('x-csrf-token','');session=get_session(request)
            okay=(bool(token) and hmac.compare_digest(token,session['csrf'])) if session else (valid_anonymous(token) and hmac.compare_digest(token,request.cookies.get(anon,'')))
            if not okay:return JSONResponse({'detail':'页面已过期，请刷新后重试。'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        policy="default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        if request.url.path.startswith('/reports/') or request.url.path=='/sample':
            policy="default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        response.headers['Content-Security-Policy']=policy
        if not settings.dev:response.headers['Strict-Transport-Security']='max-age=31536000'
        return response
    @app.exception_handler(RequestValidationError)
    async def validation(request,error):return JSONResponse({'detail':'提交内容有误，请检查字段长度与格式。'},422)
    @app.exception_handler(Conflict)
    async def conflict(request,error):return JSONResponse({'detail':str(error)},409)
    @app.exception_handler(InvalidInput)
    async def invalid(request,error):return JSONResponse({'detail':str(error)},400)
    @app.exception_handler(StopRun)
    async def stopped(request,error):return JSONResponse({'detail':str(error)},400)
    @app.get('/health')
    def health():
        if settings.worker_enabled and not worker.healthy():return JSONResponse({'status':'worker_unavailable'},503)
        return {'status':'ok'}
    @app.get('/')
    def index():return FileResponse(ROOT/'webapp/static/index.html')
    @app.get('/api/bootstrap')
    def bootstrap(request:Request):
        session=get_session(request)
        token=session['csrf'] if session else anonymous_token()
        user={'id':session['user_id'],'username':session['username'],'role':session['role']} if session else None
        status=vault.status()
        # 积分从封面搭子查，按用户缓存 30 秒；查不到时为 null，页面照常显示。
        response=JSONResponse({'user':user,'csrf':token,'ready':all(status.values()),'remaining':db.remaining(user['id']) if user else None,
                               'credits':credits.balance(session['subject']) if session else None,
                               'research_credits':settings.research_credits,'recharge_url':credits.recharge_url})
        if not session:response.set_cookie(anon,token,max_age=3600,secure=not settings.dev,httponly=True,samesite='strict',path='/')
        return response
    # 旧的用户名密码注册、登录已停用；旧账号和研究数据留在库里，不删除。
    @app.post('/api/register')
    def register():raise HTTPException(410,PHONE_LOGIN)
    @app.post('/api/login')
    def login():raise HTTPException(410,PHONE_LOGIN)
    @app.get('/auth/start')
    def auth_start():
        """跳到封面搭子授权：state 放进 10 分钟有效的签名 cookie，回来时核对。"""
        state=secrets.token_urlsafe(32)
        response=RedirectResponse(fm.authorize_url(state),302)
        response.set_cookie(state_cookie,state_token(state),max_age=STATE_SECONDS,secure=not settings.dev,httponly=True,samesite='lax',path='/')
        return response
    @app.get('/auth/callback')
    def auth_callback(request:Request,code:str='',state:str=''):
        try:limit(request,'fm-login',20,900)
        except HTTPException:return login_failed('登录太频繁，请过几分钟再试。',429)
        expected=read_state(request.cookies.get(state_cookie,''))
        # 先确认格式（compare_digest 只收 ASCII），再做常量时间比较。
        if not expected or not STATE.fullmatch(state) or not hmac.compare_digest(expected,state):
            return login_failed('这次登录已经过期或不是从本站发起的，请重新登录。',400)
        # 同一个 state 只能用一次：用过的在有效期内记一笔，重放直接拒绝。
        if not db.allow('fm-state:'+hashlib.sha256(state.encode()).hexdigest(),1,STATE_SECONDS):
            return login_failed('这个登录链接已经用过了，请重新登录。',400)
        if not CODE.fullmatch(code):
            return login_failed('封面搭子那边没有完成授权，请重新登录。',400)
        try:subject=fm.exchange(code)
        except (FmRejected,FmUnavailable) as error:
            if isinstance(error,FmRejected) and error.status==400:return login_failed('授权已过期或已经用过，请重新登录。',400)
            return login_failed('暂时连不上封面搭子，请稍后再试。',503)
        user=db.external_user(subject)
        return forget_state(start_session(RedirectResponse('/',302),user['id']))
    @app.post('/api/logout')
    def logout(request:Request):
        auth(request);db.logout(request.cookies[cookie]);response=JSONResponse({'ok':True})
        response.delete_cookie(cookie,path='/',secure=not settings.dev,httponly=True,samesite='strict');return response
    @app.get('/api/jobs')
    def jobs(request:Request):return {'jobs':[public_job(j) for j in db.jobs(auth(request)['user_id'])]}
    @app.post('/api/jobs',status_code=202)
    def create_job(request:Request,body:Research):
        user=auth(request);limit(request,'jobs',12,3600)
        if settings.worker_enabled and not worker.healthy():raise HTTPException(503,'后台服务正在恢复，请稍后重试。')
        if body.mode not in ('live','demo'):raise HTTPException(400,'请选择真实研究或合成示例。')
        simple_brief({'description':body.topic,'focus':body.focus})
        if body.mode=='live' and not all(vault.status().values()):raise HTTPException(409,'研究服务尚未配置完成，请联系管理员。')
        if body.mode=='demo':return public_job(db.enqueue(user['user_id'],body.topic.strip(),body.focus,'demo'))
        # 真实研究：先按入队规则预检每日额度，够了再扣分，免得扣了分却排不上。
        db.admit(user['user_id'],'live')
        job_id=uuid.uuid4().hex
        try:credits.charge(user['subject'],job_id)
        except InsufficientCredits as error:return shortage(error)
        except (FmUnavailable,FmRejected):raise HTTPException(503,fm_down('研究')) from None
        try:job=db.enqueue(user['user_id'],body.topic.strip(),body.focus,'live',job_id=job_id,charged=job_id)
        except Exception as error:
            # 没入队（例如预检之后额度被别的请求占了）就马上退分；退不成的记为待退，由后台重试。
            try:refunded=credits.refund_now(job_id,('charging',))
            except Exception:
                refunded=False
                logging.getLogger(__name__).error('入队失败后的积分退回暂未完成，后台循环会再试。')
            if isinstance(error,Conflict):
                raise Conflict(str(error)+('刚才扣的积分已经退回。' if refunded else '刚才扣的积分会自动退回。')) from None
            raise
        return public_job(job)
    @app.get('/api/jobs/{job_id}')
    def job(request:Request,job_id:str):return public_job(own(request,job_id))
    @app.post('/api/jobs/{job_id}/cancel')
    def cancel(request:Request,job_id:str):
        job=own(request,job_id);db.cancel(job_id,job['user_id']);worker.cancel(job_id)
        # 排队时就被取消的真实研究不会再跑：没有报告，退回积分。正在跑的等研究收尾后由后台结算。
        try:credits.settle(job_id)
        except Exception:logging.getLogger(__name__).error('取消研究后的积分结算暂未完成，后台循环会再试。')
        return public_job(db.job(job_id,job['user_id']))
    @app.post('/api/jobs/{job_id}/resume',status_code=202)
    def resume(request:Request,job_id:str):
        user=auth(request);job=own(request,job_id);limit(request,'resume',8,3600)
        if not public_job(job)['can_resume']:raise HTTPException(409,'当前没有可接续的研究数据。')
        repair=job['status'] in ('done','report_ready')
        current=db.credit_charge(job_id)
        # 没扣过分（合成示例等）或这份研究还扣着分：不另扣。接续事务里会再确认这笔还扣着。
        if current is None or current['state']=='charged':
            return public_job(db.resume(job_id,job['user_id'],allow_completed_repair=repair))
        if current['state'] in ('charging','refund_pending'):raise HTTPException(409,SETTLING)
        # 已经退过分（或封面搭子账号注销后作废）：这次接续重新扣一次，扣成了才排队。
        db.admit(job['user_id'],'resume')  # 只查有没有别的研究进行中；接续不占每日额度
        try:reference=credits.recharge(user['subject'],job_id)
        except InsufficientCredits as error:return shortage(error)
        except (FmUnavailable,FmRejected):raise HTTPException(503,fm_down('接续')) from None
        try:resumed=db.resume(job_id,job['user_id'],allow_completed_repair=repair,charged=reference)
        except Exception as error:
            try:refunded=credits.refund_now(reference,('charging',))
            except Exception:
                refunded=False
                logging.getLogger(__name__).error('接续没排上后的积分退回暂未完成，后台循环会再试。')
            if isinstance(error,Conflict):
                raise Conflict(str(error)+('刚才扣的积分已经退回。' if refunded else '刚才扣的积分会自动退回。')) from None
            raise
        return public_job(resumed)
    @app.get('/reports/{job_id}/{filename:path}')
    def report(request:Request,job_id:str,filename:str):
        own(request,job_id)
        exports={'report.html','results.csv','comparisons.csv','results.json','summary.json','candidates.json'}
        if filename not in exports and not re.fullmatch(r'covers/[a-zA-Z0-9_-]+\.(?:jpg|jpeg|png|webp)',filename):
            raise HTTPException(404,'没有找到这个文件。')
        root=run_dir(settings,job_id);path=(root/filename).resolve()
        if not path.is_relative_to(root) or not path.is_file():raise HTTPException(404,'报告还没有生成。')
        if filename=='report.html':
            content=path.read_text(encoding='utf-8')
            navigation='<nav class="report-site-nav" aria-label="网站导航" style="display:flex;justify-content:space-between;gap:20px;max-width:1200px;margin:0 auto;padding:24px 30px;font-size:13px"><a href="/">← 我的研究</a><a id="new-research-link" href="/?new=1">新研究 ↗</a></nav>'
            return HTMLResponse(re.sub(r'(<body(?:\s[^>]*)?>)',lambda match:match.group(0)+navigation,content,count=1))
        return FileResponse(path)
    @app.get('/sample')
    def sample():
        # 不用登录就能看的示例报告：管理员把一份精编版真实报告放到数据目录 public/ 下，网页只读它，不读任何用户的研究目录。
        # 只认精编版（class="report-editorial"）：非精编版会带预算、评论样本和运行细节，也有指向下载文件的相对链接。
        missing=HTMLResponse('<!doctype html><meta charset="utf-8"><title>示例报告</title><p>示例报告暂未提供。<a href="/">返回首页</a></p>',404)
        try:content=(settings.data_dir/'public'/'sample-report.html').read_text(encoding='utf-8')
        except (OSError,UnicodeDecodeError):return missing
        if 'class="report-editorial"' not in content:return missing
        navigation=('<nav class="report-site-nav" aria-label="网站导航" style="display:flex;justify-content:space-between;gap:20px;max-width:1200px;margin:0 auto;padding:24px 30px;font-size:13px">'
                    '<a href="/">← 小号爆款首页</a><a href="/#register">注册，研究你自己的方向 ↗</a></nav>'
                    '<p style="max-width:1200px;margin:0 auto;padding:0 30px 8px;font-size:13px;color:#6b6556">这是一份真实的研究报告示例，数据取自抖音公开作品。</p>')
        return HTMLResponse(re.sub(r'(<body(?:\s[^>]*)?>)',lambda match:match.group(0)+navigation,content,count=1))
    @app.get('/api/admin')
    def admin(request:Request):
        auth(request,True)
        return {'providers':vault.status(),'daily_limit':settings.daily_live_jobs,'user_limit':settings.daily_user_jobs}
    @app.post('/api/admin/providers')
    def configure(request:Request,body:Configure):
        user=auth(request,True);limit(request,'config',10,3600)
        fields={k:v.strip() for k,v in body.model_dump().items() if v.strip()}
        try:status=vault.update(fields)
        except ValueError:raise HTTPException(400,'请填写有效的后台服务密钥。') from None
        with db.connection(True) as c:c.execute('INSERT INTO audit(at,user_id,action) VALUES (?,?,?)',(time.time(),user['user_id'],'configure_providers'))
        return {'providers':status}
    @app.post('/api/admin/invites')
    def invite(request:Request):
        # 邀请注册已停用：管理员改在服务器上用 python -m webapp.manage grant-admin 指定。
        auth(request,True)
        raise HTTPException(410,'已改为手机号登录，邀请链接已停用；请在服务器上用 grant-admin 设管理员。')
    app.mount('/static',StaticFiles(directory=ROOT/'webapp/static'),name='static')
    return app
