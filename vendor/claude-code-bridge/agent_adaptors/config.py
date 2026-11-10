"""Explicit built-in factories; config cannot execute arbitrary import paths."""
import json
from pathlib import Path
from .claude import ClaudeCodeAdaptor
from .codex_mcp import CodexMcpAdaptor
from .fake import FakeAdaptor
from .registry import AdaptorRegistry


def load_registry(file):
    path = Path(file).resolve()
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    resolve = lambda value: str((path.parent / value).resolve())
    def claude(entry):
        options = dict(cwd=resolve(entry['cwd']), state_dir=resolve(entry.get('stateDir', 'data/'+entry['id'])),
                       projects_dir=resolve(entry['projectsDir']) if entry.get('projectsDir') else None,
                       binary=entry.get('binary'), env_mode=entry.get('envMode', 'inherit'), tools=entry.get('tools'))
        backend = entry.get('backend', 'cli')
        if backend == 'sdk':
            from .claude_sdk import ClaudeSdkAdaptor
            return ClaudeSdkAdaptor(**options,
                allow_external_sessions=entry.get('allowExternalSessions', False),
                model=entry.get('model'), permission_mode=entry.get('permissionMode', 'dontAsk'))
        if backend != 'cli':
            from .contracts import AdaptorError
            raise AdaptorError('invalid_config', 'Claude backend must be cli or sdk')
        return ClaudeCodeAdaptor(**options)
    factories = {
        'claude-code': claude,
        'codex-mcp': lambda e: CodexMcpAdaptor(cwd=resolve(e['cwd']), endpoint=e.get('endpoint')),
        'fake': lambda e: FakeAdaptor(available=e.get('available', True)),
    }
    return AdaptorRegistry.from_config(config, factories)
