// @ts-check
/** @typedef {{id:string,username:string,role:string}} User */
/** @typedef {{id:string,topic:string,focus:string,mode:string,status:string,message:string,created_at:number,updated_at:number,cancel_requested:number,progress:any,report_url:string|null,can_resume:boolean}} Job */
/** @template {HTMLElement} T @param {string} id @returns {T} */
function el(id){const element=document.getElementById(id);if(!element)throw new Error('Missing element');return /** @type {T} */(element);}
/** @param {string} id */const input=(id)=>/** @type {HTMLInputElement} */(el(id));
/** @param {string} id @param {boolean} visible */const show=(id,visible)=>{el(id).hidden=!visible;};
/** @param {string} id @param {string} value */const text=(id,value)=>{el(id).textContent=value;};
/** @type {User|null} */let user=null;
let csrf='',registering=false,ready=false,remaining=0,view='',poll=0;
/** @type {Job|null} */let current=null;
let autoOpen='',navigationVersion=0;
/** @param {string} message */function notice(message){text('notice',message);show('notice',!!message);}
/** @param {string} path @param {string} [method] @param {any} [body] */
async function api(path,method='GET',body){
 const response=await fetch(path,{method,credentials:'same-origin',headers:method==='GET'?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)});
 const data=await response.json();
 if(!response.ok){if(response.status===401&&user){user=null;clearTimeout(poll);await bootstrap();}throw new Error(typeof data.detail==='string'?data.detail:'提交内容有误，请检查后重试。');}return data;
}
/** @param {unknown} error */function message(error){return error instanceof Error?error.message:'暂时无法连接，请稍后重试。';}
/** @param {string} next */function switchView(next){
 clearTimeout(poll);view=next;navigationVersion++;notice('');
 for(const name of ['auth','home','progress','history','admin'])show(name+'-view',name===next);
 for(const name of ['new','history','admin'])el(name+'-nav').classList.toggle('active',next===(name==='new'?'home':name));
}
function authMode(){text('auth-title',registering?'创建账号':'登录账号');text('auth-submit',registering?'创建账号 →':'登录 →');text('auth-switch',registering?'已有账号？直接登录':'有邀请码？创建账号');text('auth-error','');show('invite-label',registering);input('invitation').required=registering;input('password').autocomplete=registering?'new-password':'current-password';}
async function bootstrap(){
 const data=await api('/api/bootstrap');user=data.user;csrf=data.csrf;ready=data.ready;remaining=data.remaining||0;
 for(const id of ['new-nav','history-nav','logout'])show(id,!!user);show('admin-nav',user?.role==='admin');text('account',user?.username||'');
 if(!user){switchView('auth');authMode();return;}
 const params=new URLSearchParams(location.hash.slice(1));const id=params.get('job');
 home();
 if(id){try{await openJob(id);}catch(error){notice(message(error));}}
}
function home(){switchView('home');history.replaceState(null,'','/');text('service-status',ready?`近24小时还可发起 ${remaining} 次研究 · 完成后会自动打开报告`:'研究服务正在配置。现在可以先体验一份合成示例。');/** @type {HTMLButtonElement} */(el('research-submit')).disabled=!ready||remaining===0;}
/** @param {string} id */async function openJob(id){const requestVersion=++navigationVersion;const job=await api('/api/jobs/'+encodeURIComponent(id));if(requestVersion!==navigationVersion)return;switchView('progress');history.replaceState(null,'','/#job='+job.id);current=job;autoOpen=job.id;renderJob(job);schedule();}
function schedule(){clearTimeout(poll);if(view==='progress'&&current&&['queued','running'].includes(current.status))poll=window.setTimeout(refreshJob,2200);}
async function refreshJob(){if(!current||view!=='progress')return;const id=current.id,version=navigationVersion;try{const job=await api('/api/jobs/'+id);if(version!==navigationVersion||view!=='progress'||current?.id!==id)return;current=job;renderJob(job);}catch(error){if(version===navigationVersion)notice(message(error));}schedule();}
/** @param {number} n */function number(n){return new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(n);}
/** @param {Job} job */function renderJob(job){
 text('job-title',job.topic);text('job-message',job.message);text('job-mode',job.mode==='demo'?'合成示例':'真实研究');
 const active=['queued','running'].includes(job.status),p=job.progress||{};
 text('job-explanation',active?(job.cancel_requested?'正在收尾，已经发出的查询会先保存结果。':'可以关闭网页，研究会在后台继续。回来后在「我的研究」中查看。'):(job.status==='done'?'研究已完成，报告和原作品链接已经保存。':'已经取得的数据会保留。接续整理只使用本轮已有数据。'));
 text('candidate-count',number(p.candidates||0));text('qualified-count',number(p.qualified||0));
 const elapsed=Math.max(0,Math.round((p.elapsed_seconds||0)+(job.status==='running'?(Date.now()/1000-job.updated_at):0)));text('elapsed',Math.floor(elapsed/60)+':'+String(elapsed%60).padStart(2,'0'));
 el('activity').classList.toggle('stopped',!active);
 const stage=['covers','render','verify_delivery'].includes(p.stage)?'write':['supplement','prepare'].includes(p.stage)?'search':p.stage;
 document.querySelectorAll('#stages li').forEach(node=>{node.classList.toggle('current',/** @type {HTMLElement} */(node).dataset.stage===stage);});
 show('cancel-job',active&&!job.cancel_requested);show('resume-job',job.can_resume);show('another-job',!active);show('open-report',!!job.report_url);
 if(job.report_url){/** @type {HTMLAnchorElement} */(el('open-report')).href=job.report_url;text('open-report',job.status==='done'?'阅读完整报告 ↗':'查看阶段报告');}
 const container=el('early-cards');container.replaceChildren();
 for(const row of p.usable_results||[]){
  const card=document.createElement('article');card.className='early-card paper';
  const title=document.createElement('h3');title.textContent=row.target.title;
  const meta=document.createElement('p');meta.className='muted';meta.textContent=row.target.author_name+' · '+number(row.target.followers||0)+' 粉丝';
  const stats=document.createElement('p');const bold=document.createElement('b');bold.textContent=number(row.play_count);stats.append(bold,document.createTextNode(' 次播放'));
  const comparison=document.createElement('p');comparison.className='comparison';comparison.textContent='约为账号正常播放量的 '+number(row.ratio)+' 倍';card.append(title,meta,stats,comparison);
  if(!row.demo&&/^\d{10,30}$/.test(row.target.id)){const link=document.createElement('a');link.href='https://www.douyin.com/video/'+row.target.id;link.target='_blank';link.rel='noopener noreferrer';link.textContent='看原作品 ↗';card.append(link);}container.append(card);
 }
 show('early-section',container.childElementCount>0);
 if(job.status==='done'&&job.report_url&&autoOpen===job.id){autoOpen='';location.assign(job.report_url);}
}
async function historyView(){switchView('history');history.replaceState(null,'','/');const {jobs}=await api('/api/jobs');const list=el('history-list');list.replaceChildren();
 if(!jobs.length){const empty=document.createElement('div');empty.className='paper empty';empty.textContent='还没有研究记录。输入一个主题，开始第一次研究。';list.append(empty);return;}
 /** @type {Record<string,string>} */ const labels={queued:'等待开始',running:'研究中',done:'已完成',error:'研究中断',needs_analysis:'阶段报告',interrupted:'可以接续',cancelled:'已停止'};
 for(const job of /** @type {Job[]} */(jobs)){const row=document.createElement('article');row.className='paper history-row';const info=document.createElement('div');const title=document.createElement('h2');title.textContent=job.topic;const meta=document.createElement('p');meta.textContent=new Date(job.created_at*1000).toLocaleString('zh-CN')+' · '+(job.mode==='demo'?'合成示例 · ':'')+(labels[job.status]||'已保存');info.append(title,meta);const link=document.createElement('a');link.className='outline';link.textContent=job.status==='done'?'阅读报告 ↗':'查看进度 →';link.href=job.status==='done'&&job.report_url?job.report_url:'/#job='+job.id;if(job.status!=='done')link.addEventListener('click',event=>{event.preventDefault();openJob(job.id).catch(e=>notice(message(e)));});row.append(info,link);list.append(row);}}
async function adminView(){switchView('admin');const data=await api('/api/admin');text('provider-status',`数据服务：${data.providers.tikhub_key?'已配置':'待配置'} · 研究助手：${data.providers.deepseek_key?'已配置':'待配置'}`);text('admin-limits',`试用版：每人近24小时最多 ${data.user_limit} 轮，全站最多 ${data.daily_limit} 轮。每轮数据查询上限 $0.50，模型上限 $0.15；失败和接续保留原费用记录。`);}
/** @param {string} mode */async function start(mode){notice('');const topic=mode==='demo'?'FDE（合成示例）':/** @type {HTMLTextAreaElement} */(el('topic')).value.trim();const job=await api('/api/jobs','POST',{topic,focus:mode==='demo'?'all':/** @type {HTMLSelectElement} */(el('focus')).value,mode});if(mode==='live')remaining=Math.max(0,remaining-1);await openJob(job.id);}
/** @param {string} id @param {() => Promise<any>|void} action */function click(id,action){el(id).addEventListener('click',async()=>{const button=/** @type {HTMLButtonElement} */(el(id));button.disabled=true;try{await action();}catch(error){notice(message(error));}finally{button.disabled=false;}});}
click('auth-switch',()=>{registering=!registering;authMode();});
el('auth-form').addEventListener('submit',async event=>{event.preventDefault();const button=/** @type {HTMLButtonElement} */(el('auth-submit'));button.disabled=true;text('auth-error','');try{const body={username:input('username').value.trim(),password:input('password').value,...(registering?{invitation:input('invitation').value.trim()}:{})};await api(registering?'/api/register':'/api/login','POST',body);input('password').value='';input('invitation').value='';history.replaceState(null,'','/');await bootstrap();}catch(error){text('auth-error',message(error));}finally{button.disabled=false;}});
el('research-form').addEventListener('submit',async event=>{event.preventDefault();const b=/** @type {HTMLButtonElement} */(el('research-submit'));b.disabled=true;try{await start('live');}catch(error){notice(message(error));}finally{b.disabled=!ready||remaining===0;}});
click('demo-start',()=>start('demo'));click('new-nav',home);click('history-new',home);click('another-job',home);click('history-nav',historyView);click('admin-nav',adminView);
click('logout',async()=>{await api('/api/logout','POST',{});current=null;await bootstrap();});
click('cancel-job',async()=>{if(current){const id=current.id,version=navigationVersion;const job=await api('/api/jobs/'+id+'/cancel','POST',{});if(version!==navigationVersion||current?.id!==id)return;current=job;renderJob(job);schedule();}});
click('resume-job',async()=>{if(current){const id=current.id,version=navigationVersion;const job=await api('/api/jobs/'+id+'/resume','POST',{});if(version!==navigationVersion||current?.id!==id)return;current=job;autoOpen=job.id;renderJob(job);schedule();}});
document.querySelectorAll('[data-topic]').forEach(button=>button.addEventListener('click',()=>{/** @type {HTMLTextAreaElement} */(el('topic')).value=/** @type {HTMLElement} */(button).dataset.topic||'';el('topic').focus();}));
el('invite-form').addEventListener('submit',async event=>{event.preventDefault();try{const data=await api('/api/admin/invites','POST',{label:input('invite-name').value.trim()||'受邀请的创作者'});input('invite-result').value=data.url;show('invite-result-label',true);input('invite-result').select();}catch(error){notice(message(error));}});
el('provider-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/api/admin/providers','POST',{tikhub_key:input('tikhub-key').value,deepseek_key:input('deepseek-key').value});input('tikhub-key').value='';input('deepseek-key').value='';const data=await api('/api/bootstrap');csrf=data.csrf;ready=data.ready;await adminView();notice('服务配置已保存。');}catch(error){notice(message(error));}});
window.addEventListener('hashchange',()=>{const id=new URLSearchParams(location.hash.slice(1)).get('job');if(id&&user)openJob(id).catch(e=>notice(message(e)));});
const invitation=new URLSearchParams(location.hash.slice(1)).get('invite');if(invitation){input('invitation').value=invitation;registering=true;history.replaceState(null,'','/');}
bootstrap().catch(error=>{switchView('auth');authMode();notice(message(error));});
