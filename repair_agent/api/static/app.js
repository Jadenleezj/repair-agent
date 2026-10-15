'use strict';
const $ = id => document.getElementById(id);
const labels = {
  QUEUED:'排队中', PENDING:'排队中', RUNNING:'处理中', TRIAGED:'已理解问题',
  INVESTIGATING:'调查中', EVIDENCE_COLLECTED:'调查中', DIAGNOSED:'已诊断',
  WAITING_APPROVAL:'待审批', APPROVAL_QUEUED:'审批已提交', APPROVED:'已批准',
  SUCCEEDED:'修复成功', NO_ANOMALY_FOUND:'未发现异常', NEED_MORE_INFORMATION:'需要补充信息',
  ESCALATED:'转人工处理', UNSUPPORTED_ISSUE:'暂不支持', REJECTED_BY_HUMAN:'已拒绝',
  PRECONDITIONS_CHANGED:'订单状态已变化', EXECUTION_FAILED:'执行失败',
  VERIFICATION_FAILED:'验收未通过', EXECUTION_OUTCOME_UNKNOWN:'结果待确认',
  FAILED:'处理失败', CANCELLED:'已取消', TIMED_OUT:'处理超时', MANUAL_REVIEW:'需人工核查'
};
const activeStates = new Set(['QUEUED','PENDING','RUNNING','TRIAGED','INVESTIGATING','EVIDENCE_COLLECTED','INVESTIGATION_COMPLETE','DIAGNOSED','DECIDED','APPROVAL_QUEUED','APPROVED','PRECONDITIONS_VALID','EXECUTED']);
const issueNames = {DELIVERY_TASK_ORDER_MISMATCH:'签收状态同步异常', PICKUP_SCAN_NOT_APPLIED:'接单状态同步异常', DISPATCH_PROJECTION_STALE:'看板状态不同步', UNKNOWN:'待确认的问题'};
const causeNames = {PROOF_OF_DELIVERY_NOT_APPLIED_TO_ORDER:'签收结果未同步到订单', TASK_ACCEPTANCE_NOT_APPLIED_TO_ORDER:'接单结果未同步到订单', DISPATCH_PROJECTION_OUT_OF_SYNC:'看板读模型与订单状态不一致', UNCONFIRMED_ANOMALY:'暂未确认异常原因', NO_ANOMALY:'当前业务状态正常'};
let selected = null, current = null, busy = false, pollTimer = null, generation = 0, failures = 0, lastView = '';
function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
function showError(message = '') { $('error').textContent = message; $('error').hidden = !message; }
async function api(path, options = {}) {
  const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {...options, signal:controller.signal, headers:{'Content-Type':'application/json', ...options.headers}});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map(x => x.msg).join('；') : data.detail;
      const error = new Error(detail || `服务返回 ${response.status}`); error.status = response.status; throw error;
    }
    return data;
  } catch (error) { if (error.name === 'AbortError') throw new Error('连接超时，请检查服务是否运行。'); throw error; }
  finally { clearTimeout(timeout); }
}
function statusClass(status) {
  if (['SUCCEEDED','NO_ANOMALY_FOUND'].includes(status)) return 'good';
  if (['WAITING_APPROVAL','NEED_MORE_INFORMATION','ESCALATED','EXECUTION_OUTCOME_UNKNOWN','MANUAL_REVIEW'].includes(status)) return 'warn';
  if (['FAILED','EXECUTION_FAILED','VERIFICATION_FAILED','TIMED_OUT','REJECTED_BY_HUMAN'].includes(status)) return 'bad';
  return '';
}
function closeSidebar() { $('sidebar').classList.remove('open'); }
function resetChat(draft = '') {
  generation++; clearTimeout(pollTimer); selected = null; current = null; lastView = ''; failures = 0;
  $('header-title').textContent = '新聊天'; $('composer').hidden = false; $('task-footer').hidden = true;
  $('composer-hint').hidden = false; $('message').value = draft; $('message').disabled = false;
  $('send').disabled = busy; showError(); renderWelcome(); refreshCases(); closeSidebar(); $('message').focus();
}
function renderWelcome() {
  const welcome = element('section','welcome');
  welcome.innerHTML = '<div class="eyebrow">ORDER REPAIR WORKSPACE</div><h1>把订单的问题，<br><span>交给修数助手。</span></h1><p>描述业务现象，附上订单号。助手会查询证据、分析原因，并在必要时等待你的审批。</p><div class="examples"></div><div class="welcome-note"><span>01 理解问题</span><span>02 调查证据</span><span>03 修复与验收</span></div>';
  for (const sample of [{tag:'状态修复', title:'签收了，订单状态没更新', message:'订单 W1001 线下已签收，但系统还是派送中，阻塞交接作业'}, {tag:'看板同步',title:'订单正确，看板还没同步',message:'订单 W3001 已送达，但派送看板显示还在派送中'}]) {
    const button = element('button','example'); button.type = 'button';
    button.append(element('span','tag',sample.tag),element('strong','',sample.title),element('p','',sample.message),element('span','arrow','填入示例 ↗'));
    button.addEventListener('click',() => { $('message').value = sample.message; $('message').focus(); });
    welcome.querySelector('.examples').append(button);
  }
  $('conversation').replaceChildren(welcome);
}
async function refreshCases() {
  try {
    const rows = await api('/cases?limit=100'); const list = $('case-list'); list.replaceChildren();
    if (!rows.length) list.append(element('p','muted list-empty','还没有修数任务，从一条问题开始。'));
    for (const row of rows) {
      const button = element('button',`case-row${row.case_id === selected ? ' active' : ''}`);
      button.append(element('span','case-title',row.order_id ? `订单 ${row.order_id}` : row.case_id));
      const meta = element('span','case-meta'); meta.append(element('span',`dot ${activeStates.has(row.status) ? 'running' : row.status === 'WAITING_APPROVAL' ? 'waiting' : ''}`),element('span','',labels[row.status] || row.status));
      button.append(meta); button.addEventListener('click',() => selectCase(row.case_id)); list.append(button);
    }
  } catch (error) { if (!$('case-list').children.length) $('case-list').append(element('p','list-empty','任务列表暂时不可用')); }
}
async function selectCase(id) {
  generation++; const token = generation; clearTimeout(pollTimer); selected = id; current = null; lastView = ''; failures = 0;
  $('header-title').textContent = id; $('composer').hidden = true; $('composer-hint').hidden = true; showError(); closeSidebar();
  $('conversation').replaceChildren(element('p','muted','正在加载任务…')); refreshCases(); await pollCase(token);
}
async function pollCase(token) {
  if (token !== generation || !selected) return;
  const id = selected;
  try {
    const [record, job] = await Promise.all([api(`/cases/${encodeURIComponent(id)}`), api(`/cases/${encodeURIComponent(id)}/task`).catch(error => { if (error.status === 404) return null; throw error; })]);
    if (token !== generation) return;
    failures = 0; current = record; showError();
    const state = record.state || {}; const status = state.status || record.status;
    const running = job ? ['PENDING','RUNNING'].includes(job.status) : activeStates.has(status);
    const signature = JSON.stringify([record,job]);
    if (signature !== lastView) { renderCase(record,job,running); lastView = signature; }
    if (running) pollTimer = setTimeout(() => pollCase(token),1500); else refreshCases();
  } catch (error) {
    if (token !== generation) return;
    showError(`进度暂时无法获取：${error.message}。正在重试，任务不会重新提交。`);
    pollTimer = setTimeout(() => pollCase(token),Math.min(8000,1500 * ++failures));
  }
}
function fact(list, title, value) { if (value === undefined || value === null || value === '') return; const box = element('div','fact'); box.append(element('dt','',title),element('dd','',String(value))); list.append(box); }
function resultCopy(status, state) {
  const messages = {
    SUCCEEDED:'修复已完成，独立查询验收通过。', NO_ANOMALY_FOUND:'根据查询到的业务证据，当前未发现需要修复的异常。',
    WAITING_APPROVAL:'证据调查已完成。这项修复需要人工批准，批准后会重新核对订单状态，再执行修复。',
    NEED_MORE_INFORMATION:`还需要补充 ${state.missing_information?.includes('order_id') ? 'Order ID（订单号）' : (state.missing_information || []).join('、')}。当前流程已结束，可以带上完整信息重新提交。`,
    ESCALATED:'当前证据或修复条件不足，已停止自动修复，请人工核查下面的原因。',
    UNSUPPORTED_ISSUE:'暂时无法识别这类问题。请补充实际业务状态与系统状态，或交由人工处理。',
    REJECTED_BY_HUMAN:'你已拒绝这项修复，任务结束。', PRECONDITIONS_CHANGED:'订单在调查或等待审批期间发生了变化，本次修复未继续执行。',
    EXECUTION_OUTCOME_UNKNOWN:'修复接口结果尚未确认，请先人工核查或进行对账。',
    EXECUTION_FAILED:'修复接口未成功执行，请查看执行结果。', VERIFICATION_FAILED:'修复请求已执行，但业务验收未通过，请人工核查。',
    CANCELLED:'任务已取消。', TIMED_OUT:'本次任务超时，已停止继续处理。', FAILED:'本次处理失败，请检查服务日志。'
  };
  return messages[status] || '处理结果已保存，可查看下方详情。';
}
function renderCase(record, job, running) {
  const state = record.state || {}; const status = state.status || record.status; const view = $('conversation');
  const user = element('div','message user-message'); user.append(element('div','user-bubble',record.raw_request || state.raw_request || '修数任务'));
  const assistant = element('div','message'); const name = element('div','assistant-label'); name.append(element('span','assistant-logo','R'),element('span','','修数助手')); assistant.append(name);
  if (running) {
    const processing = element('div','processing'); const row = element('div','loading-row'); row.append(element('span','spinner'),element('span','',status === 'APPROVAL_QUEUED' ? '审批已提交，正在继续处理…' : '任务已提交，正在查询和分析业务证据…'));
    processing.append(row,element('p','','你可以留在这里等待，也可以新建聊天发起另一项独立任务。')); assistant.append(processing);
  } else {
    const card = element('div','result-card'); const heading = element('div','result-heading'); heading.append(element('h2','',labels[status] || status),element('span',`status-pill ${statusClass(status)}`,state.risk_level ? `${{LOW:'低',MEDIUM:'中',HIGH:'高'}[state.risk_level] || state.risk_level}风险` : '处理结果'));
    card.append(heading,element('p','result-copy',resultCopy(status,state)));
    const facts = element('dl','facts'); fact(facts,'Order ID',state.order_id); fact(facts,'问题类型',issueNames[state.issue_type]); fact(facts,'诊断原因',causeNames[state.root_cause] || state.root_cause); fact(facts,'修复方案',state.playbook_id); fact(facts,'任务编号',record.case_id); card.append(facts);
    if (state.decision_reasons?.length) { const reasons = element('ul','reason-list'); for (const reason of state.decision_reasons) reasons.append(element('li','',reason)); card.append(reasons); }
    if (status === 'WAITING_APPROVAL') {
      const actions = element('div','approval-actions');
      for (const [label, approved] of [['批准并执行',true],['拒绝修复',false]]) { const button = element('button',approved ? 'approve' : '',label); button.disabled = busy; button.addEventListener('click',() => approveCase(approved)); actions.append(button); }
      card.append(actions);
    }
    if (status === 'NEED_MORE_INFORMATION') { const actions = element('div','approval-actions'); const button = element('button','inline-action','补充完整信息，重新提交'); button.addEventListener('click',() => resetChat(`${record.raw_request || state.raw_request}\n补充信息：`)); actions.append(button); card.append(actions); }
    if (state.audit?.length) { const detail = element('details'); detail.append(element('summary','','查看处理过程'),element('div','audit',state.audit.join('\n'))); card.append(detail); }
    if (state.execution_result || state.verification_result || job?.error) { const detail = element('details'); detail.append(element('summary','','查看执行与验收详情'),element('div','audit',JSON.stringify({execution:state.execution_result,verification:state.verification_result,error:job?.error},null,2))); card.append(detail); }
    assistant.append(card);
  }
  view.replaceChildren(user,assistant); view.scrollTop = view.scrollHeight;
  const footer = $('task-footer'); footer.hidden = false; footer.replaceChildren(element('span','',running ? '处理中，页面将自动更新。' : status === 'WAITING_APPROVAL' ? '任务正在等待审批。' : '此任务已结束。'));
  const button = element('button','','新建聊天'); button.addEventListener('click',() => resetChat()); footer.append(button);
}
async function approveCase(approved) {
  if (busy || !selected) return;
  busy = true; const id = selected, token = generation;
  document.querySelectorAll('.approval-actions button').forEach(button => button.disabled = true); showError();
  try {
    await api(`/cases/${encodeURIComponent(id)}/approval`,{method:'POST',body:JSON.stringify({approved,actor_id:'web-user',comment:'聊天工作台提交审批'})});
    if (token === generation) { lastView = ''; await pollCase(token); }
  } catch (error) { if (token === generation) { showError(`审批提交未确认：${error.message}。请刷新任务状态后再操作。`); lastView = ''; await pollCase(token); } }
  finally { busy = false; $('send').disabled = false; document.querySelectorAll('.approval-actions button').forEach(button => button.disabled = false); }
}
$('composer').addEventListener('submit',async event => {
  event.preventDefault(); const message = $('message').value.trim(); if (!message || busy || selected) return;
  busy = true; $('send').disabled = true; $('message').disabled = true; showError();
  try {
    const created = await api('/cases',{method:'POST',body:JSON.stringify({message,requester_id:'web-user'})});
    await selectCase(created.case_id);
  } catch (error) { showError(`提交未确认：${error.message}。可刷新左侧任务列表，确认是否已经创建。`); }
  finally { busy = false; $('send').disabled = false; $('message').disabled = false; document.querySelectorAll('.approval-actions button').forEach(button => button.disabled = false); }
});
$('message').addEventListener('keydown',event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } });
$('new-chat').addEventListener('click',() => { if (!busy) resetChat(); });
$('refresh').addEventListener('click',() => { refreshCases(); if (selected) { clearTimeout(pollTimer); pollCase(generation); } });
$('menu').addEventListener('click',() => $('sidebar').classList.toggle('open'));
document.addEventListener('keydown',event => { if (event.key.toLowerCase() === 'n' && !event.metaKey && !event.ctrlKey && !event.altKey && !['TEXTAREA','INPUT'].includes(document.activeElement.tagName) && !busy) resetChat(); if (event.key === 'Escape') closeSidebar(); });
async function connection() { try { await api('/ready'); $('connection-dot').className='connection-dot ready'; $('connection-text').textContent='服务已连接'; } catch { $('connection-dot').className='connection-dot offline'; $('connection-text').textContent='服务未就绪 · 检查后台'; } }
resetChat(); connection();
const refreshTimer = setInterval(() => { if (!document.hidden) { refreshCases(); connection(); } },10000);
window.addEventListener('pagehide',() => { clearTimeout(pollTimer); clearInterval(refreshTimer); });
