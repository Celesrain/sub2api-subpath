"""Build identities and narrowly scoped GitHub/registry clients."""
import base64
from datetime import datetime, timezone
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

RELEASE_REF = re.compile(r'v(\d+)\.(\d+)\.(\d+)(?:[-.][A-Za-z0-9_.-]+)?')
STABLE_REF = re.compile(r'v(\d+)\.(\d+)\.(\d+)')
MANIFEST_TYPES = ', '.join([
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.docker.distribution.manifest.v2+json',
])


def build_version(ref, patch_revision, environ=None):
    env = os.environ if environ is None else environ
    if not RELEASE_REF.fullmatch(ref) or type(patch_revision) is not int or patch_revision < 1:
        raise ValueError('Invalid upstream ref or patch revision')
    if env.get('GITHUB_ACTIONS') == 'true':
        run_id, attempt = env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_RUN_ATTEMPT', '')
        if not all(re.fullmatch(r'[1-9][0-9]*', value) for value in (run_id, attempt)):
            raise ValueError('A GitHub build requires its run ID and attempt')
        suffix = 'run.' + run_id + '.' + attempt
    else:
        suffix = 'local.' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ').lower() + '.' + uuid.uuid4().hex[:8]
    version = ref[1:] + '-subpath.' + str(patch_revision) + '-' + suffix
    if len(version) > 128 or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', version):
        raise ValueError('Generated image version exceeds Docker tag constraints')
    return version


def go_requirement(text):
    requirements = re.findall(r'(?m)^(?:go\s+|toolchain\s+go)(\d+)\.(\d+)(?:\.(\d+))?\s*$', text)
    if not requirements:
        raise ValueError('No supported Go version requirement found')
    return max(tuple(int(part or '0') for part in row) for row in requirements)


def go_image(images):
    entries = [(key, value) for key, value in images.items() if key.startswith('golang:')]
    if len(entries) != 1:
        raise ValueError('Exactly one pinned Go image is required')
    key, value = entries[0]
    match = re.fullmatch(r'golang:(\d+)\.(\d+)\.(\d+)-alpine', key)
    if not match or not re.fullmatch(re.escape(key) + r'@sha256:[a-f0-9]{64}', value):
        raise ValueError('Go image must be versioned and pinned by SHA256')
    return key, value, tuple(map(int, match.groups()))


def docker_go_pin(version):
    """Resolve only the official library/golang image, never a user-provided registry."""
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid compiler version')
    with urllib.request.urlopen('https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/golang:pull', timeout=60) as response:
        token = json.load(response)['token']
    request = urllib.request.Request('https://registry-1.docker.io/v2/library/golang/manifests/' + version + '-alpine',
                                     headers={'Authorization': 'Bearer ' + token, 'Accept': MANIFEST_TYPES}, method='HEAD')
    with urllib.request.urlopen(request, timeout=60) as response:
        digest = response.headers.get('Docker-Content-Digest', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise ValueError('Official compiler image digest is missing')
    return 'golang:' + version + '-alpine@' + digest


class GitHub:
    def __init__(self, token=None):
        self.token = token if token is not None else os.environ.get('GH_TOKEN', '')

    def request(self, path, data=None, method=None, missing_ok=False):
        if not path.startswith('/repos/'):
            raise ValueError('Only repository endpoints are supported')
        headers = {'User-Agent': 'sub2api-subpath-builder', 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request('https://api.github.com' + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            raise RuntimeError('GitHub request failed with HTTP ' + str(error.code)) from error


class Registry:
    def __init__(self, repository, actor, token):
        if not re.fullmatch(r'[a-z0-9_.-]+/[a-z0-9_.-]+', repository) or not actor or not token:
            raise ValueError('Missing or invalid registry credentials/repository')
        self.repository = repository
        credentials = base64.b64encode((actor + ':' + token).encode()).decode()
        query = urllib.parse.urlencode({'service': 'ghcr.io', 'scope': 'repository:' + repository + ':pull,push'})
        request = urllib.request.Request('https://ghcr.io/token?' + query,
                                         headers={'Authorization': 'Basic ' + credentials})
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
        self.token = data.get('token') or data['access_token']

    def manifest_digest(self, tag):
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag):
            raise ValueError('Invalid registry tag')
        request = urllib.request.Request('https://ghcr.io/v2/' + self.repository + '/manifests/' + tag,
                                         headers={'Authorization': 'Bearer ' + self.token, 'Accept': MANIFEST_TYPES},
                                         method='HEAD')
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                digest = response.headers.get('Docker-Content-Digest', '')
                if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
                    raise ValueError('Registry did not return a valid manifest digest')
                return digest
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise RuntimeError('Registry check failed with HTTP ' + str(error.code)) from error


def require_new_publication(registry, github, repository, version):
    if registry.manifest_digest(version) is not None:
        raise ValueError('Refusing to overwrite an existing image version: ' + version)
    if github.request('/repos/' + repository + '/releases/tags/build-' + version, missing_ok=True) is not None:
        raise ValueError('Refusing to overwrite an existing build release: ' + version)
