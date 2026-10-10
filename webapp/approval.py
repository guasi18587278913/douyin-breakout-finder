"""Task-specific financial authorization, bound to the original private journals."""
import hashlib
import json
from decimal import Decimal
from bridge import AssistanceNeeded


def approved(folder):
    path=folder/'approved-continuation.json'
    if not path.is_file():return {}
    try:
        value=json.loads(path.read_text())
        if value.get('job_id')!=folder.name or type(value.get('max_requests')) is not int or value['max_requests']!=150:raise ValueError()
        for key,amount in (('data_limit_usd','.50'),('model_limit_usd','.15'),('total_limit_usd','.65')):
            if Decimal(value[key])!=Decimal(amount):raise ValueError()
        if type(value.get('usage_settlement')) is not bool or type(value.get('expand_data')) is not bool:raise ValueError()
        for label,journal in (('model',folder/'model-events.jsonl'),('data',folder/'research/requests.jsonl')):
            length=value['prior_'+label+'_bytes'];expected=value['prior_'+label+'_sha256']
            if type(length) is not int or length<0 or not isinstance(expected,str) or len(expected)!=64:raise ValueError()
            raw=journal.read_bytes() if journal.is_file() else b''
            if len(raw)<length or hashlib.sha256(raw[:length]).hexdigest()!=expected:raise ValueError()
        hashes=value['legacy_settlement_hashes']
        if not isinstance(hashes,list) or any(not isinstance(h,str) or len(h)!=64 for h in hashes):raise ValueError()
        return value
    except (KeyError,ValueError,TypeError,ArithmeticError,OSError):
        raise AssistanceNeeded('任务费用授权与原始记录不一致，已停止新的收费操作。') from None
