"""Local, bounded file handoff to the host agent. No network or credentials."""
import json
import os
import secrets
import time
from pathlib import Path
from core import StopRun, atomic_json, digest, now, read_json

class AssistanceNeeded(StopRun):
    pass


def bounded_json(path, limit=512*1024):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>limit:
        raise StopRun('智能体交接文件缺失或超出大小限制。')
    return read_json(path)


class FileBridge:
    def __init__(self, folder, cancelled, timeout=600):
        self.folder=Path(folder).resolve()
        self.folder.mkdir(parents=True,exist_ok=True)
        if os.name!='nt':self.folder.chmod(0o700)
        self.cancelled=cancelled;self.timeout=timeout
        self.session_id=secrets.token_hex(16)
        self.last_request_id=None
        atomic_json(self.folder/'session.json',{'session_id':self.session_id,'version':1})

    def available(self):
        try:
            lease=bounded_json(self.folder/'host.json',4096)
            age=time.time()-lease.get('at',0)
            return lease.get('session_id')==self.session_id and lease.get('active') is True and 0<=age<75
        except (StopRun,TypeError,ValueError):return False

    def ask(self,stage,payload,accept):
        request={'session_id':self.session_id,'request_id':secrets.token_hex(16),
                 'stage':stage,'run_id':Path(payload['run_dir']).name,'created_at':now(),'payload':payload,'payload_hash':digest(payload)}
        self.last_request_id=request['request_id']
        for name in ['response.json','feedback.json']:
            (self.folder/name).unlink(missing_ok=True)
        atomic_json(self.folder/'request.json',request)
        started=time.monotonic();seen=None;attempts=0
        try:
            deadline=min(self.timeout,60) if stage=='prepare-research' else self.timeout
            while time.monotonic()-started<deadline:
                if self.cancelled.wait(.2):raise AssistanceNeeded('研究已停止；已保存取得的数据，未完成分析。')
                path=self.folder/'response.json'
                if not path.exists():continue
                try:
                    response=bounded_json(path)
                    signature=digest(response)
                    if signature==seen:continue
                    seen=signature
                    if not isinstance(response,dict) or any(response.get(k)!=request[k] for k in ['session_id','request_id','stage','run_id','payload_hash']):
                        raise StopRun('交接版本不匹配，拒绝旧任务或其他会话的回答。')
                    result=accept(response.get('answer'))
                except (StopRun,ValueError,TypeError,KeyError) as error:
                    attempts+=1
                    atomic_json(self.folder/'feedback.json',{'request_id':request['request_id'],'accepted':False,'error':str(error)[:1000]})
                    if attempts>=3:raise AssistanceNeeded('智能体连续三次未通过证据校验，已保留草稿，请回到原会话检查。')
                    continue
                atomic_json(self.folder/'feedback.json',{'request_id':request['request_id'],'accepted':True})
                return result
            raise AssistanceNeeded('智能体尚未完成本轮分析。草稿和数据已保存，请回到启动 Skill 的会话继续。')
        finally:
            (self.folder/'request.json').unlink(missing_ok=True)


def next_request(folder, after=None, wait=50):
    folder=Path(folder).resolve();session=bounded_json(folder/'session.json',4096)
    began=time.time()
    until=time.monotonic()+min(max(wait,0),60)
    try:
        while True:
            atomic_json(folder/'host.json',{'session_id':session['session_id'],'at':time.time(),'active':True})
            path=folder/'request.json'
            if path.exists():
                request=bounded_json(path,8*1024*1024)
                if request.get('session_id')==session['session_id']:
                    feedback=read_json(folder/'feedback.json',{})
                    if request.get('request_id')==after and feedback.get('request_id')==after and feedback.get('accepted') is False:
                        return {'stage':'revision_required','request':request,'feedback':feedback}
                    if request.get('request_id')!=after:return request
            status=read_json(folder/'status.json',{})
            if status.get('terminal') and (status.get('at',0)>=began or (after and status.get('request_id')==after)):
                return {'stage':'finished',**status}
            if time.monotonic()>=until:return {'stage':'waiting_for_user','session_id':session['session_id']}
            time.sleep(.25)
    finally:
        atomic_json(folder/'host.json',{'session_id':session['session_id'],'at':time.time(),'active':False})


def submit(folder,request_id,answer):
    folder=Path(folder).resolve();request=bounded_json(folder/'request.json',8*1024*1024)
    if request.get('request_id')!=request_id:raise StopRun('请求已变化，请重新读取当前任务。')
    if request.get('payload_hash')!=digest(request.get('payload')):raise StopRun('交接内容校验失败。')
    atomic_json(folder/'response.json',{k:request[k] for k in ['session_id','request_id','stage','run_id','payload_hash']}|{'answer':answer})
    return {'submitted':request_id,'note':'已提交，等待后端校验；accepted=true 才表示已采用。'}
