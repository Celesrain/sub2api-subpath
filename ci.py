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
from automation import GitHub, Registry, build_version, require_new_publication, go_image, STABLE_REF
from sync_upstream import stable_version

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
    version = result['build_version']
    assert result['local_image'].rsplit(':', 1)[1] == version
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        assert version == build_version(result['upstream_ref'], lock['patch_revision'])
    lock_bytes = (ROOT / 'upstream.lock.json').read_bytes()
    (DIST / 'upstream.lock.json').write_bytes(lock_bytes)
    record = {'image_reference': result['local_image'], 'image_id': result['image_id'], 'version': version,
              'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
              'source_repository': lock['builder_repository'], 'source_revision': os.environ.get('GITHUB_SHA', 'local'),
              'upstream_ref': result['upstream_ref'], 'upstream_revision': result['upstream_revision'],
              'source_sha256': result['source_sha256'], 'patch_revision': result['patch_revision'],
              'images': result['images'],
              'build_run_id': result['build_run_id'], 'build_run_attempt': result['build_run_attempt'],
              'lock_sha256': hashlib.sha256(lock_bytes).hexdigest(),
              'lock_changed': os.environ.get('UPSTREAM_LOCK_CHANGED') == 'true',
              'base_lock_sha256': os.environ.get('BASE_LOCK_SHA256') or hashlib.sha256(lock_bytes).hexdigest(),
              'patches_sha256': result['patches_sha256'], 'platform': result['platform'],
              'tested_base_paths': lock['smoke_base_paths'], 'checks_passed': True}
    (DIST / 'build.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf8')
    summary('Built and verified `' + version + '` for `linux/amd64`. Runtime bases: `/`, `/s2a/`, `/tools/ai/`.')
    print(json.dumps(record))


def may_advance_latest(record, github):
    """Only a newer verified stable build from main may move the latest pointers."""
    if os.environ.get('GITHUB_REF') != 'refs/heads/main' or not STABLE_REF.fullmatch(record['upstream_ref']):
        return False
    locked = json.loads((ROOT / 'upstream.lock.json').read_text(encoding='utf8'))
    if stable_version(record['upstream_ref']) < stable_version(locked['ref']):
        return False
    latest = github.request('/repos/' + os.environ['GITHUB_REPOSITORY'] + '/releases/latest', missing_ok=True)
    if latest is None:
        return True
    match = re.search(r'```json\s*\n([\s\S]*?)\n```', latest.get('body', ''))
    if not match:
        raise ValueError('Latest release has no verifiable build provenance; refusing to replace its pointer')
    previous = json.loads(match[1])
    def rank(item):
        return (stable_version(item['upstream_ref']), int(item['patch_revision']),
                int(item['build_run_id']), int(item['build_run_attempt']))
    return rank(record) > rank(previous)


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
    assert re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,127}', record['version'])
    assert re.fullmatch(r'[a-z0-9][a-z0-9._/:-]*', record['image_reference'])
    assert record['build_run_id'] == os.environ['GITHUB_RUN_ID']
    assert 1 <= int(record['build_run_attempt']) <= int(os.environ['GITHUB_RUN_ATTEMPT'])
    identity_env = {'GITHUB_ACTIONS': 'true', 'GITHUB_RUN_ID': record['build_run_id'],
                    'GITHUB_RUN_ATTEMPT': record['build_run_attempt']}
    assert record['version'] == build_version(record['upstream_ref'], record['patch_revision'], identity_env)
    assert hashlib.sha256((DIST / 'upstream.lock.json').read_bytes()).hexdigest() == record['lock_sha256']
    github = GitHub()
    registry = Registry(repository, os.environ['GITHUB_ACTOR'], os.environ['GH_TOKEN'])
    require_new_publication(registry, github, repository, record['version'])
    run(['docker', 'load', '--input', str(archive)])
    actual = json.loads(subprocess.check_output(['docker', 'image', 'inspect', record['image_reference']]))[0]
    assert actual['Id'] == record['image_id'], 'Loaded image differs from tested image'
    reference = 'ghcr.io/' + repository + ':' + record['version']
    run(['docker', 'tag', record['image_reference'], reference])
    run(['docker', 'push', reference])
    digest = registry.manifest_digest(record['version'])
    assert digest is not None, 'Published image was not found in the registry'
    advance_latest = may_advance_latest(record, github)
    receipt = {**record, 'published_image': reference, 'manifest_digest': digest,
               'image_by_digest': 'ghcr.io/' + repository + '@' + digest,
               'workflow_run': 'https://github.com/' + repository + '/actions/runs/' + record['build_run_id']}
    release = github.request('/repos/' + repository + '/releases', data={
        'tag_name': 'build-' + record['version'], 'target_commitish': revision,
        'name': record['version'], 'draft': False, 'prerelease': '-' in record['upstream_ref'],
        'make_latest': 'false',
        'body': 'Verified image: `' + reference + '`\n\nPinned image: `' + receipt['image_by_digest'] +
                '`\n\nBuild provenance:\n```json\n' + json.dumps(receipt, indent=2) + '\n```',
    })
    receipt['release_url'] = release['html_url']
    receipt['latest_promoted'] = False
    if advance_latest:
        alias = 'ghcr.io/' + repository + ':latest'
        run(['docker', 'tag', record['image_reference'], alias])
        run(['docker', 'push', alias])
        assert registry.manifest_digest('latest') == digest, 'Latest differs from the verified version'
        github.request('/repos/' + repository + '/releases/' + str(release['id']),
                       data={'make_latest': 'true'}, method='PATCH')
        receipt['latest_promoted'] = True
    (DIST / 'published.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf8')
    summary('Published a new build without overwriting version tags:\n\n- `' + reference +
            '`\n- `' + receipt['image_by_digest'] + '`\n- ' + receipt['release_url'])
    if receipt['latest_promoted']:
        summary('Updated the GHCR `latest` alias and GitHub Latest release to this verified build.')
    print(json.dumps({'published': reference, 'digest': digest, 'release': receipt['release_url']}))


def promote_lock():
    """Advance only the lock file after successful build, tests, and publication."""
    receipt = json.loads((DIST / 'published.json').read_text(encoding='utf8'))
    if not receipt['lock_changed']:
        return
    if os.environ.get('GITHUB_REF') != 'refs/heads/main':
        summary('Lock promotion skipped outside main.')
        return
    before_bytes = (ROOT / 'upstream.lock.json').read_bytes()
    candidate_bytes = (DIST / 'upstream.lock.json').read_bytes()
    assert hashlib.sha256(before_bytes).hexdigest() == receipt['base_lock_sha256']
    assert hashlib.sha256(candidate_bytes).hexdigest() == receipt['lock_sha256']
    before, candidate = json.loads(before_bytes), json.loads(candidate_bytes)
    mutable = {'ref', 'revision', 'commit_date', 'source_sha256', 'images'}
    assert {k: v for k, v in before.items() if k not in mutable} == {k: v for k, v in candidate.items() if k not in mutable}
    old_go, old_pin, old_version = go_image(before['images'])
    new_go, new_pin, new_version = go_image(candidate['images'])
    assert {k: v for k, v in before['images'].items() if k != old_go} == {k: v for k, v in candidate['images'].items() if k != new_go}
    assert (new_go, new_pin) == (old_go, old_pin) or new_version > old_version
    assert candidate['images'] == receipt['images']
    assert stable_version(candidate['ref']) > stable_version(before['ref'])
    assert candidate['revision'] == receipt['upstream_revision']
    assert candidate['ref'] == receipt['upstream_ref']
    assert candidate['source_sha256'] == receipt['source_sha256']
    repository = os.environ['GITHUB_REPOSITORY']
    base = os.environ['GITHUB_SHA']
    github = GitHub()
    head = github.request('/repos/' + repository + '/git/ref/heads/main')['object']['sha']
    if head != base:
        summary('Main changed during the build. The verified image is retained; lock promotion waits for the next check.')
        return
    original = github.request('/repos/' + repository + '/git/commits/' + base)
    blob = github.request('/repos/' + repository + '/git/blobs', data={'content': candidate_bytes.decode('utf8'), 'encoding': 'utf-8'})
    tree = github.request('/repos/' + repository + '/git/trees', data={
        'base_tree': original['tree']['sha'],
        'tree': [{'path': 'upstream.lock.json', 'mode': '100644', 'type': 'blob', 'sha': blob['sha']}],
    })
    commit = github.request('/repos/' + repository + '/git/commits', data={
        'message': 'Lock verified upstream ' + candidate['ref'] + ' after ' + receipt['version'],
        'tree': tree['sha'], 'parents': [base],
    })
    github.request('/repos/' + repository + '/git/refs/heads/main', data={'sha': commit['sha'], 'force': False}, method='PATCH')
    summary('Advanced upstream.lock.json to verified `' + candidate['ref'] + '`.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build', 'export', 'publish', 'promote-lock'])
    args = parser.parse_args()
    {'build': build, 'export': export, 'publish': publish, 'promote-lock': promote_lock}[args.command]()

