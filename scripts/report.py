"""Local HTML/CSV output. Never interpolate remote text as markup."""
import csv
import html
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from core import atomic_json, now
from workflow import keyword_quality
from research import report_state
from research_render import narrative

LABELS = {'qualified': '已核验', 'limited': '有限样本', 'pending': '待核验',
          'below_threshold': '未达倍数', 'excluded': '未纳入'}
E = lambda value: html.escape(str(value), quote=True)

def fmt(value):
    if value is None:
        return '未取得'
    return ('{:,.0f}'.format(value) if value == int(value) else '{:,.1f}'.format(value))

def date_text(ts):
    if ts is None:
        return '未知'
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime('%Y-%m-%d')

def csv_safe(value):
    if value is None:
        return ''
    text = str(value)
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text

def video_link(v, demo):
    if demo:
        return E(v.get('title') or v['id'])
    return '<a target="_blank" rel="noopener noreferrer" href="https://www.douyin.com/video/%s">%s</a>' % (
        E(v['id']), E(v.get('title') or v['id']))

def write_csv(path, headers, rows):
    temp = path.with_suffix('.csv.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(headers)
        for row in rows:
            w.writerow([csv_safe(v) for v in row])
    temp.replace(path)

def render(store):
    s, c = store.state, store.config
    demo = s.get('demo', False)
    results = sorted(s['results'].values(), key=lambda r: r.get('ratio') or -1, reverse=True)
    selected = [r for r in results if r['status'] in ('qualified', 'limited')]
    metrics = keyword_quality(store)
    reservations = store.reservations()
    estimated = store.reserved_cost()
    failures = [e for e in store.events if e['event'] == 'failed']
    research=report_state(store)
    summary = {
        'research_status':research['status'], 'report_complete':research['status']=='complete',
        'review_pending_count':research['review_pending_count'],
        'generated_at': now(), 'as_of': s['as_of'], 'demo': demo, 'topic': c['topic'],
        'scope': c['scope'], 'criteria': {k: c[k] for k in (
            'max_followers', 'min_ratio', 'baseline_count', 'min_baseline_count',
            'comparison_window_days', 'min_age_days', 'search_days')},
        'candidate_count': len(s['candidates']), 'selected_count': len(selected),
        'qualified_count': sum(r['status'] == 'qualified' for r in results),
        'limited_count': sum(r['status'] == 'limited' for r in results),
        'pending_count': sum(r['status'] == 'pending' for r in results),
        'request_attempts': len(reservations), 'failed_attempts': len(failures),
        'reserved_usd_at_configured_prices': str(estimated), 'budget_usd': c['budget_usd'],
        'billing_note': '离线演示无真实费用' if demo else
            '这是按配置单价为所有尝试预留的金额，不是已核实账单；失败请求也保守计入。实际以 TikHub 账单为准。',
        'keyword_quality': metrics, 'stops': s['stops'],
    }
    atomic_json(store.root / 'summary.json', summary)
    atomic_json(store.root / 'results.json', results)
    atomic_json(store.root / 'keyword-quality.json', metrics)
    rows, base_rows = [], []
    for r in results:
        t = r['target']
        rows.append([LABELS[r['status']], t['title'], t['author_name'], t.get('followers'),
                     date_text(t['published_at']), r.get('play_count'), r.get('median_plays'),
                     round(r['ratio'], 3) if r.get('ratio') is not None else None,
                     len(r['baseline']), r['reason'], '' if demo else 'https://www.douyin.com/video/' + t['id']])
        for b in r['baseline']:
            base_rows.append([t['title'], t['author_name'], b['title'], date_text(b['published_at']),
                              s['stats'].get(b['id'], {}).get('play_count'),
                              '' if demo else 'https://www.douyin.com/video/' + b['id']])
    write_csv(store.root / 'results.csv', ['状态','标题','作者','采集时粉丝','发布日期(北京时间)',
              '播放量','对照中位数','倍数','对照数量','说明','原视频'], rows)
    write_csv(store.root / 'comparisons.csv', ['目标标题','作者','对照作品','对照日期','对照播放量','对照链接'], base_rows)
    table = ''
    cards = ''
    for r in selected:
        t = r['target']; vid = t['id']; bs = r['baseline']
        ratio = fmt(r['ratio']) + '×'
        table += '<tr><td><a href="#v-%s">%s</a><small>%s · %s 粉丝</small></td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td><td>%s / %s 条</td></tr>' % (
            E(vid), E(t['title']), E(t['author_name']), fmt(t.get('followers')), fmt(r.get('play_count')),
            fmt(r.get('median_plays')), ratio, LABELS[r['status']], len(bs))
        bases = ''.join('<tr><td>%s</td><td>%s</td><td class="num">%s</td></tr>' % (
            date_text(b['published_at']), video_link(b, demo), fmt(s['stats'].get(b['id'], {}).get('play_count')))
            for b in sorted(bs, key=lambda b: b['published_at']))
        a = s['analyses'].get(vid)
        analysis = '<p class="muted">未做内容拆解。当前入选依据为已记录的相关性初审与播放对照。</p>'
        if a:
            analysis = '<div class="analysis"><p><b>分析依据：</b>%s（%s）</p>%s</div>' % (
                E(a['evidence_note']), E(a['basis']), ''.join('<p><b>%s：</b>%s</p>' % (label, E(a[key]))
                    for key,label in [('summary','摘要'),('opening','开头'),('structure','结构'),
                                      ('useful_angle','可参考之处'),('comments_summary','评论观察')] if a.get(key)))
        comments = s['comments'].get(vid)
        comment_html = ''
        if comments:
            comment_html = '<details><summary>评论样本（%s 条）</summary><p class="muted">%s</p><ul>%s</ul></details>' % (
                len(comments['items']), E(comments['sample_note']), ''.join('<li>%s</li>' % E(x['text']) for x in comments['items']))
        cards += '<article id="v-%s"><h2>%s</h2><p>%s · %s 发布 · %s</p><p>目标播放 <b>%s</b>，%s 条对照中位数 <b>%s</b>，倍数 <b>%s</b>。</p><p class="muted">相关性初审：%s</p>%s<details><summary>查看逐条对照</summary><div class="scroll"><table><tr><th>日期</th><th>同作者作品</th><th>播放量</th></tr>%s</table></div></details>%s</article>' % (
            E(vid), video_link(t,demo), E(t['author_name']),date_text(t['published_at']),LABELS[r['status']],
            fmt(r['play_count']),len(bs),fmt(r['median_plays']),ratio,E(t['review']['reason']),analysis,bases,comment_html)
    if not table:
        table = '<tr><td colspan="5">本轮暂无入选条目。请查看相关性审查、待核验原因和预算记录。</td></tr>'
    other = ''.join('<tr><td>%s<small>%s</small></td><td>%s</td><td>%s</td></tr>' % (
        video_link(r['target'],demo),E(r['target']['author_name']),LABELS[r['status']],E(r['reason']))
        for r in results if r['status'] not in ('qualified','limited'))
    quality = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
        E(q['keyword']),q['returned_unique'],q['reviewed'],
        ('%.0f%%' % (q['relevance_rate']*100)) if q['relevance_rate'] is not None else '未审',
        q['first_discovered'],q['verified_outliers']) for q in metrics)
    css = (Path(__file__).resolve().parent.parent / 'assets' / 'report.css').read_text(encoding='utf-8')
    banner = '<div class="notice demo"><b>离线演示：全部为合成数据。</b>未联网，未读取密钥，没有真实 API 费用。示例链接不指向真实抖音视频。</div>' if demo else ''
    content = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>''' + E(c['topic']) + ''' · 小号爆款报告</title><style>''' + css + '''</style></head><body><main>''' + banner
    content += narrative(store)
    content += '<details class="data-appendix"><summary>查看统计明细、筛选口径与费用记录</summary>'
    content += '<div class="notice">筛选口径：粉丝不超过 %s；目标播放量至少为对照中位数的 %s 倍。对照目标 %s 条，至少 %s 条；少于目标数量单独标注。</div>' % (
        c['max_followers'],c['min_ratio'],c['baseline_count'],c['min_baseline_count'])
    if any(v.get('review', {}).get('method') == 'keyword-rule' for v in s['candidates'].values()):
        content += '<div class="notice">本报告使用标题/描述关键词初筛。“已核验”仅表示播放对照达标；内容语义仍待复核，请先复看原视频。</div>'
    content += '<p><a href="results.csv" download>结果 CSV</a> · <a href="comparisons.csv" download>对照 CSV</a> · <a href="results.json">可复算 JSON</a></p>'
    if research['story'] and not s['analyses']: cards=''
    content += '<div class="scroll"><table><tr><th>内容 / 作者</th><th>播放量</th><th>对照中位数</th><th>倍数</th><th>状态</th></tr>' + table + '</table></div>' + cards
    content += '<h2>其他已处理候选</h2><div class="scroll"><table><tr><th>视频</th><th>状态</th><th>原因</th></tr>' + (other or '<tr><td colspan="3">无</td></tr>') + '</table></div>'
    content += '<h2>关键词结果</h2><div class="scroll"><table><tr><th>关键词</th><th>去重结果</th><th>初筛数量</th><th>初筛纳入率</th><th>首次引入</th><th>入选数量</th></tr>' + quality + '</table></div><p class="muted">相关性可能来自词规则、宿主 AI 或使用者；请查看逐条初审理由。首次引入数量受查询先后影响。搜索按点赞排序，不代表全平台最高播放榜。</p>'
    content += '<h2>预算与数据边界</h2><p>已记录 %s 次请求尝试。按配置单价预留 <b>%s USD</b>，本轮预算 %s USD。</p><p>%s</p>' % (
        len(reservations),E(str(estimated)),E(str(c['budget_usd'])),E(summary['billing_note']))
    content += '<p class="muted">当前粉丝数不代表发布时规模；累计播放没有对齐相同发布后时长；未核实投流。对照为同作者邻近作品，不保证同题材。数据缺失、历史覆盖不足和中位数为零的记录不列为已核验结果。</p>'
    if s['stops']:
        content += '<details><summary>运行中的停止原因</summary><ul>' + ''.join('<li>%s：%s</li>' % (E(x['phase']),E(x['message'])) for x in s['stops']) + '</ul></details>'
    content += '</details>'
    content += '<p><a href="summary.json">方法与预算记录</a> · <a href="candidates.json">候选与相关性审查</a></p><footer>报告只保存公开研究字段。密钥不写入配置、缓存或分享包。对外分享运行目录前，请自行检查主题、评论和内容笔记是否适合公开。</footer></main></body></html>'
    temp = store.root / 'report.html.tmp'
    temp.write_text(content,encoding='utf-8')
    temp.replace(store.root / 'report.html')
    return summary
