"""Entirely offline demonstration through the same normalization and workflow code."""
import json
from pathlib import Path
from core import Store, atomic_json, default_config, digest, now, read_json
from tikhub import normalize
from workflow import search, apply_review, verify, comments
from report import render

class ReplayClient:
    def __init__(self, store, fixture):
        if not store.state.get('demo'):
            raise ValueError('ReplayClient requires a demo run.')
        self.store, self.fixture = store, fixture

    def call(self, op, params):
        key = digest({'op': op, 'params': params})
        path = self.store.root / 'cache' / (key + '.json')
        cached = read_json(path)
        if cached:
            return cached['data']
        self.store.reserve(op, key)
        f = self.fixture
        if op == 'search':
            ids = f['queries'][params['keyword']]
            data = {'business_data': [{'data': {'aweme_info': f['videos'][i]}} for i in ids],
                    'business_config': {'has_more': 0}}
        elif op == 'posts':
            aid = params['sec_user_id']
            data = {'sec_uid': aid, 'aweme_list': [f['videos'][i] for i in f['authors'][aid]],
                    'has_more': 0, 'max_cursor': 0}
        elif op == 'stats':
            data = {'statistics_list': [dict(aweme_id=i,play_count=f['plays'][i])
                                         for i in params['aweme_ids'].split(',')]}
        elif op == 'comments':
            data = {'comments': [{'cid': 'demo-comment', 'text': '离线演示：如何连接现有业务系统？', 'digg_count': 2}],
                    'has_more': 0, 'cursor': 0}
        else:
            raise ValueError(op)
        result = normalize(op, {'code': 200, 'data': data}, params)
        atomic_json(path, {'operation':op,'params':params,'captured_at':now(),'demo':True,'data':result})
        self.store.event({'event':'saved','operation':op,'key':key,'demo':True})
        return result

def run_demo(out):
    fixture = read_json(Path(__file__).resolve().parent.parent/'assets'/'demo.json')
    c = default_config('FDE 离线演示', list(fixture['queries']), '0')
    c['scope'] = '合成的 FDE 视频示例，只用于检查安装、流程和计算，不是调研结论。'
    c['pricing']['usd_per_request'] = {k:'0' for k in c['pricing']['usd_per_request']}
    store = Store.create(out,c,as_of=fixture['as_of'],demo=True)
    with store.lock():
        client = ReplayClient(store,fixture)
        search(store,client)
        apply_review(store,fixture['reviews'])
        verify(store,client)
        chosen=[i for i,r in store.state['results'].items() if r['status']=='qualified']
        comments(store,client,chosen)
        summary=render(store)
    return store,summary
