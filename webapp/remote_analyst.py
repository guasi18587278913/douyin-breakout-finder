"""HTTP analysis with a durable, conservative per-job dollar ceiling."""
import json
import os
import time
import hashlib
import fcntl
from decimal import Decimal
from datetime import datetime, timezone
import httpx
from analyst import LocalAnalyst, answer_schema, build_prompt
from bridge import AssistanceNeeded
from core import StopRun, ValidationIssue, atomic_json, read_json

URL = 'https://api.deepseek.com/chat/completions'
MODEL = 'deepseek-flash'
LIMIT = Decimal('0.15')
PRICE_SOURCE = 'https://api-docs.deepseek.com/quick_start/pricing/'
PRICE_CHECKED_AT = '2026-10-10'
# Peak cache-miss rates; actual off-peak and cached usage may cost less.


class RemoteAnalyst(LocalAnalyst):
    def __init__(self, cancelled, key, ledger, transport=None, settle_usage=False):
        self.cancelled, self.key, self.ledger = cancelled, key, ledger
        self.metrics = []
        self.name = 'deepseek'
        self.transport = transport
        self.last_attempt = None
        self.last_stage = None
        self.settle_usage = settle_usage
        self.operation = None
        self.last_failure = None

    @property
    def label(self): return '研究助手'
    def available(self): return bool(self.key)
    def check(self):
        if not self.key: raise AssistanceNeeded('研究服务尚未配置，请联系管理员。')
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(PRICE_CHECKED_AT).replace(tzinfo=timezone.utc)).days
        if not 0<=age<=30:raise AssistanceNeeded('模型报价需要管理员重新核对，已暂停新的分析调用。')

    def entries(self):
        if not self.ledger.exists(): return []
        try:
            rows = [json.loads(line) for line in self.ledger.read_text().splitlines() if line]
            if any(not isinstance(r,dict) or r.get('kind') not in ('reserve','finish') for r in rows):raise ValueError()
            for r in rows:
                if r['kind']=='reserve':
                    cost=Decimal(r['usd'])
                    if not cost.is_finite() or cost<0:raise ValueError()
            return rows
        except Exception:
            raise AssistanceNeeded('本轮费用记录需要检查，已停止新的模型调用。') from None

    def append(self, value):
        self.ledger.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(str(self.ledger),os.O_APPEND|os.O_CREAT|os.O_WRONLY,0o600)
        with os.fdopen(fd,'a') as f:
            f.write(json.dumps(value,separators=(',',':'))+'\n');f.flush();os.fsync(f.fileno())

    def plan(self, stage, packet, feedback):
        self.check()
        if stage=='content-value-review':
            from webapp.content_quality import messages_for
            messages=messages_for(packet)
            desired=minimum=min(5000,500+230*len(packet['targets']))
        elif stage=='candidate-review':
            from webapp.expanded_analysis import review_messages
            messages=review_messages(packet)
            desired=minimum=500+75*len(packet['candidates'])
        elif stage=='cached-report':
            if feedback and not self.operation:raise AssistanceNeeded('自动修正必须绑定原稿，不能直接追加调用。')
            from webapp.cached_analysis import messages_for
            messages=messages_for(packet,feedback)
            count=len(packet['candidates']);featured=min(3,len(packet['numerically_qualified']))
            desired=min(10000,2000+70*count+800*featured)
            minimum=min(desired,800+25*count+350*featured)
        else:
            prompt=build_prompt(stage,packet,feedback)
            schema=json.dumps(answer_schema(stage,packet),ensure_ascii=False,separators=(',',':'))
            system='Return only a JSON object matching this JSON Schema. Do not include Markdown. Schema: '+schema
            if stage=='semantic-review':
                from research import RELEVANCE_POLICY
                system=RELEVANCE_POLICY+'\n'+system
            messages=[{'role':'system','content':system},{'role':'user','content':prompt}]
            desired=6000 if stage=='content-analysis' else 7000 if stage=='semantic-review' else 600
            minimum=desired
        # One token per UTF-8 byte plus framing is conservative for this tokenizer.
        input_bound=sum(len(m['content'].encode('utf-8')) for m in messages)+2048
        if input_bound>240000:raise AssistanceNeeded('本轮分析材料过大，已保留数据。')
        rows=self.entries()
        from webapp.model_budget import effective_spent
        spent=effective_spent(self.ledger,rows,self.settle_usage)
        request_hash=hashlib.sha256(json.dumps({'stage':stage,'messages':messages},ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        if self.operation:
            if set(self.operation)!={'id','step'} or self.operation['step'] not in ('draft-0','draft-1','review-0','review-1'):
                raise AssistanceNeeded('自动修正次数或步骤无效，未发送请求。')
            step=self.operation['step']
            if step!='draft-0':
                dependency='draft-0' if step in ('draft-1','review-0') else 'draft-1'
                starts=[r for r in rows if r.get('kind')=='reserve' and r.get('operation')=={'id':self.operation['id'],'step':dependency}]
                finishes=[r for r in rows if r.get('kind')=='finish' and len(starts)==1 and r.get('id')==starts[0]['id']]
                if (len(starts)!=1 or len(finishes)!=1 or not (finishes[0].get('outcome')=='received'
                    or (step=='draft-1' and finishes[0].get('response_complete') is True and finishes[0].get('failure_type')=='invalid_json'))):
                    raise AssistanceNeeded('上一请求结果不完整或未知，不能自动修正或继续收费。')
            related=[r for r in rows if r.get('kind')=='reserve' and r.get('operation')==self.operation]
            if related and (len(related)!=1 or related[0].get('request_hash')!=request_hash):
                raise AssistanceNeeded('本步骤已有不同请求记录，不能通过改变反馈重置修正次数。')
        if stage in ('cached-report','content-value-review','content-analysis','candidate-review'):
            previous=self.reusable_response(request_hash, rows)
            if previous:
                return {'messages':messages,'maximum':previous[0]['max_tokens'],'minimum':minimum,'desired':desired,
                        'input_bound':input_bound,'reserved_usd':'0','spent_usd':str(spent),
                        'request_hash':request_hash,'stage':stage,'reuse_attempt':previous[0]['id']}
        available=int((LIMIT-spent-Decimal(input_bound)*Decimal('0.00000030'))/Decimal('0.00000120'))
        maximum=min(desired,available)
        if maximum<minimum:raise AssistanceNeeded('剩余模型额度不足以完成本次自动分析，未发送请求。')
        reserve=(Decimal(input_bound)*Decimal('0.30')+Decimal(maximum)*Decimal('1.20'))/1000000
        if spent+reserve>LIMIT:raise AssistanceNeeded('本轮模型费用上限已到，未发送请求。')
        return {'messages':messages,'maximum':maximum,'minimum':minimum,'desired':desired,'input_bound':input_bound,
                'reserved_usd':str(reserve),'spent_usd':str(spent),'request_hash':request_hash,'stage':stage}

    def diagnostic(self, attempt, suffix, value):
        folder=self.ledger.parent/'model-diagnostics';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
        def redact(item):
            if isinstance(item,str):return item.replace(self.key,'[REDACTED]')
            if isinstance(item,list):return [redact(v) for v in item]
            if isinstance(item,dict):return {redact(k):redact(v) for k,v in item.items()}
            return item
        safe=redact(value)
        path=folder/('attempt-%03d-%s.json'%(attempt,suffix));atomic_json(path,safe)
        if os.name!='nt':path.chmod(0o600)
        return path

    def reusable_response(self, request_hash, rows):
        previous=next((r for r in reversed(rows) if r['kind']=='reserve' and r.get('request_hash')==request_hash),None)
        if not previous:return None
        finishes=[r for r in rows if r['kind']=='finish' and r.get('id')==previous.get('id')]
        reserves=[r for r in rows if r['kind']=='reserve' and r.get('id')==previous.get('id')]
        if len(finishes)!=1 or len(reserves)!=1:raise AssistanceNeeded('模型请求结果不唯一，已停止新调用。')
        finished=finishes[0]
        path=self.ledger.parent/'model-diagnostics'/('attempt-%03d-response.json'%previous['id'])
        if finished.get('outcome')=='received' and path.is_file():
            saved=read_json(path,{})
            raw=json.dumps(saved.get('answer'),ensure_ascii=False,separators=(',',':')).encode()
            if saved.get('request_hash')==request_hash and hashlib.sha256(raw).hexdigest()==finished.get('answer_sha256'):
                return previous,saved['answer']
        raise AssistanceNeeded('上次自动调用没有可验证的完整响应，不会自动重发。')

    def ask(self, stage, packet, accept):
        def checked(answer):
            try:
                result=accept(answer)
            except Exception as error:
                detail=error.feedback if isinstance(error,ValidationIssue) else {'rule':str(error) if isinstance(error,StopRun) else type(error).__name__}
                if self.last_attempt is not None:self.diagnostic(self.last_attempt,'validation',{'accepted':False,'detail':detail})
                raise
            if self.last_attempt is not None:self.diagnostic(self.last_attempt,'validation',{'accepted':True})
            return result
        return super().ask(stage,packet,checked)

    def generate(self, stage, packet, feedback):
        self.ledger.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(str(self.ledger)+'.lock',os.O_CREAT|os.O_RDWR,0o600)
        with os.fdopen(fd,'a') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
            from webapp.model_budget import effective_spent
            effective_spent(self.ledger,self.entries(),self.settle_usage,persist=True)
            self.require_known_outcomes()
            try:return self._generate(stage,packet,feedback)
            finally:effective_spent(self.ledger,self.entries(),self.settle_usage,persist=True)

    def require_known_outcomes(self):
        # A new stage or changed prompt cannot turn an unknown paid outcome into a retry.
        from webapp.model_budget import fingerprint
        from webapp.approval import approved as read_approval
        approved=read_approval(self.ledger.parent)
        legacy=set(approved.get('legacy_settlement_hashes',[]))
        rows=self.entries()
        for start in rows:
            if start['kind']!='reserve' or type(start.get('id')) is not int:continue
            ends=[r for r in rows if r['kind']=='finish' and r.get('id')==start['id']]
            if len(ends)!=1:raise AssistanceNeeded('已有模型请求的结果尚不明确，已停止新调用。')
            end=ends[0]
            if end.get('outcome')=='received':continue
            if end.get('provider_response_received') is True and end.get('response_complete') is True:continue
            if fingerprint([start,end]) in legacy and start['id']==approved.get('retry_of'):continue
            raise AssistanceNeeded('上次付费请求结果未知，不能切换阶段或刷新后重新收费。')

    def _generate(self, stage, packet, feedback):
        self.last_stage=stage
        self.last_failure=None
        # A retry after a local validator repair may reuse exactly the received response.
        # Unknown/failed network outcomes are never silently resubmitted.
        if self.cancelled.is_set():raise AssistanceNeeded('研究已停止。')
        plan=self.plan(stage,packet,feedback)
        if plan.get('reuse_attempt'):
            previous,answer=self.reusable_response(plan['request_hash'],self.entries())
            self.last_attempt=previous['id'];return answer
        rows=self.entries()
        if self.cancelled.is_set():raise AssistanceNeeded('研究已停止。')
        attempt=sum(r['kind']=='reserve' for r in rows)+1
        self.last_attempt=attempt
        self.append({'kind':'reserve','id':attempt,'stage':stage,'usd':plan['reserved_usd'],'at':time.time(),'model':MODEL,'price_source':PRICE_SOURCE,'price_checked_at':PRICE_CHECKED_AT,
                     'request_hash':plan['request_hash'],'input_bound':plan['input_bound'],'max_tokens':plan['maximum'],'operation':self.operation})
        payload={'model':MODEL,'messages':plan['messages'],
                 'thinking':{'type':'disabled'},'response_format':{'type':'json_object'},'max_tokens':plan['maximum'],'stream':False}
        outcome='failed'; usage={};answer_hash=None;response_complete=False;provider_response_received=False
        try:
            self.diagnostic(attempt,'request',{'stage':stage,'request_hash':plan['request_hash'],'payload':payload})
            with httpx.Client(timeout=httpx.Timeout(180,connect=15),follow_redirects=False,trust_env=False,transport=self.transport) as client:
                with client.stream('POST',URL,json=payload,headers={'Authorization':'Bearer '+self.key}) as response:
                    if response.status_code!=200:
                        raise AssistanceNeeded('研究助手暂时无法完成分析（HTTP %s），已保留数据。'%response.status_code)
                    chunks=[];size=0
                    for chunk in response.iter_bytes():
                        if self.cancelled.is_set():raise AssistanceNeeded('研究已停止，已保留现有数据。')
                        size+=len(chunk)
                        if size>2*1024*1024:raise AssistanceNeeded('模型返回内容过大，已保留数据。')
                        chunks.append(chunk)
            data=json.loads(b''.join(chunks))
            provider_response_received=True
            for name in ('prompt_tokens','completion_tokens'):
                n=(data.get('usage') or {}).get(name)
                if type(n) is int and n>=0:usage[name]=n
            choice=data['choices'][0]
            content=choice.get('message',{}).get('content')
            if choice.get('finish_reason')!='stop':
                self.diagnostic(attempt,'output-error',{'finish_reason':choice.get('finish_reason'),'error':'incomplete_output'})
                raise StopRun('Model output incomplete.')
            response_complete=True
            from webapp.model_json import decode_object, ModelJSONError
            try:answer,normalizations=decode_object(content)
            except ModelJSONError as error:
                self.last_failure='invalid_json'
                self.diagnostic(attempt,'output-error',{'error':'invalid_json','characters':len(content) if isinstance(content,str) else None,
                                                        'syntax':error.detail})
                raise StopRun('Expected valid JSON.') from None
            # A provider must not get its own credential into persisted report material.
            def contains_key(value):
                if isinstance(value,str):return self.key in value
                if isinstance(value,list):return any(contains_key(v) for v in value)
                if isinstance(value,dict):return any(contains_key(k) or contains_key(v) for k,v in value.items())
                return False
            if contains_key(answer):
                self.diagnostic(attempt,'output-error',{'error':'privacy_check_failed'})
                raise AssistanceNeeded('模型输出未通过隐私检查。')
            self.diagnostic(attempt,'raw-output',{'finish_reason':choice.get('finish_reason'),'content':content,'format_normalizations':normalizations})
            answer_hash=hashlib.sha256(json.dumps(answer,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            self.diagnostic(attempt,'response',{'request_hash':plan['request_hash'],'answer':answer})
            outcome='received';return answer
        except httpx.HTTPError:
            raise AssistanceNeeded('研究助手连接中断，已保留数据；不会自动重发这次请求。') from None
        finally:
            self.append({'kind':'finish','id':attempt,'outcome':outcome,'usage':usage,'at':time.time(),'answer_sha256':answer_hash,'provider_response_received':provider_response_received,'response_complete':response_complete,'failure_type':self.last_failure})
