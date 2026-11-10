import { timestamp, statusInfo, projectKey, projectsFor, visibleSessions, markdown, escapeHtml } from './session-utils.mjs';

import { AttachmentComposer, renderAttachments } from './attachment-ui.mjs';
import { effortLabels, effortsFor, describeConfiguration } from './session-settings.mjs';
import { clientId, copyPlainText } from './browser-compat.mjs';
import { sessionTree, isProjectExpanded, projectRows, creationReference } from './session-tree.mjs';

const $ = id => document.getElementById(id);
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const state = { ws: null, connected: false, sessions: [], project: 'all', selected: null, agent: 'codex', assistant: null,
  creating: null, expanded: {}, treeLimits: new Map(), catalogRequest: null, inlinePending: null, hasSnapshot: false,
  configurations: new Map(), catalog: null, settingsTarget: null, settingsAgent: null, settingsLoading: false, savingSettings: false,
  options: [], cache: new Map(), drafts: new Map(), pending: new Map(), requests: new Map(),
  listRequest: null, readRequest: null, loading: false, historyLoading: false, historyFailed: false, nextCursor: null, retry: 0 };
const storage = { get(key) { try { return localStorage.getItem(`codex-workspace-v2.${key}`); } catch { return null; } }, set(key, value) { try { localStorage.setItem(`codex-workspace-v2.${key}`, value); } catch { /* storage may be disabled */ } } };
const settledMessages = new Set();
let toastTimer;
const attachmentComposer = new AttachmentComposer({
  context: () => ({ key: attachmentKey(), threadId: state.selected,
    enabled: state.connected && !!currentSession() && !currentPending() && currentSession()?.writable !== false && currentSession()?.readOnly !== true }),
  changed: () => updateControls(), notify: text => toast(text),
});
function attachmentKey(threadId = state.selected) { return `${state.assistant?.adaptorId || state.agent}:${threadId || ''}`; }
function toast(text) { $('toast').textContent = text; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 3000); }
function notice(id, text = '', error = false) { $(id).textContent = text; $(id).hidden = !text; $(id).classList.toggle('error', error); }
function currentSession() { return state.sessions.find(s => s.id === state.selected); }
function currentPending() { return [...state.pending.values()].find(p => p.threadId === state.selected); }
function relativeDate(value) {
  const time = timestamp(value); if (!time) return '—';
  const diff = Math.max(0, Date.now() - time);
  if (diff < 60000) return '刚刚'; if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`;
  const date = new Date(time); return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}
function fullDate(value) { const time = timestamp(value); return time ? new Date(time).toLocaleString('zh-CN', { hour12: false }) : '更新时间未知'; }
function setConnection(text, mode = '') { $('connection').className = `connection ${mode}`; $('connection').lastElementChild.textContent = text; }
function updateControls() {
  const session = currentSession(), pending = currentPending();
  const busy = pending || statusInfo(session?.status).key === 'running';
  const readonly = session?.writable === false || session?.readOnly === true;
  $('send').disabled = !state.connected || !session || busy || readonly || state.historyLoading || state.historyFailed || !!state.inlinePending || (state.savingSettings && state.settingsTarget === state.selected) || attachmentComposer.blocked() || (!$('input').value.trim() && !attachmentComposer.ready().length);
  $('input').disabled = !session || readonly;
  $('attachFiles').disabled = !state.connected || !session || !!busy || readonly;
  $('cancel').hidden = !pending;
  $('cancel').disabled = !state.connected || !pending || ['queued', 'sending', 'cancelling', 'uncertain'].includes(pending.status);
  $('refreshHistory').disabled = !state.connected || !session || !!pending || state.historyLoading;
  $('historyLimit').disabled = !!pending;
  $('refresh').disabled = !state.connected || state.loading;
  $('loadMore').disabled = !state.connected || state.loading;
  $('openSessionSettings').disabled = !state.connected || !session || !!state.inlinePending;
  updateSettingsControls();
  updateComposerSettingsControls();
  $('export').disabled = !(state.cache.get(state.selected)?.messages.length);
  $('agentSelect').disabled = !state.connected || state.pending.size > 0 || state.loading || state.assistant?.status === 'starting' || !!state.inlinePending || !!state.creating;
  updateCreateControls();
  if (readonly) $('input').placeholder = '这个会话是只读的，可以查看和导出记录';
  else if (busy && !pending) $('input').placeholder = '会话正在其他客户端执行，请稍后刷新';
  else $('input').placeholder = '向当前会话追加消息，继续这项工作…';
}
function wireRequest(command, metadata) {
  if (!state.connected || state.ws?.readyState !== WebSocket.OPEN) { toast('连接已断开，请等待重新连接'); return null; }
  const id = clientId();
  const timeout = setTimeout(() => {
    if (!state.requests.has(id)) return;
    state.requests.delete(id);
    if (state.listRequest === id) { state.loading = false; notice('listNotice', '加载超时，保留已有会话。请重试刷新。', true); renderSessions(); }
    if (state.readRequest === id) { state.historyLoading = false; state.historyFailed = true; notice('historyNotice', '读取超时，保留已加载记录。请重试刷新。', true); renderMessages(); }
    if (metadata.kind === 'create' && state.creating?.requestId === id) { state.creating.uncertain = true; notice('listNotice', '新建确认超时，可能已创建。请刷新列表确认，不会自动重复新建。', true); updateCreateControls(); }
    if (metadata.kind === 'send') {
      const pending = state.pending.get(metadata.clientId);
      if (pending && pending.status === 'sending') { pending.status = 'uncertain'; pending.error = '发送确认超时。为避免重复发送，请刷新连接后检查会话记录。'; renderMessages(); }
    }
    if (metadata.kind === 'inline-config' && state.inlinePending?.requestId === id) { state.inlinePending = null; renderComposerSettings(); notice('composerConfigStatus', '设置保存超时，未确认应用。请重新选择或查看会话设置。', true); }
    if (metadata.kind === 'models' && state.catalogRequest === id) { state.catalogRequest = null; renderComposerSettings(); }
    if (metadata.kind === 'config' || metadata.kind === 'save-config') { state.settingsLoading = false; state.savingSettings = false; notice('sessionConfigNotice', '读取 / 保存超时，请重试。未确认成功的设置不会自动发送。', true); }
    updateControls();
  }, 35000);
  state.requests.set(id, { ...metadata, timeout });
  state.ws.send(JSON.stringify({ ...command, requestId: id })); return id;
}
function finishRequest(id) { const request = state.requests.get(id); if (request) { clearTimeout(request.timeout); state.requests.delete(id); } return request; }
function sendCommand(command) { if (!state.connected) return toast('等待连接恢复后再操作'); state.ws.send(JSON.stringify(command)); }
function requestSessions(append = false) {
  if (!state.connected) return;
  state.loading = true; notice('listNotice');
  state.listRequest = wireRequest({ type: 'sessions.list', ...(append ? { cursor: state.nextCursor } : {}) }, { kind: 'list', append, agent: state.agent });
  renderSessions(); updateControls();
}
function loadTreePreferences() {
  try { const value = JSON.parse(storage.get(`${state.agent}.expanded`) || '{}'); state.expanded = value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
  catch { state.expanded = {}; }
  state.treeLimits.clear();
}
function saveTreePreferences() { storage.set(`${state.agent}.expanded`, JSON.stringify(state.expanded)); }
function treeFilters() { return { query: $('sessionSearch').value, status: $('statusFilter').value, sort: $('sortOrder').value }; }
function renderSessions() {
  const filters = treeFilters(), groups = sessionTree(state.sessions, filters);
  const count = groups.reduce((sum, group) => sum + group.sessions.length, 0);
  $('projectCount').textContent = groups.length;
  $('listSummary').textContent = state.loading ? '正在同步…' : `${count} / ${state.sessions.length} 个会话`;
  $('loadMore').hidden = !state.nextCursor;
  const root = $('sessionList'), scroll = root.scrollTop;
  root.setAttribute('aria-busy', String(state.loading)); root.replaceChildren();
  for (const [index, project] of groups.entries()) {
    const group = document.createElement('section'); group.className = 'tree-project'; group.dataset.projectKey = project.key;
    const expanded = isProjectExpanded(project, state.expanded, filters);
    const heading = document.createElement('button'); heading.type = 'button'; heading.className = 'tree-project-heading';
    const titleRow = document.createElement('div'); titleRow.className = 'tree-project-title';
    const listId = `project-sessions-${index}`;
    heading.setAttribute('aria-expanded', String(expanded)); heading.setAttribute('aria-controls', listId); heading.title = project.displayLabel;
    heading.innerHTML = `<span class="tree-caret">${icon('chevron')}</span>${icon('folder')}<span class="tree-project-name">${escapeHtml(project.displayLabel)}</span><span class="tree-project-count">${project.sessions.length}</span>`;
    heading.addEventListener('click', () => {
      // A search expands matches without losing the user's saved collapse state.
      if (filters.query.trim() || filters.status !== 'all') {
        const open = heading.getAttribute('aria-expanded') === 'true';
        heading.setAttribute('aria-expanded', String(!open)); list.hidden = open;
      } else { state.expanded[project.key] = !expanded; saveTreePreferences(); renderSessions(); }
    });
    const reference = creationReference(project);
    const add = document.createElement('button'); add.type = 'button'; add.className = 'icon-button project-create';
    add.dataset.projectKey = project.key; add.dataset.createAvailable = String(!!reference);
    add.setAttribute('aria-label', `在 ${project.displayLabel} 新建会话`);
    add.title = reference ? `在 ${project.displayLabel} 下新建空会话` : '当前连接不支持在这个项目创建，或项目目录信息不可用';
    add.textContent = state.creating?.projectKey === project.key ? '…' : '+';
    add.addEventListener('click', () => createProjectSession(project, reference));
    titleRow.append(heading, add);
    const list = document.createElement('ul'); list.id = listId; list.className = 'tree-sessions'; list.hidden = !expanded;
    const limit = state.treeLimits.get(project.key) || 5;
    const rows = filters.query.trim() || filters.status !== 'all' ? project.sessions : projectRows(project, state.selected, limit);
    for (const session of rows) {
      const item = document.createElement('li');
      const status = statusInfo(session.status), button = document.createElement('button');
      button.type = 'button'; button.className = `session-card tree-session${session.id === state.selected ? ' selected' : ''}`;
      button.dataset.sessionId = session.id; button.setAttribute('aria-pressed', String(session.id === state.selected));
      button.setAttribute('aria-label', session.name || '未命名会话');
      button.title = `${session.name || '未命名会话'}\n${status.label} · ${fullDate(session.updatedAt)}\n${String(session.preview || '').slice(0, 180)}`;
      button.innerHTML = `<span class="tree-status ${status.key}" title="${status.label}" aria-hidden="true"></span><span class="tree-session-name">${escapeHtml(session.name || '未命名会话')}</span>${session.managed ? '<span class="managed-badge">助手</span>' : ''}<time class="tree-session-time" title="${escapeHtml(fullDate(session.updatedAt))}">${escapeHtml(relativeDate(session.updatedAt))}</time>`;
      button.addEventListener('click', () => selectSession(session.id)); item.append(button); list.append(item);
    }
    const remaining = project.sessions.length - rows.length;
    if (remaining > 0) {
      const item = document.createElement('li'), more = document.createElement('button'); more.type = 'button'; more.className = 'tree-show-more text-button';
      more.textContent = `再显示 ${Math.min(8, remaining)} 个会话`; more.setAttribute('aria-label', `${project.displayLabel}：展开更多会话`);
      more.addEventListener('click', () => { state.treeLimits.set(project.key, limit + 8); renderSessions(); }); item.append(more); list.append(item);
    }
    group.append(titleRow, list); root.append(group);
  }
  if (!groups.length) {
    const empty = document.createElement('div'); empty.className = 'list-empty';
    empty.textContent = state.loading ? '正在加载项目与会话…' : state.sessions.length ? '没有匹配的项目或会话' : '暂无可见会话，请检查连接设置';
    if (state.sessions.length && !state.loading) { const reset = document.createElement('button'); reset.type = 'button'; reset.className = 'text-button'; reset.textContent = '清除筛选'; reset.addEventListener('click', () => { $('sessionSearch').value = ''; $('statusFilter').value = 'all'; renderSessions(); }); empty.append(reset); }
    root.append(empty);
  }
  root.scrollTop = scroll; updateCreateControls();
}
function updateCreateControls() {
  const blocked = !state.connected || !!state.creating || state.loading || state.pending.size > 0 || state.assistant?.status === 'starting';
  for (const button of document.querySelectorAll('.project-create')) button.disabled = blocked || button.dataset.createAvailable !== 'true';
}
function createProjectSession(project, reference) {
  if (!state.connected || state.creating || state.loading || state.pending.size || !reference) return;
  state.creating = { projectKey: project.key, agent: state.agent, requestId: null, uncertain: false };
  notice('listNotice', `正在为 ${project.displayLabel} 创建空会话…`);
  const id = wireRequest({ type: 'session.create', projectKey: project.key, referenceId: reference.id, name: '新会话' },
    { kind: 'create', projectKey: project.key, agent: state.agent });
  if (!id) { state.creating = null; updateControls(); return; }
  state.creating.requestId = id; renderSessions(); updateControls();
}
function toggleAllProjects(open) {
  for (const project of projectsFor(state.sessions)) state.expanded[project.key] = open;
  saveTreePreferences(); renderSessions();
}
function syncSidebarAccessibility() { $('projectsPanel').inert = matchMedia('(max-width: 900px)').matches && !$('workspace').classList.contains('projects-open'); }
function openProjects() { $('workspace').classList.add('projects-open'); $('sidebarScrim').hidden = false; syncSidebarAccessibility(); }
function saveDraft() { if (state.selected) state.drafts.set(state.selected, $('input').value); }
function selectSession(id, { navigate = true } = {}) {
  if ($('sessionSettingsDialog').open && state.settingsTarget !== id) $('sessionSettingsDialog').close();
  saveDraft(); state.selected = id; const selected = currentSession(); if (selected && (navigate || state.expanded[projectKey(selected)] === undefined)) { state.expanded[projectKey(selected)] = true; saveTreePreferences(); } storage.set(`${state.agent}.selected`, id); $('input').value = state.drafts.get(id) || '';
  state.readRequest = null; state.historyLoading = false; state.historyFailed = false;
  notice('historyNotice'); $('welcome').hidden = true; $('conversation').hidden = false; if (navigate || !matchMedia('(max-width: 680px)').matches) $('workspace').classList.add('detail-open');
  if (navigate) closeProjects();
  attachmentComposer.render(); renderSessions(); renderConversationHeader(); renderComposerSettings(); renderMessages(true);
  if (!currentPending() || !state.cache.has(id)) readSession(); else updateControls();
  notice('composerConfigStatus'); requestModelCatalog();
}
function renderConversationHeader() {
  const s = currentSession(); if (!s) return;
  const status = statusInfo(s.status); $('sessionTitle').textContent = s.name || '未命名会话'; $('sessionTitle').title = $('sessionTitle').textContent;
  $('sessionProject').textContent = projectsFor(state.sessions).find(p => p.key === projectKey(s))?.displayLabel || s.projectLabel || s.project || '未指定项目';
  $('sessionId').textContent = String(s.id).slice(-8); $('sessionId').title = s.id;
  $('sessionStatus').className = `status-badge ${status.key}`; $('sessionStatus').textContent = status.label;
  $('sessionModel').textContent = describeConfiguration(state.configurations.get(state.selected));
  $('sessionModel').title = $('sessionModel').textContent;
  $('sessionUpdated').textContent = `更新于 ${relativeDate(s.updatedAt)}`; $('sessionUpdated').title = fullDate(s.updatedAt);
  $('composerTarget').replaceChildren(); const dot = document.createElement('span'); dot.className = 'target-dot';
  $('composerTarget').append(dot, document.createTextNode(`当前会话 · ${String(s.id).slice(-8)}`)); $('composerTarget').title = `消息发送到：${s.name || s.id}`;
}
function requestModelCatalog() {
  if (!state.connected || state.catalog || state.catalogRequest) return;
  state.catalogRequest = wireRequest({ type: 'models.list' }, { kind: 'models', agent: state.agent });
}
function updateComposerSettingsControls() {
  const config = state.configurations.get(state.selected), session = currentSession();
  const blocked = !state.connected || !session || !config || state.historyLoading || state.historyFailed || !!currentPending() || !!state.inlinePending || config.writable === false || state.savingSettings;
  $('composerModel').disabled = blocked || !config?.support?.model;
  $('composerEffort').disabled = blocked || !config?.support?.reasoningEffort || !state.catalog;
}
function renderComposerSettings() {
  const config = state.configurations.get(state.selected), modelSelect = $('composerModel'), effortSelect = $('composerEffort');
  if (state.inlinePending?.threadId === state.selected) return updateComposerSettingsControls();
  modelSelect.replaceChildren(); effortSelect.replaceChildren();
  if (!config) { modelSelect.append(new Option('模型', '')); effortSelect.append(new Option('思考强度', '')); return updateComposerSettingsControls(); }
  const inheritedModel = config.observed?.model || config.defaults?.model;
  modelSelect.append(new Option(`${inheritedModel || '服务端默认'} · 会话默认`, ''));
  const models = new Map((state.catalog?.models || []).map(m => [m.model, m]));
  for (const id of [config.observed?.model, config.defaults?.model, config.overrides?.model]) if (id && !models.has(id)) models.set(id, { model: id, displayName: id });
  for (const model of models.values()) modelSelect.append(new Option(model.displayName || model.model, model.model));
  modelSelect.append(new Option('自定义模型 / 详细设置…', '__custom__'));
  modelSelect.value = config.overrides?.model || '';
  const selectedModel = modelSelect.value || inheritedModel;
  const inheritedEffort = config.overrides?.model && config.overrides.model !== inheritedModel
    ? models.get(config.overrides.model)?.defaultReasoningEffort
    : config.observed?.reasoningEffort || config.defaults?.reasoningEffort;
  effortSelect.append(new Option(`${inheritedEffort ? effortLabels[inheritedEffort] || inheritedEffort : '跟随会话'} · 默认`, ''));
  for (const effort of effortsFor(state.catalog, selectedModel)) effortSelect.append(new Option(effortLabels[effort] || effort, effort));
  const saved = config.overrides?.reasoningEffort;
  if (saved && ![...effortSelect.options].some(o => o.value === saved)) { const option = new Option(`${effortLabels[saved] || saved} · 未确认支持`, saved); option.disabled = true; effortSelect.append(option); }
  effortSelect.value = saved || '';
  modelSelect.title = `下一轮模型：${config.effective?.model || '服务端默认'}；选择后自动保存，仅影响此会话`;
  effortSelect.title = `下一轮思考强度：${config.effective?.reasoningEffort || '跟随会话'}；选择后自动保存`;
  updateComposerSettingsControls();
}
function saveComposerSettings(modelChanged = false) {
  const config = state.configurations.get(state.selected);
  if (!config || !state.connected) return;
  if ($('composerModel').value === '__custom__') { renderComposerSettings(); openSessionSettings(); return; }
  const settings = { model: $('composerModel').value || null,
    reasoningEffort: modelChanged ? null : $('composerEffort').value || null };
  if (modelChanged) $('composerEffort').value = '';
  state.inlinePending = { threadId: state.selected, agent: state.agent, requestId: null };
  notice('composerConfigStatus', '正在保存下一轮设置…'); updateControls();
  const id = wireRequest({ type: 'session.config.set', threadId: state.selected, settings }, { kind: 'inline-config', threadId: state.selected, agent: state.agent });
  if (!id) { state.inlinePending = null; renderComposerSettings(); updateControls(); return; }
  state.inlinePending.requestId = id;
}
function updateSettingsControls() {
  const config = state.configurations.get(state.settingsTarget);
  const pending = [...state.pending.values()].some(p => p.threadId === state.settingsTarget);
  const blocked = !state.connected || !!state.inlinePending || state.settingsLoading || state.savingSettings || pending || !config || state.settingsAgent !== state.agent || config.writable === false;
  $('sessionModelInput').disabled = blocked || !config?.support?.model;
  $('sessionEffortInput').disabled = blocked || !config?.support?.reasoningEffort;
  $('saveSessionSettings').disabled = blocked || (!config?.support?.model && !config?.support?.reasoningEffort);
  $('resetSessionSettings').disabled = $('saveSessionSettings').disabled;
}
function renderEfforts() {
  const config = state.configurations.get(state.settingsTarget), select = $('sessionEffortInput'), previous = select.value;
  const model = $('sessionModelInput').value.trim() || config?.observed?.model || config?.defaults?.model;
  select.replaceChildren(new Option('保持会话 / Agent 默认', ''));
  for (const effort of effortsFor(state.catalog, model)) select.append(new Option(`${effortLabels[effort] || effort} · ${effort}`, effort));
  if (previous && ![...select.options].some(o => o.value === previous)) {
    const option = new Option(`${previous} · 所选模型未确认支持`, previous); option.disabled = true; select.append(option);
  }
  select.value = previous;
}
function renderSessionSettings({ fill = true } = {}) {
  const config = state.configurations.get(state.settingsTarget);
  if (!config) return updateSettingsControls();
  $('observedModel').textContent = config.observed?.model || '未知 · 历史没有记录模型';
  $('observedEffort').textContent = `思考强度：${config.observed?.reasoningEffort || '未知 · 历史没有记录'}`;
  $('observedSource').textContent = `来源：${config.observed?.source || 'unknown'}；默认模型：${config.defaults?.model || '服务端默认'}`;
  if (fill) { $('sessionModelInput').value = config.overrides?.model || ''; $('sessionEffortInput').value = ''; }
  $('sessionModelInput').placeholder = config.effective?.model || '输入模型 ID，或从建议列表选择';
  $('sessionModelChoices').replaceChildren();
  const models = [...(state.catalog?.models || [])];
  if (config.observed?.model && !models.some(m => m.model === config.observed.model)) models.push({ model: config.observed.model, displayName: '历史实际模型' });
  for (const model of models) { const option = document.createElement('option'); option.value = model.model; option.label = model.displayName || model.model; $('sessionModelChoices').append(option); }
  renderEfforts();
  if (fill && config.overrides?.reasoningEffort) {
    const effort = config.overrides.reasoningEffort;
    if (![...$('sessionEffortInput').options].some(o => o.value === effort)) $('sessionEffortInput').append(new Option(`${effort} · 未确认支持`, effort));
    $('sessionEffortInput').value = effort;
  }
  $('modelCatalogNote').textContent = state.catalog?.note || (state.catalog?.verified ? '模型清单来自当前 runtime；已知模型显示其支持的强度。自定义模型的可用性由下一轮请求确认。' : '正在读取模型选项…');
  updateSettingsControls();
}
function openSessionSettings() {
  if (!state.selected || !state.connected) return;
  state.settingsTarget = state.selected; state.settingsAgent = state.agent; state.settingsLoading = true; state.savingSettings = false;
  const session = currentSession(); $('settingsTarget').textContent = `${session?.name || '会话'} · ${String(state.selected).slice(-8)}`;
  $('observedModel').textContent = '正在读取…'; $('observedEffort').textContent = ''; $('observedSource').textContent = ''; notice('sessionConfigNotice');
  $('sessionSettingsDialog').showModal(); updateSettingsControls();
  wireRequest({ type: 'session.config.get', threadId: state.settingsTarget }, { kind: 'config', threadId: state.settingsTarget, agent: state.agent });
  wireRequest({ type: 'models.list' }, { kind: 'models', agent: state.agent });
}
function saveSessionSettings(reset = false) {
  if ($('saveSessionSettings').disabled) return;
  state.savingSettings = true; notice('sessionConfigNotice'); updateSettingsControls();
  const settings = { model: reset ? null : $('sessionModelInput').value.trim() || null, reasoningEffort: reset ? null : $('sessionEffortInput').value || null };
  wireRequest({ type: 'session.config.set', threadId: state.settingsTarget, settings }, { kind: 'save-config', threadId: state.settingsTarget, agent: state.agent });
}
function readSession() {
  if (!state.selected || !state.connected) return;
  state.historyLoading = true; state.historyFailed = false; notice('historyNotice');
  state.readRequest = wireRequest({ type: 'session.read', threadId: state.selected, limit: Number($('historyLimit').value) }, { kind: 'read', threadId: state.selected, agent: state.agent });
  renderMessages(); updateControls();
}
function messagesFor(id) {
  const messages = [...(state.cache.get(id)?.messages || [])];
  for (const pending of state.pending.values()) {
    if (pending.threadId !== id) continue;
    if (!messages.some(m => m.clientId === pending.id || (m.role === 'user' && m.text === pending.text && JSON.stringify((m.attachments || []).map(a => a.id)) === JSON.stringify((pending.attachments || []).map(a => a.id))))) messages.push({ role: 'user', text: pending.text, clientId: pending.id, attachments: pending.attachments || [] });
    if (pending.reply) messages.push({ role: 'assistant', text: pending.reply, clientId: `${pending.id}-reply`, streaming: true });
  }
  return messages;
}
function renderMessages(toBottom = false) {
  const root = $('messages'), nearBottom = root.scrollHeight - root.scrollTop - root.clientHeight < 90, previousScroll = root.scrollTop;
  const all = messagesFor(state.selected), query = $('historySearch').value.trim().toLowerCase(), visible = all.filter(m => !query || [m.text, ...(m.attachments || []).map(a => a.name)].join(' ').toLowerCase().includes(query));
  root.replaceChildren(); root.setAttribute('aria-busy', String(state.historyLoading)); $('messageCount').textContent = all.length;
  $('findCount').textContent = query ? `${visible.length} 条匹配` : '';
  const intro = document.createElement('div'); intro.className = 'record-intro';
  intro.textContent = state.historyLoading ? '正在读取最新记录…' : query ? '仅显示匹配的已加载记录' : `最近 ${state.cache.get(state.selected)?.limit || $('historyLimit').value} 条记录 · 不含工具执行日志`;
  root.append(intro);
  if (!visible.length) { const empty = document.createElement('div'); empty.className = 'list-empty'; empty.textContent = state.historyLoading ? '正在加载会话记录…' : query ? '已加载的记录中没有匹配内容' : state.historyFailed ? '暂时无法读取这个会话，请重试刷新' : '这个会话还没有消息，可以从下面开始'; root.append(empty); }
  visible.forEach((message, index) => {
    const role = ['user', 'assistant', 'system'].includes(message.role) ? message.role : 'system';
    const article = document.createElement('article'); article.className = `message ${role}${message.streaming ? ' streaming' : ''}`;
    const avatar = document.createElement('div'); avatar.className = 'avatar'; avatar.innerHTML = role === 'assistant' ? icon(state.agent === 'claude' ? 'claude' : 'code') : role === 'user' ? '你' : '!'; avatar.setAttribute('aria-hidden', 'true');
    const content = document.createElement('div'); content.className = 'message-content';
    const header = document.createElement('div'); header.className = 'message-heading';
    header.innerHTML = `<div class="message-author">${role === 'user' ? '你' : role === 'system' ? '系统' : escapeHtml(state.assistant?.agentLabel || 'Codex')}<span class="message-index">${message.streaming ? '正在生成…' : `#${all.indexOf(message) + 1}`}</span></div>`;
    const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'icon-button copy-message'; copy.setAttribute('aria-label', `复制第 ${index + 1} 条消息`); copy.title = '复制消息'; copy.innerHTML = icon('copy'); copy.addEventListener('click', () => copyText(message.text, '消息已复制')); header.append(copy);
    const body = document.createElement('div'); body.className = 'message-body';
    if (message.streaming) body.textContent = message.text; else body.innerHTML = markdown(message.text);
    content.append(header, body);
    if (message.attachments?.length) content.append(renderAttachments(message.attachments));
    if (message.error) { const error = document.createElement('div'); error.className = 'message-error'; error.textContent = message.error; content.append(error); }
    article.append(avatar, content); root.append(article);
  });
  const pending = currentPending(); $('messageStatus').classList.toggle('error', !!pending?.error);
  $('messageStatus').textContent = pending ? pending.error || ({ sending: '正在确认发送…', queued: '消息已排队', delivered: '消息已送达', running: '助手正在生成回复…', cancelling: '正在停止生成…', uncertain: '连接中断，发送结果待确认。不会自动重发。' }[pending.status] || pending.status) : '';
  if (toBottom || nearBottom) root.scrollTop = root.scrollHeight; else root.scrollTop = previousScroll;
  updateControls();
}
function sendText() {
  updateControls(); if ($('send').disabled) return;
  const text = $('input').value.trim(), id = clientId(), threadId = state.selected, attachments = attachmentComposer.ready();
  state.pending.set(id, { id, text, threadId, attachments, status: 'sending', reply: '' }); $('input').value = ''; state.drafts.delete(threadId);
  wireRequest({ type: 'session.send', clientMessageId: id, threadId, text, attachmentIds: attachments.map(a => a.id) }, { kind: 'send', clientId: id });
  attachmentComposer.clear();
  renderMessages(true);
}
function settleMessage(payload, failed = false) {
  if (settledMessages.has(payload.clientMessageId)) return;
  settledMessages.add(payload.clientMessageId);
  if (settledMessages.size > 200) settledMessages.delete(settledMessages.values().next().value);
  const pending = state.pending.get(payload.clientMessageId);
  const id = payload.threadId || pending?.threadId; if (!id) return;
  const cache = state.cache.get(id) || { messages: [], limit: Number($('historyLimit').value) };
  if (pending && !cache.messages.some(m => m.clientId === pending.id)) cache.messages.push({ role: 'user', text: pending.text, clientId: pending.id, attachments: pending.attachments || [] });
  const reply = typeof payload.reply === 'string' ? payload.reply : pending?.reply;
  if (reply) cache.messages.push({ role: 'assistant', text: reply });
  if (failed && pending) { attachmentComposer.restore(attachmentKey(id), pending.attachments); state.drafts.set(id, pending.text); if (id === state.selected && !$('input').value) $('input').value = pending.text; }
  if (failed) cache.messages.push({ role: 'system', text: payload.status === 'cancelled' ? '已停止生成。' : `发送失败：${payload.error || '未知错误'}` });
  state.cache.set(id, cache); state.pending.delete(payload.clientMessageId);
  for (const [requestId, request] of state.requests) if (request.clientId === payload.clientMessageId) finishRequest(requestId);
  const session = state.sessions.find(s => s.id === id); if (session) { session.status = failed && payload.status !== 'cancelled' ? 'error' : 'idle'; session.updatedAt = Date.now(); if (reply) session.preview = reply.slice(0, 200); }
  if (id === state.selected) { renderConversationHeader(); renderMessages(true); }
  renderSessions(); updateControls();
}
function renderAgentOptions(options) {
  state.options = options; $('agentOptions').replaceChildren(); $('agentSelect').replaceChildren();
  for (const o of options) {
    const enabled = o.configured && o.config?.enabled !== false, available = enabled && o.loaded;
    const label = o.type === 'codex' ? 'Codex' : o.label || o.type;
    const option = document.createElement('option'); option.value = o.type; option.textContent = label + (!available ? ' · 未配置 / 不可用' : ''); option.disabled = !available || (!state.assistant?.adaptorId && o.type !== state.agent); $('agentSelect').append(option);
    const runtime = !enabled ? '未配置' : !o.loaded ? '加载失败' : o.runtime == null ? '待检测' : o.runtime.ok ? '可用' : '不可用';
    const card = document.createElement('div'); card.className = `agent-card ${o.runtime?.ok ? 'ok' : !available || o.runtime?.ok === false ? 'off' : ''}`;
    const title = document.createElement('strong'); title.textContent = `${label} · ${runtime}`; card.append(title);
    const detail = document.createElement('div'); detail.textContent = o.error?.message || o.runtime?.error?.message || o.runtime?.error || (o.config?.mode ? `模式：${o.config.mode}` : o.config?.endpoint ? `Endpoint：${o.config.endpoint}` : available ? '已加载独立会话适配器' : '检查启动参数和配置文件'); card.append(detail); if (o.config?.backend === 'sdk') { const note = document.createElement('div'); note.textContent = 'SDK 已配置；登录/自定义端点是否可用要在发送时确认。V2 使用独立状态目录。'; card.append(note); } $('agentOptions').append(card);
  }
  $('agentSelect').value = state.agent; document.documentElement.dataset.agent = state.agent; $('agentMark').innerHTML = icon(state.agent === 'claude' ? 'claude' : 'code'); updateControls();
}
function setAssistant(payload) {
  state.assistant = { ...state.assistant, ...payload }; state.agent = payload.agentType || state.agent;
  document.documentElement.dataset.agent = state.agent; $('agentMark').innerHTML = icon(state.agent === 'claude' ? 'claude' : 'code'); document.title = `${state.agent === 'claude' ? 'Claude' : 'Codex'} Workspace · V2`; $('agentSelect').value = state.agent;
  $('demoBadge').hidden = state.assistant.provider !== 'fake'; $('agentLabel').textContent = state.assistant.agentLabel || 'Codex';
  $('assistantState').textContent = `受管助手：${state.assistant.status || 'unknown'}${state.assistant.error ? ` · ${state.assistant.error}` : ''}`;
  const capabilities = state.assistant.attachmentCapabilities;
  $('attachmentMode').textContent = capabilities?.imageMode === 'file-reference' ? '此连接以本地文件引用发送图片与文件' : '图片：原生视觉输入 · 文件：本地路径引用';
  $('retryInit').hidden = !['error', 'degraded'].includes(state.assistant.status); updateControls();
}
function handle(message) {
  const p = message.payload || {}, request = message.requestId ? finishRequest(message.requestId) : null;
  if (message.type === 'harness.snapshot') {
    const previousAgent = state.agent; setAssistant(p); if (!state.hasSnapshot || previousAgent !== state.agent) loadTreePreferences(); state.hasSnapshot = true; if (p.agentOptions) renderAgentOptions(p.agentOptions);
    const live = new Set();
    for (const item of p.messagesInFlight || []) { live.add(item.clientMessageId); state.pending.set(item.clientMessageId, { ...item, id: item.clientMessageId }); }
    // Never replay a write after reconnect. The next history read is authoritative.
    for (const [id, item] of state.pending) if (!live.has(id)) { if (item.text) state.drafts.set(item.threadId, item.text); attachmentComposer.restore(attachmentKey(item.threadId), item.attachments); state.pending.delete(id); }
    if (!state.loading) requestSessions();
  } else if (message.type === 'agent.options') { if (p.selected) state.agent = p.selected; renderAgentOptions(p.options || []); }
  else if (message.type === 'agent.selected') {
    saveDraft(); $('sessionSettingsDialog').close(); state.settingsTarget = null; state.catalog = null; state.catalogRequest = null; state.inlinePending = null; state.configurations.clear(); state.creating = null; state.agent = p.agentType; state.project = 'all'; state.selected = null; state.sessions = []; state.cache.clear(); state.drafts.clear(); state.pending.clear(); state.readRequest = null; attachmentComposer.render();
    $('conversation').hidden = true; $('welcome').hidden = false; $('workspace').classList.remove('detail-open'); $('sessionModel').textContent = ''; setAssistant({ ...p, attachmentCapabilities: null }); loadTreePreferences(); renderSessions(); renderComposerSettings(); requestSessions();
  } else if (message.type === 'assistant.session.ready') { setAssistant({ ...p, status: 'ready' }); if (!state.loading) requestSessions(); }
  else if (message.type === 'assistant.session.error') { setAssistant(p); notice('listNotice', `受管助手初始化失败，但仍可尝试读取会话：${p.error || ''}`, true); }
  else if (message.type === 'session.created') {
    const creation = request?.kind === 'create' ? request : state.creating?.requestId === message.requestId ? state.creating : null;
    if (!creation || creation.agent !== state.agent || p.agentType !== state.agent || !p.session?.id) return;
    state.creating = null;
    state.sessions = [...new Map([...state.sessions, p.session].map(session => [session.id, session])).values()];
    state.expanded[p.session.projectKey] = true; saveTreePreferences();
    $('sessionSearch').value = ''; $('statusFilter').value = 'all';
    notice('listNotice'); selectSession(p.session.id); $('input').focus(); toast('已创建空会话，可以开始输入'); updateControls();
  }
  else if (message.type === 'sessions.snapshot') {
    if (message.requestId && (message.requestId !== state.listRequest || !request || request.agent !== state.agent)) return;
    state.loading = false;
    if (p.ok !== false && state.creating?.uncertain) state.creating = null;
    if (p.ok === false) { notice('listNotice', p.error?.message || p.error || '读取列表失败，保留已有内容', true); renderSessions(); updateControls(); return; }
    const rows = (p.sessions || []).filter(s => s.id);
    state.sessions = request?.append ? [...new Map([...state.sessions, ...rows].map(s => [s.id, s])).values()] : rows;
    state.nextCursor = p.nextCursor || null;
    if (p.ok === false) notice('listNotice', p.error?.message || p.error || '读取列表失败', true); else notice('listNotice');
    $('syncTime').textContent = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
    renderSessions();
    if (!state.selected) {
      const saved = storage.get(`${state.agent}.selected`), preferred = state.sessions.find(s => s.id === saved || s.nativeId === saved) || visibleSessions(state.sessions).find(s => !s.managed) || state.sessions[0];
      if (preferred) selectSession(preferred.id, { navigate: false });
    } else if (currentSession()) { renderConversationHeader(); if (!currentPending() && !state.historyLoading) readSession(); }
    else { saveDraft(); state.selected = null; $('conversation').hidden = true; $('welcome').hidden = false; $('workspace').classList.remove('detail-open'); }
    updateControls();
  } else if (message.type === 'session.read') {
    if (p.threadId !== state.selected || (message.requestId && (message.requestId !== state.readRequest || request?.agent !== state.agent))) return;
    state.historyLoading = false;
    if (p.ok === false) { state.historyFailed = true; notice('historyNotice', p.error?.message || p.error || '无法读取记录', true); }
    else { state.historyFailed = false; if (p.configuration) state.configurations.set(p.threadId, p.configuration); renderConversationHeader(); renderComposerSettings(); state.cache.set(p.threadId, { messages: p.messages || [], limit: p.limit || Number($('historyLimit').value) }); notice('historyNotice'); }
    renderMessages(true);
  } else if (message.type === 'models.snapshot') {
    if (p.agentType !== state.agent || request?.agent !== state.agent) return;
    state.catalogRequest = null; state.catalog = p; renderComposerSettings(); if ($('sessionSettingsDialog').open) renderSessionSettings({ fill: false });
  } else if (message.type === 'session.config') {
    if (request?.agent !== state.agent) return;
    state.configurations.set(p.threadId, p.configuration);
    if (request?.kind === 'inline-config' && state.inlinePending?.requestId === message.requestId) { state.inlinePending = null; if (p.threadId === state.selected) notice('composerConfigStatus', '已保存 · 下一次发送时生效'); }
    if (p.threadId === state.selected) { renderConversationHeader(); renderComposerSettings(); updateControls(); }
    if (p.threadId === state.settingsTarget) { state.settingsLoading = false; state.savingSettings = false; renderSessionSettings(); if (p.saved) notice('sessionConfigNotice', '已保存。下一次通过 V2 发送消息时应用；不会立即修改历史或 Desktop。'); }
    updateControls();
  } else if (message.type === 'chat.status') {
    if (settledMessages.has(p.clientMessageId)) return;
    const pending = state.pending.get(p.clientMessageId) || { id: p.clientMessageId, threadId: p.threadId, text: p.text || '', attachments: p.attachments || [], reply: '' };
    if (!pending.threadId) return;
    Object.assign(pending, p); state.pending.set(p.clientMessageId, pending);
    const session = state.sessions.find(s => s.id === pending.threadId); if (session) session.status = 'running';
    renderSessions(); if (pending.threadId === state.selected) { renderConversationHeader(); renderMessages(); } updateControls();
  } else if (message.type === 'chat.delta') {
    const pending = state.pending.get(p.clientMessageId); if (!pending || (p.threadId && p.threadId !== pending.threadId)) return;
    pending.reply += p.text || ''; if (pending.threadId === state.selected) renderMessages();
  } else if (message.type === 'chat.completed') settleMessage(p);
  else if (message.type === 'chat.error') settleMessage(p, true);
  else if (message.type === 'command.error') {
    if (p.command === 'session.create' && state.creating?.requestId === message.requestId) { state.creating = null; notice('listNotice', `新建会话失败：${p.error || '请重试'}`, true); renderSessions(); }
    else if (['session.send', 'chat.send'].includes(p.command) && p.clientMessageId) { settleMessage(p, true); toast('消息未成功发送，请检查记录和连接状态'); }
    else if (p.command === 'sessions.list' && (!message.requestId || message.requestId === state.listRequest)) { state.loading = false; notice('listNotice', `读取失败，保留已有列表：${p.error}`, true); renderSessions(); }
    else if (p.command === 'session.read' && (!message.requestId || message.requestId === state.readRequest)) { state.historyLoading = false; state.historyFailed = true; notice('historyNotice', `读取失败，保留已有记录：${p.error}`, true); renderMessages(); }
    else if (p.command?.startsWith('session.config.') && request?.kind === 'inline-config' && request?.agent === state.agent) { if (state.inlinePending?.requestId === message.requestId) state.inlinePending = null; if (request.threadId === state.selected) { renderComposerSettings(); notice('composerConfigStatus', (p.error || '设置保存失败') + '；已恢复原设置。', true); } }
    else if (p.command?.startsWith('session.config.') && request?.agent === state.agent && request?.threadId === state.settingsTarget) { state.settingsLoading = false; state.savingSettings = false; notice('sessionConfigNotice', p.error || '设置操作失败', true); }
    else toast(p.error || '操作失败'); $('agentSelect').value = state.agent; updateControls();
  }
}
function connect() {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws'; const ws = new WebSocket(`${protocol}://${location.host}/ws`); state.ws = ws;
  ws.onopen = () => { state.connected = true; state.retry = 0; setConnection('已连接', 'ready'); state.hasSnapshot = false; updateControls(); };
  ws.onmessage = event => { try { handle(JSON.parse(event.data)); } catch (error) { console.error('处理服务事件失败', error); toast('处理服务数据失败，请刷新页面'); } };
  ws.onclose = () => {
    if (state.ws !== ws) return; state.settingsLoading = false; state.savingSettings = false; state.inlinePending = null; state.catalogRequest = null; if (state.creating) state.creating.uncertain = true; state.connected = false; state.loading = false; state.historyLoading = false;
    for (const request of state.requests.values()) clearTimeout(request.timeout); state.requests.clear();
    for (const pending of state.pending.values()) { pending.status = 'uncertain'; pending.error = '连接中断，发送结果待确认。不会自动重发。'; }
    setConnection('重连中', 'error'); notice('listNotice', '连接中断，保留已有内容，正在自动重连。', true); renderMessages(); renderSessions(); updateControls();
    setTimeout(connect, Math.min(1000 * 2 ** state.retry++, 10000));
  };
  ws.onerror = () => setConnection('连接异常', 'error');
}
async function copyText(text, confirmation) { try { await copyPlainText(text); toast(confirmation); } catch { toast('复制不可用，请在记录中手动选择复制'); } }
function exportHistory() {
  const session = currentSession(), cache = state.cache.get(state.selected); if (!session || !cache) return;
  const text = `# ${session.name || '未命名会话'}\n\n项目：${session.projectLabel || session.project || '未指定项目'}\n会话 ID：${session.id}\n导出时间：${new Date().toLocaleString('zh-CN')}\n范围：当前已加载的最近 ${cache.limit} 条记录（非完整历史）\n\n` + messagesFor(state.selected).map(m => `## ${m.role === 'user' ? '用户' : m.role === 'assistant' ? '助手' : '系统'}\n\n${m.text}${(m.attachments || []).length ? '\n\n附件：' + m.attachments.map(a => a.name).join('、') : ''}`).join('\n\n---\n\n');
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' })), a = document.createElement('a');
  a.href = url; a.download = `${String(session.name || 'session').replace(/[\\/:*?"<>|]/g, '-').slice(0, 70)}.md`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); toast('已导出当前加载的记录');
}
function closeProjects() { $('workspace').classList.remove('projects-open'); $('sidebarScrim').hidden = true; syncSidebarAccessibility(); }
function toggleFind(open) { $('findBar').hidden = !open; if (open) $('historySearch').focus(); else { $('historySearch').value = ''; renderMessages(); } }
$('refresh').addEventListener('click', () => requestSessions()); $('loadMore').addEventListener('click', () => requestSessions(true));
$('expandAll').addEventListener('click', () => toggleAllProjects(true)); $('collapseAll').addEventListener('click', () => toggleAllProjects(false));
$('sessionSearch').addEventListener('input', renderSessions); $('statusFilter').addEventListener('change', renderSessions); $('sortOrder').addEventListener('change', renderSessions);
$('refreshHistory').addEventListener('click', readSession); $('historyLimit').addEventListener('change', readSession);
$('jumpLatest').addEventListener('click', () => { $('messages').scrollTop = $('messages').scrollHeight; });
$('toggleFind').addEventListener('click', () => toggleFind($('findBar').hidden)); $('closeFind').addEventListener('click', () => toggleFind(false)); $('historySearch').addEventListener('input', () => renderMessages(true));
$('input').addEventListener('input', () => { saveDraft(); $('input').style.height = 'auto'; $('input').style.height = `${Math.min(150, Math.max(50, $('input').scrollHeight))}px`; updateControls(); });
$('input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); sendText(); } });
$('send').addEventListener('click', sendText); $('cancel').addEventListener('click', () => { const p = currentPending(); if (p) { p.status = 'cancelling'; sendCommand({ type: 'chat.cancel', clientMessageId: p.id }); renderMessages(); } });
$('openSessionSettings').addEventListener('click', openSessionSettings);
$('closeSessionSettings').addEventListener('click', () => $('sessionSettingsDialog').close());
$('sessionModelInput').addEventListener('input', renderEfforts);
$('composerModel').addEventListener('change', () => saveComposerSettings(true));
$('composerEffort').addEventListener('change', () => saveComposerSettings(false));
$('saveSessionSettings').addEventListener('click', () => saveSessionSettings());
$('resetSessionSettings').addEventListener('click', () => saveSessionSettings(true));
$('copyId').addEventListener('click', () => copyText(state.selected, '会话 ID 已复制')); $('export').addEventListener('click', exportHistory);
$('openSettings').addEventListener('click', () => { $('settingsDialog').showModal(); sendCommand({ type: 'agent.status' }); }); $('closeSettings').addEventListener('click', () => $('settingsDialog').close());
$('refreshAgents').addEventListener('click', () => sendCommand({ type: 'agent.status' })); $('retryInit').addEventListener('click', () => sendCommand({ type: 'harness.retry-init' }));
$('agentSelect').addEventListener('change', () => { const agentType = $('agentSelect').value; if (agentType !== state.agent) { $('agentSelect').disabled = true; sendCommand({ type: 'agent.select', agentType }); } });
$('showProjects').addEventListener('click', openProjects); $('closeProjects').addEventListener('click', closeProjects); $('sidebarScrim').addEventListener('click', closeProjects);
$('backToList').addEventListener('click', openProjects);
addEventListener('keydown', event => {
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName) || document.activeElement?.isContentEditable;
  if (event.key === '/' && !typing && !$('settingsDialog').open && !$('sessionSettingsDialog').open && !$('imageDialog').open) { event.preventDefault(); if (matchMedia('(max-width: 900px)').matches) openProjects(); $('sessionSearch').focus(); }
  if (event.key === 'Escape') { closeProjects(); if (!$('findBar').hidden) toggleFind(false); }
});
addEventListener('resize', syncSidebarAccessibility);
syncSidebarAccessibility(); renderSessions(); updateControls(); connect();
