"""Offline subprocess fixture. Never imports or invokes a real model/provider."""
import json
import os
import sys
import time
from pathlib import Path

provider, behavior, audit = sys.argv[1:4]
args = sys.argv[4:]
request = json.loads(sys.stdin.read().split('\n', 1)[1])
if audit != '-':
    Path(audit).write_text(json.dumps({'request': request, 'environment': dict(os.environ)}))
if behavior == 'slow':
    time.sleep(20)
if behavior == 'fail':
    sys.exit(1)
if behavior == 'oversized':
    print('x' * (3 * 1024 * 1024))
    time.sleep(20)
packet = request['packet']
stage = request['stage']
if stage == 'prepare-research':
    answer = {'ready': True, 'search_keywords': [] if packet.get('resume') else packet.get('initial_keywords', ['FDE'])}
elif stage == 'semantic-review':
    answer = {'reviews': {v['id']: {'decision': 'include', 'reason': '离线合成候选，与本轮主题相关。', 'priority': 1}
                          for v in packet['candidates']}, 'next_keywords': [], 'search_reason': ''}
elif packet.get('schema') == 'selection-report-v1':
    from report_fixture import selection_answer
    answer = selection_answer(packet)
else:
    cases = {}
    for row in packet['results']:
        if row['status'] not in ('qualified', 'limited'):
            continue
        vid = row['target']['id']
        entries = packet['evidence'][vid]
        deep = next((e for e in entries if e['kind'] == 'transcript'), None)
        cases[vid] = {'title': '离线合成任务案例', 'category': '任务演示', 'summary': '以录入任务解释流程。',
                      'title_angle': '具体任务作为入口。', 'opening': '提出重复录入问题。' if deep else '',
                      'outline': ['展示输入', '核对输出'] if deep else [], 'takeaway': '参考任务讲解方式。',
                      'limitations': '合成测试材料。', 'evidence_ids': [(deep or entries[0])['id']]}
    vids = list(cases) or [v['id'] for v in packet['candidates']][:1]
    item = {'title': '任务组织内容', 'text': '合成案例通过具体任务帮助理解，实际表现需要进一步验证。', 'video_ids': vids}
    answer = {'headline': '自动生成的离线合成报告', 'lead': '用于验证后台分析，不是真实调研。',
              'findings': [item] if vids else [], 'categories': [item] if cases else [],
              'cases': cases, 'next_topics': [item] if cases else [], 'limitations': ['全部为合成测试数据。']}
if behavior == 'invalid':
    answer = {'unexpected': True}
if provider == 'codex':
    path = Path(args[args.index('--output-last-message') + 1])
    path.write_text(json.dumps(answer, ensure_ascii=False), encoding='utf-8')
else:
    print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': answer}))
