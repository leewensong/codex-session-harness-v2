"""Rotate the public V2 htpasswd atomically; read the requested password from stdin.
Run on the ingress host as root. Does not change Nginx, V1 or session files.
"""
from __future__ import annotations
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import warnings


def rotate(password, path):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        import crypt
    if not isinstance(password, str) or not 8 <= len(password) <= 1024 or any(ord(c) < 32 for c in password):
        raise ValueError('Password must contain 8 to 1024 printable characters')
    path = Path(path)
    original = path.read_bytes()
    info = path.stat()
    rows = original.decode('utf-8').splitlines()
    users = [row.split(':', 1)[0] for row in rows if row and not row.startswith('#') and ':' in row]
    if len(users) != 1 or not users[0]:
        raise ValueError('Expected exactly one V2 account; refusing ambiguous rotation')
    hashed = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    if not hashed or not hashed.startswith('$6$') or not hmac.compare_digest(crypt.crypt(password, hashed) or '', hashed):
        raise RuntimeError('System crypt did not produce a verifiable SHA-512 password hash')
    backups = Path('/var/backups/codex-v2-edge') / ('password-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backups.mkdir(mode=0o700, parents=True)
    backup = backups / 'htpasswd'
    backup.write_bytes(original)
    backup.chmod(0o600)
    fd, temporary = tempfile.mkstemp(prefix='.v2-password-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write((users[0] + ':' + hashed + '\n').encode('utf-8'))
            handle.flush(); os.fsync(handle.fileno())
        os.chown(temporary, info.st_uid, info.st_gid)
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    print(json.dumps({'passwordUpdated': True, 'passwordVerified': True,
        'credentialModePreserved': oct(stat.S_IMODE(path.stat().st_mode)), 'backup': str(backup),
        'priorWebSessionsInvalidated': hashlib.sha256(original).digest() != hashlib.sha256(path.read_bytes()).digest()}))


if __name__ == '__main__':
    payload = json.loads(sys.stdin.read())
    rotate(payload['password'], '/etc/nginx/.htpasswd-codex-v2')
