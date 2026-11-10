import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.adapters.unified import UnifiedAdaptorPool


class AdaptorStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_configuration_is_visible_and_secrets_are_excluded(self):
        from agent_adaptors.registry import AdaptorRegistry
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'adaptors.json'
            file.write_text(json.dumps({'contractVersion': 1, 'adaptors': [
                {'id': 'cc', 'type': 'claude-code', 'enabled': False,
                 'cwd': '.', 'stateDir': 'state', 'token': 'secret'},
                {'id': 'cx', 'type': 'codex-mcp', 'enabled': False,
                 'cwd': '.', 'endpoint': 'http://user:password@localhost:8765/mcp?token=secret#secret'}
            ]}), encoding='utf-8')
            registry = AdaptorRegistry()
            registry.probe = AsyncMock(return_value={'adaptors': {}})
            with patch('app.adapters.unified.load_registry', return_value=registry):
                pool = UnifiedAdaptorPool.from_config(str(file))
            options = await pool.status_options()
            cc = next(o for o in options if o['type'] == 'claude')
            self.assertTrue(cc['configured'])
            self.assertFalse(cc['loaded'])
            self.assertFalse(cc['config']['enabled'])
            self.assertEqual(cc['config']['cwd'], directory)
            self.assertIn('stateDir', cc['config'])
            self.assertNotIn('secret', json.dumps(options))
            self.assertNotIn('password', json.dumps(options))
            await pool.close()

    async def test_runtime_status_is_distinct_from_loaded(self):
        from agent_adaptors.registry import AdaptorRegistry
        registry = AdaptorRegistry()
        registry.probe = AsyncMock(return_value={'adaptors': {'cx': {
            'ok': False, 'error': {'code': 'probe_failed', 'message': 'unavailable'},
            'diagnostics': {'token': 'private'}
        }}})
        pool = UnifiedAdaptorPool(registry=registry, service=AsyncMock(),
            adapters={'codex': object()}, configured_types={'codex': 'cx'},
            configurations={'codex': {'enabled': True}})
        options = await pool.status_options()
        codex = options[0]
        self.assertTrue(codex['loaded'])
        self.assertFalse(codex['runtime']['ok'])
        self.assertNotIn('private', json.dumps(options))
        self.assertEqual(pool.options()[0]['runtime'], codex['runtime'])


if __name__ == '__main__':
    unittest.main()
