"""Allowlist displayable text only; discard thinking, tool JSON and subagents."""
import json
from .contracts import AdaptorError


class StreamParser:
    def __init__(self, session_id):
        self.session_id = session_id
        self.result = None

    def feed(self, raw):
        if len(raw) > 4 * 1024 * 1024:
            raise AdaptorError('output_limit', 'stream record exceeds 4 MiB')
        try: event = json.loads(raw)
        except (ValueError, UnicodeError):
            raise AdaptorError('protocol_error', 'invalid stream JSON') from None
        if not isinstance(event, dict):
            raise AdaptorError('protocol_error', 'stream record must be object')
        if event.get('parent_tool_use_id'):
            return ''
        if event.get('session_id') != self.session_id:
            raise AdaptorError('protocol_error', 'stream session mismatch')
        if event.get('type') == 'result':
            if self.result is not None:
                raise AdaptorError('protocol_error', 'duplicate terminal result')
            self.result = event
            return ''
        nested = event.get('event')
        if event.get('type') != 'stream_event' or not isinstance(nested, dict):
            return ''
        delta = nested.get('delta')
        if nested.get('type') == 'content_block_delta' and isinstance(delta, dict) and delta.get('type') == 'text_delta' and isinstance(delta.get('text'), str):
            return delta['text']
        return ''
