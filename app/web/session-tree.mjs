import { projectKey, projectsFor, visibleSessions, timestamp } from './session-utils.mjs';

/** Two-level project/session data with shared search and stable project IDs. */
export function sessionTree(sessions, { query = '', status = 'all', sort = 'recent' } = {}) {
  const visible = visibleSessions(sessions, { query, status, sort });
  const byProject = new Map();
  for (const session of visible) {
    const key = projectKey(session);
    if (!byProject.has(key)) byProject.set(key, []);
    byProject.get(key).push(session);
  }
  return projectsFor(sessions).filter(project => byProject.has(project.key)).map(project => ({
    ...project, sessions: byProject.get(project.key),
    latest: Math.max(0, ...byProject.get(project.key).map(s => timestamp(s.updatedAt || s.createdAt))),
  })).sort((a, b) => sort === 'name' ? a.displayLabel.localeCompare(b.displayLabel, 'zh-CN') : b.latest - a.latest || a.displayLabel.localeCompare(b.displayLabel, 'zh-CN'));
}

export function isProjectExpanded(project, expanded, { query = '', status = 'all' } = {}) {
  return Boolean(query.trim() || status !== 'all') || expanded[project.key] !== false;
}

export function projectRows(project, selected, limit = 5) {
  const rows = project.sessions.slice(0, limit);
  const selection = project.sessions.find(s => s.id === selected);
  if (selection && !rows.includes(selection)) rows.push(selection);
  return rows;
}

/** Pick a server-validated project reference, never a client-supplied path. */
export function creationReference(project) {
  return project.sessions.find(session => session.canCreateSession === true) || null;
}
