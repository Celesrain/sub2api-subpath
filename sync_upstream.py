"""Resolve the latest stable release into a local lock; promotion follows successful publishing."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request
from automation import GitHub, STABLE_REF

ROOT = Path(__file__).resolve().parent


def stable_version(ref):
    match = STABLE_REF.fullmatch(ref)
    if not match:
        raise ValueError('Automatic updates accept stable vMAJOR.MINOR.PATCH tags only')
    return tuple(map(int, match.groups()))


def source_digest(repository, revision):
    digest = hashlib.sha256()
    with urllib.request.urlopen('https://codeload.github.com/' + repository + '/tar.gz/' + revision, timeout=120) as response:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def resolve(lock, github, digest_source=source_digest):
    repository = lock['repository']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid upstream repository')
    release = github.request('/repos/' + repository + '/releases/latest')
    if release.get('draft') or release.get('prerelease'):
        raise ValueError('Upstream latest endpoint returned a draft or prerelease')
    ref = release['tag_name']
    current, latest = stable_version(lock['ref']), stable_version(ref)
    if latest < current:
        raise ValueError('Refusing to downgrade the locked upstream release')
    if latest == current:
        return copy.deepcopy(lock), False
    commit = github.request('/repos/' + repository + '/commits/' + ref)
    revision = commit['sha']
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Upstream commit is not a full SHA')
    digest = digest_source(repository, revision)
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('Invalid source archive checksum')
    result = copy.deepcopy(lock)
    result.update(ref=ref, revision=revision, commit_date=commit['commit']['committer']['date'], source_sha256=digest)
    return result, True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='GitHub Actions output file')
    args = parser.parse_args()
    path = ROOT / 'upstream.lock.json'
    before = path.read_bytes()
    lock, changed = resolve(json.loads(before), GitHub())
    if changed:
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(lock, indent=2) + '\n', encoding='utf8', newline='\n')
        os.replace(temporary, path)
    if args.output:
        with args.output.open('a', encoding='utf8') as stream:
            stream.write('changed=' + str(changed).lower() + '\n')
            stream.write('base_lock_sha256=' + hashlib.sha256(before).hexdigest() + '\n')
    print(json.dumps({'upstream_ref': lock['ref'], 'upstream_revision': lock['revision'], 'changed': changed}))


if __name__ == '__main__':
    main()
