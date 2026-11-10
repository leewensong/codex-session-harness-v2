"""Bounded local attachments. HTTP accepts opaque IDs, never client file paths."""
from __future__ import annotations

import base64
import hashlib
import json
import ntpath
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ATTACHMENTS = 8
_ID = re.compile(r"^[a-f0-9]{32}$")
_IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
_MANIFEST = re.compile(r'(?:\n\n|^)<harness_attachments_v1>\n(.*?)\n</harness_attachments_v1>', re.DOTALL)


def image_mime(data: bytes) -> str | None:
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def safe_name(value: str) -> str:
    name = ntpath.basename(str(value).replace('\x00', ''))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', name).strip(' .')
    if not name:
        name = 'attachment'
    if name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        name = '_' + name
    suffix = Path(name).suffix[:16]
    return name if len(name) <= 160 else name[:140] + suffix


@dataclass(frozen=True)
class Attachment:
    id: str
    name: str
    size: int
    mime: str
    kind: str
    path: Path
    scope: str
    thread_id: str

    def public(self) -> dict[str, Any]:
        value = {'id': self.id, 'name': self.name, 'size': self.size, 'mime': self.mime,
                 'kind': self.kind, 'url': f'/api/attachments/{self.id}/download'}
        if self.kind == 'image':
            value['previewUrl'] = f'/api/attachments/{self.id}/preview'
        return value


def attachment_prompt(text: str, attachments: list[Attachment]) -> str:
    if not attachments:
        return text
    rows = [{'id': a.id, 'name': a.name, 'path': str(a.path), 'kind': a.kind} for a in attachments]
    # JSON escapes newlines and quotes in filenames; file contents are never interpolated as instructions.
    return text + '\n\n<harness_attachments_v1>\n' + json.dumps(rows, ensure_ascii=False) + '\n</harness_attachments_v1>'


def message_images(content: Any) -> list[dict[str, str]]:
    """Preserve structured Codex image parts, including image-only user messages."""
    result = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get('type') in {'input_image', 'image'}:
            value = part.get('image_url') or part.get('url')
            if isinstance(value, dict):
                value = value.get('url')
            if isinstance(value, str) and value:
                result.append({'url': value})
        elif part.get('type') in {'localImage', 'local_image'} and isinstance(part.get('path'), str):
            result.append({'path': part['path']})
    return result


class AttachmentStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._history: dict[str, dict] = {}

    def save(self, data: bytes, name: str, *, scope: str, thread_id: str, require_image: bool = False) -> Attachment:
        if not data:
            raise ValueError('不能上传空文件')
        if len(data) > MAX_FILE_BYTES:
            raise ValueError('单个文件不能超过 20 MB')
        name = safe_name(name)
        mime = image_mime(data)
        if (require_image or Path(name).suffix.lower() in _IMAGE_SUFFIXES) and not mime:
            raise ValueError('图片内容无效；支持 PNG、JPEG、GIF 和 WebP')
        # SVG/HTML/scripts are downloadable files, never inline executable content.
        kind = 'image' if mime else 'file'
        aid = uuid.uuid4().hex
        folder = self.root / aid
        folder.mkdir(parents=True)
        path = folder / ("upload-" + name)
        try:
            path.write_bytes(data)
            metadata = {'id': aid, 'name': name, 'size': len(data), 'mime': mime or 'application/octet-stream',
                        'kind': kind, 'scope': scope, 'threadId': thread_id}
            (folder / 'metadata.json').write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        except BaseException:
            path.unlink(missing_ok=True)
            (folder / 'metadata.json').unlink(missing_ok=True)
            folder.rmdir()
            raise
        return Attachment(aid, name, len(data), metadata['mime'], kind, path, scope, thread_id)

    def get(self, aid: str, *, scope: str | None = None, thread_id: str | None = None) -> Attachment:
        if not isinstance(aid, str) or not _ID.fullmatch(aid):
            raise ValueError('无效的附件 ID')
        folder = (self.root / aid).resolve()
        if folder.parent != self.root:
            raise ValueError('无效的附件路径')
        try:
            row = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))
            name = safe_name(row['name'])
            path = (folder / ("upload-" + name)).resolve()
            if path.parent != folder or not path.is_file():
                raise ValueError('附件文件不存在')
        except (OSError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError('附件不存在或已失效，请重新上传') from exc
        if (scope is not None and row['scope'] != scope) or (thread_id is not None and row['threadId'] != thread_id):
            raise ValueError('附件属于另一个会话或 Agent，请在目标会话重新上传')
        return Attachment(aid, name, path.stat().st_size, row['mime'], row['kind'], path, row['scope'], row['threadId'])

    def resolve(self, ids: Any, *, scope: str, thread_id: str) -> list[Attachment]:
        if not isinstance(ids, list) or len(ids) > MAX_ATTACHMENTS or any(not isinstance(i, str) for i in ids):
            raise ValueError('每条消息最多添加 8 个附件')
        if len(set(ids)) != len(ids):
            raise ValueError('附件不能重复')
        items = [self.get(i, scope=scope, thread_id=thread_id) for i in ids]
        if sum(a.size for a in items) > MAX_TOTAL_BYTES:
            raise ValueError('附件总大小不能超过 64 MB')
        return items

    def discard(self, aid: str) -> None:
        """Rollback only an upload created by the current failed HTTP request."""
        item = self.get(aid)
        item.path.unlink(missing_ok=True)
        (item.path.parent / 'metadata.json').unlink(missing_ok=True)
        item.path.parent.rmdir()

    def _image(self, ref: dict, *, scope: str, thread_id: str, cwd: str | None, trusted: bool) -> dict:
        value = ref.get('path') or ref.get('url') or ''
        name = ref.get('name') or '历史图片'
        if not isinstance(value, str) or not value:
            return {'kind': 'image', 'name': name, 'unavailable': True}
        if value.startswith(('https://', 'http://')):
            # No server-side URL fetching and no automatic third-party requests from the browser.
            return {'kind': 'image', 'name': name, 'externalUrl': value}
        key = hashlib.sha256((scope + '\0' + thread_id + '\0' + value).encode()).hexdigest()
        if key in self._history:
            return self._history[key]
        try:
            if value.startswith('data:'):
                header, encoded = value.split(',', 1)
                if header not in {'data:image/png;base64', 'data:image/jpeg;base64', 'data:image/gif;base64', 'data:image/webp;base64'} or len(encoded) > MAX_FILE_BYTES * 4 // 3 + 8:
                    raise ValueError('unsupported data image')
                data = base64.b64decode(encoded, validate=True)
                name = 'image.' + header.split('/')[1].split(';')[0]
                stable = hashlib.sha256(scope.encode() + thread_id.encode() + data).hexdigest()
            else:
                # Structured input images are explicit file references. Assistant Markdown is
                # restricted to the session workspace (after resolving symlinks).
                if re.match(r'^[a-z]+:', value, re.I) and not re.match(r'^[a-z]:[\\/]', value, re.I):
                    raise ValueError('unsupported URL')
                path = Path(value)
                if not path.is_absolute():
                    if not cwd:
                        raise ValueError('no workspace')
                    path = Path(cwd) / path
                path = path.resolve()
                if not trusted and (not cwd or not path.is_relative_to(Path(cwd).resolve())):
                    raise ValueError('outside workspace')
                size = path.stat().st_size
                if not 0 < size <= MAX_FILE_BYTES:
                    raise ValueError('invalid image size')
                data = path.read_bytes()
                name = path.name
                stable = hashlib.sha256((scope + thread_id + str(path) + str(path.stat().st_mtime_ns)).encode()).hexdigest()
            if stable in self._history:
                return self._history[stable]
            item = self.save(data, name, scope=scope, thread_id=thread_id, require_image=True)
            value = item.public()
            self._history[key] = self._history[stable] = value
            if len(self._history) > 512:
                self._history.pop(next(iter(self._history)))
            return value
        except (OSError, ValueError):
            return {'kind': 'image', 'name': str(name), 'unavailable': True}

    def decorate(self, result: dict, *, scope: str, thread_id: str) -> dict:
        """Resolve only images actually referenced by these messages, not arbitrary HTTP paths."""
        messages = []
        for original in result.get('messages') or []:
            message = dict(original)
            text = str(message.get('text') or '')
            attachments = []
            native_images = list(message.pop('images', []) or [])
            # Reconstruct our attachment cards from the persisted, exact server-generated manifest.
            for match in list(_MANIFEST.finditer(text)):
                try:
                    rows = json.loads(match[1])
                    if not isinstance(rows, list) or not rows or len(rows) > MAX_ATTACHMENTS:
                        continue
                    items = self.resolve([r['id'] for r in rows], scope=scope, thread_id=thread_id)
                    if match[0].lstrip('\n') != attachment_prompt('', items).lstrip('\n'):
                        continue
                    attachments.extend(a.public() for a in items)
                    text = text.replace(match[0], '')
                    # Native image parts following this manifest duplicate its image cards.
                    native_images = []
                except (ValueError, KeyError, TypeError):
                    continue
            for item in message.get('attachments') or []:
                try:
                    attachments.append(self.get(item['id'], scope=scope, thread_id=thread_id).public())
                except (ValueError, KeyError, TypeError):
                    pass
            for ref in native_images[:MAX_ATTACHMENTS]:
                if isinstance(ref, dict):
                    attachments.append(self._image(ref, scope=scope, thread_id=thread_id, cwd=result.get('cwd'), trusted=message.get('role') == 'user'))
            # Markdown images get an explicit attachment card; raw HTML stays escaped by the UI.
            def markdown_image(match):
                if len(attachments) >= MAX_ATTACHMENTS:
                    return match[0]
                alt, target = match[1], match[2].strip().strip('<>')
                attachments.append(self._image({'url': target, 'name': alt or '图片'}, scope=scope, thread_id=thread_id, cwd=result.get('cwd'), trusted=False))
                return ''
            # Do not interpret image syntax inside fenced code blocks.
            parts = re.split(r'(```.*?```)', text, flags=re.DOTALL)
            for index in range(0, len(parts), 2):
                parts[index] = re.sub(r'!\[([^\]\n]*)\]\(([^\n]+?)\)', markdown_image, parts[index])
            message['text'] = ''.join(parts).strip()
            message['attachments'] = list({a.get('id', str(i)): a for i, a in enumerate(attachments)}.values())
            messages.append(message)
        return {**result, 'messages': messages}
