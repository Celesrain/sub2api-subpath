"""Render the public Nginx mount from the same APP_BASE_PATH value as the app."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def base_from_env_file(env_file):
    """Use Compose's own dotenv/interpolation rules, matching the app deployment."""
    env_file = Path(env_file).resolve()
    if not env_file.is_file():
        raise ValueError('Environment file does not exist: ' + str(env_file))
    # Resolve only this public setting; other .env values never enter the output.
    model = '''services:
  route-config:
    image: scratch
    environment:
      APP_BASE_PATH: ${APP_BASE_PATH:?Set APP_BASE_PATH in the environment file}
'''
    result = subprocess.run(['docker', 'compose', '--project-name', 'sub2api-route-config',
                             '--env-file', str(env_file), '-f', '-', 'config', '--format', 'json'],
                            input=model, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError('Cannot resolve APP_BASE_PATH from the environment file with Docker Compose')
    value = json.loads(result.stdout)['services']['route-config']['environment']['APP_BASE_PATH']
    return normalize_base(value)


def normalize_base(value):
    value = value.strip()
    if value in ('', '/'):
        return '/'
    if not value.startswith('/'):
        raise ValueError('Base path must start with /')
    value = value.removesuffix('/')
    if not all(re.fullmatch(r'[A-Za-z0-9_~-][A-Za-z0-9._~-]*', part) for part in value[1:].split('/')):
        raise ValueError('Base path must contain URL-safe segments; URLs, empty and dot segments are not allowed')
    return value + '/'


def render_locations(base_path, upstream='http://127.0.0.1:20080', upgrade_var='s2a_connection_upgrade'):
    base = normalize_base(base_path)
    if not re.fullmatch(r'http://(?:127\.0\.0\.1|app):\d+', upstream):
        raise ValueError('Unexpected upstream address')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', upgrade_var):
        raise ValueError('Unexpected WebSocket map name')
    prefix = base.rstrip('/')
    redirect = '' if not prefix else f'''location = {prefix} {{
    absolute_redirect off;
    return 308 {base}$is_args$args;
}}
'''
    extra = '' if not prefix else f'''    proxy_redirect ~^(/(?!{re.escape(prefix[1:])}(?:/|$)).*)$ {prefix}$1;
    proxy_cookie_path / {base};
'''
    return redirect + f'''location ^~ {base} {{
    proxy_pass {upstream}/;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Prefix {prefix or '/'};
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection ${upgrade_var};
    proxy_connect_timeout 5s;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    proxy_buffering off;
    proxy_request_buffering off;
    client_max_body_size 256m;
    proxy_redirect default;
{extra}}}
'''


def render_smoke(base_path):
    base = normalize_base(base_path)
    fallback = '' if base == '/' else 'location / { return 404; }'
    return '''map $http_upgrade $app_connection_upgrade { default upgrade; "" close; }
server {
    listen 80;
    server_name _;
''' + render_locations(base, 'http://app:8080', 'app_connection_upgrade') + fallback + '\n}\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path(__file__).with_name('.env'))
    parser.add_argument('--base-path', help='Explicit override; otherwise read the environment file')
    args = parser.parse_args()
    try:
        base_path = args.base_path if args.base_path is not None else base_from_env_file(args.env_file)
        print(render_locations(base_path), end='')
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
