"""V2-local registry factories. Existing V1 adaptors and state are not modified."""
from __future__ import annotations

import contextvars
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from agent_adaptors.claude import ClaudeCodeAdaptor
from agent_adaptors.claude_sdk import ClaudeSdkAdaptor
from agent_adaptors.codex_mcp import CodexMcpAdaptor
from agent_adaptors.contracts import Capabilities, AdaptorError
from agent_adaptors.fake import FakeAdaptor
from agent_adaptors.registry import AdaptorRegistry
from agent_adaptors.history import History, native_id
import asyncio

from app.adapters.codex.adapter import CodexSessionAdapter
from app.attachments import attachment_prompt


class NativeCodexAdaptor:
    provider = 'codex'
    adaptor_version = 'v2-native-1'
    capabilities = Capabilities(stream=True, external_session_write=True)
    image_mode = 'native'
    supports_project_create = True
    settings_support = {'model': True, 'reasoningEffort': True}

    def __init__(self, cwd):
        self.adapter = CodexSessionAdapter(cwd=cwd)

    @property
    def default_settings(self):
        return self.adapter.default_settings

    async def model_catalog(self):
        return await self.adapter.model_catalog()

    async def runtime_status(self):
        return await self.adapter.runtime_status()

    async def create_session(self, *, name):
        row = await self.adapter.create_session(name=name)
        return {**row, 'id': row['threadId']}

    async def create_project_session(self, *, name, cwd):
        row = await self.adapter.create_project_session(name=name, cwd=cwd)
        return {**row, 'id': row['threadId']}

    async def resume_session(self, session_id):
        row = await self.adapter.resume_session(session_id)
        return {**row, 'id': row['threadId']}

    async def list_sessions(self, *, limit=50, cursor=None):
        return await self.adapter.list_sessions(limit=limit, cursor=cursor)

    async def read_session(self, session_id, *, limit=30):
        row = await self.adapter.read_session(session_id, limit=limit)
        return {**row, 'id': session_id}

    async def send_turn(self, session_id, text, *, on_delta=None, timeout=180):
        return await self.adapter.send_turn(session_id, text, on_delta=on_delta, timeout=timeout)

    async def send_configured_turn(self, session_id, text, *, settings=None, attachments=None, on_delta=None, timeout=180):
        return await self.adapter.send_turn(session_id, text, settings=settings, attachments=attachments, on_delta=on_delta, timeout=timeout)

    async def interrupt(self, session_id):
        await self.adapter.interrupt(session_id)
        return {'ok': True, 'status': 'interrupt-requested'}

    async def close(self):
        await self.adapter.close()


class ConfigurableClaudeSdkAdaptor(ClaudeSdkAdaptor):
    """Bind model/effort to a single SDK query, never to global adaptor attributes.

    The SDK adaptor constructs ClaudeAgentOptions internally. A task-local SDK
    factory overlays those options while keeping its existing timeout, UUID,
    cleanup and writer ownership implementation intact. Concurrent turns see
    independent factories; V1's shared module is left unchanged.
    """
    image_mode = 'file-reference'
    supports_project_create = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._turn_options = contextvars.ContextVar('v2_claude_turn_options', default=None)

    def _project_history(self, sid):
        cwd = self.sessions.get(sid, {}).get('projectCwd')
        return History(Path(cwd), self.history.projects) if cwd else None

    def _summary(self, sid):
        row = super()._summary(sid)
        row['cwd'] = self.sessions[sid].get('projectCwd') or row['cwd']
        return row

    async def create_project_session(self, *, name, cwd):
        self._check()
        directory = Path(cwd).resolve()
        if not directory.is_dir():
            raise AdaptorError('project_unavailable', '目标项目目录不存在')
        # Persist the original project with the reserved UUID. The SDK will use
        # session_id on the first send and resume on later sends, with this cwd.
        import uuid
        import time
        sid = str(uuid.uuid4())
        self.sessions[sid] = {'name': name, 'started': False, 'updatedAt': time.time(), 'projectCwd': str(directory)}
        try:
            self._save()
        except BaseException:
            self.sessions.pop(sid, None)
            raise
        return self._decorate(self._summary(sid), owned=True)

    async def resume_session(self, session_id):
        self._check()
        sid = native_id(session_id)
        history = self._project_history(sid)
        if history is None:
            return await super().resume_session(sid)
        if not Path(self.sessions[sid]['projectCwd']).is_dir():
            raise AdaptorError('project_unavailable', '目标项目目录已不存在')
        if self.sessions[sid]['started'] and not history.path(sid).is_file():
            raise AdaptorError('session_not_found', 'owned session history is missing')
        return self._decorate(self._summary(sid), owned=True)

    async def read_session(self, session_id, *, limit=30):
        self._check()
        sid = native_id(session_id)
        history = self._project_history(sid)
        if history is None:
            return await super().read_session(sid, limit=limit)
        if not self.sessions[sid]['started'] and not history.path(sid).is_file():
            return {**self._decorate(self._summary(sid), owned=True), 'messages': []}
        row = await asyncio.to_thread(history.read, sid, limit)
        return self._decorate({**row, **self._summary(sid)}, owned=True)

    async def read_full_session(self, session_id):
        self._check()
        sid = native_id(session_id)
        history = self._project_history(sid)
        if history is None:
            return await super().read_full_session(sid)
        if not self.sessions[sid]['started'] and not history.path(sid).is_file():
            return {**self._decorate(self._summary(sid), owned=True), 'messages': [], 'records': [],
                    'diagnostics': {'complete': True, 'messageCount': 0, 'recordCount': 0}}
        row = await asyncio.to_thread(history.read_full, sid)
        return self._decorate({**row, **self._summary(sid)}, owned=True)

    def _sdk(self):
        sdk = super()._sdk()
        overrides = self._turn_options.get()
        if not overrides:
            return sdk
        def options(**kwargs):
            return sdk.ClaudeAgentOptions(**{**kwargs, **overrides})
        return SimpleNamespace(ClaudeSDKClient=sdk.ClaudeSDKClient, ClaudeAgentOptions=options)

    @property
    def settings_support(self):
        try:
            params = inspect.signature(super()._sdk().ClaudeAgentOptions).parameters
            return {'model': 'model' in params, 'reasoningEffort': 'effort' in params}
        except (TypeError, ValueError, ImportError, AdaptorError):
            return {'model': False, 'reasoningEffort': False}

    @property
    def default_settings(self):
        return {'model': self.model, 'reasoningEffort': None}

    async def model_catalog(self):
        return {'models': [{'model': m, 'displayName': m, 'supportedReasoningEfforts': ['low', 'medium', 'high', 'xhigh', 'max']}
                for m in dict.fromkeys([m for m in [self.model, 'sonnet', 'opus', 'haiku'] if m])],
                'efforts': ['low', 'medium', 'high', 'xhigh', 'max'], 'source': 'sdk-config-and-aliases',
                'verified': False, 'supportsCustomModel': True,
                'note': '模型别名与已配置模型；不是服务商在线授权清单。第三方模型可能不支持 effort。'}

    async def send_configured_turn(self, session_id, text, *, settings=None, attachments=None, on_delta=None, timeout=180):
        overrides = {}
        if settings and settings.get('model'):
            overrides['model'] = settings['model']
        if settings and settings.get('reasoningEffort'):
            overrides['effort'] = settings['reasoningEffort']
        token = self._turn_options.set(overrides)
        try:
            return await super().send_turn(session_id, attachment_prompt(text, attachments or []), on_delta=on_delta, timeout=timeout)
        finally:
            self._turn_options.reset(token)


def load_v2_registry(file):
    path = Path(file).resolve()
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    resolve = lambda value: str((path.parent / value).resolve())
    def claude(entry):
        options = dict(cwd=resolve(entry['cwd']), state_dir=resolve(entry.get('v2StateDir') or entry.get('stateDir', 'data/' + entry['id'])),
                       projects_dir=resolve(entry['projectsDir']) if entry.get('projectsDir') else None,
                       binary=entry.get('binary'), env_mode=entry.get('envMode', 'inherit'), tools=entry.get('tools'))
        if entry.get('backend', 'cli') == 'sdk':
            return ConfigurableClaudeSdkAdaptor(**options, allow_external_sessions=entry.get('allowExternalSessions', False),
                model=entry.get('model'), permission_mode=entry.get('permissionMode', 'dontAsk'))
        return ClaudeCodeAdaptor(**options)
    return AdaptorRegistry.from_config(config, {
        'codex-native': lambda e: NativeCodexAdaptor(cwd=resolve(e['cwd'])),
        'codex-mcp': lambda e: CodexMcpAdaptor(cwd=resolve(e['cwd']), endpoint=e.get('endpoint')),
        'claude-code': claude, 'fake': lambda e: FakeAdaptor(available=e.get('available', True)),
    })
