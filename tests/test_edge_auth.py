import http.client
from http.cookies import SimpleCookie
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from urllib.parse import urlencode

_MODULE = Path(__file__).resolve().parents[1] / 'deploy' / 'edge-auth' / 'auth_server.py'
_spec = importlib.util.spec_from_file_location('v2_edge_auth', _MODULE)
auth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(auth)
PASSWORD = 'synthetic-test-password-only'


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'htpasswd'
        self.file.write_text('codex:$6$fixture$fixturehash\n', encoding='utf-8')
        self.clock = [1800000000]
        self.passwords = auth.PasswordFile(self.file, verifier=lambda password, encoded: password == PASSWORD)
        self.gate = auth.Gate(self.passwords, self.root / 'state', 'https://workspace.test:8061', now=lambda: self.clock[0])

    def test_existing_credential_is_used_without_basic_auth_or_plaintext_storage(self):
        self.assertIsNone(self.passwords.verify('bad'))
        verified = self.passwords.verify(PASSWORD)
        token = self.gate.issue(verified)
        self.assertTrue(self.gate.authenticated(token))
        self.assertFalse(self.gate.authenticated('forged'))
        self.assertFalse(self.gate.authenticated('A' * 43))
        self.assertNotIn(token.encode(), self.gate.db.read_bytes())
        self.assertNotIn(PASSWORD.encode(), self.gate.db.read_bytes())

    def test_session_survives_restart_but_expiry_rotation_and_logout_revoke_it(self):
        token = self.gate.issue(self.passwords.verify(PASSWORD))
        restarted = auth.Gate(self.passwords, self.root / 'state', 'https://workspace.test:8061', now=lambda: self.clock[0])
        self.assertTrue(restarted.authenticated(token))
        self.gate.revoke(token)
        self.assertFalse(restarted.authenticated(token))
        token = self.gate.issue(self.passwords.verify(PASSWORD))
        self.file.write_text('codex:$6$new$changedhash\n', encoding='utf-8')
        self.assertFalse(self.gate.authenticated(token))
        token = self.gate.issue(self.passwords.verify(PASSWORD))
        self.clock[0] += auth.SESSION_SECONDS + 1
        self.assertFalse(self.gate.authenticated(token))

    def test_csrf_is_bound_to_form_cookie_signed_and_time_limited(self):
        token = self.gate.csrf()
        self.assertTrue(self.gate.valid_csrf(token, token))
        self.assertFalse(self.gate.valid_csrf(token, 'different'))
        self.assertFalse(self.gate.valid_csrf('汉字', '汉字'))
        self.assertFalse(self.gate.valid_csrf(token, 'é'))
        forged = token[:-4] + '0000'
        self.assertFalse(self.gate.valid_csrf(forged, forged))
        self.clock[0] += 1801
        self.assertFalse(self.gate.valid_csrf(token, token))

    def test_missing_corrupt_credential_and_secret_fail_closed(self):
        token = self.gate.issue(self.passwords.verify(PASSWORD))
        self.file.write_text('not a credential', encoding='utf-8')
        self.assertFalse(self.gate.authenticated(token))
        with self.assertRaises(ValueError): self.passwords.snapshot()
        self.file.unlink()
        self.assertFalse(self.gate.authenticated(token))
        (self.root / 'state' / 'csrf-secret').write_bytes(b'bad')
        with self.assertRaises(ValueError):
            auth.Gate(self.passwords, self.root / 'state', 'https://workspace.test:8061')

    def test_cookie_is_secure_httponly_host_only_and_lax(self):
        value = auth.cookie_header(auth.COOKIE, 'opaque', max_age=auth.SESSION_SECONDS)
        self.assertIn('HttpOnly', value); self.assertIn('Secure', value)
        self.assertIn('SameSite=Lax', value); self.assertIn('Path=/', value)
        self.assertNotIn('Domain=', value)

    def test_open_redirect_and_header_injection_are_not_allowed(self):
        for value in ['https://attacker.test', '//attacker.test', '/\\attacker.test', '/%2f/attacker.test', '/\r\nSet-Cookie: bad', '/logout', None]:
            self.assertEqual(auth.safe_next(value), '/')
        self.assertEqual(auth.safe_next('/?view=recent'), '/?view=recent')

    def test_repeated_failures_are_rate_limited(self):
        for _ in range(10):
            self.assertTrue(self.gate.allowed_attempt('192.0.2.1'))
            self.gate.record_failure('192.0.2.1')
        self.assertFalse(self.gate.allowed_attempt('192.0.2.1'))
        self.assertTrue(self.gate.allowed_attempt('192.0.2.2'))
        self.clock[0] += 601
        self.assertTrue(self.gate.allowed_attempt('192.0.2.1'))

    def test_remote_password_compatibility_with_real_libcrypt_when_available(self):
        try:
            import crypt
        except ImportError:
            self.skipTest('libcrypt compatibility checked on Linux ingress host')
        self.file.write_text('codex:' + crypt.crypt(PASSWORD, '$6$rounds=5000$v2testsalt$') + '\n', encoding='utf-8')
        store = auth.PasswordFile(self.file)
        self.assertIsNotNone(store.verify(PASSWORD))
        self.assertIsNone(store.verify('incorrect'))


class FormHttpTests(GateTests):
    def setUp(self):
        super().setUp()
        for port in range(8056, 8150):
            try:
                self.server = auth.ThreadingHTTPServer(('127.0.0.1', port), auth.Handler)
                break
            except OSError:
                continue
        else: self.fail('no available test port')
        self.port = port
        self.server.gate = self.gate
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def request(self, method, path, data=None, *, cookie='', origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Cookie': cookie, 'X-Real-IP': '192.0.2.100'}
        if origin is not None: headers['Origin'] = origin
        body = None
        if data is not None:
            body = urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        connection.request(method, path, body=body, headers=headers)
        result = connection.getresponse()
        status, raw_headers, body = result.status, result.getheaders(), result.read().decode('utf-8')
        connection.close()
        return status, raw_headers, body

    def form(self, path='/access'):
        status, headers, body = self.request('GET', path)
        self.assertEqual(status, 200)
        csrf = re.search(r'name="csrf" value="([^"]+)"', body)[1]
        cookie = next(value.split(';')[0] for name, value in headers if name.lower() == 'set-cookie')
        return csrf, cookie

    def login(self):
        csrf, cookie = self.form()
        status, headers, body = self.request('POST', '/access', {'csrf': csrf, 'password': PASSWORD}, cookie=cookie, origin='https://workspace.test:8061')
        self.assertEqual(status, 303)
        session = next(value.split(';')[0] for name, value in headers if name.lower() == 'set-cookie' and value.startswith(auth.COOKIE + '='))
        return session

    def test_page_login_wrong_password_never_trigger_browser_basic_popup(self):
        status, headers, body = self.request('GET', '/access')
        self.assertEqual(status, 200)
        self.assertIn('type="password"', body)
        self.assertNotIn('WWW-Authenticate', dict(headers))
        csrf, cookie = self.form()
        status, headers, body = self.request('POST', '/access', {'csrf': csrf, 'password': 'wrong'}, cookie=cookie)
        self.assertEqual(status, 200); self.assertIn('密码不正确', body)
        self.assertNotIn('WWW-Authenticate', dict(headers))
        status, headers, _ = self.request('GET', '/verify')
        self.assertEqual(status, 401); self.assertNotIn('WWW-Authenticate', dict(headers))
        session = self.login()
        self.assertEqual(self.request('GET', '/verify', cookie=session)[0], 204)

    def test_logout_really_revokes_the_session_and_requires_csrf(self):
        session = self.login()
        self.assertEqual(self.request('POST', '/logout', {}, cookie=session)[0], 403)
        csrf, cookie = self.form('/logout')
        status, headers, _ = self.request('POST', '/logout', {'csrf': csrf}, cookie=session + '; ' + cookie, origin='https://workspace.test:8061')
        self.assertEqual(status, 303)
        self.assertTrue(any('Max-Age=0' in value for name, value in headers if name.lower() == 'set-cookie'))
        self.assertEqual(self.request('GET', '/verify', cookie=session)[0], 401)

    def test_cross_origin_and_forged_csrf_login_are_rejected(self):
        csrf, cookie = self.form()
        self.assertEqual(self.request('POST', '/access', {'csrf': csrf, 'password': PASSWORD}, cookie=cookie, origin='https://attacker.test')[0], 403)
        self.assertEqual(self.request('POST', '/access', {'csrf': csrf + 'broken', 'password': PASSWORD}, cookie=cookie)[0], 403)
        self.assertEqual(self.request('POST', '/access', {'password': PASSWORD})[0], 403)
        self.assertEqual(self.request('POST', '/access', {'csrf': csrf, 'password': PASSWORD}, cookie=cookie, origin='null')[0], 403)

    def test_rejected_form_closes_connection_before_unread_password_can_be_parsed(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        payload = urlencode({'password': 'synthetic-password-must-not-be-a-method'}).encode()
        connection.request('POST', '/access', body=payload, headers={
            'Origin': 'https://attacker.test', 'Content-Type': 'application/x-www-form-urlencoded'})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        self.assertEqual(response.getheader('Connection'), 'close')
        response.read(); connection.close()

    def test_headers_no_cache_no_framing_and_template_escapes(self):
        status, headers, body = self.request('GET', '/access?next=//attacker.test')
        values = dict(headers)
        self.assertIn('no-store', values['Cache-Control'])
        self.assertEqual(values['X-Frame-Options'], 'DENY')
        self.assertEqual(values['Referrer-Policy'], 'same-origin', 'native same-origin form POST must retain a non-null Origin')
        self.assertIn("frame-ancestors 'none'", values['Content-Security-Policy'])
        self.assertIn('name="next" value="/"', body)
        self.assertNotIn('<script>', auth.render_page('token', error='<script>bad</script>').decode())

    def test_bad_content_type_oversize_and_unknown_route_are_rejected(self):
        self.assertEqual(self.request('GET', '/etc/passwd')[0], 404)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('POST', '/access', headers={'Content-Length': str(auth.MAX_BODY + 1)})
        response = conn.getresponse(); self.assertEqual(response.status, 413); response.read(); conn.close()


if __name__ == '__main__': unittest.main()
