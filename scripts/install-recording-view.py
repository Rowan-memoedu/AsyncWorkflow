"""Executed on the server over stdin. Change only this service and its nginx include."""
from pathlib import Path
import hashlib
import json
import os
import pwd
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

stage = Path(sys.argv[1])
expected = sys.argv[2]
assert stage.parent == Path('/tmp') and stage.name == 'recording-view-'+expected[:16]
assert not stage.is_symlink() and stage.resolve() == stage
bundle = stage/'service.tar.gz'
assert hashlib.sha256(bundle.read_bytes()).hexdigest() == expected
base = Path('/opt/recording-view')
state = Path('/var/lib/recording-view')
release = base/'releases'/expected[:16]
def run(*args):
    subprocess.run(args, check=True, timeout=300)
try:
    account = pwd.getpwnam('recording-view')
except KeyError:
    run('useradd', '--system', '--home', str(state), '--shell', '/usr/sbin/nologin', 'recording-view')
    account = pwd.getpwnam('recording-view')
state.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chown(state, account.pw_uid, account.pw_gid)
os.chmod(state, 0o700)
credentials = state/'credentials.json'
incoming = json.loads((stage/'credentials.json').read_text())
if credentials.exists():
    assert json.loads(credentials.read_text()) == incoming, 'Refuse implicit credential rotation'
else:
    credentials.write_text(json.dumps(incoming))
os.chown(credentials, account.pw_uid, account.pw_gid)
os.chmod(credentials, 0o600)
release.mkdir(parents=True, exist_ok=True)
with tarfile.open(bundle) as archive:
    archive.extractall(release, filter='data')
venv = base/'venv'
if not (venv/'bin/python').exists():
    run('python3', '-m', 'venv', str(venv))
run(str(venv/'bin/pip'), 'install', '--no-cache-dir', '--disable-pip-version-check',
    'fastapi==0.142.2', 'uvicorn==0.54.0')
service = Path('/etc/systemd/system/recording-view.service')
service.write_text('''[Unit]
Description=Private recording destinations view
After=network.target
[Service]
User=recording-view
Group=recording-view
WorkingDirectory=/opt/recording-view/app
ExecStart=/opt/recording-view/venv/bin/python -m async_workflow.mirror_server
Environment=RECORDINGS_STATE=/var/lib/recording-view
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/recording-view
UMask=0077
MemoryMax=256M
[Install]
WantedBy=multi-user.target
''')
current = base/'app'
previous = current.resolve() if current.is_symlink() else None
assert not current.exists() or current.is_symlink(), 'Unexpected application directory'
candidate = base/'app.next'
if candidate.is_symlink():
    candidate.unlink()
candidate.symlink_to(release, target_is_directory=True)
os.replace(candidate, current)
site = Path('/etc/nginx/sites-available/graspmemoedu')
before = site.read_text()
snippet = Path('/etc/nginx/snippets/recording-view.conf')
snippet_before = snippet.read_text() if snippet.exists() else None
snippet.write_text('''location = /recordings { return 302 /recordings/; }
location ^~ /recordings/ {
    client_max_body_size 32m;
    proxy_pass http://127.0.0.1:43143/;
    proxy_set_header Host 124.221.144.155;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Connection "";
    proxy_connect_timeout 5s;
    proxy_read_timeout 90s;
    gzip on;
    gzip_types application/json text/css application/javascript;
    gzip_min_length 1024;
    access_log off;
}
''')
include = '    include /etc/nginx/snippets/recording-view.conf;'
backup = base/'nginx-before.conf'
if not backup.exists():
    backup.write_text(before)
if include not in before:
    assert before.count('    listen 443 ssl default_server;') == 1
    updated = before.replace('    listen 443 ssl default_server;', '    listen 443 ssl default_server;\n'+include, 1)
    assert site.read_text() == before, 'Concurrent nginx change'
    site.write_text(updated)
try:
    run('nginx', '-t')
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'recording-view')
    run('systemctl', 'restart', 'recording-view')
    ready = False
    for _ in range(30):
        try:
            urllib.request.urlopen('http://127.0.0.1:43143/api/recordings', timeout=2)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                ready = True
                break
        except OSError:
            pass
        time.sleep(1)
    assert ready, 'Mirror failed to start'
    run('systemctl', 'reload', 'nginx')
except Exception:
    if include not in before:
        now = site.read_text()
        site.write_text(now.replace(include+'\n', '', 1))
    if snippet_before is not None:
        snippet.write_text(snippet_before)
    if previous:
        candidate.symlink_to(previous, target_is_directory=True)
        os.replace(candidate, current)
        run('systemctl', 'restart', 'recording-view')
    else:
        run('systemctl', 'disable', '--now', 'recording-view')
    raise
(base/'deployment.json').write_text(json.dumps({'bundle_sha256': expected, 'release': str(release)}))
print(json.dumps({'service': 'recording-view', 'bundle_sha256': expected, 'authenticated': True}))
# Remove only this verified staging directory; durable state and rollback remain in place.
shutil.rmtree(stage)
