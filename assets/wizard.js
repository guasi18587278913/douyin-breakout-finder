'use strict';
/** @param {string} id */
function $(id) { const el = document.getElementById(id); if (!el) throw new Error('页面缺少元素：' + id); return el; }
/** @param {string} id */
function field(id) { const el = $(id); if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement)) throw new Error('输入项不正确'); return el; }
/** @param {string} id */
function checkbox(id) { const el = $(id); if (!(el instanceof HTMLInputElement)) throw new Error('选择项不正确'); return el; }
/** @typedef {{topic: string, audience: string, keywords: string[], anchors: string[], excludes: string[]}} Answers */
/** @typedef {{status: string, message: string, mode: string, pricing: {usd_per_request: Record<string,string>}, answers: Answers, summary: {candidate_count:number, qualified_count:number, limited_count:number}, report_url:string, output_dir:string}} Snapshot */
let step = 0;
/** @type {string | null} */ let mode = null;
/** @type {Partial<Snapshot>} */ let snapshot = {};
/** @type {number | undefined} */ let pollTimer;
/** @param {string} message */
function error(message) { $('error').textContent = message || ''; $('error').hidden = !message; }
/** @param {string} path @param {object} [data] @returns {Promise<Snapshot>} */
async function api(path, data) {
  /** @type {Record<string,string>} */ const headers = {'X-Wizard': '1'};
  /** @type {RequestInit} */ const options = {headers};
  if (data !== undefined) { options.method = 'POST'; headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(data); }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '操作未完成。');
  return result;
}
/** @param {number} index */
function show(index) {
  step = index; error('');
  /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('[data-step]')).forEach(el => { el.hidden = Number(el.dataset.step) !== step; });
  document.querySelectorAll('#steps li').forEach((el, i) => { el.className = i === step ? 'active' : i < step ? 'done' : ''; });
  $('eyebrow').textContent = ['开始之前', '第一轮 · 明确目的', '第二轮 · 确认边界', '第三轮 · 范围与预算', '你的研究结果'][step];
  window.scrollTo(0, 0);
}
function answers() { return Object.fromEntries(['topic', 'current', 'goal', 'audience', 'keywords', 'anchors', 'excludes'].map(id => [id, field(id).value])); }
/** @param {HTMLElement} button @param {()=>Promise<unknown>} fn */
async function action(button, fn) {
  if (!(button instanceof HTMLButtonElement)) throw new Error('操作按钮不正确');
  error(''); button.disabled = true;
  try { await fn(); } catch(e) { error(e instanceof Error ? e.message : '操作未完成。'); }
  finally { button.disabled = false; }
}
/** @param {Partial<Snapshot>} data */
function prices(data) {
  snapshot = data;
  $('price-status').textContent = data.pricing ? '报价已就绪（USD / 次）：' + Object.entries(data.pricing.usd_per_request).map(([key, value]) => ({search:'搜索', posts:'作者作品', stats:'播放统计', comments:'评论'}[key] + ' ' + value)).join(' · ') : '尚无有效报价，正式研究不会启动。';
}
async function refreshPrices() {
  $('price-status').textContent = '正在读取官方公开报价…';
  try { prices(await api('/api/prices', {})); }
  catch(e) { $('price-status').textContent = e instanceof Error ? e.message : '操作未完成。'; }
}
/** @param {string} which */
async function setup(which) {
  snapshot = await api('/api/setup', {mode: which, key: which === 'live' ? field('key').value : ''});
  field('key').value = ''; mode = which;
  $('mode-label').textContent = mode === 'demo' ? '免费示例 · 合成数据' : 'TikHub · 本机会话';
  if (mode === 'demo') { field('topic').value = 'FDE'; field('keywords').value = 'FDE, 前沿部署工程师'; field('anchors').value = 'FDE, 前沿部署工程师'; field('audience').value = '想了解 AI 企业落地的内容创作者'; }
  show(1);
}
$('live').onclick = () => action($('live'), () => setup('live'));
$('demo').onclick = () => action($('demo'), () => setup('demo'));
$('next1').onclick = () => action($('next1'), async () => {
  await api('/api/intake', answers());
  if (!field('keywords').value.trim()) field('keywords').value = field('topic').value;
  show(2);
});
$('next2').onclick = () => action($('next2'), async () => {
  snapshot = await api('/api/intake', answers());
  const a = snapshot.answers;
  $('brief').textContent = '研究主题：' + a.topic + '\n受众：' + (a.audience || '未限定') + '\n搜索词：' + a.keywords.join('、') + '\n纳入词：' + a.anchors.join('、') + '\n排除词：' + (a.excludes.join('、') || '无');
  $('live-budget').hidden = mode !== 'live'; $('demo-note').hidden = mode !== 'demo';
  show(3);
  if (mode === 'live' && !snapshot.pricing) await refreshPrices();
  else if (mode === 'live') prices(snapshot);
});
$('refresh-prices').onclick = () => action($('refresh-prices'), refreshPrices);
$('save-prices').onclick = () => action($('save-prices'), async () => prices(await api('/api/manual-prices', {
  verified: checkbox('price-verified').checked, source_url: field('price-source').value,
  usd_per_request: Object.fromEntries(['search','posts','stats','comments'].map(op => [op, field('price-' + op).value]))
})));
/** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('.back')).forEach(button => { button.onclick = () => show(step - 1); });
/** @param {number} value @param {string} label */
function metric(value, label) { const div = document.createElement('div'), strong = document.createElement('strong'), span = document.createElement('span'); strong.textContent = String(value); span.textContent = label; div.append(strong, span); return div; }
async function poll() {
  try {
    snapshot = await api('/api/state'); $('progress').textContent = snapshot.message;
    if (snapshot.status === 'running') { pollTimer = setTimeout(poll, 1100); return; }
    $('result-label').textContent = snapshot.status === 'error' ? '本轮需要检查' : '研究完成';
    $('result-title').textContent = snapshot.status === 'error' ? '现有证据，已保留。' : '把值得复看的，留给你。';
    if (snapshot.report_url) {
      const s = snapshot.summary;
      $('result-metrics').replaceChildren(metric(s.candidate_count, '发现候选'), metric(s.qualified_count, '已核验'), metric(s.limited_count, '有限样本'));
      $('result-metrics').hidden = false; $('result').hidden = false; /** @type {HTMLAnchorElement} */ ($('report-link')).href = snapshot.report_url;
      $('saved').textContent = '报告保存在你的电脑：' + snapshot.output_dir;
    }
    $('retry').hidden = snapshot.status !== 'error';
  } catch(e) { error(e instanceof Error ? e.message : '操作未完成。'); }
}
$('start').onclick = () => action($('start'), async () => {
  snapshot = await api('/api/start', {...answers(), budget: field('budget').value, consent: checkbox('consent').checked});
  $('result').hidden = true; $('result-metrics').hidden = true; $('retry').hidden = true;
  $('result-label').textContent = '研究进行中'; $('result-title').textContent = '证据，正在汇合。';
  show(4); await poll();
});
$('again').onclick = () => { checkbox('consent').checked = false; show(0); };
$('retry').onclick = () => show(0);
$('quit').onclick = () => action($('quit'), async () => { const result = await api('/api/quit', {}); clearTimeout(pollTimer); document.getElementsByTagName('main')[0].textContent = result.message + ' 报告仍保存在你的电脑。'; });
(async () => {
  try {
    const token = location.hash.slice(1); history.replaceState(null, '', '/');
    snapshot = token ? await api('/api/connect', {token}) : await api('/api/state');
    if (snapshot.status === 'running' || snapshot.summary) { mode = snapshot.mode; show(4); await poll(); }
  } catch(e) { error(e instanceof Error ? e.message : '操作未完成。'); }
})();
