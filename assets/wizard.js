'use strict';
/** @param {string} id */
function $(id) { const el=document.getElementById(id); if(!el) throw new Error('页面元素缺失'); return el; }
/** @param {string} id */
function input(id) { const el=$(id); if(!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) throw new Error('输入项缺失'); return el; }
/** @typedef {{topic:string, keywords:string[], anchors:string[]}} Brief */
/** @typedef {{status:string,message:string,mode:string,key_ready:boolean,answers:Brief,summary:{demo:boolean,candidate_count:number,qualified_count:number,limited_count:number,pending_count:number,review_pending_count:number,report_complete:boolean,research_status:string,generated_at:string},report_url:string,output_dir:string}} Snapshot */
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
function updateConnection(){
  $('connection-label').textContent=state.key_ready ? 'Key 已就绪' : '连接 TikHub';
  $('connect').classList.toggle('ready',Boolean(state.key_ready));
}
/** @param {boolean} pending */
function openKey(pending){
  pendingStart=pending;error('','key-error');$('key-action').textContent=pending?'连接并生成报告':'连接';
  /** @type {HTMLDialogElement} */ ($('key-dialog')).showModal();input('key').focus();
}
function mainView(){
  clearTimeout(timer);$('workspace').hidden=false;$('research').hidden=true;error('');window.scrollTo(0,0);
}
function researchView(){
  clearTimeout(timer);lastReportStamp='';
  $('workspace').hidden=true;$('research').hidden=false;$('loading').hidden=false;
  $('research-label').textContent='RESEARCH IN PROGRESS';$('result-title').textContent='正在寻找，值得复看的内容。';
  $('progress').textContent='正在连接 TikHub，准备本轮研究…';
  $('result-actions').hidden=true;$('result-metrics').hidden=true;$('demo-banner').hidden=true;$('retry').hidden=true;
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
  const chosen=results.filter(r=>r.status==='qualified'||r.status==='limited').slice(0,4);
  for(const r of chosen){
    const card=document.createElement('article');card.className='highlight';
    const title=document.createElement('h3');title.textContent=r.target.title;
    const meta=document.createElement('div');meta.className='meta';meta.textContent=r.target.author_name+' · '+number(r.target.followers)+' 粉丝 · '+(r.status==='limited'?'有限样本':'播放对照已核验');
    const stats=document.createElement('div');stats.className='stats';
    const ratio=document.createElement('div');ratio.className='ratio';ratio.textContent=number(r.ratio)+'×';
    const sub=document.createElement('small');sub.textContent='同作者邻近作品中位数';ratio.append(sub);
    const comparison=document.createElement('div');comparison.className='comparison';comparison.textContent='目标播放 '+number(r.play_count)+' / 对照 '+number(r.median_plays);
    stats.append(ratio,comparison);card.append(title,meta,stats);$('highlights').append(card);
  }
}
async function poll(){
  try{
    state=await api('/api/state');$('progress').textContent=state.message;updateConnection();
    if(state.status==='running'){timer=window.setTimeout(poll,1200);return;}
    $('loading').hidden=true;$('research-label').textContent=state.status==='error'?'RESEARCH PAUSED':state.status==='needs_analysis'?'ANALYSIS INCOMPLETE':'RESEARCH COMPLETE';
    const summary=state.summary;
    if(state.status==='needs_analysis'){timer=window.setTimeout(poll,3000);}
    if(summary && summary.generated_at===lastReportStamp)return;
    lastReportStamp=summary?.generated_at || '';
    $('result-title').textContent=state.status==='error'?'这次查询没有完成。':state.status==='needs_analysis'?'研究还没完成，现有证据已保留。':summary?.qualified_count?'有这些内容，值得你复看。':'研究已结束，先看看现有证据。';
    $('new-research').hidden=false;$('retry').hidden=state.status!=='error';
    if(summary && state.report_url){
      $('result-metrics').replaceChildren(metric(summary.candidate_count,'发现候选'),metric(summary.qualified_count,'播放核验达标'),metric(summary.pending_count+summary.review_pending_count,'待进一步核验'));
      $('result-metrics').hidden=false;$('demo-banner').hidden=!summary.demo;$('result-actions').hidden=false;
      /** @type {HTMLAnchorElement} */ ($('report-link')).href=state.report_url;
      $('saved').textContent='已保存到本机：'+state.output_dir;
      $('report-link-label').textContent=summary.report_complete?'打开完整研究报告':'查看阶段报告';
      $('result-note').textContent=summary.report_complete?'报告包含主题发现、逐条案例与对照证据；仍请复看原视频。':'还需要智能体完成分析或补充证据。请回到启动 Skill 的会话，说“继续完成小号爆款研究”。';
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
    $('refinement').hidden=false;$('start-label').textContent='生成报告';refined=true;return;
  }
  if(!state.key_ready){openKey(true);return;}await begin();
});
input('description').oninput=()=>{refined=false;$('refinement').hidden=true;$('start-label').textContent='帮我找爆款';error('');};
document.querySelectorAll('[data-example]').forEach(button=>{
  if(!(button instanceof HTMLButtonElement))return;
  button.onclick=()=>{input('description').value=button.dataset.example;input('description').dispatchEvent(new Event('input'));input('description').focus();};
});
document.querySelectorAll('[data-focus]').forEach(button=>{
  if(!(button instanceof HTMLButtonElement))return;
  button.onclick=()=>{selectedFocus=button.dataset.focus;document.querySelectorAll('[data-focus]').forEach(el=>el.classList.toggle('selected',el===button));};
});
$('connect').onclick=()=>openKey(false);
$('close-dialog').onclick=()=>{/** @type {HTMLDialogElement} */ ($('key-dialog')).close();};
$('key-form').onsubmit=async event=>{
  event.preventDefault();const button=/** @type {HTMLButtonElement} */ ($('save-key'));button.disabled=true;error('','key-error');
  try{
    state=await api('/api/setup',{mode:'live',key:input('key').value});input('key').value='';updateConnection();
    /** @type {HTMLDialogElement} */ ($('key-dialog')).close();if(pendingStart)await begin();
  }catch(e){error(message(e),'key-error');}finally{button.disabled=false;}
};
$('demo').onclick=()=>action($('demo'),async()=>{
  state=await api('/api/setup',{mode:'demo'});updateConnection();researchView();$('progress').textContent='正在生成免费示例；不使用你的 Key，也不产生费用。';
  try{state=await api('/api/start',{description:'FDE'});await poll();}catch(e){mainView();error(message(e));}
});
$('new-research').onclick=mainView;$('retry').onclick=()=>{mainView();openKey(false);};
$('quit').onclick=()=>action($('quit'),async()=>{const response=await api('/api/quit',{});clearTimeout(timer);document.getElementsByTagName('main')[0].textContent=response.message;});
(async()=>{
  try{
    const token=location.hash.slice(1);history.replaceState(null,'','/');
    state=token?await api('/api/connect',{token}):await api('/api/state');updateConnection();
    if(state.answers?.topic)input('description').value=state.answers.topic;
    if(state.status==='running'||state.summary){researchView();await poll();}
  }catch(e){error(message(e));}
})();
