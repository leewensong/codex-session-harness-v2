"""End-to-end gate + isolated Nginx fixture; no production password changes.
Run as root on ingress after deploying auth_server.py.
"""
import base64
import contextlib
import crypt
import http.client
from http.cookies import SimpleCookie
import importlib.util
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode

spec = importlib.util.spec_from_file_location('edge_auth', '/opt/codex-v2-auth/auth_server.py')
auth = importlib.util.module_from_spec(spec); spec.loader.exec_module(auth)


def request(port, method, path, *, data=None, cookie='', headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    final = {'Cookie': cookie, **(headers or {})}
    body = None
    if data is not None:
        body = urlencode(data).encode()
        final['Content-Type'] = 'application/x-www-form-urlencoded'
    connection.request(method, path, body, final)
    response = connection.getresponse()
    result = response.status, response.getheaders(), response.read().decode('utf-8', errors='replace')
    connection.close()
    return result


def response_cookies(headers):
    result = {}
    for name, value in headers:
        if name.lower() == 'set-cookie':
            cookie = SimpleCookie(value)
            result.update({k: v.value for k, v in cookie.items()})
    return result


def run():
    # Test listeners are loopback-only and explicitly bound before use.
    http_port, gate_port = 18063, 18064
    with tempfile.TemporaryDirectory(prefix='v2-nginx-auth-test-') as temporary:
        root = Path(temporary); root.chmod(0o755)
        password = 'isolated-fixture-' + auth.secrets.token_hex(16)
        htpasswd = root / 'htpasswd'
        htpasswd.write_text('fixture:' + crypt.crypt(password, '$6$v2fixture$') + '\n')
        gate = auth.Gate(auth.PasswordFile(htpasswd), root / 'state', f'https://fixture.test:{http_port}')
        server = auth.ThreadingHTTPServer(('127.0.0.1', gate_port), auth.Handler)
        server.gate = gate
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        production = Path('/etc/nginx/sites-available/condexmobile-v2').read_text()
        server_block = production[production.index('server {'):]
        server_block = re.sub(r'    listen 8061 ssl;\n    listen \[::\]:8061 ssl;', f'    listen 127.0.0.1:{http_port};', server_block)
        server_block = re.sub(r'^    ssl_.*\n|^    include /etc/letsencrypt/.*\n', '', server_block, flags=re.M)
        server_block = server_block.replace('127.0.0.1:18062', f'127.0.0.1:{gate_port}')
        server_block = server_block.replace('/var/log/nginx/codex-v2-8061.access.log', str(root / 'access.log')).replace('/var/log/nginx/codex-v2-8061.error.log', str(root / 'error.log'))
        config = root / 'nginx.conf'
        config.write_text(f'pid {root}/nginx.pid; error_log {root}/nginx-error.log; events {{}} http {{\n' + production[:production.index('server {')] + server_block + '\n}')
        subprocess.run(['nginx', '-t', '-p', str(root), '-c', str(config)], check=True, capture_output=True)
        process = subprocess.Popen(['nginx', '-p', str(root), '-c', str(config), '-g', 'daemon off;'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(30):
                try:
                    request(http_port, 'GET', '/access'); break
                except OSError: time.sleep(.1)
            for method, path in [('GET', '/'), ('GET', '/health'), ('GET', '/static/workspace.js'), ('POST', '/api/attachments'), ('GET', '/api/attachments/' + 'a'*32 + '/preview')]:
                status, headers, _ = request(http_port, method, path)
                assert status == 401 and not any(name.lower() == 'www-authenticate' for name, value in headers), (method, path, status)
            assert request(http_port, 'GET', '/', headers={'Accept': 'text/html'})[0] == 303
            assert request(http_port, 'GET', '/_v2_auth')[0] == 404
            status, headers, body = request(http_port, 'GET', '/access')
            cookies = response_cookies(headers)
            csrf = re.search(r'name="csrf" value="([^"]+)"', body)[1]
            wrong = request(http_port, 'POST', '/access', data={'csrf': csrf, 'password': 'wrong'}, cookie=f'{auth.CSRF_COOKIE}={cookies[auth.CSRF_COOKIE]}')
            assert wrong[0] == 200 and '密码不正确' in wrong[2]
            cookies.update(response_cookies(wrong[1])); csrf = re.search(r'name="csrf" value="([^"]+)"', wrong[2])[1]
            status, headers, _ = request(http_port, 'POST', '/access', data={'csrf': csrf, 'password': password}, cookie=f'{auth.CSRF_COOKIE}={cookies[auth.CSRF_COOKIE]}', headers={'Origin': f'https://fixture.test:{http_port}'})
            assert status == 303
            cookies.update(response_cookies(headers))
            session = f'{auth.COOKIE}={cookies[auth.COOKIE]}'
            status, _, body = request(http_port, 'GET', '/', cookie=session)
            assert status == 200 and 'Codex Workspace' in body, (status, body[:100])
            status, _, body = request(http_port, 'GET', '/health', cookie=session)
            assert status == 200 and json.loads(body).get('adaptorId'), (status, body[:100])
            # Check real WebSocket authorization via a minimal HTTP upgrade.
            def websocket(cookie):
                raw = socket.create_connection(('127.0.0.1', http_port), timeout=5)
                raw.sendall((f'GET /ws HTTP/1.1\r\nHost: fixture.test:{http_port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: '+base64.b64encode(b'0123456789abcdef').decode()+f'\r\nSec-WebSocket-Version: 13\r\nCookie: {cookie}\r\n\r\n').encode())
                answer = raw.recv(4096).decode('latin-1'); raw.close()
                return int(answer.split(' ',2)[1])
            assert websocket('') == 401
            assert websocket(session) == 101
            status, headers, body = request(http_port, 'GET', '/logout', cookie=session)
            csrf = re.search(r'name="csrf" value="([^"]+)"', body)[1]
            csrf_cookie = response_cookies(headers)[auth.CSRF_COOKIE]
            status, _, _ = request(http_port, 'POST', '/logout', data={'csrf':csrf}, cookie=session + f'; {auth.CSRF_COOKIE}={csrf_cookie}')
            assert status == 303
            assert request(http_port, 'GET', '/health', cookie=session)[0] == 401
            print(json.dumps({'ok':True,'unauthenticatedRoutesProtected':True,'noBasicPopup':True,'passwordFormLogin':True,'nginxAuthorizedPage':True,'websocket401Then101':True,'logoutRevoked':True,'productionCredentialsUnchanged':True}))
        finally:
            process.terminate(); process.wait(timeout=10)
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == '__main__': run()
