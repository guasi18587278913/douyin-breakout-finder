"""Fixed report layout. Model prose is escaped; all observations come from stored data."""
import html
import math
import re
from datetime import datetime, timezone, timedelta
from core import read_json, stamp
from cover_cache import valid_cached

E = lambda x: html.escape(str(x), quote=True)


def number(value):
    return '未取得' if value is None else format(value, ',.1f').rstrip('0').rstrip('.') if isinstance(value, float) else format(value, ',')


def compact(value):
    if value is None:
        return '未取得'
    if value >= 10000:
        return ('%.1f' % (value / 10000)).rstrip('0').rstrip('.') + '万'
    return number(value)


def date(ts):
    return '日期未取得' if ts is None else datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime('%Y-%m-%d')


def video_link(vid):
    return 'https://www.douyin.com/video/' + vid


def opening(store, story):
    ids = list(story['cases'])
    topic = story['topic_label']
    if not ids:
        return '这次围绕%s查找，暂时没有足够的已核验案例可放进正文。' % topic
    targets = [store.state['candidates'][v] for v in ids]
    plays = [store.state['results'][v]['play_count'] for v in ids]
    count = len(ids)
    which = {1: '这条', 2: '这两条', 3: '这三条', 4: '这四条', 5: '这五条'}[count]
    followers = [v.get('followers') for v in targets]
    prefix = '%s作品' % which
    if any(v is None for v in followers):
        return prefix + '，有几个值得参考的选题切口。'
    ceiling = (int(max(followers)) // 1000 + 1) * 1000
    step = 10000 if min(plays) >= 10000 else 1000 if min(plays) >= 1000 else 1
    floor = int((min(plays) - 1) // step) * step
    every = '都' if count > 1 else ''
    if floor > 0:
        return '%s，作者粉丝%s不到 %s，播放量却%s超过了 %s。' % (prefix, every, compact(ceiling).replace(',', ''), every, compact(floor))
    return '%s，作者粉丝%s不到 %s，本轮记录的播放量最高为 %s。' % (prefix, every, compact(ceiling), compact(max(plays)))


def original_title(v):
    return (v.get('title') or '作品标题未取得').split('\n', 1)[0][:200]


def display_title(v):
    title=re.split(r'\s*#',original_title(v),maxsplit=1)[0].strip()
    if len(title)>45 and '。' in title:title=title.split('。',1)[0]
    return title or original_title(v)


def card(store, vid, covers, compact_row=False):
    v = store.state['candidates'][vid]
    r = store.state['results'][vid]
    demo = store.state.get('demo', False)
    cover = covers.get(vid)
    picture = ('<img class="selection-cover" src="data:%s;base64,%s" alt="%s的原作品封面">' %
               (E(cover['mime']), E(cover['base64']), E(v['author_name']))) if valid_cached(cover) else '<div class="selection-cover cover-missing">暂无原封面</div>'
    links = '' if demo else '<a href="'+video_link(vid)+'" target="_blank" rel="noopener noreferrer">看原视频 ↗</a>'
    aid = v.get('author_id')
    if not demo and isinstance(aid, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,200}', aid):
        links += '<a href="https://www.douyin.com/user/'+E(aid)+'" target="_blank" rel="noopener noreferrer">作者主页 ↗</a>'
    status = '<span class="selection-limited">有限样本参考</span>' if r['status'] == 'limited' else ''
    out = '<article class="selection-card%s" id="case-%s"><div class="selection-card-top">%s<div class="selection-work"><h3 title="%s">%s</h3><p class="selection-author">%s · %s 粉丝</p><p class="selection-date">%s 发布 %s</p></div><dl class="selection-metrics"><div><dt>这条作品播放量</dt><dd>%s</dd></div><div><dt>账号正常播放量</dt><dd>%s</dd></div><div><dt>约为正常播放</dt><dd>%s<small>倍</small></dd></div></dl><div class="selection-links">%s</div></div>' % (
        ' selection-row' if compact_row else '', E(vid), picture, E(original_title(v)), E(display_title(v)), E(v['author_name']), number(v.get('followers')), date(v.get('published_at')), status,
        compact(r['play_count']), compact(r['median_plays']), number(round(r['ratio'])), links)
    out += '<details class="selection-basis"><summary>查看完整标题与数据对照</summary><p class="selection-original-title">'+E(original_title(v))+'</p>'
    maximum = max(r['play_count'], r['median_plays'])
    out += '<div class="selection-comparison" role="img" aria-label="%s">' % E('这条作品播放量%s次，账号正常播放量%s次。' % (number(r['play_count']), number(r['median_plays'])))
    for label, value in [('这条作品播放量', r['play_count']), ('账号正常播放量', r['median_plays'])]:
        width = value / maximum * 100
        out += '<div class="selection-bar-row"><div><span>%s</span><span>%s 次</span></div><div class="selection-track"><span style="width:%.5f%%"></span></div></div>' % (label, number(value), width)
    out += '</div><p><b>账号正常播放量怎么来的？</b><br>参考同一作者附近发布的 %s 条作品。按播放量排序，取中间水平，减少偶尔一条高播放作品的影响。全部为采集时累计播放，发布时间和发布后经过的天数可能不同。</p><div class="scroll"><table><thead><tr><th>参考作品</th><th>发布日期</th><th>播放量</th></tr></thead><tbody>' % len(r.get('baseline', []))
    for b in r.get('baseline', []):
        title = E(original_title(b))
        if not demo:
            title = '<a target="_blank" rel="noopener noreferrer" href="'+video_link(b['id'])+'">'+title+'</a>'
        out += '<tr><td>%s</td><td>%s</td><td>%s</td></tr>' % (title, date(b.get('published_at')), number(store.state['stats'].get(b['id'], {}).get('play_count')))
    return out + '</tbody></table></div></details></article>'


def empty_intro(store):
    """No-case outcome statements come from saved facts, never model guesses."""
    from research import review_fingerprint
    candidates = store.state['candidates']
    relevant = sum(v.get('review', {}).get('decision') == 'include'
                   and v['review'].get('method') == 'human-or-agent'
                   and v['review'].get('input_hash') == review_fingerprint(store, vid)
                   for vid, v in candidates.items())
    found = '本轮取得 '+str(len(candidates))+' 条候选作品'
    if relevant:
        found += '，其中 '+str(relevant)+' 条已通过内容相关性复核'
    return [found+'。目前还没有核验出同时符合本次粉丝范围、账号对照和播放倍数条件的案例，因此暂不提供基于案例的解读。']


def render_selection(store, story):
    covers = read_json(store.root / 'covers.json', {})
    if not isinstance(covers, dict):
        covers = {}
    from selection_report import eligible_ids,delivery_target
    count=len(eligible_ids(store))
    collected = datetime.fromtimestamp(stamp(store.state['as_of']), timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    out = '<section class="selection-report"><header class="selection-header" id="overview"><p class="selection-kicker">选题研究 · '+E(collected[:10])+'</p><h1 class="selection-title">'+E(story['topic_label'])+'<span>小号爆款研究报告</span></h1><div class="selection-summary"><div><strong>'+str(count)+'</strong><span>篇合格作品</span></div><div><strong>'+str(len(story['cases']))+'</strong><span>篇精选解读</span></div><div><strong>'+str(len(story['lessons']))+'</strong><span>个选题方法</span></div></div></header>'
    out += '<nav class="selection-index" aria-label="报告目录"><a href="#overview">报告概览</a><a href="#featured">精选解读</a><a href="#more">更多作品</a><a href="#report-notes">数据说明</a></nav><section class="selection-intro"><h2 class="selection-section-label">本轮发现</h2><p class="selection-opening">'+E(opening(store,story))+'</p>'
    if not store.state.get('demo'):
        if delivery_target(store)<20:
            note='正文精选 '+str(len(story['cases']))+' 篇展开，其余作品列在“更多小号爆款”。'
        else:
            note='已达到本轮20–30篇的结果池目标。' if count>=20 else '尚未达到本轮20–30篇的结果池目标，以下只展示已核验的作品。'
        out+='<p class="selection-pool-status">本轮共找到 '+str(count)+' 篇合格作品。'+note+'</p>'
    intro = story['intro'] if story['cases'] else empty_intro(store)
    out += ''.join('<p>'+E(p)+'</p>' for p in intro)
    if story['status'] != 'complete':
        out += '<p class="selection-incomplete">部分数据或核验没有完成，以下只交代本轮已取得的信息；不能据此判断这个主题没有爆款。</p>'
    out+='</section><div id="featured">'
    for n, lesson in enumerate(story['lessons'], 1):
        out += '<section class="selection-lesson"><h2><span class="selection-method-number">方法 '+str(n).zfill(2)+'</span>'+E(lesson['title'])+'</h2>'
        for case in lesson['cases']:
            vid = case['video_id']
            v = store.state['candidates'][vid]
            out += '<div class="selection-case"><div class="selection-case-prose"><h3 class="selection-case-name">'+E(display_title(v))+'</h3>'
            out += ''.join('<p>'+E(p)+'</p>' for p in case['paragraphs'])
            out += '</div>'+card(store,vid,covers)+'</div>'
        out += '<div class="selection-applications"><h3 class="selection-borrow">这个思路怎么借鉴？</h3><div class="selection-examples">'
        for app in lesson['applications']:
            role = '如果你是' if any(x in app['audience'] for x in ('创作者', '博主', '老师', '教师')) else '如果你做'
            out += '<div class="selection-example"><p>'+role+E(app['audience'])+'，可以试试：</p><p>“'+E(app['idea'])+'”</p></div>'
        out += '</div><p class="selection-takeaway">'+E(lesson['takeaway'])+'</p></div>'
        out += '</section>'
    out+='</div>'
    closing = story['closing'] if story['cases'] else {
        'questions': ['你想解决的具体问题是什么？', '你更想参考教程、案例，还是观点表达？'],
        'paragraphs': ['可以把主题缩小到一个具体用途，再开始下一轮研究。这份记录会保留，便于之后核对。']}
    out += '<section class="selection-closing"><h2>下次找选题，先想清楚这几件事</h2><ol>'
    out += ''.join('<li>'+E(q)+'</li>' for q in closing['questions'])+'</ol>'
    out += ''.join('<p>'+E(p)+'</p>' for p in closing['paragraphs'])+'</section>'
    from selection_report import eligible_ids
    more = sorted(eligible_ids(store) - set(story['cases']), key=lambda vid: -(store.state['results'][vid].get('ratio') or 0))
    if more:
        out += '<section class="selection-more" id="more"><div class="selection-more-heading"><h2>更多小号爆款 <span>'+str(len(more))+' 篇</span></h2><p>按相对账号正常播放量的倍数排序</p></div><div class="selection-list">'
        out += ''.join(card(store, vid, covers, compact_row=True) for vid in more)+'</div>'
        out += '</section>'
    else:out+='<div id="more"></div>'
    title_only = any(c['basis'] == 'title-description' for c in story['cases'].values())
    basis = '本轮没有足够的合格案例用于解读。' if not story['cases'] else '本报告包含依据标题与简介的选题解读。' if title_only else '案例解读引用了本轮登记的内容证据。'
    out += '<details class="selection-notes" id="report-notes"><summary>数据来源与解读依据</summary><p>数据采集于 '+E(collected)+'（北京时间）。'+basis+'你可以结合自己的内容尝试，实际表现还需要验证。</p><ul>'
    limitations = story['limitations'] if story['cases'] else ['这份记录没有可供解读的充分核验案例，不据此判断整个主题的创作机会。']
    out += ''.join('<li>'+E(note)+'</li>' for note in limitations)
    out += '</ul></details></section>'
    return out
