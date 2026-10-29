'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
  'chevron-left':'m15 18-6-6 6-6','chevron-right':'m9 18 6-6-6-6',
  calendar:'M8 2v4m8-4v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14H3V6a2 2 0 0 1 2-2Z',
  refresh:'M20 7a8 8 0 1 0 0 10m0-14v5h-5', search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
  moon:'M21 13A9 9 0 0 1 11 3 9 9 0 1 0 21 13',
  settings:'m9 3-1 3-3 1-2 3 2 2-1 3 2 3 3-1 3 2 3-2 3 1 2-3-1-3 2-2-2-3-3-1-1-3Zm6 9a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  plus:'M12 5v14M5 12h14',shield:'M12 3 3 6v6c0 5 9 9 9 9s9-4 9-9V6l-9-3Zm-4 9 3 3 5-6',
  messages:'M21 14V5H3v14l4-4h11m-9-5h6m-9 0h.01M9 19h8l4 3v-7',
  lock:'M6 11h12v10H6V11Zm2 0V7a4 4 0 0 1 8 0v4m-4 4v2',
  file:'M14 2H4v20h16V8l-6-6Zm0 0v6h6M8 13h8m-8 4h6',sparkles:'m12 3 2 6 6 2-6 2-2 6-2-6-6-2 6-2 2-6Zm7 13 1 3 3 1-3 1-1 3-1-3-3-1 3-1 1-3',
  clock:'M12 7v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
  save:'M19 21H3V3h14l4 4v14h-2ZM7 3v6h10V3M7 21v-8h10v8',
  eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0',
  send:'m22 2-7 20-4-9-9-4L22 2ZM11 13 22 2',x:'m6 6 12 12M6 18 18 6',
  folder:'M3 7V4h6l2 3h10v13H3V7Zm0 0h8',user:'M20 21a8 8 0 0 0-16 0m12-14a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  bot:'M12 3v3M5 6h14v13H5V6Zm3 5h.01M16 11h.01M9 15h6M2 10v5m20-5v5', menu:'M3 6h18M3 12h18M3 18h18'
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.file}"></path></svg>`;
$$('[data-icon]').forEach(el => el.innerHTML = icon(el.dataset.icon));
const sourceName = source => ({codex:'Codex',workbuddy:'WorkBuddy',chatgpt:'ChatGPT',manual:'人工记录',git:'Git',markdown:'工作笔记'}[source] || source);
const statuses = {planned:['计划中','neutral'],in_progress:['进行中',''],implemented_unverified:['待验证','warning'],verified_completed:['已验证','success'],blocked:['受阻','danger'],unknown:['待确认','neutral']};
const badge = value => {const [label, cls] = statuses[value] || [value,'neutral']; return `<span class="badge ${cls}">${escapeHTML(label)}</span>`;};
const labels = {completed:'今日完成工作',progress:'当前进展',problems:'遇到的问题',solutions:'解决措施',plan:'后续计划'};
const placeholders = {completed:'记录有证据支持的工作成果；待验证的内容请如实标注。',progress:'当前推进到哪一步？还有哪些工作正在进行？',problems:'记录本次工作中实际遇到的问题。',solutions:'为解决问题采取了哪些措施？',plan:'写下下一步工作安排，计划会单独保留。'};
const state = {day:new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date()),kind:'daily',source:'all',tasks:[],selected:null,view:'messages',evidence:null,report:null,data:null,dirty:false,editVersion:0,saving:null,loadId:0,busy:false};
let saveTimer, toastTimer, evidenceId = 0;
function toast(message) {$('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 4800);}
function showModal(title, body) {$('#modal-title').textContent = title; $('#modal-body').innerHTML = body; if (!$('#modal').open) $('#modal').showModal();}
$('#modal-close').onclick = () => $('#modal').close();
$('#modal').addEventListener('click', e => {if (e.target === $('#modal')) {const r = e.target.getBoundingClientRect(); if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) e.target.close();}});
async function api(path, method = 'GET', body) {
  const token = sessionStorage.getItem('lablog-token');
  const headers = {'Content-Type':'application/json'};
  if (token) headers.Authorization = `Bearer ${token}`;
  let response;
  try {response = await fetch('/api/v1/workspace' + path, {method,headers,body:body === undefined ? undefined : JSON.stringify(body)});} catch {throw new Error('服务器连接中断。你的编辑仍在页面中，请恢复连接后保存。');}
  const result = await response.json();
  if (response.status === 401) {showLogin(); throw new Error('需要服务器访问令牌。');}
  if (!response.ok) {const err = new Error(result.error?.message || (response.status === 422 ? '请检查填写内容。' : '操作未完成，请稍后重试。')); err.code = result.error?.code; throw err;}
  return result.data;
}
function showLogin() {
  showModal('连接服务器', '<p>输入启动服务器时配置的访问令牌。令牌仅保存在当前浏览器会话中。</p><form id="login-form"><label>访问令牌<input name="token" type="password" required autocomplete="off"></label><button class="button primary">连接</button></form>');
  $('#login-form').onsubmit = async e => {e.preventDefault(); sessionStorage.setItem('lablog-token', new FormData(e.target).get('token')); $('#modal').close(); await load();};
}
function empty(title, description, name = 'messages') {return `<div class="empty-state"><span class="empty-illustration">${icon(name)}</span><h3>${escapeHTML(title)}</h3><p>${escapeHTML(description).replace(/\n/g,'<br>')}</p></div>`;}
function visibleTasks() {
  const query = $('#task-search').value.trim().toLocaleLowerCase(), project = $('#project-filter').value, status = $('#status-filter').value;
  return state.tasks.filter(t => (state.source === 'all' || t.source === state.source) && (!project || project === t.project) && (!status || status === t.status) && (!query || `${t.title} ${t.summary} ${t.project}`.toLocaleLowerCase().includes(query)));
}
function renderTree() {
  if (state.data?.conversations_enabled) return renderConversationTree();
  const tasks = visibleTasks(), groups = new Map();
  for (const task of tasks) {if (!groups.has(task.project)) groups.set(task.project,new Map()); const sessions = groups.get(task.project), key = `${task.source}:${task.session_id}`; if (!sessions.has(key)) sessions.set(key,[]); sessions.get(key).push(task);}
  $('#task-count').textContent = `${tasks.length} 个任务`;
  $('#task-tree').innerHTML = [...groups].map(([project,sessions]) => `<details class="project-group" open><summary class="project-title">${icon('folder')}<span>${escapeHTML(project)}</span><span class="tree-count">${[...sessions.values()].flat().length}</span></summary>${[...sessions.values()].map(items => `<div class="session"><div class="session-title"><span class="source-mark ${items[0].source === 'manual' ? 'manual' : ''}">${items[0].source === 'manual' ? 'M' : sourceName(items[0].source)[0]}</span>${escapeHTML(sourceName(items[0].source))}<span class="tree-count">${items.length}</span></div>${items.map(task => `<div class="task-row ${state.selected === task.id ? 'active' : ''}" data-id="${escapeHTML(task.id)}"><input type="checkbox" ${task.selected ? 'checked' : ''} aria-label="将 ${escapeHTML(task.title)} 纳入报告"><button class="task-title" title="${escapeHTML(task.title)}">${escapeHTML(task.title)}</button>${badge(task.status)}</div>`).join('')}</div>`).join('')}</details>`).join('') || empty(state.source === 'workbuddy' ? 'WorkBuddy 数据源待接入' : '这里还没有任务',state.source === 'workbuddy' ? '尚未验证会话格式。\n原有 WorkBuddy 自动发送可继续使用。' : state.tasks.length ? '试试调整搜索关键词或筛选条件。' : '同步真实会话，或添加线下工作。','folder');
  $$('.task-row').forEach(row => {
    row.onclick = e => {if (e.target.type !== 'checkbox') selectTask(row.dataset.id);};
    $('input',row).onchange = async e => {try {await setSelection(row.dataset.id,e.target.checked); renderStats(); if (state.report?.manual_saved) showAlert('候选任务已更新。已保存的正文保持不变，请根据证据手动合并。');} catch(err) {e.target.checked = !e.target.checked; toast(err.message);}};
  });
}
async function setSelection(id, selected) {const task = state.tasks.find(t => t.id === id); await api('/selection','PATCH',{ids:[id],selected,revision:task.revision}); task.selected = selected; task.revision++;}
function renderStats() {
  if (state.data?.conversations_enabled) return renderConversationStats();
  const total = state.tasks.length, selected = state.tasks.filter(t => t.selected).length, percent = total ? Math.round(selected / total * 100) : 0;
  $('#count-all').textContent = total; for (const name of ['codex','workbuddy']) $(`#count-${name}`).textContent = state.tasks.filter(t => t.source === name).length;
  $('#count-chatgpt').textContent = state.tasks.filter(t => t.source === 'chatgpt').length;
  $('#selected-count').textContent = selected; $('#unselected-count').textContent = total-selected; $('#unknown-count').textContent = state.tasks.filter(t => ['unknown','implemented_unverified','blocked'].includes(t.status)).length;
  $('#coverage-value').innerHTML = `${selected}<small>/${total}</small>`; $('#coverage-ring').style.setProperty('--coverage',`${percent}%`); $('#coverage-percent').textContent = `覆盖率 ${percent}%`; $('#stats-date').textContent = state.day.slice(5).replace('-',' / ');
}
function highlight(text) {const query = $('#evidence-search').value.trim(); if (!query) return escapeHTML(text); return String(text).split(new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')})`,'gi')).map((part,i) => i%2 ? `<mark>${escapeHTML(part)}</mark>` : escapeHTML(part)).join('');}
function renderEvidence() {
  if (state.data?.conversations_enabled && state.selected?.includes(':')) return renderConversationEvidence();
  const task = state.tasks.find(t => t.id === state.selected);
  if (!task) {$('#evidence-content').innerHTML = empty('把工作过程，变成清晰的记录','从左侧选择任务，查看它的真实来源。\n原始消息按需读取，任务摘要长期保留。'); return;}
  $('#task-info').innerHTML = `<div class="breadcrumb">${escapeHTML(task.project)}<span>/</span>${escapeHTML(sourceName(task.source))}<span>/</span>任务证据</div><h3>${escapeHTML(task.title)}</h3><div class="task-meta"><span class="badge">${escapeHTML(sourceName(task.source))}</span><span>${icon('calendar')}${escapeHTML(task.date)}</span><span>${icon('clock')}${formatTime(task.time)}</span>${badge(task.status)}</div>`;
  $('#evidence-caption').textContent = state.view === 'summary' ? '持久化任务摘要' : '任务对应的原始消息';
  if (state.view === 'summary') {$('#evidence-content').innerHTML = `<div class="summary-card"><h3>${escapeHTML(task.title)}</h3>${badge(task.status)}<p>${highlight(task.summary)}</p></div>`; return;}
  if (!state.evidence) {$('#evidence-content').innerHTML = empty('正在读取原始证据','从源会话定位关联消息…'); return;}
  if (state.evidence.error) {$('#evidence-content').innerHTML = empty('原始证据暂不可访问',state.evidence.error + '\n切换到“任务摘要”可查看已保存内容。','lock'); return;}
  if (state.view === 'tools') {$('#evidence-content').innerHTML = empty('暂无可定位的执行记录','当前任务仅关联消息证据。\n未采集到工具调用或文件差异。','file'); return;}
  $('#evidence-content').innerHTML = (state.evidence.note ? `<div class="evidence-note">${escapeHTML(state.evidence.note)}</div>` : '') + state.evidence.messages.map(m => `<article class="message ${m.role === 'assistant' ? 'assistant' : ''}"><span class="message-avatar ${m.role === 'assistant' ? 'assistant' : ''}">${icon(m.role === 'assistant' ? 'bot' : 'user')}</span><div class="message-body"><div class="message-header"><strong>${escapeHTML(m.role === 'assistant' ? sourceName(task.source) : m.role === 'user' ? '用户' : sourceName(m.role))}</strong><time>${formatTime(m.time)}</time>${m.line ? `<span class="small-label">L${m.line}</span>` : ''}</div><div class="message-text">${highlight(m.text)}</div></div></article>`).join('');
}
function formatTime(value) {if (!value) return ''; const date = new Date(value); return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date);}
async function selectTask(id) {state.selected = id; state.evidence = null; const requestId = ++evidenceId; renderTree(); renderEvidence(); $('#tasks-panel').classList.remove('open'); try {const data = await api(`/tasks/${encodeURIComponent(id)}/evidence`); if (requestId === evidenceId) state.evidence = data;} catch(err) {if (requestId === evidenceId) state.evidence = {error:err.message};} if (requestId === evidenceId) renderEvidence();}
function showAlert(message,error = false) {$('#report-alert').hidden = !message; $('#report-alert').textContent = message; $('#report-alert').classList.toggle('error',error);}
function renderReport() {
  const report = state.report, name = {daily:'日报',weekly:'周报',monthly:'月报'}[state.kind];
  $('#report-heading').textContent = `${name}生成与编辑`; $('#report-date-label').textContent = `${state.day} · ${name}`;
  labels.completed = {daily:'今日完成工作',weekly:'本周完成工作',monthly:'本月工作内容'}[state.kind];
  const reportStatus = {empty:'尚未生成',draft:'草稿',verified:'已提交并核验',submitted:'已提交',unknown:'结果未知',submitting:'提交中',mismatch:'回执待核对'};
  $('#report-status').textContent = reportStatus[report.status] || report.status;
  $('#report-status').className = `badge ${report.status === 'verified' ? 'success' : report.readonly ? 'warning' : 'neutral'}`;
  $('#report-editor').innerHTML = Object.entries(labels).map(([key,label],i) => `<section class="report-section" id="section-${key}"><h3><span class="section-number">0${i+1}</span>${label}</h3><textarea data-field="${key}" aria-label="${label}" placeholder="${placeholders[key]}" ${report.readonly ? 'readonly' : ''}>${escapeHTML(report.sections[key] || '')}</textarea></section>`).join('');
  $$('textarea[data-field]').forEach(el => {resize(el); el.oninput = () => {resize(el); state.dirty = true; state.editVersion++; saveLabel('有未保存的修改','saving'); countWords(); clearTimeout(saveTimer); saveTimer = setTimeout(() => saveDraft().catch(() => {}),900);};});
  $('#generate-btn').disabled = report.readonly || report.manual_saved; $('#save-btn').disabled = report.readonly;
  $('#submit-btn').disabled = report.readonly || !report.hash;
  if (report.readonly) showAlert('该版本已有提交记录，已冻结；未知结果须先核查，不能重复发送。');
  else if (report.local_only && report.manual_saved) showAlert('当前仅为工作草稿：尚未绑定真实钉钉模板，不会交给自动任务发送。');
  else if (report.blockers?.length) showAlert('提交阻断：' + report.blockers.map(x => ({'no dated completion evidence':'缺少当期完成证据','date encoding unverified':'日期编码尚未验证'}[x] || x)).join('；'));
  else showAlert('');
  if (!state.dirty) saveLabel(report.updated_at ? `已保存 ${formatTime(report.updated_at)}` : '内容保存在服务器');
  countWords();
}
function resize(el) {el.style.height = 'auto'; el.style.height = Math.max(57,el.scrollHeight) + 'px';}
function sections() {return Object.fromEntries($$('textarea[data-field]').map(el => [el.dataset.field,el.value]));}
function countWords() {$('#word-count').textContent = `${Object.values(sections()).join('').length} 字`;}
function saveLabel(text,cls = '') {$('#save-state').textContent = text; $('#save-state').className = cls;}
async function saveDraft() {
  clearTimeout(saveTimer);
  if (state.saving) {await state.saving; if (state.dirty) return saveDraft(); return;}
  if (!state.dirty || state.report.readonly) return;
  const version = state.editVersion, body = {date:state.day,kind:state.kind,revision:state.report.revision,sections:sections()};
  saveLabel('正在保存…','saving');
  state.saving = (async () => {try {const report = await api('/report','PATCH',body); state.report = report; state.dirty = version !== state.editVersion; saveLabel(state.dirty ? '有未保存的修改' : '已保存 ' + formatTime(report.updated_at),state.dirty ? 'saving' : ''); $('#report-status').textContent = '草稿'; $('#generate-btn').disabled = true; $('#submit-btn').disabled = !report.hash;} catch(err) {saveLabel('保存失败 · 点击重试','save-error'); showAlert(err.message,true); toast(err.message); throw err;}})();
  try {await state.saving;} finally {state.saving = null;}
}
async function load(day = state.day,kind = state.kind) {
  const id = ++state.loadId;
  state.loading = true;
  $$('textarea[data-field], [data-kind], #date, #add-manual').forEach(el => el.disabled = true);
  try {
    const data = await api(`?day=${encodeURIComponent(day)}&kind=${kind}`);
    if (id !== state.loadId) return;
    let directory;
    if (data.conversations_enabled) directory = await fetchDirectory();
    if (id !== state.loadId) return;
    state.day = day; state.kind = kind;
    state.data = data; state.tasks = data.tasks; state.report = data.report;
    if (directory) applyDirectory(directory);
    else {const project = $('#project-filter').value; $('#project-filter').innerHTML = '<option value="">全部项目</option>' + [...new Set(data.tasks.map(t => t.project))].map(p => `<option value="${escapeHTML(p)}">${escapeHTML(p)}</option>`).join(''); $('#project-filter').value = project;}
    $('#date').value = state.day; $$('[data-kind]').forEach(el => el.classList.toggle('active',el.dataset.kind === state.kind));
    renderTree(); renderStats(); renderReport(); renderAutomation();
    const sync = data.sync; $('#sync-state').innerHTML = `<i class="dot ${sync.status === 'success' ? 'green' : sync.status ? 'amber' : ''}"></i>${sync.at ? `${sync.status === 'success' ? '已同步' : '同步需检查'}<br>${formatTime(sync.at)}` : '等待同步'}`;
    if (sync.warnings?.length) $('#sync-state').title = sync.warnings.join('\n');
    if (data.conversations_enabled && state.conversations.length) await selectConversation(state.conversations.some(c => c.key === state.selected) ? state.selected : state.conversations[0].key);
    else if (state.selected && state.tasks.some(t => t.id === state.selected)) await selectTask(state.selected);
    else if (state.tasks.length) await selectTask((state.tasks.find(t => ['verified_completed','implemented_unverified','in_progress'].includes(t.status)) || state.tasks[0]).id);
    else {state.selected = null; state.evidence = null; $('#task-info').innerHTML = '<div class="breadcrumb">工作空间 <span>/</span> 选择任务查看证据</div><h3>每一项工作，都有迹可循</h3><p>从左侧选择任务，查看它的真实来源与工作状态。</p>'; renderEvidence();}
    return true;
  } catch(err) {toast(err.message); $('#date').value = state.day; if (!state.data) {$('#task-tree').innerHTML = empty('暂时无法连接',err.message,'refresh'); $('#evidence-content').innerHTML = empty('工作空间暂不可用','恢复连接后点击“同步最新会话”重试。');} return false;}
  finally {if (id === state.loadId) {state.loading = false; $$('textarea[data-field], [data-kind], #date, #add-manual').forEach(el => el.disabled = false);}}
}
function renderAutomation() {
  const a = state.data.automation;
  $('#automation-title').textContent = a.enabled ? '已配置后台自动发送' : '后台自动发送';
  $('#automation-description').textContent = a.enabled ? `${a.time ? '配置时间 ' + a.time + ' · ' : ''}${a.timezone} · 外部调度器执行` : '自动提交未启用 · 可在设置中查看现有配置';
}
async function navigate(day,kind = state.kind) {
  if (state.loading || state.busy) {$('#date').value = state.day; return;}
  try {await saveDraft();} catch {$('#date').value = state.day; return;}
  state.selected = null; state.evidence = null; state.dirty = false;
  await load(day,kind);
}
async function withBusy(button, text, fn) {if (state.busy) return; state.busy = true; const old = button.innerHTML; button.disabled = true; button.innerHTML = icon('refresh') + escapeHTML(text); button.classList.add('spinning'); try {await fn();} catch(err) {toast(err.message); showAlert(err.message,true);} finally {state.busy = false; button.innerHTML = old; button.disabled = false; button.classList.remove('spinning'); if (state.report) {$('#generate-btn').disabled = state.report.manual_saved || state.report.readonly; $('#submit-btn').disabled = state.report.readonly || !state.report.hash;}}}
$('#sync-btn').onclick = () => withBusy($('#sync-btn'),'正在同步…',async () => {await saveDraft(); const data = await api('/sync','POST',{date:state.day,kind:state.kind}); await load(); toast(data.sync.status === 'success' ? `同步完成 · ${data.tasks.length} 个真实任务` : '同步完成，部分来源需检查。');});
$('#generate-btn').onclick = () => withBusy($('#generate-btn'),'正在生成…',async () => {await saveDraft(); state.report = await api('/generate','POST',{date:state.day,kind:state.kind}); renderReport(); toast('已根据选中证据生成草稿。');});
$('#save-btn').onclick = async () => {try {await saveDraft(); toast('草稿已保存。');} catch {}};
$('#date').onchange = () => {if ($('#date').value) navigate($('#date').value);};
for (const [id,delta] of [['prev-date',-1],['next-date',1]]) $(`#${id}`).onclick = () => {const d = new Date(state.day + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + delta); navigate(d.toISOString().slice(0,10));};
$$('[data-kind]').forEach(el => el.onclick = () => navigate(state.day,el.dataset.kind));
$$('[data-source]').forEach(el => el.onclick = () => {state.source = el.dataset.source; $$('[data-source]').forEach(x => x.classList.toggle('active',x === el)); if (state.data?.conversations_enabled) reloadConversations().catch(err => toast(err.message)); else renderTree();});
for (const id of ['task-search','project-filter','status-filter']) $(`#${id}`).addEventListener(id === 'task-search' ? 'input' : 'change',() => {if (state.data?.conversations_enabled) {if (id === 'project-filter') localStorage.setItem('lablog-project', $('#project-filter').value); clearTimeout(state.searchTimer); state.searchTimer = setTimeout(() => reloadConversations().catch(err => toast(err.message)),id === 'task-search' ? 200 : 0);} else renderTree();});
$('#evidence-search').oninput = renderEvidence;
$$('[data-view]').forEach(el => el.onclick = () => {state.view = el.dataset.view; $$('[data-view]').forEach(x => {x.classList.toggle('active',x === el); x.setAttribute('aria-selected',x === el);}); if (state.data?.conversations_enabled && state.selected?.includes(':') && state.view !== 'summary') selectConversation(state.selected); else renderEvidence();});
$$('[data-section]').forEach(el => el.onclick = () => {$$('[data-section]').forEach(x => x.classList.toggle('active',x === el)); $(`#section-${el.dataset.section}`).scrollIntoView({behavior:'smooth',block:'nearest'}); $(`textarea[data-field="${el.dataset.section}"]`).focus({preventScroll:true});});
$('#select-all').onclick = async () => {
  const button = $('#select-all');
  if (button.disabled) return;
  button.disabled = true;
  try {
    if (state.data?.conversations_enabled) {
      const project = $('#project-filter').value;
      if (!project) return;
      const params = conversationParams(project,0);
      params.set('limit','200');
      let total = Infinity;
      const items = [];
      for (let offset = 0; offset < total; offset += 200) {
        const query = new URLSearchParams(params);
        query.set('offset',offset);
        const page = await api('/conversations?' + query);
        total = page.total;
        items.push(...page.items);
        if (!page.items.length) break;
      }
      const selectable = items.filter(c => !c.deleted && !c.excluded && c.available);
      if (!selectable.length) return;
      const selected = !selectable.every(c => c.selected);
      const changes = selectable.filter(c => c.selected !== selected).map(c => ({key:c.key,selected,revision:c.revision}));
      for (let offset = 0; offset < changes.length; offset += 200) {
        await api('/conversations/selection','PATCH',{changes:changes.slice(offset,offset + 200)});
      }
      if (!changes.length) return;
      await reloadConversations(false);
      selectionNotice();
      return;
    }
    const tasks = visibleTasks(), selected = !tasks.every(t => t.selected);
    for (const t of tasks) if (t.selected !== selected) await setSelection(t.id,selected);
    renderTree(); renderStats();
    if (state.report.manual_saved) showAlert('候选任务已更新，已保存正文保持不变。');
  } catch(err) {toast(err.message);}
  finally {button.disabled = false;}
};
$('#add-manual').onclick = () => {
  showModal('添加线下工作',`<p style="margin-bottom:16px">记录会议、现场测试等工作，来源会明确标记为人工记录。</p><form id="manual-form"><label>任务名称<input name="title" maxlength="120" required placeholder="例如：现场设备联调"></label><label>工作内容<textarea name="summary" maxlength="6000" required placeholder="具体做了什么、取得什么结果？"></textarea></label><label>所属项目<input name="project" maxlength="100" value="线下工作"></label><label>状态<select name="status">${Object.entries(statuses).map(([key,[label]]) => `<option value="${key}" ${key === 'implemented_unverified' ? 'selected' : ''}>${label}</option>`).join('')}</select></label><button class="button primary" type="submit">${icon('plus')}保存工作记录</button></form>`);
  $('#manual-form').onsubmit = async e => {e.preventDefault(); const button = $('button[type=submit]',e.target); button.disabled = true; try {const item = await api('/manual','POST',{date:state.day,...Object.fromEntries(new FormData(e.target))}); $('#modal').close(); await saveDraft(); await load(); await selectTask(item.id); toast('已添加人工工作记录。');} catch(err) {toast(err.message);} finally {button.disabled = false;}};
};
function settings() {
  if (!state.data) {showLogin(); return;}
  const a = state.data.automation;
  showModal('自动化与数据源',`<h3>后台执行</h3><div class="settings-row"><span>自动提交配置</span><span>${a.enabled ? '已启用' : '未启用'}</span></div><div class="settings-row"><span>执行时间 / 时区</span><span>${escapeHTML(a.time || '未设置')} · ${escapeHTML(a.timezone)}</span></div><div class="settings-row"><span>执行器</span><span>${escapeHTML(a.scheduler || '外部 WorkBuddy / Windows 计划任务')}</span></div><div class="settings-row"><span>生成引擎</span><span>${escapeHTML(a.engine || '未配置')}</span></div><h3>会话来源</h3>${state.data.sources.map(s => `<div class="settings-row"><span>${escapeHTML(sourceName(s.id))}</span><span>${escapeHTML(s.message)}</span></div>`).join('')}<h3>最近后台心跳</h3><pre>${escapeHTML(JSON.stringify(a.heartbeat || {},null,2))}</pre><p class="settings-note">界面与现有脚本共享 --config 指定的数据目录。请继续使用 WorkBuddy 或 scripts/auto.ps1 运行自动发送；浏览器不负责调度。这里显示配置，不能据此确认外部计划任务正在运行。</p><button id="settings-theme" class="button">${icon('moon')}切换深浅主题</button>`);
  $('#settings-theme').onclick = toggleTheme;
}
$('#settings-btn').onclick = settings; $('#automation-settings').onclick = settings;
function toggleTheme() {document.body.classList.toggle('dark'); localStorage.setItem('lablog-theme',document.body.classList.contains('dark') ? 'dark' : 'light');}
document.body.classList.toggle('dark',localStorage.getItem('lablog-theme') === 'dark'); $('#theme-btn').onclick = toggleTheme;
$('#history-btn').onclick = async () => {showModal('历史日志','<p>正在读取历史记录…</p>'); try {const items = await api('/history'); $('#modal-body').innerHTML = items.length ? items.map((r,i) => `<button class="history-item" data-history="${i}"><span>${escapeHTML(r.date)} · ${{daily:'日报',weekly:'周报',monthly:'月报'}[r.kind]}<br><small>${escapeHTML(r.template || '未绑定模板')}</small></span>${badge(({verified:'verified_completed',unknown:'unknown',draft:'planned'})[r.status] || r.status)}</button>`).join('') : empty('还没有历史日志','生成报告后，版本与提交状态会显示在这里。','file'); $$('[data-history]').forEach(b => b.onclick = async () => {const r = items[Number(b.dataset.history)]; $('#modal').close(); await navigate(r.date,r.kind);});} catch(err) {$('#modal-body').innerHTML = empty('历史暂不可用',err.message);}};
async function preview(submit = false) {
  try {await saveDraft();} catch {return;}
  const report = state.report;
  showModal(submit ? '确认提交到钉钉' : '日志预览',`<p>${escapeHTML(state.day)} · ${{daily:'日报',weekly:'周报',monthly:'月报'}[state.kind]}</p>${report.template ? `<p>模板：${escapeHTML(report.template)}<br>接收人：${escapeHTML((report.recipients || []).join('、'))}</p>` : '<p>当前为本地工作草稿，生成并绑定真实模板后才可提交。</p>'}${Object.entries(sections()).filter(([,value]) => value.trim()).map(([key,value]) => `<section class="preview-section"><h3>${labels[key]}</h3><p>${escapeHTML(value)}</p></section>`).join('') || '<p class="settings-note">草稿还没有内容。</p>'}${submit && report.hash && !report.readonly ? '<p class="settings-note">确认后会通过现有 DWS 流程发送这一版草稿，并查询回执。结果未知时禁止重复发送。</p><button class="button primary" id="confirm-submit">确认发送当前版本</button>' : ''}`);
  if ($('#confirm-submit')) $('#confirm-submit').onclick = async () => {const button = $('#confirm-submit'); button.disabled = true; button.textContent = '提交并核查回执中…'; try {const result = await api('/submit','POST',{date:state.day,kind:state.kind,hash:report.hash}); $('#modal').close(); await load(); toast(result.status === 'verified' ? '已提交并完成回执核验。' : '提交结果需核查，请查看历史。');} catch(err) {button.textContent = '提交未完成，请关闭后查看状态'; toast(err.message); await load();}};
}
$('#preview-btn').onclick = () => preview(); $('#submit-btn').onclick = () => preview(true);
$('#open-tasks').onclick = () => $('#tasks-panel').classList.add('open'); $('#close-tasks').onclick = () => $('#tasks-panel').classList.remove('open');
document.addEventListener('keydown',e => {if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {e.preventDefault(); $('#tasks-panel').classList.add('open'); $('#task-search').focus();} if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {e.preventDefault(); saveDraft().catch(() => {});}});
window.addEventListener('beforeunload',e => {if (state.dirty || state.saving) {e.preventDefault(); e.returnValue = '';}});
for (const [id,key,min,max,direction] of [['split-left','--left',240,400,1],['split-right','--right',360,600,-1]]) {
  const saved = Number(localStorage.getItem('lablog-' + key)); if (saved >= min && saved <= max) document.documentElement.style.setProperty(key,saved + 'px');
  const update = value => {const width = Math.min(max,Math.max(min,value)); document.documentElement.style.setProperty(key,width + 'px'); localStorage.setItem('lablog-' + key,width); $$('textarea[data-field]').forEach(resize);};
  $(`#${id}`).onpointerdown = e => {const start = e.clientX, panel = id === 'split-left' ? $('.tasks-panel') : $('.report-panel'), initial = panel.getBoundingClientRect().width; e.target.setPointerCapture(e.pointerId); e.target.onpointermove = ev => update(initial + (ev.clientX-start)*direction); e.target.onpointerup = () => e.target.onpointermove = null;};
  $(`#${id}`).onkeydown = e => {if (['ArrowLeft','ArrowRight'].includes(e.key)) {e.preventDefault(); update(parseInt(getComputedStyle(document.documentElement).getPropertyValue(key)) + (e.key === 'ArrowRight' ? 10 : -10)*direction);}};
}
// Native conversation browsing is independent of message/task counts.
async function fetchDirectory() {
  const projects = await api('/projects');
  const wanted = $('#project-filter').value || localStorage.getItem('lablog-project');
  const project = projects.find(p => p.id === wanted) || projects.find(p => p.name === '钉钉') || projects[0];
  const params = conversationParams(project?.id, 0);
  const page = project ? await api('/conversations?' + params) : {items:[],total:0,selected_count:0};
  return {projects,project,page};
}
function conversationParams(project, offset = 0) {
  const params = new URLSearchParams({project_id:project || '',limit:50,offset,q:$('#task-search').value.trim()});
  if (state.source !== 'all') params.set('source',state.source);
  const status = $('#status-filter').value;
  if (['archived','deleted'].includes(status)) params.set('include_archived','true');
  if (status === 'deleted') params.set('include_deleted','true');
  return params;
}
function applyDirectory(directory) {
  state.projects = directory.projects; state.conversations = directory.page.items;
  state.conversationPage = directory.page; state.conversationOffset = 0;
  $('#project-filter').innerHTML = '<option value="">选择项目</option>' + state.projects.map(p => `<option value="${p.id}">${escapeHTML(p.name)} (${p.conversation_count})</option>`).join('');
  $('#project-filter').value = directory.project?.id || '';
  const status = $('#status-filter').value;
  $('#status-filter').innerHTML = '<option value="">当前会话</option><option value="archived">包含归档</option><option value="deleted">包含已删除</option>';
  if (['archived','deleted'].includes(status)) $('#status-filter').value = status;
  $('#task-search').placeholder = '搜索项目内会话标题…';
  $('.stats-title').firstChild.textContent = '会话选择';
  const thirdLabel = $('.stat-labels>span:last-child');
  thirdLabel.innerHTML = '<i class="dot amber"></i>已同步任务 <b id="unknown-count">0</b>';
  $('.panel-footnote').innerHTML = icon('shield') + '会话选择持续生效 · 随时可取消';
}
async function reloadConversations(reset = true) {
  const requestId = (state.conversationRequestId || 0) + 1;
  state.conversationRequestId = requestId;
  if (reset) state.conversationOffset = 0;
  const project = $('#project-filter').value;
  $$('.conversation-row input, .conversation-title').forEach(el => el.disabled = true);
  let page;
  try {page = project ? await api('/conversations?' + conversationParams(project,state.conversationOffset || 0)) : {items:[],total:0,selected_count:0};}
  catch(err) {if (requestId === state.conversationRequestId) {state.conversations = []; state.conversationPage = {items:[],total:0,selected_count:0}; renderStats(); $('#task-tree').innerHTML = empty('会话列表加载失败',err.message+'\n重新选择项目可重试。','refresh');} throw err;}
  if (requestId !== state.conversationRequestId) return;
  state.conversations = page.items; state.conversationPage = page;
  renderTree(); renderStats();
  if (state.conversations.length && !state.conversations.some(c => c.key === state.selected)) await selectConversation(state.conversations[0].key);
  else if (!state.conversations.length) {state.selected = null; state.evidence = null; renderEvidence();}
}
function selectionNotice() {if (state.report?.manual_saved) showAlert('会话范围已保存，之后持续生效。当前人工草稿保持不变，请按需合并新增工作。'); else toast('会话范围已保存，网页和后台将使用同一选择。');}
function renderConversationTree() {
  const items = state.conversations || [], page = state.conversationPage || {total:0};
  const project = (state.projects || []).find(p => p.id === $('#project-filter').value);
  $('#task-count').textContent = `${page.total} 个会话 · 每页 50 条`;
  $('#select-all').textContent = '选择当前项目';
  $('#select-all').title = '按当前来源、搜索词和归档筛选结果批量选择';
  const native = items.map(item => `<div class="task-row conversation-row ${state.selected === item.key ? 'active' : ''}" data-key="${escapeHTML(item.key)}"><input type="checkbox" aria-label="将 ${escapeHTML(item.title)} 会话纳入报告" ${item.selected ? 'checked' : ''} ${item.deleted || item.excluded || !item.available ? 'disabled' : ''}><span class="source-mark ${item.source}">${item.source === 'workbuddy' ? 'W' : item.source === 'chatgpt' ? 'G' : 'C'}</span><button class="conversation-title" title="${escapeHTML(item.title)}"><span>${escapeHTML(item.title)}</span><small>${escapeHTML(sourceName(item.source))} · ${formatTime(item.updated_at)}</small></button>${item.deleted ? '<span class="badge danger">已删除</span>' : item.archived ? '<span class="badge neutral">归档</span>' : item.excluded ? '<span class="badge warning">已排除</span>' : ''}</div>`).join('');
  const manual = state.tasks.filter(t => t.source === 'manual' && (!project || t.project === project.name));
  const manualHTML = manual.map(t => `<div class="task-row manual-row" data-id="${t.id}"><input type="checkbox" ${t.selected ? 'checked' : ''} aria-label="将 ${escapeHTML(t.title)} 纳入报告"><button class="task-title">${escapeHTML(t.title)}</button>${badge(t.status)}</div>`).join('');
  $('#task-tree').innerHTML = (native ? `<details class="project-group" open><summary class="project-title">${icon('folder')}${escapeHTML(project?.name || '项目会话')}<span class="tree-count">${page.total}</span></summary><div class="conversation-list">${native}</div></details>` : empty(project ? '当前项目暂无匹配会话' : '先选择一个项目',project ? '调整来源、标题或归档筛选后重试。' : '按大项目查看 Codex 与 WorkBuddy 会话。','folder')) + (manual.length ? `<div class="project-title">${icon('file')}线下工作</div>${manualHTML}` : '');
  $$('.conversation-row').forEach(row => {
    const item = items.find(c => c.key === row.dataset.key);
    $('.conversation-title',row).onclick = () => selectConversation(item.key);
    $('input',row).onchange = async e => {
      e.target.disabled = true;
      try {const rows = await api('/conversations/selection','PATCH',{changes:[{key:item.key,selected:e.target.checked,revision:item.revision}]}); Object.assign(item,rows[0]); await reloadConversations(false); selectionNotice();}
      catch(err) {toast(err.message); await reloadConversations(false).catch(() => {});} finally {e.target.disabled = false;}
    };
  });
  $$('.manual-row').forEach(row => {$('.task-title',row).onclick = () => selectTask(row.dataset.id); $('input',row).onchange = async e => {try {await setSelection(row.dataset.id,e.target.checked);} catch(err) {toast(err.message);}};});
  $('#conversation-paging').hidden = page.total <= 50;
  $('#conversation-page').textContent = `${Math.floor((state.conversationOffset || 0)/50)+1} / ${Math.max(1,Math.ceil(page.total/50))}`;
  $('#conversations-prev').disabled = !state.conversationOffset;
  $('#conversations-next').disabled = (state.conversationOffset || 0) + 50 >= page.total;
}
function renderConversationStats() {
  const page = state.conversationPage || {total:0,selected_count:0};
  const total = page.total, selected = page.selected_count, percent = total ? Math.round(selected/total*100) : 0;
  $('#count-all').textContent = Object.values(page.counts || {}).reduce((a,b) => a+b,0);
  for (const source of ['codex','workbuddy','chatgpt']) $(`#count-${source}`).textContent = page.counts?.[source] || 0;
  $('#selected-count').textContent = selected; $('#unselected-count').textContent = total-selected;
  $('#unknown-count').textContent = state.tasks.length;
  $('#coverage-value').innerHTML = `${selected}<small>/${total}</small>`;
  $('#coverage-ring').style.setProperty('--coverage',percent+'%'); $('#coverage-percent').textContent = `会话选入 ${percent}%`;
  $('#stats-date').textContent = state.day.slice(5).replace('-',' / ');
}
async function selectConversation(key, offset = 0) {
  const item = (state.conversations || []).find(c => c.key === key) || state.openConversation;
  if (!item || item.key !== key) return;
  state.openConversation = item; state.selected = key; state.evidence = null;
  const requestId = ++evidenceId;
  renderTree(); renderEvidence(); $('#tasks-panel').classList.remove('open');
  try {const query = new URLSearchParams({day:state.day,kind:state.kind,offset,limit:50,view:state.view === 'tools' ? 'tools' : 'messages'}); const data = await api(`/conversations/${encodeURIComponent(key)}/messages?${query}`); if (requestId === evidenceId) state.evidence = {...data,offset};}
  catch(err) {if (requestId === evidenceId) state.evidence = {error:err.message};}
  if (requestId === evidenceId) renderEvidence();
}
function renderConversationEvidence() {
  const item = state.openConversation;
  if (!item) return;
  $('#task-info').innerHTML = `<div class="breadcrumb">${escapeHTML(item.project_name)}<span>/</span>${escapeHTML(sourceName(item.source))}<span>/</span>原生会话</div><h3>${escapeHTML(item.title)}</h3><div class="task-meta"><span class="badge">${escapeHTML(sourceName(item.source))}</span><span>${icon('clock')}${formatTime(item.updated_at)}</span><span class="badge ${item.selected ? 'success' : 'neutral'}">${item.selected ? '持续纳入日报' : '未选入日报'}</span></div>`;
  const e = state.evidence;
  $('#evidence-caption').textContent = state.view === 'summary' ? '原生会话索引' : `${state.day} · ${state.view === 'tools' ? '真实执行记录' : '原始消息'}`;
  if (state.view === 'summary') {
    const summary = e?.summary || item;
    $('#evidence-content').innerHTML = `<div class="summary-card"><h3>${escapeHTML(item.title)}</h3><p>项目：${escapeHTML(item.project_name)}<br>来源：${sourceName(item.source)}<br>消息数：${summary.message_count ?? '尚未读取'}<br>工具记录数：${summary.tool_count ?? '尚未读取'}<br>报告选择：${item.selected ? '持续生效' : '未选入'}</p><p class="evidence-note">消息完成状态表示写入状态；助手自述完成的工作仍需验证。</p></div>`;
    return;
  }
  if (!e) {$('#evidence-content').innerHTML = empty('正在读取原始会话','按所选日期定位消息与证据…'); return;}
  if (e.error) {$('#evidence-content').innerHTML = empty('原始会话暂不可访问',e.error+'\n会话标题和选择仍保留。','lock'); return;}
  const records = state.view === 'tools' ? e.tools : e.messages;
  let body = records.map(m => `<article class="message ${m.role === 'assistant' ? 'assistant' : ''}"><span class="message-avatar ${m.role === 'assistant' ? 'assistant' : ''}">${icon(m.role === 'assistant' ? 'bot' : m.role === 'tool' ? 'file' : 'user')}</span><div class="message-body"><div class="message-header"><strong>${m.role === 'assistant' ? sourceName(item.source) : m.role === 'tool' ? '工具记录' : '用户'}</strong><time>${formatTime(m.time)}</time>${m.status === 'completed' ? '<span class="badge warning">工作待验证</span>' : ''}</div><div class="message-text">${highlight(m.text)}</div></div></article>`).join('');
  if (!records.length) body = empty(state.view === 'tools' ? '本期没有工具执行记录' : '本期没有消息','可以切换报告日期查看该会话的其他工作。',state.view === 'tools' ? 'file' : 'messages');
  body += `<div class="message-paging"><button id="messages-prev" class="button compact" ${(e.offset || 0) === 0 ? 'disabled' : ''}>上一页</button><span>共 ${e.total} ${state.view === 'tools' ? '条执行记录' : '条消息'}</span><button id="messages-next" class="button compact" ${e.next_offset === null ? 'disabled' : ''}>下一页</button></div>`;
  $('#evidence-content').innerHTML = body;
  $('#messages-prev').onclick = () => selectConversation(item.key,Math.max(0,e.offset-50));
  $('#messages-next').onclick = () => selectConversation(item.key,e.next_offset);
}
$('#conversations-prev').onclick = () => {state.conversationOffset = Math.max(0,state.conversationOffset-50); reloadConversations(false).catch(err => toast(err.message));};
$('#conversations-next').onclick = () => {state.conversationOffset += 50; reloadConversations(false).catch(err => toast(err.message));};
$('#date').value = state.day;
$('#evidence-content').innerHTML = empty('把工作过程，变成清晰的记录','同步会话后，在这里查看真实证据。\n也可以先添加一项线下工作。');
load();
