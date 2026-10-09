'use strict';
/** @param {string} id */
function $(id) { const el=document.getElementById(id); if(!el) throw new Error('页面元素缺失'); return el; }
/** @param {string} id */
function input(id) { const el=$(id); if(!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) throw new Error('输入项缺失'); return el; }
/** @typedef {{topic:string, keywords:string[], anchors:string[]}} Brief */
/** @typedef {{status:string,message:string,mode:string,key_ready:boolean,key_saved:boolean,key_storage_available:boolean,key_notice:string,assistant_mode:string,assistant_label:string,can_resume:boolean,answers:Brief,summary:{report_format:string,demo:boolean,candidate_count:number,qualified_count:number,limited_count:number,pending_count:number,review_pending_count:number,report_complete:boolean,research_status:string,generated_at:string},report_url:string,output_dir:string}} Snapshot */
/** @type {Partial<Snapshot>} */ let state={};
let lastReportStamp='';
let refined=false, selectedFocus='all', pendingStart=false;
/** @type {number|undefined} */ let timer;
/** @param {unknown} e */ function message(e){return e instanceof Error ? e.message : '操作未完成，请稍后重试。';}
/** @param {string} text @param {string} [id] */
function error(text,id='error'){ $(id).textContent=text; $(id).hidden=!text; }
/** @param {string} path @param {object} [body] @returns {Promise<Snapshot>} */
async function api(path,body){
  /** @type {RequestInit} */ const options={headers:{'X-Wizard':'1'}};
  if(body!==undefined){options.method='POST';options.headers={'X-Wizard':'1','Content-Type':'application/json'};options.body=JSON.stringify(body);}
  const response=await fetch(path,options);const result=await response.json();
  if(!response.ok)throw new Error(result.error || '连接未完成，请重试。');return result;
}
function updateStartNotice(){ $('start-cost-note').hidden=!(refined && state.key_ready); }
function updateConnection(){
  updateStartNotice();
  $('saved-key-status').hidden=!state.key_saved;
  $('key-notice').textContent=state.key_notice || ''; $('key-notice').hidden=!state.key_notice;
  const remember=/** @type {HTMLInputElement} */ ($('remember-key'));
  remember.disabled=!state.key_storage_available;
  if(remember.disabled)remember.checked=false;
  $('analysis-note').textContent=state.assistant_mode==='host'?'报告由当前智能体分析，请保持助手运行。':'报告自动使用'+(state.assistant_label||'本机智能体')+'的登录与额度；无需回到聊天。';
}
/** @param {boolean} pending */
function openKey(pending){
  pendingStart=pending;error('','key-error');$('key-action').textContent=pending?'连接并开始查询':'使用此密钥';
  /** @type {HTMLDialogElement} */ ($('key-dialog')).showModal();
  input('key').focus({preventScroll:true});$('key-dialog').scrollTop=0;
}
function mainView(){
  history.replaceState(null,'','/?new=1');
  clearTimeout(timer);$('workspace').hidden=false;$('research').hidden=true;error('');window.scrollTo(0,0);
}
function researchView(){
  history.replaceState(null,'','/');
  clearTimeout(timer);lastReportStamp='';
  $('workspace').hidden=true;$('research').hidden=false;$('loading').hidden=false;
  $('result-title').textContent='正在寻找，值得复看的内容。';
  $('progress').textContent='正在连接 TikHub，准备本轮研究…';
  $('result-actions').hidden=true;$('result-metrics').hidden=true;$('demo-banner').hidden=true;$('retry').hidden=true;$('resume-analysis').hidden=true;
  $('highlights').replaceChildren();$('new-research').hidden=true;$('saved').textContent='';$('result-note').textContent='';
  window.scrollTo(0,0);
}
/** @param {HTMLElement} button @param {()=>Promise<unknown>} fn */
async function action(button,fn){
  if(!(button instanceof HTMLButtonElement))return;
  button.disabled=true;error('');try{await fn();}catch(e){error(message(e));}finally{button.disabled=false;}
}
/** @param {number} value @param {string} label */
function metric(value,label){const div=document.createElement('div'),strong=document.createElement('strong'),span=document.createElement('span');strong.textContent=String(value);span.textContent=label;div.append(strong,span);return div;}
/** @param {number|null} value */
function number(value){return value==null?'未取得':new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(value);}
/** @param {Array<{status:string,ratio:number,play_count:number,median_plays:number,target:{title:string,author_name:string,followers:number}}>} results */
function highlights(results){
  const chosen=results.filter(r=>r.status==='qualified').slice(0,4);
  for(const r of chosen){
    const card=document.createElement('article');card.className='highlight';
    const title=document.createElement('h3');title.textContent=r.target.title;
    const meta=document.createElement('div');meta.className='meta';meta.textContent=r.target.author_name+' · '+number(r.target.followers)+' 粉丝 · '+'符合筛选条件';
    const stats=document.createElement('div');stats.className='stats';
    const ratio=document.createElement('div');ratio.className='ratio';ratio.textContent=number(r.ratio)+'×';
    const sub=document.createElement('small');sub.textContent='账号正常播放量的倍数';ratio.append(sub);
    const comparison=document.createElement('div');comparison.className='comparison';comparison.textContent='目标播放 '+number(r.play_count)+' / 对照 '+number(r.median_plays);
    stats.append(ratio,comparison);card.append(title,meta,stats);$('highlights').append(card);
  }
}
async function poll(){
  try{
    state=await api('/api/state');$('progress').textContent=state.message;updateConnection();
    if(state.status==='running'){timer=window.setTimeout(poll,1200);return;}
    const summary=state.summary;
    if(state.status==='done' && summary?.report_complete && state.report_url){
      clearTimeout(timer);location.replace(state.report_url);return;
    }
    $('loading').hidden=true;
    $('resume-analysis').hidden=!state.can_resume;
    $('new-research').hidden=false;$('retry').hidden=Boolean(state.can_resume)||!(state.status==='error'||(state.status==='needs_analysis'&&!summary));
    if(state.status==='needs_analysis'){timer=window.setTimeout(poll,3000);}
    if(summary && summary.generated_at===lastReportStamp)return;
    lastReportStamp=summary?.generated_at || '';
    $('result-title').textContent=state.status==='error'?'这次查询没有完成。':state.status==='needs_analysis'?'研究还没完成，现有证据已保留。':summary?.qualified_count?'有这些内容，值得你复看。':'研究已结束，先看看现有证据。';
    if(summary && state.report_url){
      $('result-metrics').replaceChildren(metric(summary.candidate_count,'搜索到的作品'),metric(summary.qualified_count,'符合条件的小号作品'));
      $('result-metrics').hidden=false;$('demo-banner').hidden=!summary.demo;$('result-actions').hidden=false;
      /** @type {HTMLAnchorElement} */ ($('report-link')).href=state.report_url;
      $('saved').textContent='已保存到本机：'+state.output_dir;
      $('report-link-label').textContent=summary.report_complete?(summary.report_format==='selection-report-v1'?'打开选题解读报告':'打开完整研究报告'):'查看阶段报告';
      $('result-note').textContent=summary.report_complete?'报告包含主题发现、逐条案例与对照证据；仍请复看原视频。':state.assistant_mode==='host'?'阶段报告已保存，等待宿主助手补充分析或证据。':state.can_resume?'分析中断，现有结果已保存。点击“继续生成报告”会复用已有数据，不再次查询 TikHub；分析仍使用当前智能体额度。':'已完成现有材料的分析。报告中标明了尚缺的正文证据或数据，重复生成不能补齐这些材料。';
      $('highlights').replaceChildren();
      const response=await fetch('/report/results.json');if(response.ok)highlights(await response.json());
    }
  }catch(e){$('loading').hidden=true;$('progress').textContent=message(e);$('retry').hidden=false;}
}
async function begin(){
  researchView();
  try{
    state=await api('/api/start',{description:input('description').value,focus:selectedFocus,consent:true});
    await poll();
  }catch(e){mainView();error(message(e));}
}
$('start').onclick=()=>action($('start'),async()=>{
  if(!refined){
    state=await api('/api/brief',{description:input('description').value,focus:selectedFocus});
    $('brief-title').textContent='我会从「'+state.answers.topic+'」开始。你更想参考哪一类？';
    $('refinement').hidden=false;$('start-label').textContent='生成报告';refined=true;updateStartNotice();return;
  }
  if(!state.key_ready){openKey(true);return;}await begin();
});
input('description').oninput=()=>{refined=false;$('refinement').hidden=true;$('start-label').textContent='帮我找爆款';updateStartNotice();error('');};
document.querySelectorAll('[data-example]').forEach(button=>{
  if(!(button instanceof HTMLButtonElement))return;
  button.onclick=()=>{input('description').value=button.dataset.example;input('description').dispatchEvent(new Event('input'));input('description').focus();};
});
document.querySelectorAll('[data-focus]').forEach(button=>{
  if(!(button instanceof HTMLButtonElement))return;
  button.onclick=()=>{selectedFocus=button.dataset.focus;document.querySelectorAll('[data-focus]').forEach(el=>el.classList.toggle('selected',el===button));};
});
$('close-dialog').onclick=()=>{/** @type {HTMLDialogElement} */ ($('key-dialog')).close();};
$('key-form').onsubmit=async event=>{
  event.preventDefault();const button=/** @type {HTMLButtonElement} */ ($('save-key'));button.disabled=true;error('','key-error');
  try{
    state=await api('/api/setup',{mode:'live',key:input('key').value,remember:/** @type {HTMLInputElement} */ ($('remember-key')).checked});input('key').value='';updateConnection();
    /** @type {HTMLDialogElement} */ ($('key-dialog')).close();if(pendingStart)await begin();
  }catch(e){error(message(e),'key-error');}finally{button.disabled=false;}
};
$('forget-key').onclick=()=>action($('forget-key'),async()=>{state=await api('/api/forget-key',{});updateConnection();});
$('new-research').onclick=mainView;$('retry').onclick=()=>{mainView();openKey(false);};
$('resume-analysis').onclick=()=>action($('resume-analysis'),async()=>{
  state=await api('/api/resume-analysis',{});researchView();await poll();
});
(async()=>{
  try{
    const newResearch=new URLSearchParams(location.search).get('new')==='1';
    const token=location.hash.slice(1);history.replaceState(null,'',newResearch?'/?new=1':'/');
    state=token?await api('/api/connect',{token}):await api('/api/state');updateConnection();
    if(state.answers?.topic)input('description').value=state.answers.topic;
    if(state.status==='running'||(!newResearch && state.summary)){researchView();await poll();}
  }catch(e){error(message(e));}
})();
