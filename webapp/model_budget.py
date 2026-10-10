"""Append-only usage settlements; original reservations and failures remain immutable."""
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from bridge import AssistanceNeeded

INPUT=Decimal('0.30'); OUTPUT=Decimal('1.20')
SOURCE='https://api-docs.deepseek.com/quick_start/pricing/'
VERSION='deepseek-flash-peak-2026-10-10'


def fingerprint(row):
    return hashlib.sha256(json.dumps(row,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def append(path, row):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd=os.open(str(path),os.O_APPEND|os.O_CREAT|os.O_WRONLY,0o600)
    with os.fdopen(fd,'a') as stream:
        stream.write(json.dumps(row,separators=(',',':'))+'\n');stream.flush();os.fsync(stream.fileno())


def proposal(rows, legacy_hashes=()):
    """Only known complete usage is releasable; any ambiguous attempt keeps its hold."""
    result={}
    for reserve in rows:
        if reserve.get('kind')!='reserve':continue
        ident=reserve.get('id')
        if type(ident) is not int or ident<1:continue
        same=[r for r in rows if r.get('id')==ident]
        if len(same)!=2 or same[0]!=reserve or same[1].get('kind')!='finish':continue
        finish=same[1];usage=finish.get('usage',{})
        legacy=fingerprint([reserve,finish]) in legacy_hashes
        if (('input_bound' not in reserve or 'max_tokens' not in reserve or finish.get('provider_response_received') is not True) and not legacy):continue
        if reserve.get('model')!='deepseek-flash' or reserve.get('price_source')!=SOURCE or reserve.get('price_checked_at')!='2026-10-10':continue
        try:
            date=datetime.fromtimestamp(reserve['at'],timezone.utc).date().isoformat()
            if date!='2026-10-10' or finish['at']<reserve['at']:continue
            counts=[usage.get('prompt_tokens'),usage.get('completion_tokens')]
            if any(type(n) is not int or n<0 for n in counts) or counts[0]==0:continue
            if 'input_bound' in reserve and counts[0]>reserve['input_bound']:continue
            if 'max_tokens' in reserve and counts[1]>reserve['max_tokens']:continue
            amount=(Decimal(counts[0])*INPUT+Decimal(counts[1])*OUTPUT)/1000000
            held=Decimal(reserve['usd'])
            if not held.is_finite() or not 0<=amount<=held:continue
        except (ValueError,TypeError,KeyError,ArithmeticError,OverflowError):continue
        result[ident]={'kind':'usage-settlement','id':ident,'reserve_sha256':fingerprint(reserve),
            'finish_sha256':fingerprint(finish),'rate_version':VERSION,'price_source':SOURCE,
            'usage':usage,'held_usd':str(held),'settled_upper_usd':str(amount),'released_usd':str(held-amount)}
    return result


def effective_spent(ledger, rows, enabled=False, persist=False):
    gross=sum((Decimal(r['usd']) for r in rows if r['kind']=='reserve'),Decimal(0))
    if not enabled:return gross
    path=Path(str(ledger)+'.settlements.jsonl')
    from webapp.approval import approved
    approval=approved(Path(ledger).parent)
    if not approval.get('usage_settlement'):raise AssistanceNeeded('本任务尚未授权用量结算。')
    legacy=approval.get('legacy_settlement_hashes',[])
    expected=proposal(rows,legacy);recorded={}
    try:
        for line in path.read_text().splitlines() if path.exists() else []:
            row=json.loads(line);ident=row['id']
            if ident in recorded or expected.get(ident)!=row:raise ValueError()
            recorded[ident]=row
    except Exception:raise AssistanceNeeded('模型结算记录与原始用量不一致，已停止新的调用。') from None
    if persist:
        for ident,row in expected.items():
            if ident not in recorded:append(path,row)
    # Dry planning may preview eligible settlements; generate persists them before reserving.
    return gross-sum((Decimal(r['released_usd']) for r in expected.values()),Decimal(0))
