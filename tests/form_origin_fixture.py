"""Local-only browser regression for native form Origin under Referrer-Policy.

The fixture uses synthetic credentials and temporary state. Never deploy it.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
from urllib.parse import urlsplit

_spec = importlib.util.spec_from_file_location('fixture_auth', Path(__file__).resolve().parents[1] / 'deploy' / 'edge-auth' / 'auth_server.py')
auth = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(auth)


class FixtureHandler(auth.Handler):
    def send_header(self, name, value):
        if name == 'Referrer-Policy' and getattr(self, 'legacy', False):
            value = 'no-referrer'
        return super().send_header(name, value)

    def do_GET(self):
        self.legacy = False
        route = urlsplit(self.path).path
        if route == '/legacy':
            self.legacy = True
            return self.page()
        if route == '/':
            return self.respond(200 if self.gate.authenticated(self.cookie(auth.COOKIE)) else 401,
                'Synthetic browser login succeeded'.encode())
        if route == '/fixture-status':
            return self.respond(200, json.dumps(self.server.observed).encode(), content_type='application/json')
        return super().do_GET()

    def do_POST(self):
        self.server.observed.append({'origin': self.headers.get('Origin'), 'fetchSite': self.headers.get('Sec-Fetch-Site')})
        return super().do_POST()


def main():
    port = 8057
    with tempfile.TemporaryDirectory(prefix='v2-form-origin-') as directory:
        root = Path(directory)
        password_file = root / 'htpasswd'
        password_file.write_text('fixture:$6$fixture$unused\n', encoding='utf-8')
        gate = auth.Gate(auth.PasswordFile(password_file, verifier=lambda password, encoded: password == 'synthetic-fixture-only'), root / 'state', 'https://fixture.test')
        # Test-only: loopback is a trustworthy browser context. This permits
        # exercising native forms and Secure cookies without TLS exceptions.
        gate.public_origin = f'http://127.0.0.1:{port}'
        server = auth.ThreadingHTTPServer(('127.0.0.1', port), FixtureHandler)
        server.gate = gate; server.observed = []; server.daemon_threads = True
        print(f'Local synthetic browser fixture listening on 127.0.0.1:{port}', flush=True)
        try: server.serve_forever()
        finally: server.server_close()


if __name__ == '__main__': main()
