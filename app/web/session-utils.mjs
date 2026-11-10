export function timestamp(value) {
  if (value == null || value === '') return 0;
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return numeric < 1e12 ? numeric * 1000 : numeric;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function statusInfo(value) {
  const raw = typeof value === 'object' && value ? value.type || value.status : value;
  const status = String(raw || 'unknown').toLowerCase();
  if (['running', 'inprogress', 'in_progress', 'starting', 'queued', 'delivered', 'busy'].includes(status)) return { key: 'running', label: '进行中' };
  if (['error', 'failed', 'degraded'].includes(status)) return { key: 'error', label: '异常' };
  if (['idle', 'completed', 'success', 'ready', 'notloaded', 'not_loaded'].includes(status)) return { key: 'idle', label: '空闲' };
  if (['cancelled', 'canceled', 'interrupted'].includes(status)) return { key: 'idle', label: '已停止' };
  return { key: 'unknown', label: '未知' };
}

export function projectKey(session) {
  return String(session.projectKey || session.cwd || session.projectLabel || session.project || 'unassigned');
}

export function projectsFor(sessions) {
  const groups = new Map();
  for (const session of sessions) {
    const key = projectKey(session);
    if (!groups.has(key)) groups.set(key, { key, label: session.projectLabel || session.project || '未指定项目', count: 0 });
    groups.get(key).count++;
  }
  const projects = [...groups.values()].sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, 'zh-CN') || a.key.localeCompare(b.key));
  const totals = new Map();
  projects.forEach(p => totals.set(p.label, (totals.get(p.label) || 0) + 1));
  const ordinals = new Map();
  return projects.map(p => {
    const ordinal = (ordinals.get(p.label) || 0) + 1;
    ordinals.set(p.label, ordinal);
    return { ...p, displayLabel: p.label + (totals.get(p.label) > 1 ? ` · ${ordinal}` : '') };
  });
}

export function visibleSessions(sessions, { project = 'all', query = '', status = 'all', sort = 'recent' } = {}) {
  const needle = query.trim().toLocaleLowerCase();
  return sessions.filter(s => (project === 'all' || projectKey(s) === project)
    && (status === 'all' || statusInfo(s.status).key === status)
    && (!needle || [s.name, s.preview, s.project, s.projectLabel, s.id].join(' ').toLocaleLowerCase().includes(needle)))
    .sort((a, b) => sort === 'name' ? String(a.name || '').localeCompare(String(b.name || ''), 'zh-CN') : timestamp(b.updatedAt || b.createdAt) - timestamp(a.updatedAt || a.createdAt));
}

export function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function inline(text) {
  let result = '', end = 0;
  const pattern = /`([^`\n]+)`|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\(([^\s)]+)\)/g;
  for (const match of text.matchAll(pattern)) {
    result += escapeHtml(text.slice(end, match.index));
    if (match[1]) result += `<code>${escapeHtml(match[1])}</code>`;
    else if (match[2]) result += `<strong>${escapeHtml(match[2])}</strong>`;
    else {
      let url;
      try { url = new URL(match[4]); } catch { /* only absolute, safe web links */ }
      result += url && ['https:', 'http:'].includes(url.protocol)
        ? `<a href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(match[3])}</a>`
        : escapeHtml(match[0]);
    }
    end = match.index + match[0].length;
  }
  return result + escapeHtml(text.slice(end));
}

// Deliberately small Markdown subset. Raw HTML, images and executable URLs are never rendered.
export function markdown(text) {
  const lines = String(text ?? '').replace(/\r\n/g, '\n').split('\n');
  let output = '', paragraph = [], list = null;
  const flush = () => { if (paragraph.length) { output += `<p>${paragraph.map(inline).join('<br>')}</p>`; paragraph = []; } if (list) { output += `</${list}>`; list = null; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush(); const language = line.replace(/^\s*```/, '').trim(); const code = [];
      while (++i < lines.length && !/^\s*```\s*$/.test(lines[i])) code.push(lines[i]);
      output += `<div class="code-block"><div class="code-label">${escapeHtml(language || 'code')}</div><pre><code>${escapeHtml(code.join('\n'))}</code></pre></div>`;
    } else if (/^#{1,4}\s+/.test(line)) {
      flush(); output += `<h3>${inline(line.replace(/^#{1,4}\s+/, ''))}</h3>`;
    } else if (/^\s*(?:[-*]|\d+\.)\s+/.test(line)) {
      if (paragraph.length) { output += `<p>${paragraph.map(inline).join('<br>')}</p>`; paragraph = []; }
      const type = /^\s*\d+\./.test(line) ? 'ol' : 'ul';
      if (list !== type) { if (list) output += `</${list}>`; output += `<${type}>`; list = type; }
      output += `<li>${inline(line.replace(/^\s*(?:[-*]|\d+\.)\s+/, ''))}</li>`;
    } else if (!line.trim()) flush();
    else { if (list) { output += `</${list}>`; list = null; } paragraph.push(line); }
  }
  flush(); return output;
}
