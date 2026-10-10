"""Synthetic reviewer response. Tests protocol, not real model judgment."""
def verdict(packet, fail_path=None):
    sources={e['id']:e['text'] for items in packet['evidence'].values() for e in items}
    return {'input_sha256':packet['input_sha256'],'checks':[
        {'path':target['path'],'criterion':target['criterion'],'pass':target['path']!=fail_path,
         'quote':target['text'][:60],'reason':'合成审核说明：已检查这段案例与材料及具体创作对象。',
         'evidence_refs':[{'id':ref,'quote':sources[ref][:50]} for ref in target['evidence_ids'][:1]]}
        for target in packet['targets']]}


def approval(folder,legacy=()):
    import json,hashlib
    value={'job_id':folder.name,'usage_settlement':True,'expand_data':False,'max_requests':150,
           'data_limit_usd':'.50','model_limit_usd':'.15','total_limit_usd':'.65','legacy_settlement_hashes':list(legacy)}
    for name,path in (('model',folder/'model-events.jsonl'),('data',folder/'research/requests.jsonl')):
        data=path.read_bytes() if path.is_file() else b''
        value['prior_'+name+'_bytes']=len(data);value['prior_'+name+'_sha256']=hashlib.sha256(data).hexdigest()
    (folder/'approved-continuation.json').write_text(json.dumps(value))
    return value
