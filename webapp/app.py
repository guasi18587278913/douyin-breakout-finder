"""Private, invitation-only browser entry for the research engine."""
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field
from webapp.settings import Settings
from webapp.db import Database, Conflict, InvalidInput
from webapp.vault import Secrets
from webapp.engine import Worker, ROOT, run_dir
from core import StopRun
from product import simple_brief

class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')
class Login(Body):
    username:str=Field(min_length=2,max_length=40)
    password:str=Field(min_length=10,max_length=128)
class Register(Login):
    invitation:str=Field(min_length=30,max_length=100)
class Research(Body):
    topic:str=Field(min_length=1,max_length=500)
    focus:str='all'
    mode:str='live'
class Invite(Body):
    label:str=Field(default='受邀请的创作者',max_length=80)
class Configure(Body):
    tikhub_key:str=Field(default='',max_length=500)
    deepseek_key:str=Field(default='',max_length=500)


def create_app(settings=None):
    settings=settings or Settings.from_env()
    db=Database(settings);vault=Secrets(settings);worker=Worker(settings,db,vault)
    cookie='breakout_session' if settings.dev else '__Host-breakout_session'
    anon='breakout_csrf' if settings.dev else '__Host-breakout_csrf'
    def sign(value):return hmac.new(settings.master_key.encode(),value.encode(),hashlib.sha256).hexdigest()
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
        return {**{k:job[k] for k in ('id','topic','focus','mode','status','message','created_at','updated_at','cancel_requested')},
                'progress':json.loads(job['progress_json']),
                'report_url':'/reports/'+job['id']+'/report.html' if (folder/'report.html').is_file() else None,
                'can_resume':job['mode']=='live' and job['status'] in ('interrupted','error','needs_analysis','cancelled') and (folder/'state.json').is_file()}
    def limit(request,bucket,count,seconds):
        address=request.client.host if request.client else 'unknown'
        if not db.allow(bucket+':'+hashlib.sha256(address.encode()).hexdigest(),count,seconds):
            raise HTTPException(429,'操作太频繁，请稍后再试。')
    def session_response(user):
        token,csrf=db.create_session(user['id'])
        response=JSONResponse({'user':user,'csrf':csrf})
        response.set_cookie(cookie,token,max_age=settings.session_days*86400,secure=not settings.dev,httponly=True,samesite='strict',path='/')
        response.delete_cookie(anon,path='/',secure=not settings.dev,httponly=True,samesite='strict')
        return response
    @asynccontextmanager
    async def lifespan(app):
        if settings.worker_enabled:worker.start()
        yield
        if settings.worker_enabled:worker.close()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.db=db;app.state.vault=vault;app.state.worker=worker;app.state.settings=settings
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
        if request.url.path.startswith('/reports/'):
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
        response=JSONResponse({'user':user,'csrf':token,'ready':all(status.values()),'remaining':db.remaining(user['id']) if user else None})
        if not session:response.set_cookie(anon,token,max_age=3600,secure=not settings.dev,httponly=True,samesite='strict',path='/')
        return response
    @app.post('/api/register')
    def register(request:Request,body:Register):
        limit(request,'register',8,900)
        return session_response(db.register(body.invitation,body.username,body.password))
    @app.post('/api/login')
    def login(request:Request,body:Login):
        limit(request,'login',10,900)
        user=db.login(body.username,body.password)
        if not user:raise HTTPException(401,'用户名或密码不正确。')
        return session_response(user)
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
        return public_job(db.enqueue(user['user_id'],body.topic.strip(),body.focus,body.mode))
    @app.get('/api/jobs/{job_id}')
    def job(request:Request,job_id:str):return public_job(own(request,job_id))
    @app.post('/api/jobs/{job_id}/cancel')
    def cancel(request:Request,job_id:str):
        job=own(request,job_id);db.cancel(job_id,job['user_id']);worker.cancel(job_id)
        return public_job(db.job(job_id,job['user_id']))
    @app.post('/api/jobs/{job_id}/resume',status_code=202)
    def resume(request:Request,job_id:str):
        job=own(request,job_id);limit(request,'resume',8,3600)
        if not public_job(job)['can_resume']:raise HTTPException(409,'当前没有可接续的研究数据。')
        return public_job(db.resume(job_id,job['user_id']))
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
            navigation='<nav style="display:flex;justify-content:space-between;gap:20px;max-width:1120px;margin:0 auto;padding:24px 30px;font:14px sans-serif"><a href="/">← 我的研究手记</a><a id="new-research-link" href="/?new=1">新研究 ↗</a></nav>'
            return HTMLResponse(re.sub(r'(<body(?:\s[^>]*)?>)',lambda match:match.group(0)+navigation,content,count=1))
        return FileResponse(path)
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
    def invite(request:Request,body:Invite):
        auth(request,True);limit(request,'invite',20,3600)
        token=db.invite(body.label)
        return {'url':settings.public_url+'/#invite='+token,'expires_hours':48}
    app.mount('/static',StaticFiles(directory=ROOT/'webapp/static'),name='static')
    return app
