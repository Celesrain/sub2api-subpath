"""Build, test, export and publish only verified Sub2API images."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
DIST = ROOT / 'dist'


def run(args):
    subprocess.run(args, cwd=ROOT, check=True)


def summary(text):
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf8') as stream:
            stream.write(text + '\n')


def build():
    command = [sys.executable, str(ROOT / 'build.py')]
    ref = os.environ.get('UPSTREAM_REF', '').strip()
    if ref:
        command += ['--ref', ref]
    run(command)


def export():
    result = json.loads((ROOT / '.artifacts/latest-success.json').read_text(encoding='utf8'))
    lock = json.loads((ROOT / 'upstream.lock.json').read_text(encoding='utf8'))
    assert result['build_passed'] and result['smoke_passed']
    assert [item['base_path'] for item in result['smoke']] == lock['smoke_base_paths']
    assert all(not Path(item['env_file']).exists() for item in result['smoke']), 'Synthetic credentials were not cleaned'
    actual = json.loads(subprocess.check_output(['docker', 'image', 'inspect', result['local_image']]))[0]
    assert actual['Id'] == result['image_id']
    DIST.mkdir(exist_ok=True)
    archive = DIST / 'image.tar.gz'
    process = subprocess.Popen(['docker', 'save', result['local_image']], stdout=subprocess.PIPE)
    try:
        with archive.open('wb') as target, gzip.GzipFile(fileobj=target, mode='wb', mtime=0) as stream:
            shutil.copyfileobj(process.stdout, stream)
    finally:
        process.stdout.close()
    assert process.wait() == 0, 'Image export failed'
    version = result['local_image'].rsplit(':', 1)[1]
    record = {'image_reference': result['local_image'], 'image_id': result['image_id'], 'version': version,
              'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
              'source_repository': lock['builder_repository'], 'source_revision': os.environ.get('GITHUB_SHA', 'local'),
              'upstream_ref': result['upstream_ref'], 'upstream_revision': result['upstream_revision'],
              'patches_sha256': result['patches_sha256'], 'platform': result['platform'],
              'tested_base_paths': lock['smoke_base_paths'], 'checks_passed': True}
    (DIST / 'build.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf8')
    summary('Built and verified `' + version + '` for `linux/amd64`. Runtime bases: `/`, `/s2a/`, `/tools/ai/`.')
    print(json.dumps(record))


def publish():
    record = json.loads((DIST / 'build.json').read_text(encoding='utf8'))
    archive = DIST / 'image.tar.gz'
    assert record['checks_passed']
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == record['archive_sha256'], 'Artifact digest mismatch'
    repository = os.environ['GITHUB_REPOSITORY'].lower()
    assert re.fullmatch(r'[a-z0-9_.-]+/[a-z0-9_.-]+', repository)
    revision = os.environ['GITHUB_SHA']
    assert re.fullmatch(r'[a-f0-9]{40}', revision) and revision == record['source_revision']
    assert record['source_repository'].lower() == repository
    assert re.fullmatch(r'[a-z0-9][a-z0-9._-]*', record['version'])
    assert re.fullmatch(r'[a-z0-9][a-z0-9._/:-]*', record['image_reference'])
    run(['docker', 'load', '--input', str(archive)])
    actual = json.loads(subprocess.check_output(['docker', 'image', 'inspect', record['image_reference']]))[0]
    assert actual['Id'] == record['image_id'], 'Loaded image differs from tested image'
    references = ['ghcr.io/' + repository + ':' + tag for tag in [record['version'], 'sha-' + revision[:12], 'latest']]
    for reference in references:
        run(['docker', 'tag', record['image_reference'], reference])
        run(['docker', 'push', reference])
    summary('Published the tested image:\n\n' + '\n'.join('- `' + value + '`' for value in references))
    print(json.dumps({'published': references}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'export', 'publish'])
    args = parser.parse_args()
    {'build': build, 'export': export, 'publish': publish}[args.command]()
