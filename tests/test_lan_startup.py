import unittest
from unittest.mock import patch
from app.gateway.server import main


class LanStartupTests(unittest.TestCase):
    def run_main(self, *arguments):
        with patch('sys.argv', ['server', *arguments]), patch('app.gateway.server.HarnessServer') as server, patch('app.gateway.server.build_app') as build, patch('app.gateway.server.web.run_app') as run:
            main()
            return server.call_args.kwargs, run.call_args.kwargs

    def test_default_supports_lan_and_codex_claude(self):
        with patch.dict('os.environ', {}, clear=True):
            constructor, listener=self.run_main()
        self.assertEqual(listener['host'],'0.0.0.0')
        self.assertEqual(listener['port'],8050)
        self.assertTrue(constructor['browse_only'])
        self.assertTrue(constructor['adaptor_config'].endswith('adaptors.json'))

    def test_explicit_local_only_binding_is_preserved(self):
        constructor,listener=self.run_main('--host','127.0.0.1','--port','8053')
        self.assertEqual(listener['host'],'127.0.0.1')
        self.assertEqual(listener['port'],8053)

    def test_environment_can_opt_out_of_lan(self):
        with patch.dict('os.environ', {'HARNESS_HOST':'127.0.0.1'}):
            _,listener=self.run_main()
        self.assertEqual(listener['host'],'127.0.0.1')

if __name__=='__main__': unittest.main()
