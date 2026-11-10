"""Per-session next-turn overrides. Never edit provider transcripts or global config."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

CODEX_EFFORTS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']


class SessionPreferences:
    def __init__(self, path: Path):
        self.path = path
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
            self.rows = saved['sessions'] if saved.get('version') == 1 and isinstance(saved.get('sessions'), dict) else {}
        except (OSError, ValueError, KeyError, TypeError):
            self.rows = {}

    @staticmethod
    def key(thread_id: str) -> str:
        return hashlib.sha256(thread_id.encode()).hexdigest()

    def get(self, thread_id):
        row = self.rows.get(self.key(thread_id), {})
        return {'model': row.get('model'), 'reasoningEffort': row.get('reasoningEffort')}

    def save(self, thread_id, value, *, support, catalog):
        if not isinstance(value, dict) or set(value) - {'model', 'reasoningEffort'}:
            raise ValueError('只允许修改 model 和 reasoningEffort')
        for key in ('model', 'reasoningEffort'):
            if value.get(key) is not None and not isinstance(value.get(key), str):
                raise ValueError('模型和强度必须是字符串或 null')
        model = value.get('model') or None
        effort = value.get('reasoningEffort') or None
        if model is not None and (not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,159}', model)):
            raise ValueError('模型 ID 无效；不能包含空格、引号或控制字符')
        if model and not support.get('model'):
            raise ValueError('当前 adaptor 不支持设置模型')
        if effort and (not isinstance(effort, str) or effort not in catalog.get('efforts', [])):
            raise ValueError('当前 Agent 不支持这个思考强度')
        if effort and not support.get('reasoningEffort'):
            raise ValueError('当前 adaptor 不支持设置思考强度')
        known = next((m for m in catalog.get('models', []) if m.get('model') == model), None)
        if effort and known and effort not in known.get('supportedReasoningEfforts', catalog.get('efforts', [])):
            raise ValueError('所选模型不支持这个思考强度，请选择模型支持的值或保持默认')
        row = {'model': model, 'reasoningEffort': effort}
        updated = {**self.rows, self.key(thread_id): row}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': 1, 'sessions': updated}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)
        self.rows = updated
        return row


def observed_claude_settings(row):
    model = effort = None
    for entry in row.get('records') or []:
        record = entry.get('record') or {}
        if record.get('isSidechain') or record.get('isMeta'):
            continue
        message = record.get('message') or {}
        candidate_model = message.get('model')
        # Compaction/synthetic transcript entries are not actual inference models.
        if record.get('type') == 'assistant' and isinstance(candidate_model, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,159}', candidate_model) and candidate_model.lower() not in {'synthetic', 'unknown'}:
            model = candidate_model
        if record.get('type') in {'system', 'turn_context'}:
            candidate = record.get('effort') or record.get('reasoningEffort')
            if isinstance(candidate, str):
                effort = candidate
    return {'model': model, 'reasoningEffort': effort, 'source': 'claude-jsonl' if model or effort else 'unknown'}
