"""Evidence-bound narrative reports, separate from computed statistical results."""
import hashlib
import re
import os
import time
from contextlib import contextmanager
from pathlib import Path
from core import StopRun, atomic_json, digest, now, read_json
from workflow import apply_review, keyword_quality, MAX_SEARCH_ROUNDS

SELECTED=('qualified','limited')


def text(value,name,limit=3000,required=True):
    if not isinstance(value,str) or len(value)>limit or (required and not value.strip()):
        raise StopRun('%s 需要非空文字且不超过 %s 字。'%(name,limit))
    return value.strip()


def ids(value,allowed,name,minimum=1,maximum=20):
    if not isinstance(value,list) or not minimum<=len(value)<=maximum or any(not isinstance(i,str) or i not in allowed for i in value):
        raise StopRun('%s 必须引用本轮已有视频 ID。'%name)
    if len(set(value))!=len(value):raise StopRun('%s 含重复引用。'%name)
    return value


def evidence(store,vid):
    v=store.state['candidates'][vid]
    items=[{'id':vid+':title','kind':'title-description','text':v.get('title','')}]
    comments=store.state['comments'].get(vid)
    if comments and comments['items']:
        items.append({'id':vid+':comments','kind':'comments','text':'\n'.join(c['text'] for c in comments['items'])[:20000]})
    for entry in read_json(store.root/'evidence-index.json',{}).get(vid,[]):
        relative=Path(entry.get('file',''))
        path=store.root/relative
        if relative.is_absolute() or '..' in relative.parts or not relative.parts or relative.parts[0]!='evidence':continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size>2*1024*1024:continue
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=entry.get('sha256'):continue
        items.append({'id':entry['id'],'kind':entry['kind'],'text':raw.decode('utf-8'),
                      'source_note':entry['source_note'],'sha256':entry['sha256'],'file':str(path)})
    return items


@contextmanager
def asset_lock(store):
    path=store.root/'.asset-index.lock';deadline=time.monotonic()+3
    while True:
        try:
            fd=os.open(str(path),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            break
        except FileExistsError:
            if time.monotonic()>=deadline:raise StopRun('另一个证据登记正在进行，请稍后重试。')
            time.sleep(.03)
    try:
        os.close(fd)
        yield
    finally:path.unlink(missing_ok=True)


def add_evidence(store,vid,source,kind,note):
    if vid not in store.state['candidates'] or not re.fullmatch(r'\d{10,30}',vid):raise StopRun('未知视频 ID。')
    if kind not in ('transcript','video-notes'):raise StopRun('证据类型应为 transcript 或 video-notes。')
    source=Path(source)
    if not source.is_file() or source.stat().st_size>2*1024*1024:raise StopRun('证据文件缺失或过大。')
    raw=source.read_bytes();content=raw.decode('utf-8')
    if len(content.strip())<30:raise StopRun('证据文字过少，无法支持内容拆解。')
    note=text(note,'来源说明',1000)
    sha=hashlib.sha256(raw).hexdigest();relative='evidence/%s-%s-%s.txt'%(vid,kind,sha[:12])
    path=store.root/relative;path.parent.mkdir(exist_ok=True);path.write_bytes(raw)
    entry={'id':vid+':'+kind+':'+sha[:12],'kind':kind,'file':relative,'sha256':sha,'source_note':note,'added_at':now()}
    with asset_lock(store):
        index=read_json(store.root/'evidence-index.json',{})
        rows=index.setdefault(vid,[])
        if not any(r['id']==entry['id'] for r in rows):rows.append(entry)
        atomic_json(store.root/'evidence-index.json',index)
    return entry


def review_fingerprint(store, vid):
    """Bind semantic decisions to their input, not to an agent's completion claim."""
    candidate = store.state['candidates'][vid]
    return digest({'conditions': {key: store.config.get(key) for key in
                                  ('topic', 'scope', 'intake', 'max_followers')},
                   'candidate': {key: candidate.get(key) for key in
                                 ('id', 'title', 'author_id', 'author_name', 'followers', 'likes')},
                   'evidence': [{key: item.get(key) for key in ('id', 'kind', 'text', 'sha256')}
                                for item in evidence(store, vid)]})


def pending_reviews(store):
    return {vid: review_fingerprint(store, vid) for vid, candidate in store.state['candidates'].items()
            if candidate.get('review', {}).get('decision') not in ('include', 'exclude')
            or candidate['review'].get('method') != 'human-or-agent'
            or candidate['review'].get('input_hash') != review_fingerprint(store, vid)}


def review_packet(store,round_number):
    pending = pending_reviews(store)
    candidates = []
    for vid in pending:
        row = {key: store.state['candidates'][vid].get(key) for key in
               ('id', 'title', 'author_name', 'followers', 'likes', 'queries', 'review')}
        row['evidence'] = [{'kind': item['kind'], 'text': item['text'][:2000],
                            'truncated': len(item['text']) > 2000}
                           for item in evidence(store, vid) if item['kind'] != 'title-description']
        candidates.append(row)
    return {'run_dir':str(store.root),'topic':store.config['topic'],'scope':store.config['scope'],
            'round':round_number,'max_rounds':MAX_SEARCH_ROUNDS,'keyword_quality':keyword_quality(store),'max_followers':store.config['max_followers'],
            'remaining_requests':max(0,store.config['max_requests']-len(store.reservations())),
            'candidates':candidates, 'review_fingerprints':pending,
            'candidate_count':len(store.state['candidates']),
            'reused_review_count':len(store.state['candidates']) - len(pending),
            'queries':[{k:q.get(k) for k in ['pages','complete','error']}|{'keyword':kw} for kw,q in store.state['queries'].items()],
            'instructions':'逐条语义复核，不能用连续词匹配代替理解。candidates 仅包含新增、证据或研究条件改变、或上次不确定的候选；旧判断由程序合并，不要重复输出。小号主榜仍遵守粉丝限制。可建议至多3个同主题的新搜索词：优先具体用途、问题或使用场景，避开只反复返回大号或重复作品的词。程序会先核验已有小号，再按结果决定换词或翻页；正文精选数量不限制总结果数。round>=2时next_keywords必须为空，最多补搜两轮；不能调整预算。返回 reviews（本次 candidates 的全部ID）、next_keywords、search_reason。'}


def accept_review(store,answer,expected=None):
    if not isinstance(answer,dict) or not isinstance(answer.get('reviews'),dict):raise StopRun('请提交 reviews 对象。')
    current = pending_reviews(store)
    expected = current if expected is None else expected
    if expected != current:raise StopRun('审核期间候选或研究条件发生变化，请重新读取审核材料。')
    if set(answer['reviews'])!=set(expected):raise StopRun('必须覆盖本次待审候选，不能静默漏掉或重复提交已复用的视频。')
    reviews={}
    for vid,r in answer['reviews'].items():
        if not isinstance(r,dict) or r.get('decision') not in ('include','exclude','unsure'):raise StopRun('请明确 include/exclude/unsure 和逐条理由。')
        reviews[vid]={**r,'method':'human-or-agent'}
    words=answer.get('next_keywords',[])
    if not isinstance(words,list) or len(words)>3 or any(not isinstance(w,str) or not w.strip() or len(w)>80 for w in words):
        raise StopRun('补搜词至多3个，每个不超过80字。')
    reason=text(answer.get('search_reason',''),'补搜理由',500,required=bool(words))
    apply_review(store,reviews)
    for vid, fingerprint in expected.items():
        store.state['candidates'][vid]['review']['input_hash'] = fingerprint
    store.save()
    return {'next_keywords':list(dict.fromkeys(words)),'search_reason':reason}


def analysis_packet(store):
    from selection_report import FORMAT, eligible_ids, instructions, unresolved_data
    featured = eligible_ids(store)
    compact_results=[]
    for vid, r in store.state['results'].items():
        if vid not in featured: continue
        row={k:r.get(k) for k in ['status','reason','play_count','median_plays','ratio','limited_sample']}
        row['target']={k:r['target'].get(k) for k in ['id','title','author_name','followers','published_at']}
        row['baseline']=[{'id':v['id'],'title':v['title'][:500],'published_at':v['published_at'],'play_count':store.state['stats'].get(v['id'],{}).get('play_count')} for v in r.get('baseline',[])]
        compact_results.append(row)
    listed={}
    for vid in sorted(featured):
        listed[vid]=[]
        for e in evidence(store,vid):
            item=dict(e);item['text']=e['text'][:4000];item['truncated']=len(e['text'])>4000
            listed[vid].append(item)
    return {'run_dir':str(store.root),'topic':store.config['topic'],'scope':store.config['scope'],
            'results':compact_results,
            'candidates':[{k:v.get(k) for k in ['id','title','author_name','followers','review','cover_url','original_audio_url']} for v in store.state['candidates'].values() if v['id'] in featured],
            'dataset_summary':{'searched_count':len(store.state['candidates']), 'qualified_count':len(featured), 'data_incomplete':unresolved_data(store)},
            'evidence':listed,
            'schema':FORMAT, 'featured_candidates':sorted(featured),
            'instructions':instructions()}


def validate_research(store,answer):
    from selection_report import FORMAT, validate
    if isinstance(answer, dict) and answer.get('format') == FORMAT:
        return validate(store, answer)
    if not isinstance(answer,dict):raise StopRun('研究报告需要对象格式。')
    if set(answer)-{'headline','lead','findings','categories','cases','next_topics','limitations'}:
        raise StopRun('报告只能提交分析文字与证据引用，不能提交统计或状态字段。')
    allowed=set(store.state['candidates']);selected={vid for vid,r in store.state['results'].items() if r['status'] in SELECTED}
    result={'headline':text(answer.get('headline'),'标题',160),'lead':text(answer.get('lead'),'研究摘要',2500),
            'findings':[],'categories':[],'cases':{},'next_topics':[],
            'limitations':[],'written_at':now(),'kind':'host-analysis'}
    if not isinstance(answer.get('limitations'),list) or not 1<=len(answer['limitations'])<=10:raise StopRun('请列出实际证据与覆盖边界。')
    result['limitations']=[text(v,'边界',1000) for v in answer['limitations']]
    for key in ['findings','categories','next_topics']:
        rows=answer.get(key)
        if not isinstance(rows,list) or len(rows)>8:raise StopRun('%s 应为至多8项列表。'%key)
        for row in rows:
            if not isinstance(row,dict):raise StopRun('%s 格式错误。'%key)
            result[key].append({'title':text(row.get('title'),key+'标题',180),
                                'text':text(row.get('text'),key+'说明',2000),
                                'video_ids':ids(row.get('video_ids'),allowed,key)})
    if allowed and not result['findings']:raise StopRun('请写出本轮发现及引用，不能只交空模板。')
    cases=answer.get('cases')
    if not isinstance(cases,dict) or len(cases)>20 or not selected.issubset(cases) or any(vid not in allowed for vid in cases):
        raise StopRun('案例需覆盖所有达标视频；仅能引用本轮候选。')
    if cases and (not result['categories'] or not result['next_topics']):
        raise StopRun('有案例时必须提供内容分类和有引用的选题建议。')
    shallow=[]
    for vid,case in cases.items():
        if not isinstance(case,dict):raise StopRun('案例应为对象。')
        available={e['id']:e for e in evidence(store,vid)}
        refs=ids(case.get('evidence_ids'),set(available),'案例证据',1,10)
        deep=any(available[i]['kind'] in ('transcript','video-notes') for i in refs)
        opening=text(case.get('opening',''),'开场',2000,False)
        outline=case.get('outline',[])
        if not isinstance(outline,list) or len(outline)>10:raise StopRun('正文结构应为至多10项列表。')
        if not deep and (opening or outline):raise StopRun('视频 %s 只有标题证据，不能填写真实开场或正文结构。'%vid)
        if deep and (not opening or not outline):raise StopRun('已读取正文的案例需提供具体开场和结构。')
        result['cases'][vid]={k:text(case.get(k),'案例'+k,3000) for k in ['category','summary','title_angle','takeaway','limitations']}
        result['cases'][vid]['title']=text(case.get('title',store.state['candidates'][vid]['title'][:120]),'案例标题',160)
        result['cases'][vid].update(opening=opening,outline=[text(x,'结构节点',1000) for x in outline],
                                    evidence_ids=refs,basis='content-read' if deep else 'title-description')
        if vid in selected and not deep:shallow.append(vid)
    result['data_hash']=digest({'candidates':store.state['candidates'],'results':store.state['results']})
    result['evidence_hash']=evidence_hash(store,result['cases'])
    result['shallow_cases']=shallow
    result['status']='partial' if shallow or (not selected and (store.state['stops'] or any(r.get('status')=='pending' for r in store.state['results'].values()))) or any(v['review']['decision'] in ('pending','unsure') for v in store.state['candidates'].values()) else 'complete'
    return result


def accept_research(store,answer,expected_format=None):
    if expected_format and (not isinstance(answer,dict) or answer.get('format')!=expected_format):
        raise StopRun('请按当前选题报告格式提交，不能用旧版报告代替。')
    report=validate_research(store,answer)
    store.state['research']=report;store.save()
    atomic_json(store.root/'research.json',report)
    return report


def evidence_hash(store,cases):
    return digest({vid:evidence(store,vid) for vid in cases})


def report_state(store):
    s=store.state;story=s.get('research')
    awaiting=sum(v['review']['decision'] in ('unsure','pending') for v in s['candidates'].values())
    valid=bool(story and story.get('data_hash')==digest({'candidates':s['candidates'],'results':s['results']}) and story.get('evidence_hash')==evidence_hash(store,story.get('cases',{})))
    from selection_report import FORMAT, completion_hash
    if valid and story.get('format') == FORMAT:
        valid = story.get('completion_hash') == completion_hash(store)
    if not valid:return {'status':'awaiting_analysis','label':'研究尚未完成','review_pending_count':awaiting,'story':None}
    complete=story.get('status')=='complete'
    return {'status':'complete' if complete else 'partial','label':('选题解读报告' if story.get('format')==FORMAT else '研究报告') if complete else '阶段报告 · 证据待补',
            'review_pending_count':awaiting,'story':story}
