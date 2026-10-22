"""Allow only reviewed source files in Git; deployment data must stay local."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent
ALLOWED = {
    '.env.template', '.gitattributes', '.gitignore', '.github/workflows/build.yml',
    'LICENSE', 'NOTICE', 'build.py', 'ci.py', 'check_public_tree.py', 'compose.yml',
    'nginx_config.py', 'smoke_test.py', 'test_nginx_env.py', 'upstream.lock.json',
    'patches/frontend-subpath.patch', 'patches/runtime-base.patch', 'patches/docker-build.patch',
    'smoke/compose.yml',
}


def main():
    data = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    paths = set(data.decode().split('\0')) - {''}
    unexpected = paths - ALLOWED
    if unexpected:
        raise SystemExit('Unapproved upload paths: ' + ', '.join(sorted(unexpected)))
    for name in sorted(paths):
        text = (ROOT / name).read_text(encoding='utf8')
        if re.search(r'-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----', text):
            raise SystemExit('Private key material found: ' + name)
        if re.search(r'gh[pousr]_[A-Za-z0-9]{30,}', text):
            raise SystemExit('GitHub credential pattern found: ' + name)
    template = (ROOT / '.env.template').read_text(encoding='utf8')
    for line in template.splitlines():
        match = re.match(r'^([A-Z][A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|KEY|CLIENT_ID))=(.*)$', line)
        if match and match[2] and not match[2].startswith('REPLACE_WITH_'):
            raise SystemExit('Credential template contains a non-placeholder value: ' + match[1])
    print('Public-source allowlist passed: ' + str(len(paths)) + ' files; no README or live environment file.')


if __name__ == '__main__':
    main()
