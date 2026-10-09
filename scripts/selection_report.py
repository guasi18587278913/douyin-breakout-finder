"""Versioned, evidence-bound selection explainers; legacy research remains separate."""
import json
import math
import re
from pathlib import Path
from core import StopRun, digest, now, stamp

FORMAT = 'selection-report-v1'
ROOT = Path(__file__).resolve().parents[1]
FIELDS = {'format', 'topic_label', 'title', 'intro', 'lessons', 'closing', 'limitations'}
BANNED = ('同学们', '下午好', '今天我们就聊', '筛了一轮', '通知摘要', 'qualified', 'limited', '中位数')
NUMBER = r'(?:\d[\d,]*(?:\.\d+)?|[零〇一二两三四五六七八九十百千万亿]+)'
STATEMENT = re.compile(r'(?:播放(?:量)?|粉丝(?:量|数)?|倍数)[^。！？；\n]{0,16}' + NUMBER + r'|'+NUMBER+r'\s*(?:万次|亿次|万|亿|次|个|位|名)?\s*(?:播放|粉丝|倍)')
BODY_CLAIMS = ('视频开头', '视频结尾', '第一镜头', '第二镜头', '镜头切换', '口播原话', '逐字稿显示', '我看完', '我看了', '开场说', '开头说')


def instructions():
    try:
        guide = (ROOT / 'references/report-standard.md').read_text(encoding='utf-8')
        example = (ROOT / 'references/report-example.json').read_text(encoding='utf-8')
    except OSError:
        raise StopRun('报告标准文件缺失，请重新获取完整仓库后重试。') from None
    return ('以下是用户认可的写作样稿和本工具交付标准。写作时优先保留样稿的讲述方式与展开程度。'
            '先讲创作者会遇到的具体困难，再用案例讲明白一种做法，接着给读者能照着尝试的选题。'
            '原句准确、适合当前案例时可以直接保留，不要求为了换一种说法而改写。'
            '若本轮与样稿是相同案例且证据吻合，可以沿用对应原句；案例、主题或证据不同时，必须依据本轮材料重新写，不能把样稿当事实来源。'
            '案例ID只能从本轮候选中选择，证据ID只能引用本轮提供的条目。'
            '借鉴例子尽量包含日常生活中一眼能想象的场景，避免全部局限于同类工具。'
            '不要给已认可的样稿再叠加简化英语或去AI味改写步骤。'
            '系统会用真实数据生成开场统计和案例播放段落，intro 不重复写这些统计。'
            '自由文字不要自行写播放量、粉丝量或倍数。只解释选题方法，不声称统计证明传播因果。\n'
            + guide + '\n写作示例：\n' + example)


def eligible_ids(store):
    return {vid for vid, r in store.state['results'].items()
            if r.get('status') == 'qualified'
            and store.state['candidates'].get(vid, {}).get('review', {}).get('decision') == 'include'
            and store.state['candidates'][vid]['review'].get('method') == 'human-or-agent'}


def schema(packet):
    from analyst import object_schema
    string = {'type': 'string'}
    strings = {'type': 'array', 'items': string}
    allowed = packet.get('featured_candidates', [])
    citations = sorted({e['id'] for vid in allowed for e in packet['evidence'].get(vid, [])})
    case = object_schema({'video_id': {'type': 'string', 'enum': allowed} if allowed else string,
                          'paragraphs': strings,
                          'evidence_ids': {'type': 'array', 'items': {'type': 'string', 'enum': citations} if citations else string}})
    lesson = object_schema({'title': string, 'cases': {'type': 'array', 'items': case},
                            'applications': {'type': 'array', 'items': object_schema({'audience': string, 'idea': string})},
                            'takeaway': string})
    return object_schema({'format': {'type': 'string', 'enum': [FORMAT]}, 'topic_label': string, 'title': string,
                          'intro': strings, 'lessons': {'type': 'array', 'items': lesson},
                          'closing': object_schema({'questions': strings, 'paragraphs': strings}),
                          'limitations': strings})


def exact(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise StopRun('%s 的字段不完整或包含多余字段。' % name)


def prose(value, name, limit=1000, suggestion=False, body_allowed=False):
    from research import text
    value = text(value, name, limit)
    if any(word in value for word in BANNED) or re.search(r'<(?:!--|/?[A-Za-z])|```|\*\*', value):
        raise StopRun('%s 请使用报告场景的中文口语纯文本，不含社群开场、内部术语或标记。' % name)
    if name == '示例选题' and value.startswith(('新选题建议', '选题建议：', '选题方向：')):
        raise StopRun('示例选题直接写具体内容，不重复建议前缀。')
    # Keep suggestions distinct from claims about the observed source videos.
    if STATEMENT.search(value):
        raise StopRun('%s 不要自行填写播放、粉丝或倍数统计，程序会统一展示。' % name)
    proposed = suggestion and any(word in value for word in ('你可以', '可以试', '假设', '如果'))
    if not body_allowed and not proposed and any(word in value for word in BODY_CLAIMS):
        raise StopRun('%s 不能在没有对应正文引用的位置还原视频开场、镜头或口播。' % name)
    return value


def paragraphs(value, name, minimum=1, maximum=4, **options):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise StopRun('%s 需要 %s–%s 段文字。' % (name, minimum, maximum))
    return [prose(v, name, **options) for v in value]


def unresolved_data(store):
    s = store.state
    if any(q.get('error') or not q.get('pages', 0) for q in s['queries'].values()):
        return True
    if not any(q.get('pages', 0) > 0 for q in s['queries'].values()):
        return True
    for vid, v in s['candidates'].items():
        review = v.get('review', {})
        if review.get('decision') in ('pending', 'unsure') or review.get('method') != 'human-or-agent':
            return True
        if review.get('decision') == 'include' and s['results'].get(vid, {}).get('status') not in ('qualified', 'limited', 'below_threshold', 'excluded'):
            return True
    last = {}
    for event in store.events:
        if event.get('key') and event.get('event') in ('reserved', 'request', 'failed', 'saved'):
            last[event['key']] = event['event']
    if any(r.get('status') == 'pending' for r in s['results'].values()) or any(v != 'saved' for v in last.values()):
        return True
    operations = {e['key']: e.get('operation') for e in store.events if e.get('event') == 'reserved' and e.get('key')}
    for stop in s.get('stops', []):
        phase = stop.get('phase')
        if phase not in ('search', 'verify'):
            continue
        relevant = ('search',) if phase == 'search' else ('posts', 'stats')
        try:
            stopped_at = stamp(stop['at'])
            recovered = any(e.get('event') == 'saved' and operations.get(e.get('key'), e.get('operation')) in relevant and stamp(e['at']) > stopped_at for e in store.events)
        except (KeyError, TypeError, ValueError, StopRun):
            recovered = False
        if not recovered:
            return True
    return False


def completion_hash(store):
    return digest({'format': FORMAT, 'queries': store.state['queries'], 'stats': store.state['stats'],
                   'criteria': store.config, 'events': store.events, 'stops': store.state.get('stops', [])})


def validate(store, answer):
    from research import evidence, evidence_hash, ids
    exact(answer, FIELDS, '选题报告')
    if answer['format'] != FORMAT:
        raise StopRun('选题报告格式版本不匹配。')
    eligible = eligible_ids(store)
    result = {'format': FORMAT, 'kind': 'selection-explainer', 'topic_label': prose(answer['topic_label'], '主题', 40),
              'headline': prose(answer['title'], '报告标题', 120), 'intro': paragraphs(answer['intro'], '引言', 1, 3),
              'lessons': [], 'cases': {}, 'limitations': paragraphs(answer['limitations'], '依据说明', 1, 5),
              'written_at': now()}
    lessons = answer['lessons']
    if not isinstance(lessons, list) or not (1 if eligible else 0) <= len(lessons) <= 2:
        raise StopRun('请基于已核验案例给出 1–2 个选题方法；无合格案例时不编造案例。')
    seen = set()
    for lesson in lessons:
        exact(lesson, {'title', 'cases', 'applications', 'takeaway'}, '选题方法')
        item = {'title': prose(lesson['title'], '方法标题', 100), 'cases': [], 'applications': [],
                'takeaway': prose(lesson['takeaway'], '可借鉴的做法', 500)}
        if not isinstance(lesson['cases'], list) or not 1 <= len(lesson['cases']) <= 3:
            raise StopRun('每个方法需要 1–3 个本轮已核验案例。')
        for case in lesson['cases']:
            exact(case, {'video_id', 'paragraphs', 'evidence_ids'}, '案例解读')
            vid = case['video_id']
            if not isinstance(vid, str) or vid not in eligible or vid in seen:
                raise StopRun('案例必须通过本轮相关性与数据核验，且不能重复列入。')
            seen.add(vid)
            if len(seen) > 3:
                raise StopRun('正文精选案例至多 3 条，其他结果保留在明细。')
            r = store.state['results'][vid]
            values = [r.get('play_count'), r.get('median_plays'), r.get('ratio')]
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values):
                raise StopRun('精选案例的播放统计尚未完成，不能纳入正文。')
            available = {e['id']: e for e in evidence(store, vid)}
            refs = ids(case['evidence_ids'], set(available), '案例依据', 1, 10)
            deep = any(available[i]['kind'] in ('transcript', 'video-notes') for i in refs)
            body = paragraphs(case['paragraphs'], '案例讲述', 2, 6, body_allowed=deep)
            if not deep and any(word in ''.join(body) for word in BODY_CLAIMS):
                raise StopRun('只有标题和简介的案例不能还原视频开场、镜头或口播。')
            clean = {'video_id': vid, 'paragraphs': body, 'evidence_ids': refs,
                     'basis': 'content-read' if deep else 'title-description'}
            item['cases'].append(clean)
            result['cases'][vid] = clean
        apps = lesson['applications']
        if not isinstance(apps, list) or not 1 <= len(apps) <= 4:
            raise StopRun('每个方法需要 1–4 个具体的迁移示例。')
        for app in apps:
            exact(app, {'audience', 'idea'}, '迁移示例')
            item['applications'].append({'audience': prose(app['audience'], '示例场景', 60),
                                         'idea': prose(app['idea'], '示例选题', 300, suggestion=True, body_allowed=True)})
        result['lessons'].append(item)
    exact(answer['closing'], {'questions', 'paragraphs'}, '结尾')
    result['closing'] = {'questions': paragraphs(answer['closing']['questions'], '选题提问', 1, 4),
                         'paragraphs': paragraphs(answer['closing']['paragraphs'], '结尾建议', 1, 3, suggestion=True)}
    result['lead'] = '\n\n'.join(result['intro'])
    result['status'] = 'complete' if seen or not unresolved_data(store) else 'partial'
    result['data_hash'] = digest({'candidates': store.state['candidates'], 'results': store.state['results']})
    result['evidence_hash'] = evidence_hash(store, result['cases'])
    result['completion_hash'] = completion_hash(store)
    return result
