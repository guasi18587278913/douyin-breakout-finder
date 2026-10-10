"""HTTP analysis with a durable, conservative per-job dollar ceiling."""
import json
import os
import time
from decimal import Decimal
from datetime import datetime, timezone
import httpx
from analyst import LocalAnalyst, answer_schema, build_prompt
from bridge import AssistanceNeeded
from core import StopRun

URL = 'https://api.deepseek.com/chat/completions'
MODEL = 'deepseek-flash'
LIMIT = Decimal('0.15')
PRICE_SOURCE = 'https://api-docs.deepseek.com/quick_start/pricing/'
PRICE_CHECKED_AT = '2026-10-10'
# Peak cache-miss rates; actual off-peak and cached usage may cost less.


class RemoteAnalyst(LocalAnalyst):
    def __init__(self, cancelled, key, ledger, transport=None):
        self.cancelled, self.key, self.ledger = cancelled, key, ledger
        self.metrics = []
        self.name = 'deepseek'
        self.transport = transport

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

    def generate(self, stage, packet, feedback):
        self.check()
        prompt=build_prompt(stage,packet,feedback)
        schema=json.dumps(answer_schema(stage,packet),ensure_ascii=False,separators=(',',':'))
        system='Return only a JSON object matching this JSON Schema. Do not include Markdown. Schema: '+schema
        maximum=12000 if stage=='content-analysis' else 7000 if stage=='semantic-review' else 600
        # One token per UTF-8 byte plus framing is conservative for this tokenizer.
        input_bound=len((system+prompt).encode('utf-8'))+2048
        if input_bound>240000:raise AssistanceNeeded('本轮分析材料过大，已保留数据。')
        reserve=(Decimal(input_bound)*Decimal('0.30')+Decimal(maximum)*Decimal('1.20'))/1000000
        rows=self.entries(); spent=sum((Decimal(r['usd']) for r in rows if r['kind']=='reserve'),Decimal(0))
        if spent+reserve>LIMIT:raise AssistanceNeeded('本轮模型费用上限已到，已保留数据和阶段报告。')
        if self.cancelled.is_set():raise AssistanceNeeded('研究已停止。')
        attempt=sum(r['kind']=='reserve' for r in rows)+1
        self.append({'kind':'reserve','id':attempt,'stage':stage,'usd':str(reserve),'at':time.time(),'model':MODEL,'price_source':PRICE_SOURCE,'price_checked_at':PRICE_CHECKED_AT})
        payload={'model':MODEL,'messages':[{'role':'system','content':system},{'role':'user','content':prompt}],
                 'thinking':{'type':'disabled'},'response_format':{'type':'json_object'},'max_tokens':maximum,'stream':False}
        outcome='failed'; usage={}
        try:
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
            for name in ('prompt_tokens','completion_tokens'):
                n=(data.get('usage') or {}).get(name)
                if type(n) is int and n>=0:usage[name]=n
            choice=data['choices'][0]
            if choice.get('finish_reason')!='stop':raise StopRun('Model output incomplete.')
            answer=json.loads(choice['message']['content'])
            if not isinstance(answer,dict):raise StopRun('Expected a JSON object.')
            # A provider must not get its own credential into persisted report material.
            if self.key in json.dumps(answer,ensure_ascii=False):raise AssistanceNeeded('模型输出未通过隐私检查。')
            outcome='received';return answer
        except httpx.HTTPError:
            raise AssistanceNeeded('研究助手连接中断，已保留数据；不会自动重发这次请求。') from None
        finally:
            self.append({'kind':'finish','id':attempt,'outcome':outcome,'usage':usage,'at':time.time()})
