"""Deterministic intake, conservative relevance and evidence-only report notes."""
import json
import re
import urllib.parse
import urllib.request
from core import StopRun, default_config, money, now, validate_config
from tikhub import BASE, ENDPOINTS, NoRedirect
from workflow import apply_review, annotate

GOALS = {'topics': '寻找选题', 'opening': '研究标题切入', 'validate': '验证内容方向'}


def text_field(data, name, limit=500, required=False):
    value = data.get(name, '')
    if not isinstance(value, str) or len(value) > limit:
        raise StopRun('请检查「%s」：需要不超过 %s 字的文字。' % (name, limit))
    value = value.strip()
    if required and not value:
        raise StopRun('请填写「%s」。' % name)
    return value


def terms(value, limit=8):
    values = list(dict.fromkeys(x.strip() for x in re.split(r'[,，、;；\n]', value) if x.strip()))
    if len(values) > limit or any(len(x) > 80 for x in values):
        raise StopRun('请使用至多 %s 个短关键词，以逗号分隔。' % limit)
    return values


def intake(data):
    if not isinstance(data, dict):
        raise StopRun('需求格式不正确。')
    topic = text_field(data, 'topic', 120, True)
    goal = data.get('goal', 'topics')
    if goal not in GOALS:
        raise StopRun('请选择本次目标。')
    audience = text_field(data, 'audience', 200)
    current = text_field(data, 'current', 500)
    keywords = terms(text_field(data, 'keywords', 700)) or [topic]
    anchors = terms(text_field(data, 'anchors', 700)) or keywords
    excludes = terms(text_field(data, 'excludes', 700))
    return dict(topic=topic, goal=goal, audience=audience, current=current,
                keywords=keywords, anchors=anchors, excludes=excludes)


def config_for(answers, pricing, budget):
    b = money(budget, '预算')
    if b <= 0 or b > 100:
        raise StopRun('本轮预算请填写大于 0、至多 100 的美元金额。')
    c = default_config(answers['topic'], answers['keywords'], str(b))
    c['scope'] = '%s；目标：%s；受众：%s；纳入词：%s；排除词：%s。' % (
        answers['topic'], GOALS[answers['goal']], answers['audience'] or '未限定',
        '、'.join(answers['anchors']), '、'.join(answers['excludes']) or '无')
    c['intake'] = answers
    c['pricing'] = pricing
    return validate_config(c)


def contains(text, term):
    # Latin abbreviations must be separate words: FDE must not match abcFDExyz.
    if re.fullmatch(r'[A-Za-z0-9_ -]+', term):
        return bool(re.search(r'(?<![A-Za-z0-9_])' + re.escape(term) + r'(?![A-Za-z0-9_])', text, re.I))
    return term.casefold() in text.casefold()


def review_by_rules(store, answers):
    reviews = {}
    for vid, video in store.state['candidates'].items():
        title = video.get('title') or ''
        denied = [term for term in answers['excludes'] if contains(title, term)]
        found = [term for term in answers['anchors'] if contains(title, term)]
        if denied:
            decision, reason = 'exclude', '标题/描述包含排除词：' + '、'.join(denied)
        elif found:
            decision, reason = 'include', '规则初筛：标题/描述包含纳入词「' + '、'.join(found) + '」。尚未观看视频，语义相关性仍需复核。'
        else:
            decision, reason = 'unsure', '标题/描述没有明确纳入词，留待语义复核；不自动消耗作者核验预算。'
        reviews[vid] = dict(decision=decision, reason=reason, priority=1, method='keyword-rule')
    apply_review(store, reviews)
    return reviews


def evidence_notes(store):
    """No generative model controls numbers, provenance, selection or facts."""
    analyses = {}
    for vid, r in store.state['results'].items():
        if r['status'] not in ('qualified', 'limited'):
            continue
        title = r['target'].get('title') or ''
        patterns = []
        if re.search(r'\d|[一二三四五六七八九十]个|[一二三四五六七八九十]步', title):
            patterns.append('含数字或步骤表达')
        if re.search(r'怎么|如何|为什么|[？?]', title):
            patterns.append('以问题引入')
        if re.search(r'避坑|误区|别再|不要|踩坑', title):
            patterns.append('含避坑或纠偏表达')
        observed = '；'.join(patterns) if patterns else '未命中预设标题结构；建议打开原视频观察'
        analyses[vid] = {
            'basis': 'title-description',
            'evidence_note': '程序读取了标题/描述、公开指标与同作者对照。未观看视频或读取逐字稿。',
            'summary': '本条通过播放倍数筛选，标题/描述为「%s」。这说明其累计播放高于本轮选定的同作者邻近作品，不证明某个写法导致爆款。' % title[:300],
            'opening': '可见标题观察：' + observed + '。真实口播开场尚未核验。',
            'structure': '正文叙事结构暂缺证据。复看时记录：开场提出的问题 → 使用的案例/演示 → 结尾行动。',
            'useful_angle': '围绕「%s」中的具体问题，用自己的案例制作一个小范围测试；保留原视频链接，比较主题与受众是否适合自己。' % title[:120],
            'comments_summary': '未自动查询评论；不推断观众态度。',
        }
    annotate(store, analyses)


def parse_price(payload, endpoint):
    """Fail closed unless both endpoint identity and USD unit are explicit."""
    if not isinstance(payload, dict) or payload.get('code', 200) not in (0, 200, '0', '200'):
        raise StopRun('价格服务暂未返回有效报价。')
    row = payload.get('data', payload)
    if not isinstance(row, dict):
        raise StopRun('价格数据格式发生变化。')
    if row.get('endpoint', row.get('uri', row.get('path'))) != endpoint:
        raise StopRun('无法确认报价对应的接口，请在高级设置中核对。')
    value = row.get('price_usd_per_request')
    if value is None:
        if str(row.get('currency', '')).upper() != 'USD':
            raise StopRun('报价未明确美元单位，不能自动开始收费查询。')
        # Do not interpret package prices or total_price as per-call prices.
        value = row.get('price_per_request')
        if value is None and row.get('billing_unit') in ('request', 'per_request'):
            value = row.get('unit_price')
    amount = money(value, '接口单价')
    if amount <= 0 or amount > 100:
        raise StopRun('报价单价无效，请核对 TikHub 后台。')
    return str(amount)


def fetch_prices():
    prices = {}
    opener = urllib.request.build_opener(NoRedirect())
    try:
        for op, (_, endpoint) in ENDPOINTS.items():
            url = BASE + '/api/v1/tikhub/user/get_endpoint_info?' + urllib.parse.urlencode({'endpoint': endpoint})
            # Anonymous metadata only: no key is sent or account billed by this code.
            req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'douyin-breakout-finder/0.2.0'})
            with opener.open(req, timeout=12) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise StopRun('价格数据超过安全大小限制。')
            prices[op] = parse_price(json.loads(raw), endpoint)
    except StopRun:
        raise
    except Exception:
        raise StopRun('自动报价暂不可用。请展开高级设置，按 TikHub 后台填写已核实单价；也可以先体验免费示例。')
    return {'usd_per_request': prices, 'checked_at': now(), 'source_url': BASE + '/api/v1/tikhub/user/get_endpoint_info'}
