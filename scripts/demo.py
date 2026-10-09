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

def run_demo(out, selection=False):
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
        demo_research(store)
        if selection: demo_selection(store)
        summary=render(store)
    return store,summary


def demo_research(store):
    """Synthetic narrative and transcript fixture; never used in real research."""
    from research import add_evidence,accept_research
    selected=[vid for vid,r in store.state['results'].items() if r['status'] in ('qualified','limited')]
    if not selected:return
    cases={}
    for vid in selected:
        source=store.root/('demo-'+vid+'.txt')
        source.write_text('合成演示逐字稿：每天都有人把供应商信息手工录入 ERP。今天我们用一个简化任务展示 AI 如何从文件中提取字段、校验必填项，再把数据提交到系统。先看原来的重复操作，再逐步对照自动化流程。最后检查异常信息，保留人工确认。这是教学测试内容，不代表任何真实项目。',encoding='utf-8')
        entry=add_evidence(store,vid,source,'transcript','合成教学逐字稿，与演示视频条目一同构造，不是真实视频。')
        source.unlink()
        cases[vid]={'title':'用一个真实感的业务任务讲清 AI 落地','category':'任务演示',
            'summary':'合成案例围绕供应商信息录入，把抽象的 AI 落地变成能逐步核对的业务操作。',
            'title_angle':'用具体任务名称降低理解门槛，让观看者先知道这段内容解决什么问题。',
            'opening':'合成讲述先提出每天重复录入信息的问题，再交代本次要演示的任务。',
            'outline':['原来的重复操作','提取字段与检查必填项','把信息提交到系统','异常处理与人工确认'],
            'takeaway':'可以选择自己行业中的一个小任务，展示输入、处理、输出和人工检查；效果需要自行验证。',
            'limitations':'完全合成的教学样例；没有真实客户、项目效果或平台传播结论。',
            'evidence_ids':[entry['id']]}
    # The fixture's labels explicitly cover its synthetic transcripts as well.
    apply_review(store, {vid: dict(v['review']) for vid, v in store.state['candidates'].items()})
    answer={'headline':'从具体任务出发，观察内容怎样组织','lead':'这是一份完整研究结构的合成演示：先归纳内容方向，再拆解具体案例，最后把观察转换成可测试的选题。所有数据与逐字稿均为教学构造。',
        'findings':[{'title':'用任务解释抽象概念','text':'样例没有停在概念介绍，而是沿一个可以检查的业务动作组织讲述。','video_ids':selected},
                    {'title':'把人工检查写进流程','text':'自动化演示以异常处理和人工确认收尾，提醒读者结果仍需核实。','video_ids':selected}],
        'categories':[{'title':'业务任务演示','text':'按问题、操作、结果与检查来组织内容，适合在自己的行业寻找小场景。','video_ids':selected}],
        'cases':cases,'next_topics':[{'title':'我把一项重复录入工作交给 AI，逐步检查会发生什么','text':'换成自己的具体任务，展示真实输入和失败情况，不预先承诺效率提升。','video_ids':selected}],
        'limitations':['本页为合成数据示例，没有调用真实付费接口。','内容结构来自明确标注的合成逐字稿。','其他未核验样本仍单列，不并入已核验数量。']}
    accept_research(store,answer)


def demo_selection(store):
    """Current delivery example; the older fixture remains available for compatibility tests."""
    from selection_report import FORMAT, eligible_ids
    from research import accept_research
    selected = sorted(eligible_ids(store))[:3]
    cases = [{'video_id':vid, 'paragraphs':[
        'FDE 这个词听起来有点抽象。这条合成案例把它放进了一个具体任务：每天有人需要把供应商信息录进系统。',
        '读者先知道自己会在什么地方用到这套办法，再去理解它怎么处理信息，就容易跟上了。',
        '你也可以从工作里挑一件反复做的小事，解释原来怎么做、现在准备怎么做，以及结果该怎么检查。'],
        'evidence_ids':[vid+':title']} for vid in selected]
    answer = {'format':FORMAT, 'topic_label':'FDE', 'title':'免费示例：把抽象概念放进具体任务',
              'intro':['下面用合成案例演示报告的阅读方式。正式调研会按你输入的主题，使用实际取得的作品和数据。'],
              'lessons':[{'title':'概念不好懂，就从眼前的一件事讲起', 'cases':cases,
                          'applications':[{'audience':'办公教程','idea':'同一份订单信息，怎样减少一次重复录入？'},
                                          {'audience':'收纳','idea':'每天出门都找不到钥匙，玄关怎样安排才顺手？'}],
                          'takeaway':'先让人知道这件事能帮上什么忙，再解释用到了什么工具。'}] if cases else [],
              'closing':{'questions':['我的用户眼前需要完成什么事？','我能拿什么例子把它讲清楚？'],
                         'paragraphs':['把例子换成你熟悉的一件事，先拍一个小范围演示，再检查效果。']},
              'limitations':['全部为合成数据和教学示例，不是对真实账号的研究。']}
    accept_research(store, answer, expected_format=FORMAT)
