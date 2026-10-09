"""Readable study presentation; no model-supplied numeric fields are rendered."""
import html
from datetime import datetime,timezone,timedelta
from core import read_json, stamp
from research import report_state
E=lambda x:html.escape(str(x),quote=True)


def num(x):return '未取得' if x is None else format(x,',.1f').rstrip('0').rstrip('.') if isinstance(x,float) else format(x,',')

def refs(video_ids,candidates,demo):
    return '<div class="sources">参考：'+''.join(('<span>'+E(candidates[v]['author_name'])+'</span>') if demo else (
        '<a href="https://www.douyin.com/video/'+E(v)+'" target="_blank" rel="noopener noreferrer">'+E(candidates[v]['author_name'])+' ↗</a>') for v in video_ids)+'</div>'


def narrative(store):
    state=report_state(store);s=store.state;c=store.config;story=state['story'];demo=s.get('demo',False)
    from selection_report import FORMAT
    if story and story.get('format') == FORMAT:
        from selection_render import render_selection
        return render_selection(store, story)
    selected=[r for r in s['results'].values() if r['status'] in ('qualified','limited')]
    suitable=sum(v.get('followers') is not None and v['followers']<=c['max_followers'] for v in s['candidates'].values())
    headline=story['headline'] if story else c['topic']+'：研究正在进行'
    lead=story['lead'] if story else '目前已保存候选数据，主题归纳与内容分析尚未完成。这份文件是研究进度草稿，不能当作“没有爆款”的结论。'
    collected=datetime.fromtimestamp(stamp(s['as_of']),timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    out='<section class="study-hero"><p class="eyebrow">内容研究 / '+E(state['label'])+'</p><h1>'+E(headline)+'</h1><p class="study-lead">'+E(lead)+'</p><p class="muted">数据快照：'+collected+'（北京时间）</p></section>'
    out+='<div class="study-metrics">'+''.join('<div><b>'+E(value)+'</b><span>'+label+'</span></div>' for value,label in [
        (len(s['candidates']),'发现候选'),(state['review_pending_count'],'待语义复核'),(suitable,'粉丝符合主榜'),(len(selected),'播放对照达标')])+'</div>'
    if state['status']!='complete':
        out+='<div class="study-status">'+E(state['label'])+'：'+('等待启动 Skill 的智能体复核候选、阅读内容并提交分析。' if not story else '部分证据尚缺，以下已区分可确认的观察和待核验线索。')+'</div>'
    if story:
        out+='<section class="study-section"><p class="eyebrow">01 / 这轮看到了什么</p><h2>值得留意的内容方向</h2><div class="findings">'
        for i,f in enumerate(story['findings'],1):
            out+='<article class="finding"><span class="section-number">%02d</span><h3>%s</h3><p>%s</p>%s</article>'%(i,E(f['title']),E(f['text']),refs(f['video_ids'],s['candidates'],demo))
        out+='</div></section>'
        if story['categories']:
            out+='<section class="study-section"><p class="eyebrow">02 / 内容分类</p><h2>把零散视频放回内容地图</h2><div class="category-list">'
            for cat in story['categories']:
                out+='<div><h3>'+E(cat['title'])+'</h3><p>'+E(cat['text'])+'</p>'+refs(cat['video_ids'],s['candidates'],demo)+'</div>'
            out+='</div></section>'
        covers=read_json(store.root/'covers.json',{})
        cases=story['cases'];ordered=sorted(cases,key=lambda vid:(s['results'].get(vid,{}).get('status') not in ('qualified','limited'),-(s['results'].get(vid,{}).get('ratio') or 0)))
        out+='<section class="study-section"><p class="eyebrow">03 / 逐条案例</p><h2>具体看什么，怎样参考</h2>'
        for index,vid in enumerate(ordered,1):
            case=cases[vid];v=s['candidates'][vid];r=s['results'].get(vid,{})
            qualified=r.get('status') in ('qualified','limited')
            status='播放对照达标' if r.get('status')=='qualified' else '有限对照样本' if qualified else '参考线索 · 未确认为小号爆款'
            cover=covers.get(vid);cover_html=''
            if cover and cover.get('mime') in ('image/jpeg','image/png','image/webp'):
                cover_html='<img class="case-cover" alt="原视频封面" src="data:'+E(cover['mime'])+';base64,'+E(cover.get('base64',''))+'">'
            out+='<article class="study-case" id="case-%s"><div class="case-top">%s<div><p class="case-kicker">%02d / %s</p><h2>%s</h2><p class="case-author">%s · %s 粉丝</p><span class="case-status">%s</span>%s<p class="case-summary">%s</p></div></div>'%(
                E(vid),cover_html,index,E(case['category']),E(case['title']),E(v['author_name']),num(v.get('followers')),status,refs([vid],s['candidates'],demo),E(case['summary']))
            out+='<p class="case-limit">内容依据：'+('已登记的逐字稿或观看记录。' if case['basis']=='content-read' else '仅标题与描述，正文尚未读取。')+'</p>'
            out+='<div class="case-analysis"><div><h3>标题与切入</h3><p>'+E(case['title_angle'])+'</p></div>'
            if case['basis']=='content-read':
                out+='<div><h3>内容如何展开</h3><p>'+E(case['opening'])+'</p><ol>'+''.join('<li>'+E(x)+'</li>' for x in case['outline'])+'</ol></div>'
            else:out+='<div><h3>正文证据待补</h3><p>目前只读取标题/描述，尚不能判断真实口播开场和叙事结构。</p></div>'
            out+='<div><h3>可以参考什么</h3><p>'+E(case['takeaway'])+'</p></div></div>'
            out+='<div class="case-numbers">'+''.join('<div><b>'+E(value)+'</b><span>'+label+'</span></div>' for value,label in [(num(r.get('play_count')),'目标播放'),(num(r.get('median_plays')),'账号正常播放量'),((num(r['ratio'])+'×') if r.get('ratio') is not None else '未核验','相对倍数')])+'</div>'
            if r.get('baseline'):
                out+='<details><summary>查看 '+str(len(r['baseline']))+' 条同作者对照</summary><div class="scroll"><table><tr><th>作品</th><th>发布时间</th><th>播放量</th></tr>'
                for b in r['baseline']:
                    date=datetime.fromtimestamp(b['published_at'],timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
                    out+='<tr><td>'+E(b['title'])+refs([b['id']],{b['id']:b},demo)+'</td><td>'+date+'</td><td>'+num(s['stats'].get(b['id'],{}).get('play_count'))+'</td></tr>'
                out+='</table></div></details>'
            out+='<p class="case-limit">证据边界：'+E(case['limitations'])+'</p></article>'
        out+='</section>'
        if story['next_topics']:
            out+='<section class="study-section"><p class="eyebrow">04 / 转成你的选题</p><h2>可以小范围测试的方向</h2><div class="topic-list">'
            for topic in story['next_topics']:
                out+='<div><h3>'+E(topic['title'])+'</h3><p>'+E(topic['text'])+'</p>'+refs(topic['video_ids'],s['candidates'],demo)+'</div>'
            out+='</div><p class="muted">以上是基于样本提出的选题假设，不是播放效果承诺。</p></section>'
        out+='<div class="study-limits"><h3>这份报告能说明什么</h3><ul>'+''.join('<li>'+E(x)+'</li>' for x in story['limitations'])+'</ul></div>'
    pending=[v for v in s['candidates'].values() if v['review']['decision'] in ('unsure','pending') or s['results'].get(v['id'],{}).get('status')=='pending']
    if pending:
        out+='<section class="study-section"><h2>还没走完核验的线索</h2><p class="muted">这些条目不会被计作已经证实的爆款，也没有从报告中消失。</p><div class="scroll"><table><tr><th>视频 / 作者</th><th>粉丝</th><th>尚缺什么</th></tr>'
        for v in pending:
            reason=s['results'].get(v['id'],{}).get('reason') or v['review'].get('reason')
            out+='<tr><td>'+E(v['title'])+refs([v['id']],s['candidates'],demo)+'</td><td>'+num(v.get('followers'))+'</td><td>'+E(reason)+'</td></tr>'
        out+='</table></div></section>'
    return out
