"""Build a version-pinned Sub2API image with a runtime-configurable public mount path."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from automation import build_version

ROOT = Path(__file__).resolve().parent


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'sub2api-s2a-image-builder'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def command(args, *, cwd=None, log=None):
    if log:
        with log.open('w', encoding='utf8') as output:
            result = subprocess.run(args, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, text=True)
        if result.returncode:
            tail = log.read_text(encoding='utf8', errors='replace').splitlines()[-45:]
            raise RuntimeError(f'{args[0]} failed ({result.returncode}); log: {log}\n' + '\n'.join(tail))
        return ''
    try:
        return subprocess.check_output(args, cwd=cwd, stderr=subprocess.STDOUT, text=True).strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f'{args[0]} failed ({error.returncode}):\n{error.output[-10000:]}') from error


def prepare(lock, ref, work, source_archive=None):
    if ref == lock['ref']:
        revision, commit_date = lock['revision'], lock['commit_date']
    else:
        commit = request_json(f'https://api.github.com/repos/{lock["repository"]}/commits/{ref}')
        revision, commit_date = commit['sha'], commit['commit']['committer']['date']
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Upstream did not return a full commit SHA')
    archive = work / 'source.tar.gz'
    if source_archive:
        shutil.copyfile(source_archive, archive)
    else:
        url = f'https://codeload.github.com/{lock["repository"]}/tar.gz/{revision}'
        with urllib.request.urlopen(url, timeout=120) as response, archive.open('wb') as output:
            shutil.copyfileobj(response, output)
    digest = sha256(archive)
    if ref == lock['ref'] and digest != lock['source_sha256']:
        raise ValueError('Source archive differs from the locked checksum')
    context = work / 'context'
    context.mkdir()
    with tarfile.open(archive) as source:
        expected = 'sub2api-' + revision
        for item in source.getmembers():
            parts = PurePosixPath(item.name).parts
            if not parts or parts[0] != expected or '..' in parts or item.issym() or item.islnk():
                raise ValueError('Unexpected archive path or link: ' + item.name)
            relative = parts[1:]
            if not relative:
                continue
            destination = context.joinpath(*relative)
            if not destination.resolve().is_relative_to(context.resolve()):
                raise ValueError('Archive path escapes build context')
            if item.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(item) as stream, destination.open('wb') as output:
                    shutil.copyfileobj(stream, output)
                destination.chmod(item.mode & 0o777)
            else:
                raise ValueError('Unsupported archive entry: ' + item.name)
    # Isolate patch application from any enclosing workspace Git repository.
    command(['git', 'init', '--quiet'], cwd=context)
    patches = {}
    for name in ['frontend-subpath.patch', 'runtime-base.patch', 'docker-build.patch']:
        patch = ROOT / 'patches' / name
        patches[name] = sha256(patch)
        command(['git', '-c', 'core.autocrlf=false', 'apply', '--check', '--whitespace=error', str(patch)], cwd=context)
        command(['git', '-c', 'core.autocrlf=false', 'apply', '--whitespace=error', str(patch)], cwd=context)
    manifest = {'upstream_repository': lock['repository'], 'upstream_ref': ref,
                'upstream_revision': revision, 'commit_date': commit_date,
                'source_sha256': digest, 'patches_sha256': patches,
                'patch_revision': lock['patch_revision'], 'base_path': lock['base_path'],
                'images': lock['images'], 'runtime_base_env': 'APP_BASE_PATH', 'default_base_path': '/'}
    (context / 's2a-build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf8')
    # Preserve the build recipe and identity inside the runtime image.
    dockerfile = context / 'Dockerfile'
    dockerfile.write_text(dockerfile.read_text(encoding='utf8') +
                         '\nCOPY --chown=sub2api:sub2api s2a-build-manifest.json /app/s2a-build-manifest.json\n',
                         encoding='utf8', newline='\n')
    return context, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', help='Upstream release tag, e.g. v0.2.17; default: upstream.lock.json')
    parser.add_argument('--image', help='Local image repository without a tag; every build gets a unique tag')
    parser.add_argument('--source-archive', type=Path, help='Optional locked upstream source archive')
    parser.add_argument('--keep-smoke', action='store_true', help='Keep isolated smoke containers for browser verification')
    parser.add_argument('--prepare-only', action='store_true', help='Validate source and patches without building')
    args = parser.parse_args()
    lock = json.loads((ROOT / 'upstream.lock.json').read_text(encoding='utf8'))
    ref = args.ref or lock['ref']
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:[-.][A-Za-z0-9_.-]+)?', ref):
        parser.error('--ref must be an explicit release tag; latest/main are deliberately rejected')
    version = build_version(ref, lock['patch_revision'])
    repository = args.image or 'sub2api-subpath'
    if ':' in repository.rsplit('/', 1)[-1] or '@' in repository:
        parser.error('--image is a repository, not a tag or digest')
    image = repository + ':' + version
    if not re.fullmatch(r'[a-z0-9][a-z0-9._/:-]*', image):
        parser.error('Invalid local image tag')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    work = ROOT / '.build' / stamp
    work.mkdir(parents=True)
    artifacts = ROOT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    context, manifest = prepare(lock, ref, work, args.source_archive)
    manifest.update({'local_image': image, 'platform': 'linux/amd64', 'build_version': version,
                     'build_run_id': os.environ.get('GITHUB_RUN_ID'),
                     'build_run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT')})
    result_file = artifacts / (stamp + '.json')
    result_file.write_text(json.dumps(manifest, indent=2), encoding='utf8')
    print(json.dumps({'stage': 'prepared', 'image': image, 'revision': manifest['upstream_revision'],
                      'context': str(context), 'manifest': str(result_file)}), flush=True)
    if args.prepare_only:
        return
    log = artifacts / (stamp + '.build.log')
    build = ['docker', 'buildx', 'build', '--load', '--platform', 'linux/amd64', '--progress', 'plain',
             '--metadata-file', str(artifacts / (stamp + '.buildkit.json')), '-t', image,
             '--build-arg', 'VERSION=' + version,
             '--build-arg', 'COMMIT=' + manifest['upstream_revision'],
             '--build-arg', 'DATE=' + manifest['commit_date'],
             '--build-arg', 'GOPROXY=https://proxy.golang.org,direct',
             '--build-arg', 'GOSUMDB=sum.golang.org']
    for argument, source in [('NODE_IMAGE', 'node:24-alpine'), ('GOLANG_IMAGE', 'golang:1.27.2-alpine'),
                             ('ALPINE_IMAGE', 'alpine:3.21'), ('POSTGRES_IMAGE', 'postgres:18-alpine')]:
        build += ['--build-arg', argument + '=' + lock['images'][source]]
    for key, value in {'org.opencontainers.image.source': 'https://github.com/' + lock['builder_repository'],
                       'org.opencontainers.image.revision': os.environ.get('GITHUB_SHA', manifest['upstream_revision']),
                       'org.opencontainers.image.version': version,
                       'io.suzuira.sub2api.base-path': lock['base_path'],
                       'io.suzuira.sub2api.variant': 'embedded-runtime-base',
                       'io.suzuira.sub2api.upstream-revision': manifest['upstream_revision']}.items():
        build += ['--label', key + '=' + value]
    build += [str(context)]
    print(json.dumps({'stage': 'building', 'log': str(log)}), flush=True)
    command(build, log=log)
    info = json.loads(command(['docker', 'image', 'inspect', image]))[0]
    manifest.update({'image_id': info['Id'], 'image_size_bytes': info['Size'], 'build_passed': True})
    print(json.dumps({'stage': 'built', 'image_id': info['Id'], 'size_bytes': info['Size']}), flush=True)
    # Only the isolated smoke project is touched; the server's database is never used.
    from smoke_test import smoke
    manifest['smoke'] = [smoke(image, lock, work / ('smoke-' + str(i)), base_path=base, keep=args.keep_smoke and i == len(lock['smoke_base_paths']) - 1) for i, base in enumerate(lock['smoke_base_paths'])]
    manifest['smoke_passed'] = True
    result_file.write_text(json.dumps(manifest, indent=2), encoding='utf8')
    (artifacts / 'latest-success.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
    print(json.dumps({'stage': 'verified', 'image': image, 'image_id': info['Id'],
                      'report': str(result_file), 'smoke': manifest['smoke']}), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

