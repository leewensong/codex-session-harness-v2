"""Install the edge gate without changing passwords or exposing an unprotected site.
Run on the ingress host as root after the stage tests pass.
"""
import datetime
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import urllib.error

stage = Path(__file__).resolve().parent
source = stage / 'deploy' / 'edge-auth'
config = Path('/etc/nginx/sites-available/condexmobile-v2')
credential = Path('/etc/nginx/.htpasswd-codex-v2')
unit = Path('/etc/systemd/system/codex-v2-auth.service')
app_dir = Path('/opt/codex-v2-auth')
backup = Path('/var/backups/codex-v2-edge') / datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
backup.mkdir(parents=True, mode=0o700)
shutil.copy2(config, backup / 'nginx.conf')
if unit.exists(): shutil.copy2(unit, backup / 'codex-v2-auth.service')
if app_dir.exists(): shutil.copytree(app_dir, backup / 'app')
password_digest = hashlib.sha256(credential.read_bytes()).hexdigest()

def run(*args): subprocess.run(args, check=True)

app_dir.mkdir(mode=0o755, exist_ok=True)
shutil.copyfile(source / 'auth_server.py', app_dir / 'auth_server.py')
(app_dir / 'auth_server.py').chmod(0o644)
shutil.copyfile(source / 'codex-v2-auth.service', unit)
unit.chmod(0o644)
run('systemctl', 'daemon-reload')
run('systemctl', 'enable', '--now', 'codex-v2-auth.service')
run('systemctl', 'restart', 'codex-v2-auth.service')
import time
for _ in range(30):
    try:
        response = urllib.request.urlopen('http://127.0.0.1:18062/access', timeout=2)
        assert response.status == 200 and b'type="password"' in response.read()
        break
    except Exception:
        time.sleep(.2)
else:
    raise RuntimeError('auth daemon failed; existing Nginx Basic Auth remains active')
try:
    urllib.request.urlopen('http://127.0.0.1:18062/verify', timeout=2)
except urllib.error.HTTPError as response:
    assert response.code == 401 and not response.headers.get('WWW-Authenticate')
else:
    raise RuntimeError('unauthenticated verification did not fail closed')

shutil.copyfile(source / 'condexmobile-v2.nginx.conf', config)
try:
    run('nginx', '-t')
    run('systemctl', 'reload', 'nginx')
except Exception:
    shutil.copyfile(backup / 'nginx.conf', config)
    run('nginx', '-t')
    run('systemctl', 'reload', 'nginx')
    raise
assert hashlib.sha256(credential.read_bytes()).hexdigest() == password_digest, 'password file unexpectedly changed'
print('Installed edge form authentication; existing credential file unchanged.')
print('Rollback configuration backup:', backup)
