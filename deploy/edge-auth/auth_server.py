"""Loopback-only form authentication for the V2 Nginx HTTPS ingress.

No packages or plaintext passwords are required. Password verification reuses
Nginx's existing htpasswd file. This service never proxies Agent requests.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import hmac
import html
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

COOKIE = '__Host-codex_v2_access'
CSRF_COOKIE = '__Host-codex_v2_csrf'
MAX_BODY = 16 * 1024
SESSION_SECONDS = 30 * 24 * 60 * 60
log = logging.getLogger('codex-v2-auth')


def safe_next(value):
    if not isinstance(value, str) or not value.startswith('/') or value.startswith('//'):
        return '/'
    # Be conservative: no URL decoding ambiguities or redirect header injection.
    if value.startswith(('/access', '/logout', '/_v2_auth')) or re.search(r'[\\\x00-\x20\x7f]', value) or '%' in value:
        return '/'
    return value[:2048]


class PasswordFile:
    def __init__(self, path, verifier=None):
        self.path = Path(path)
        self._verifier = verifier

    def snapshot(self):
        raw = self.path.read_bytes()
        if not raw or len(raw) > 65536:
            raise ValueError('invalid credential file')
        rows = []
        for line in raw.decode('utf-8').splitlines():
            if not line or line.startswith('#'):
                continue
            username, sep, encoded = line.partition(':')
            if not sep or not username or not encoded or len(rows) >= 32:
                raise ValueError('invalid credential record')
            if not encoded.startswith(('$6$', '$5$', '$2a$', '$2b$', '$2y$')):
                raise ValueError('unsupported password hash; no plaintext fallback')
            rows.append((username, encoded))
        if not rows:
            raise ValueError('empty credential file')
        return hashlib.sha256(raw).hexdigest(), rows

    def verify(self, password):
        if not isinstance(password, str) or not password or len(password.encode('utf-8')) > 1024:
            return None
        version, rows = self.snapshot()
        if self._verifier is None:
            # Debian Python 3.11/libcrypt supports the deployment's SHA-512 hash.
            # Fail closed on unsupported Python versions; never invent a password.
            import crypt
            verifier = lambda supplied, encoded: hmac.compare_digest(crypt.crypt(supplied, encoded) or '', encoded)
        else:
            verifier = self._verifier
        for username, encoded in rows:
            if verifier(password, encoded):
                return version, username
        return None


class Gate:
    def __init__(self, password_file, state_dir, public_origin, *, now=time.time):
        self.passwords = password_file
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.now = now
        self.public_origin = public_origin.rstrip('/')
        origin = urlsplit(self.public_origin)
        if origin.scheme != 'https' or not origin.netloc or origin.path or origin.query or origin.fragment or origin.username:
            raise ValueError('public origin must be an HTTPS origin')
        secret_path = self.state_dir / 'csrf-secret'
        try:
            secret = secret_path.read_bytes()
        except FileNotFoundError:
            secret = secrets.token_bytes(32)
            try:
                with secret_path.open('xb') as stream:
                    stream.write(secret)
                secret_path.chmod(0o600)
            except FileExistsError:
                secret = secret_path.read_bytes()
        if len(secret) != 32:
            raise ValueError('invalid CSRF secret; restore state instead of silently resetting it')
        self.secret = secret
        self.db = self.state_dir / 'sessions.sqlite3'
        with self.connection() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, credential_version TEXT NOT NULL, username TEXT NOT NULL, expires INTEGER NOT NULL)')
        self.db.chmod(0o600)
        self._lock = threading.Lock()
        self._failures = {}
        self._attempts = []
        self.verify_slots = threading.BoundedSemaphore(2)
        # Validate the credentials at startup rather than exposing an unprotected site.
        self.passwords.snapshot()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.db, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def issue(self, verified):
        version, username = verified
        token = secrets.token_urlsafe(32)
        with self.connection() as connection:
            connection.execute('DELETE FROM sessions WHERE expires <= ?', (int(self.now()),))
            connection.execute('INSERT INTO sessions VALUES (?,?,?,?)', (self.token_hash(token), version, username, int(self.now()) + SESSION_SECONDS))
        return token

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode('ascii')).hexdigest()

    def authenticated(self, token):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return False
        try:
            version, _ = self.passwords.snapshot()
            with self.connection() as connection:
                row = connection.execute('SELECT credential_version, expires FROM sessions WHERE token_hash=?', (self.token_hash(token),)).fetchone()
            return bool(row and row[1] > self.now() and hmac.compare_digest(row[0], version))
        except (OSError, ValueError, sqlite3.Error):
            log.error('credential/session store unavailable; access denied')
            return False

    def revoke(self, token):
        if isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            with self.connection() as connection:
                connection.execute('DELETE FROM sessions WHERE token_hash=?', (self.token_hash(token),))

    def csrf(self):
        payload = f'{int(self.now())}.{secrets.token_urlsafe(24)}'
        return payload + '.' + hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def valid_csrf(self, supplied, cookie):
        if not isinstance(supplied, str) or not isinstance(cookie, str) or not re.fullmatch(r'[0-9]{1,12}\.[A-Za-z0-9_-]{32}\.[a-f0-9]{64}', supplied) or not re.fullmatch(r'[0-9]{1,12}\.[A-Za-z0-9_-]{32}\.[a-f0-9]{64}', cookie) or not hmac.compare_digest(supplied, cookie):
            return False
        try:
            timestamp, nonce, signature = supplied.split('.')
            if not re.fullmatch(r'[A-Za-z0-9_-]{32}', nonce) or not 0 <= self.now() - int(timestamp) <= 1800:
                return False
            expected = hmac.new(self.secret, f'{timestamp}.{nonce}'.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except (ValueError, TypeError):
            return False

    def allowed_attempt(self, address):
        now = self.now()
        with self._lock:
            if address not in self._failures and len(self._failures) >= 4096:
                self._failures.pop(next(iter(self._failures)))
            failures = [t for t in self._failures.get(address, []) if now - t < 600]
            self._failures[address] = failures
            self._attempts = [t for t in self._attempts if now - t < 60]
            if len(failures) >= 10 or len(self._attempts) >= 120:
                return False
            self._attempts.append(now)
            return True

    def record_failure(self, address):
        with self._lock:
            if address not in self._failures and len(self._failures) >= 4096:
                self._failures.pop(next(iter(self._failures)))
            self._failures.setdefault(address, []).append(self.now())


def cookie_header(name, value, *, max_age):
    cookie = SimpleCookie()
    cookie[name] = value
    cookie[name]['path'] = '/'
    cookie[name]['secure'] = True
    cookie[name]['httponly'] = True
    cookie[name]['samesite'] = 'Lax'
    cookie[name]['max-age'] = max_age
    return cookie.output(header='').strip()


def render_page(csrf, *, error='', next_path='/', logout=False):
    escape = html.escape
    title = '退出访问验证' if logout else '访问验证'
    description = '退出后，此浏览器需要重新输入密码。不会停止正在运行的 Agent 任务。' if logout else '请输入原来的 V2 访问密码。验证成功后，此浏览器 30 天内免重复输入。'
    field = '' if logout else '<label for="password">访问密码</label><input id="password" name="password" type="password" autocomplete="current-password" placeholder="输入访问密码" maxlength="1024" autofocus required>'
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>Codex Workspace V2 · {title}</title>
<style>*{{box-sizing:border-box}}body{{margin:0;min-height:100dvh;display:grid;place-items:center;padding:24px;background:#f3f6f7;color:#34484b;font:14px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}main{{width:min(100%,398px);padding:32px;border:1px solid #e0e8e6;border-radius:17px;background:white;box-shadow:0 16px 56px #2543380b}}.brand{{display:flex;align-items:center;gap:10px;margin-bottom:30px;font-weight:600;font-size:16px}}.mark{{display:grid;place-items:center;width:32px;height:32px;border-radius:9px;background:#315c55;color:#fff;font:15px monospace}}.badge{{font-size:10px;background:#f1f4f3;padding:1px 5px;border-radius:4px;color:#96a49e}}h1{{margin:0 0 12px;font-size:22px;font-weight:600}}p{{color:#89968f;font-size:13px;margin:0 0 24px}}label{{display:block;font-size:12px;margin-bottom:7px}}input{{width:100%;border:1px solid #dce5df;border-radius:8px;padding:12px;font:inherit;color:#33483f;background:#fcfdfc}}input:focus{{outline:2px solid #8bb7a1;outline-offset:2px}}button{{width:100%;padding:11px;margin-top:18px;border:0;border-radius:8px;background:#376f5f;color:#fff;cursor:pointer;font:inherit}}button:hover{{background:#2f6454}}.error{{padding:10px 12px;margin-bottom:18px;font-size:12px;border-radius:7px;background:#fff1ed;color:#ac6253}}.foot{{margin:24px 0 0;font-size:10px;color:#a2aea7;line-height:1.8}}a{{color:#517b69}}.back{{display:block;text-align:center;margin-top:14px;font-size:12px}}</style></head><body><main>
<div class="brand"><span class="mark">&lt;/&gt;</span><span>Codex Workspace</span><span class="badge">V2</span></div><h1>{title}</h1><p>{description}</p>
{f'<div class="error" role="alert">{escape(error)}</div>' if error else ''}
<form method="post" action="{'/logout' if logout else '/access'}"><input type="hidden" name="csrf" value="{escape(csrf, quote=True)}"><input type="hidden" name="next" value="{escape(safe_next(next_path), quote=True)}">{field}<button type="submit">{'退出登录' if logout else '进入工作空间'}</button></form>
{f'<a class="back" href="/">返回工作空间</a>' if logout else ''}<p class="foot">密码只用于访问验证，不会交给 Codex / Claude。<br>请仅在自己的设备上登录，并通过 HTTPS 访问。</p></main></body></html>'''.encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    server_version = 'WorkspaceAccess/1'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    @property
    def gate(self): return self.server.gate

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Never log posted passwords, cookie values or CSRF tokens.
        log.info('%s %s %s', self.client_address[0], self.command, urlsplit(self.path).path)

    def cookie(self, name):
        try:
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            return cookie[name].value if name in cookie else None
        except Exception:
            return None

    def respond(self, status, body=b'', *, cookies=(), location=None, content_type='text/html; charset=utf-8', retry=False):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'no-store, private')
        self.send_header('Pragma', 'no-cache')
        # Native form POSTs under no-referrer can carry Origin: null. Keep a
        # same-origin referrer so the browser sends its real Origin; cross-site
        # destinations still receive no referrer. Never relax the Origin/CSRF check.
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        if location: self.send_header('Location', location)
        if retry: self.send_header('Retry-After', '600')
        for cookie in cookies: self.send_header('Set-Cookie', cookie)
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(body)

    def page(self, *, error='', next_path='/', logout=False, status=200):
        csrf = self.gate.csrf()
        self.respond(status, render_page(csrf, error=error, next_path=next_path, logout=logout),
            cookies=[cookie_header(CSRF_COOKIE, csrf, max_age=1800)], retry=status == 429)

    def do_GET(self):
        route = urlsplit(self.path)
        if route.path == '/verify':
            valid = self.gate.authenticated(self.cookie(COOKIE))
            return self.respond(204 if valid else 401, content_type='text/plain')
        if route.path not in {'/access', '/logout'}:
            return self.respond(404, b'Not found', content_type='text/plain')
        if route.path == '/access':
            next_path = safe_next(parse_qs(route.query).get('next', ['/'])[0])
            if self.gate.authenticated(self.cookie(COOKIE)):
                return self.respond(303, location=next_path)
            return self.page(next_path=next_path)
        return self.page(logout=True)

    def do_HEAD(self): return self.do_GET()

    def do_POST(self):
        # Rejected forms can return before their body is consumed. Never reuse
        # that socket or parse leftover password bytes as a later HTTP method.
        self.close_connection = True
        route = urlsplit(self.path)
        if route.path not in {'/access', '/logout'}:
            return self.respond(404, b'Not found', content_type='text/plain')
        if (self.headers.get('Origin') not in {None, self.gate.public_origin}
                or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
            return self.respond(403, b'Forbidden', content_type='text/plain')
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY or self.headers.get('Transfer-Encoding'):
            self.close_connection = True
            return self.respond(413, b'Form too large', content_type='text/plain')
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
            return self.respond(415, b'Form content type required', content_type='text/plain')
        try:
            form = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True, max_num_fields=8)
            if any(len(values) != 1 for values in form.values()):
                raise ValueError('duplicate form fields')
        except (UnicodeDecodeError, ValueError):
            return self.respond(400, b'Invalid form', content_type='text/plain')
        if not self.gate.valid_csrf(form.get('csrf', [''])[0], self.cookie(CSRF_COOKIE)):
            return self.respond(403, b'Invalid form session; reload /access', content_type='text/plain')
        if route.path == '/logout':
            self.gate.revoke(self.cookie(COOKIE))
            return self.respond(303, location='/access', cookies=[cookie_header(COOKIE, '', max_age=0), cookie_header(CSRF_COOKIE, '', max_age=0)])
        address = self.headers.get('X-Real-IP') or self.client_address[0]
        try:
            address = str(ipaddress.ip_address(address))
        except ValueError:
            address = 'unknown'
        if not self.gate.allowed_attempt(address):
            return self.page(error='尝试次数过多，请 10 分钟后重试。', status=429)
        if not self.gate.verify_slots.acquire(blocking=False):
            return self.page(error='验证繁忙，请稍后重试。', status=429)
        try:
            verified = self.gate.passwords.verify(form.get('password', [''])[0])
            if not verified:
                self.gate.record_failure(address)
                return self.page(error='密码不正确，请重试。', next_path=safe_next(form.get('next', ['/'])[0]))
            token = self.gate.issue(verified)
        except Exception:
            log.exception('password verification unavailable')
            return self.page(error='验证服务暂不可用，请稍后重试。', status=503)
        finally:
            self.gate.verify_slots.release()
        return self.respond(303, location=safe_next(form.get('next', ['/'])[0]),
            cookies=[cookie_header(COOKIE, token, max_age=SESSION_SECONDS), cookie_header(CSRF_COOKIE, '', max_age=0)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=18062)
    parser.add_argument('--password-file', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--public-origin', required=True)
    args = parser.parse_args()
    if not ipaddress.ip_address(args.listen).is_loopback:
        parser.error('authentication service must listen on loopback only')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    gate = Gate(PasswordFile(args.password_file), args.state_dir, args.public_origin)
    server = ThreadingHTTPServer((args.listen, args.port), Handler)
    server.daemon_threads = True
    server.gate = gate
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
