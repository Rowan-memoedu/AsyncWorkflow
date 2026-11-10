"""Deploy the independent recording mirror through the existing verified SSH route."""
from pathlib import Path
import hashlib
import json
import secrets
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path('D:/Artifacts/AsyncWorkflow/recording-view-20261101')
PRIVATE = Path('D:/AppData/RecordingView')
URL = 'https://124.221.144.155/recordings/'
HOST = 'ubuntu@124.221.144.155'
OPTIONS = ['-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15']


def run(command, **kwargs):
    return subprocess.run(command, check=True, timeout=600, **kwargs)


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    credentials_path = PRIVATE/'access.json'
    if not credentials_path.exists():
        credentials_path.write_text(json.dumps({'url': URL, 'desktop': secrets.token_urlsafe(32),
            'phone': secrets.token_urlsafe(32), 'publisher': secrets.token_urlsafe(32)}, indent=2), encoding='utf-8')
    access = json.loads(credentials_path.read_text(encoding='utf-8'))
    config = {'readers': {k: hashlib.sha256(access[k].encode()).hexdigest() for k in ('desktop', 'phone')},
              'publisher_hash': hashlib.sha256(access['publisher'].encode()).hexdigest()}
    (PRIVATE/'server-credentials.json').write_text(json.dumps(config), encoding='utf-8')
    (PRIVATE/'phone-connection.json').write_text(json.dumps({'url': URL, 'username': 'phone', 'password': access['phone']}), encoding='utf-8')
    (Path('D:/AppData/AsyncWorkflow')/'mirror-connection.json').write_text(json.dumps(
        {'url': URL, 'publisher_token': access['publisher']}), encoding='utf-8')
    bundle = ARTIFACTS/'service.tar.gz'
    names = ['__init__.py', 'mirror.py', 'mirror_server.py', 'web/index.html', 'web/app.js', 'web/app.css', 'web/tokens.css']
    with tarfile.open(bundle, 'w:gz') as archive:
        for name in names:
            archive.add(ROOT/'src'/'async_workflow'/name, arcname='async_workflow/'+name)
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    stage = '/tmp/recording-view-' + digest[:16]
    run(['ssh', *OPTIONS, HOST, 'mkdir -p ' + stage])
    run(['scp', *OPTIONS, str(bundle), HOST+':'+stage+'/service.tar.gz'])
    run(['scp', *OPTIONS, str(PRIVATE/'server-credentials.json'), HOST+':'+stage+'/credentials.json'])
    installer = (ROOT/'scripts'/'install-recording-view.py').read_text(encoding='utf-8')
    result = run(['ssh', *OPTIONS, HOST, 'sudo -n python3 - '+stage+' '+digest],
                 input=installer.encode('utf-8'), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (ARTIFACTS/'deployment.log').write_bytes(result.stdout)
    print(result.stdout.decode('utf-8', errors='replace'))
    print('Deployed private recording view; credential material remains outside Git.')


if __name__ == '__main__':
    main()
