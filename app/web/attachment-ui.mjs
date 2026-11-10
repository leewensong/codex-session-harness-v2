import { clientId } from './browser-compat.mjs';

export const MAX_FILES = 8;
export const MAX_FILE_BYTES = 20 * 1024 * 1024;
export const MAX_TOTAL_BYTES = 64 * 1024 * 1024;
export function formatBytes(size) { return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.ceil(size / 1024))} KB`; }
export function localAttachmentUrl(value, action) { return typeof value === 'string' && new RegExp(`^/api/attachments/[a-f0-9]{32}/${action}$`).test(value) ? value : null; }
function externalUrl(value) { try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null; } catch { return null; } }

function openImage(src, name) {
  const dialog = document.getElementById('imageDialog');
  const image = document.getElementById('imageDialogImage');
  image.src = src; image.alt = name || '图片'; image.referrerPolicy = 'no-referrer';
  document.getElementById('imageDialogName').textContent = name || '图片';
  dialog.showModal();
}

export function renderAttachments(items, { compact = false } = {}) {
  const root = document.createElement('div'); root.className = compact ? 'attachment-gallery compact' : 'attachment-gallery';
  for (const attachment of items || []) {
    const card = document.createElement('div'); card.className = `attachment-card ${attachment.kind === 'image' ? 'is-image' : 'is-file'}`;
    const name = String(attachment.name || '附件');
    const preview = localAttachmentUrl(attachment.previewUrl, 'preview');
    const download = localAttachmentUrl(attachment.url, 'download');
    const remote = externalUrl(attachment.externalUrl);
    if (preview) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'attachment-image'; button.setAttribute('aria-label', `预览图片 ${name}`);
      const image = document.createElement('img'); image.src = preview; image.alt = name; image.loading = 'lazy'; image.decoding = 'async';
      image.addEventListener('error', () => { button.disabled = true; button.replaceChildren(document.createTextNode('图片已失效')); }, { once: true });
      button.append(image); button.addEventListener('click', () => openImage(preview, name)); card.append(button);
    } else if (remote) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'external-image';
      button.textContent = '点击加载外部图片'; button.title = `加载后浏览器会连接 ${new URL(remote).host}`;
      button.addEventListener('click', () => {
        const image = document.createElement('img'); image.src = remote; image.alt = name; image.referrerPolicy = 'no-referrer';
        image.addEventListener('error', () => { button.textContent = '外部图片无法加载'; button.disabled = true; }, { once: true });
        button.replaceChildren(image); button.className = 'attachment-image'; button.onclick = () => openImage(remote, name);
      }, { once: true }); card.append(button);
    } else if (attachment.kind === 'image') {
      const placeholder = document.createElement('div'); placeholder.className = 'unavailable-image'; placeholder.textContent = '图片不可用 · 文件可能已移动'; card.append(placeholder);
    } else {
      const badge = document.createElement('span'); badge.className = 'file-type'; badge.textContent = name.includes('.') ? name.split('.').pop().slice(0, 6).toUpperCase() : 'FILE'; card.append(badge);
    }
    const info = document.createElement('div'); info.className = 'attachment-info';
    const label = document.createElement('span'); label.textContent = name; label.title = name; info.append(label);
    const meta = document.createElement('small'); meta.textContent = attachment.size ? formatBytes(attachment.size) : remote ? new URL(remote).host : '历史图片'; info.append(meta); card.append(info);
    if (download) { const link = document.createElement('a'); link.href = download; link.download = name; link.className = 'attachment-download'; link.textContent = '下载'; link.setAttribute('aria-label', `下载 ${name}`); card.append(link); }
    root.append(card);
  }
  return root;
}

export class AttachmentComposer {
  constructor({ context, changed, notify }) {
    this.context = context; this.changed = changed; this.notify = notify; this.drafts = new Map();
    this.picker = document.getElementById('attachmentPicker'); this.tray = document.getElementById('attachmentTray'); this.area = document.getElementById('composerDropzone');
    document.getElementById('attachFiles').addEventListener('click', () => this.picker.click());
    this.picker.addEventListener('change', () => { this.addFiles(this.picker.files); this.picker.value = ''; });
    this.area.addEventListener('paste', event => {
      const files = [...(event.clipboardData?.items || [])].filter(i => i.kind === 'file').map(i => i.getAsFile()).filter(Boolean);
      if (files.length) { event.preventDefault(); this.addFiles(files); }
    });
    let depth = 0;
    this.area.addEventListener('dragenter', event => { if ([...(event.dataTransfer?.types || [])].includes('Files')) { event.preventDefault(); depth++; this.area.classList.add('drag-over'); } });
    this.area.addEventListener('dragover', event => { if ([...(event.dataTransfer?.types || [])].includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; } });
    this.area.addEventListener('dragleave', () => { if (--depth <= 0) this.area.classList.remove('drag-over'); });
    this.area.addEventListener('drop', event => { depth = 0; this.area.classList.remove('drag-over'); if (event.dataTransfer?.files.length) { event.preventDefault(); this.addFiles(event.dataTransfer.files); } });
    // Prevent accidentally navigating away when a user drops a file outside the composer.
    document.addEventListener('dragover', event => { if ([...(event.dataTransfer?.types || [])].includes('Files')) event.preventDefault(); });
    document.addEventListener('drop', event => { if (event.dataTransfer?.files.length) event.preventDefault(); });
    document.getElementById('closeImageDialog').addEventListener('click', () => document.getElementById('imageDialog').close());
    document.getElementById('imageDialog').addEventListener('close', () => document.getElementById('imageDialogImage').removeAttribute('src'));
  }
  rows(key = this.context().key) { return this.drafts.get(key) || []; }
  ready() { return this.rows().filter(r => r.status === 'ready').map(r => r.attachment); }
  blocked() { return this.rows().some(r => r.status !== 'ready'); }
  clear(key = this.context().key) { for (const r of this.rows(key)) r.xhr?.abort(); this.drafts.delete(key); this.render(); }
  restore(key, attachments) {
    const rows = this.rows(key); const ids = new Set(rows.map(r => r.attachment?.id));
    for (const a of attachments || []) if (!ids.has(a.id)) rows.push({ id: clientId(), status: 'ready', attachment: a, name: a.name, size: a.size });
    this.drafts.set(key, rows); this.render();
  }
  addFiles(files) {
    const context = this.context();
    if (!context.enabled || !context.threadId) { this.notify('请先连接并选择可发送的会话'); return; }
    const rows = this.rows(context.key); const incoming = [...files];
    if (rows.length + incoming.length > MAX_FILES) { this.notify('每条消息最多添加 8 个附件'); return; }
    if (incoming.some(f => f.size > MAX_FILE_BYTES || !f.size)) { this.notify('不能上传空文件；单个文件最大 20 MB'); return; }
    if ([...rows, ...incoming].reduce((sum, f) => sum + f.size, 0) > MAX_TOTAL_BYTES) { this.notify('附件总大小最大 64 MB'); return; }
    for (const file of incoming) rows.push({ id: clientId(), file, name: file.name, size: file.size, status: 'waiting', progress: 0 });
    this.drafts.set(context.key, rows); this.render(); this.changed();
    for (const row of rows.filter(r => r.status === 'waiting')) this.upload(row, context);
  }
  upload(row, context) {
    row.status = 'uploading'; row.error = ''; row.progress = 0;
    const form = new FormData(); form.append('files', row.file, row.name);
    const xhr = new XMLHttpRequest(); row.xhr = xhr;
    xhr.open('POST', `/api/attachments?threadId=${encodeURIComponent(context.threadId)}`);
    xhr.setRequestHeader('X-Harness-Upload', '1'); xhr.timeout = 100000;
    const fail = text => { row.status = 'error'; row.error = text; this.render(); this.changed(); };
    xhr.upload.onprogress = event => { if (event.lengthComputable) { row.progress = Math.round(event.loaded / event.total * 100); this.render(); } };
    xhr.onload = () => {
      let result; try { result = JSON.parse(xhr.responseText); } catch { fail('服务未返回附件结果，请重试'); return; }
      if (xhr.status !== 201 || !result.attachments?.[0]) { fail(result.error || '上传失败，请重试'); return; }
      row.attachment = result.attachments[0]; row.status = 'ready'; row.xhr = null; this.render(); this.changed();
    };
    xhr.onerror = () => fail('网络错误，请重试'); xhr.ontimeout = () => fail('上传超时，请重试');
    xhr.onabort = () => { row.status = 'error'; row.error = '已取消上传'; };
    xhr.send(form); this.render(); this.changed();
  }
  render() {
    const context = this.context(), rows = this.rows(); this.tray.replaceChildren(); this.tray.hidden = !rows.length;
    for (const row of rows) {
      const card = document.createElement('div'); card.className = `attachment-draft ${row.status}`;
      const url = localAttachmentUrl(row.attachment?.previewUrl, 'preview');
      if (url) { const image = document.createElement('img'); image.src = url; image.alt = row.name; card.append(image); }
      else { const badge = document.createElement('span'); badge.className = 'draft-file-icon'; badge.textContent = '↥'; card.append(badge); }
      const info = document.createElement('div'); info.className = 'draft-info';
      const name = document.createElement('strong'); name.textContent = row.name; name.title = row.name;
      const status = document.createElement('small'); status.textContent = row.status === 'ready' ? `${formatBytes(row.size)} · 待发送` : row.status === 'error' ? row.error : `上传中 ${row.progress}%`;
      info.append(name, status); card.append(info);
      if (row.status === 'error' && row.file) { const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'text-button'; retry.textContent = '重试'; retry.addEventListener('click', () => this.upload(row, context)); card.append(retry); }
      const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'icon-button'; remove.textContent = '×'; remove.setAttribute('aria-label', `移除附件 ${row.name}`);
      remove.addEventListener('click', () => { row.xhr?.abort(); this.drafts.set(context.key, rows.filter(r => r !== row)); this.render(); this.changed(); }); card.append(remove); this.tray.append(card);
    }
    document.getElementById('attachmentHint').textContent = rows.length ? `${rows.length} / 8 个附件 · 上传不等于发送` : '拖拽文件或粘贴截图 · 单文件 ≤ 20 MB';
  }
}
