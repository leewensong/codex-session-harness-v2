"""Verify a deployed V2 login using stdin credentials without logging secrets.

Reads {"password": "..."} from stdin. Only lists sessions; never sends Agent messages.
The temporary login created by this check is revoked before exit.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
import sys
from urllib.parse import urlsplit
from aiohttp import ClientSession, CookieJar, WSMsgType


async def verify(base, password):
    parsed = urlsplit(base)
    if parsed.scheme != 'https' or parsed.path not in {'', '/'} or parsed.username:
        raise ValueError('Use an HTTPS public origin')
    base = base.rstrip('/')
    async with ClientSession(cookie_jar=CookieJar(), trust_env=False) as client:
        async with client.get(base + '/', headers={'Accept': 'text/html'}, allow_redirects=False, timeout=15) as response:
            assert response.status == 303 and urlsplit(response.headers.get('Location', '')).path == '/access'
            assert 'WWW-Authenticate' not in response.headers
        async with client.get(base + '/access', timeout=15) as response:
            page = await response.text()
            assert response.status == 200 and 'type="password"' in page
            assert response.headers.get('Referrer-Policy') == 'same-origin'
            csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
        async with client.post(base + '/access', data={'csrf': csrf, 'password': password}, headers={'Origin': base, 'Sec-Fetch-Site': 'same-origin'}, allow_redirects=False, timeout=15) as response:
            assert response.status == 303, f'Login failed HTTP {response.status}; no credentials logged'
            assert 'WWW-Authenticate' not in response.headers
        try:
            async with client.get(base + '/', timeout=20) as response:
                assert response.status == 200 and 'Codex Workspace' in await response.text()
            async with client.get(base + '/health', timeout=20) as response:
                health = await response.json()
                assert response.status == 200 and health.get('adaptorId'), 'Authenticated backend is not V2'
            async with client.get(base + '/static/workspace.js', timeout=20) as response:
                assert response.status == 200 and 'function connect()' in await response.text()
            async with client.ws_connect(base.replace('https:', 'wss:', 1) + '/ws', origin=base, timeout=20) as ws:
                await ws.send_json({'type': 'sessions.list', 'requestId': 'public-login-read-only-check'})
                snapshot = None
                for _ in range(12):
                    message = await ws.receive(timeout=25)
                    assert message.type == WSMsgType.TEXT, f'Unexpected WebSocket message {message.type}'
                    event = json.loads(message.data)
                    if event.get('type') == 'sessions.snapshot' and event.get('requestId') == 'public-login-read-only-check':
                        snapshot = event.get('payload', {})
                        assert snapshot.get('ok') is not False
                        break
                    if event.get('type') == 'command.error':
                        raise AssertionError('Read-only session listing failed')
                assert snapshot is not None, 'Session list not received'
                session_count = len(snapshot.get('sessions', []))
        finally:
            async with client.get(base + '/logout', timeout=15) as response:
                page = await response.text()
                csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
            async with client.post(base + '/logout', data={'csrf': csrf}, headers={'Origin': base, 'Sec-Fetch-Site': 'same-origin'}, allow_redirects=False, timeout=15) as response:
                assert response.status == 303
        async with client.get(base + '/health', allow_redirects=False, timeout=15) as response:
            assert response.status == 401 and 'WWW-Authenticate' not in response.headers
    print(json.dumps({'ok': True, 'loginSucceeded': True, 'pageLoaded': True, 'staticAssetsLoaded': True,
        'websocketConnected': True, 'sessionCount': session_count, 'agent': health.get('agentType'),
        'temporaryTestLoginRevoked': True, 'agentMessagesSent': 0}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='https://copyto.me:8061')
    args = parser.parse_args()
    password = json.load(sys.stdin)['password']
    asyncio.run(verify(args.url, password))


if __name__ == '__main__': main()
