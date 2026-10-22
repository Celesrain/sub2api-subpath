"""Test an image using disposable local PostgreSQL, Redis and Nginx containers."""
import http.client
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
from urllib.parse import urlsplit, urljoin
import uuid
from nginx_config import normalize_base, render_smoke

ROOT = Path(__file__).resolve().parent


def execute(command):
    return subprocess.check_output(command, stderr=subprocess.STDOUT, text=True).strip()


def request(origin, path, method='GET', body=None, token=None):
    url = urlsplit(origin)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=30)
    headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    response = conn.getresponse()
    data = response.read()
    result = response.status, dict(response.getheaders()), data
    conn.close()
    return result


def smoke(image, lock, work, *, base_path='/', keep=False):
    base_path = normalize_base(base_path)
    mount = base_path.rstrip('/')
    work.mkdir(parents=True)
    shutil.copyfile(ROOT / 'smoke/compose.yml', work / 'compose.yml')
    (work / 'nginx.conf').write_text(render_smoke(base_path), encoding='utf8', newline='\n')
    project = 's2asmoke-' + uuid.uuid4().hex[:12]
    assert project.startswith('s2asmoke-') and re.fullmatch(r'[a-z0-9-]+', project)
    credentials = {'SMOKE_DB_PASSWORD': secrets.token_hex(24),
                   'SMOKE_ADMIN_PASSWORD': secrets.token_hex(24),
                   'SMOKE_JWT_SECRET': secrets.token_hex(32),
                   'SMOKE_TOTP_KEY': secrets.token_hex(32)}
    values = {'APP_BASE_PATH': base_path, 'SMOKE_APP_IMAGE': image, 'POSTGRES_IMAGE': lock['images']['postgres:18-alpine'],
              'REDIS_IMAGE': lock['images']['redis:8-alpine'],
              'NGINX_IMAGE': lock['images']['nginx:stable-alpine'], **credentials}
    env = work / 'smoke.env'
    env.write_text(''.join(f'{key}={value}\n' for key, value in values.items()), encoding='utf8')
    env.chmod(0o600)
    compose = ['docker', 'compose', '--project-name', project, '--env-file', str(env), '-f', str(work / 'compose.yml')]
    succeeded = False
    checks = []
    try:
        execute(compose + ['up', '-d', '--wait', '--wait-timeout', '240'])
        port = execute(compose + ['port', 'proxy', '80']).splitlines()[0]
        assert port.startswith('127.0.0.1:'), port
        origin = 'http://' + port
        for suffix in ['/', '/home', '/login', '/admin/dashboard',
                       '/health?x=1&next=%2Fhome', '/setup/status', '/api/v1/settings/public', '/logo.svg']:
            path = mount + suffix
            status, headers, body = request(origin, path)
            assert status == 200, (path, status, body[:200])
            checks.append({'path': path, 'status': status})
            if headers.get('Content-Type', '').startswith('text/html'):
                assert f'<base href="{base_path}">' in body.decode(), path
                assets = re.findall(r'(?:src|href)="([^\"]*/assets/[^\"]+)"', body.decode())
                assert assets and all(x.startswith('./assets/') for x in assets), assets
                for asset in assets:
                    asset = urlsplit(urljoin(origin + base_path, asset)).path
                    assert asset.startswith(mount + '/assets/'), asset
                    asset_status, _, data = request(origin, asset)
                    assert asset_status == 200 and data and not data.startswith(b'<!doctype'), asset
        if mount:
            status, headers, _ = request(origin, mount + '?next=%2Fhome&x=1', 'POST', {})
            assert status == 308 and headers['Location'] == base_path + '?next=%2Fhome&x=1'
            checks.append({'path': mount + '?next=%2Fhome&x=1', 'method': 'POST', 'status': status})
            assert request(origin, mount + '-extra')[0] == 404
            assert request(origin, '/health')[0] == 404
        for suffix, method in [('/v1/models', 'GET'), ('/v1/messages', 'POST'), ('/api/v1/auth/me', 'GET')]:
            path = mount + suffix
            status, _, _ = request(origin, path, method, {} if method == 'POST' else None)
            assert status == 401, (path, status)
            checks.append({'path': path, 'status': status, 'auth_required': True})
        status, headers, data = request(origin, mount + '/api/v1/auth/login', 'POST',
                                        {'email': 'smoke@example.invalid', 'password': credentials['SMOKE_ADMIN_PASSWORD']})
        assert status == 200, ('isolated login failed', status)
        login = json.loads(data)
        assert login.get('code') == 0
        token = login['data']['access_token']
        assert token
        status, _, data = request(origin, mount + '/api/v1/auth/me', token=token)
        assert status == 200 and json.loads(data).get('code') == 0
        checks.append({'path': mount + '/api/v1/auth/login -> ' + mount + '/api/v1/auth/me', 'authenticated': True})
        cookie = headers.get('Set-Cookie', '')
        if cookie:
            assert 'path=' + base_path in cookie.lower()
        embedded = json.loads(execute(compose + ['exec', '-T', 'app', 'cat', '/app/s2a-build-manifest.json']))
        assert embedded['runtime_base_env'] == 'APP_BASE_PATH'
        result = {'project': project, 'origin': origin, 'url': origin + base_path, 'base_path': base_path,
                  'compose_file': str(work / 'compose.yml'), 'env_file': str(env),
                  'checks': checks, 'embedded_source_revision': embedded['upstream_revision'],
                  'isolated_database': True, 'production_data_used': False, 'kept_for_browser': keep}
        (work / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
        succeeded = True
        return result
    except Exception:
        # Only synthetic test secrets are present. Still redact them before recording logs.
        try:
            logs = execute(compose + ['logs', '--no-color', '--tail', '120'])
            for value in credentials.values():
                logs = logs.replace(value, '<redacted-test-secret>')
            (work / 'failure.log').write_text(logs, encoding='utf8')
        except Exception:
            pass
        raise
    finally:
        if not (keep and succeeded):
            execute(compose + ['down', '--volumes', '--remove-orphans'])
            env.unlink(missing_ok=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image')
    parser.add_argument('--work', type=Path)
    parser.add_argument('--keep', action='store_true')
    parser.add_argument('--base-path', default='/')
    parser.add_argument('--cleanup', type=Path, help='Path to a kept smoke result.json')
    args = parser.parse_args()
    if args.cleanup:
        path = args.cleanup.resolve()
        if not path.is_relative_to((ROOT / '.build').resolve()):
            parser.error('Cleanup is restricted to this package .build directory')
        state = json.loads(path.read_text(encoding='utf8'))
        if not re.fullmatch(r's2asmoke-[a-f0-9]{12}', state['project']):
            parser.error('Unexpected smoke project name')
        for key in ['env_file', 'compose_file']:
            if not Path(state[key]).resolve().is_relative_to(path.parent):
                parser.error('Unexpected cleanup path')
        execute(['docker', 'compose', '-p', state['project'], '--env-file', state['env_file'],
                 '-f', state['compose_file'], 'down', '--volumes', '--remove-orphans'])
        Path(state['env_file']).unlink(missing_ok=True)
        state['kept_for_browser'] = False
        path.write_text(json.dumps(state, indent=2), encoding='utf8')
        print(json.dumps({'cleaned_project': state['project']}))
    else:
        if not args.image or not args.work:
            parser.error('--image and --work are required')
        lock = json.loads((ROOT / 'upstream.lock.json').read_text(encoding='utf8'))
        print(json.dumps(smoke(args.image, lock, args.work.resolve(), base_path=args.base_path, keep=args.keep)))
