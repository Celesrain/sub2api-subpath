"""Regression tests for upstream resolution, retained versions and publish boundaries."""
import copy
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from automation import GitHub, Registry, build_version, require_new_publication, go_image, go_requirement
from sync_upstream import resolve
import ci


ENV = {'GITHUB_ACTIONS': 'true', 'GITHUB_RUN_ID': '123456789', 'GITHUB_RUN_ATTEMPT': '1',
       'GITHUB_REPOSITORY': 'Celesrain/sub2api-subpath', 'GITHUB_SHA': 'a' * 40,
       'GITHUB_ACTOR': 'test-actor', 'GH_TOKEN': 'synthetic-test-token', 'GITHUB_REF': 'refs/heads/main'}
LOCK = {'repository': 'Wei-Shaw/sub2api', 'ref': 'v0.2.17', 'revision': '1' * 40,
        'commit_date': '2026-10-16T04:28:11Z', 'source_sha256': '2' * 64,
        'patch_revision': 2, 'images': {'example': 'sha256:pinned',
        'golang:1.27.2-alpine': 'golang:1.27.2-alpine@sha256:' + '5' * 64}}


class BuildIdentityTests(unittest.TestCase):
    def test_runs_and_attempts_get_distinct_versions(self):
        first = build_version('v0.2.18', 2, ENV)
        self.assertEqual(first, '0.2.18-subpath.2-run.123456789.1')
        self.assertNotEqual(first, build_version('v0.2.18', 2, {**ENV, 'GITHUB_RUN_ATTEMPT': '2'}))
        self.assertNotEqual(first, build_version('v0.2.18', 2, {**ENV, 'GITHUB_RUN_ID': '123456790'}))

    def test_local_rebuilds_are_unique(self):
        first = build_version('v0.2.18', 2, {})
        self.assertNotEqual(first, build_version('v0.2.18', 2, {}))
        self.assertRegex(first, r'^[a-z0-9][a-z0-9._-]*$')

    def test_incomplete_or_invalid_identity_is_rejected(self):
        for env in [{'GITHUB_ACTIONS': 'true'}, {**ENV, 'GITHUB_RUN_ATTEMPT': '0'}, {**ENV, 'GITHUB_RUN_ID': '../old'}]:
            with self.assertRaises(ValueError):
                build_version('v0.2.18', 2, env)
        for ref, revision in [('main', 2), ('v0.2.18/evil', 2), ('v0.2.18', 0), ('v0.2.18', True)]:
            with self.assertRaises(ValueError):
                build_version(ref, revision, ENV)


class SyncTests(unittest.TestCase):
    def test_no_new_release_keeps_lock_and_avoids_download(self):
        github, digest = Mock(), Mock()
        github.request.return_value = {'tag_name': 'v0.2.17', 'draft': False, 'prerelease': False}
        result, changed = resolve(LOCK, github, digest)
        self.assertFalse(changed)
        self.assertEqual(result, LOCK)
        digest.assert_not_called()

    def test_new_release_pins_source_without_changing_recipe(self):
        github, digest = Mock(), Mock(return_value='4' * 64)
        github.request.side_effect = [
            {'tag_name': 'v0.2.18', 'draft': False, 'prerelease': False},
            {'sha': '3' * 40, 'commit': {'committer': {'date': '2026-10-23T07:00:00Z'}}},
            {'encoding': 'base64', 'content': base64.b64encode(b'module example\ngo 1.27.2\n').decode()},
        ]
        before = copy.deepcopy(LOCK)
        result, changed = resolve(LOCK, github, digest)
        self.assertTrue(changed)
        self.assertEqual(result['revision'], '3' * 40)
        self.assertEqual(result['source_sha256'], '4' * 64)
        self.assertEqual(result['images'], LOCK['images'])
        self.assertEqual(LOCK, before)
        digest.assert_called_once_with('Wei-Shaw/sub2api', '3' * 40)

    def test_downgrades_prereleases_and_malformed_versions_stop(self):
        for release in [{'tag_name': 'v0.2.16'}, {'tag_name': 'v0.2.18-rc1'},
                        {'tag_name': 'v0.2.18', 'prerelease': True}, {'tag_name': 'v0.2.18', 'draft': True}]:
            github = Mock()
            github.request.return_value = release
            with self.assertRaises(ValueError):
                resolve(LOCK, github, Mock())

    def test_invalid_commit_cannot_be_locked(self):
        github = Mock()
        github.request.side_effect = [{'tag_name': 'v0.2.18'}, {'sha': 'short'}]
        with self.assertRaises(ValueError):
            resolve(LOCK, github, Mock())

    def test_new_go_requirement_updates_only_the_compiler_pin(self):
        github = Mock()
        github.request.side_effect = [{'tag_name': 'v0.2.18'},
            {'sha': '3' * 40, 'commit': {'committer': {'date': '2026-10-23T07:00:00Z'}}},
            {'encoding': 'base64', 'content': base64.b64encode(b'module example\ngo 1.27.3\n').decode()}]
        pin = Mock(return_value='golang:1.27.3-alpine@sha256:' + '6' * 64)
        result, changed = resolve(LOCK, github, Mock(return_value='4' * 64), pin)
        self.assertTrue(changed)
        pin.assert_called_once_with('1.27.3')
        self.assertEqual(go_image(result['images'])[2], (1, 27, 3))
        self.assertEqual(result['images']['example'], LOCK['images']['example'])
        self.assertIn('golang:1.27.2-alpine', LOCK['images'])

    def test_go_directives_and_unpinned_images_are_checked(self):
        self.assertEqual(go_requirement('module example\ngo 1.27.3\ntoolchain go1.27.4\n'), (1, 27, 4))
        with self.assertRaises(ValueError):
            go_requirement('module example\n')
        with self.assertRaises(ValueError):
            go_image({'golang:1.27.3-alpine': 'golang:1.27.3-alpine'})


class RegistryTests(unittest.TestCase):
    def registry(self):
        registry = Registry.__new__(Registry)
        registry.repository, registry.token = 'celesrain/sub2api-subpath', 'synthetic'
        return registry

    def test_only_404_means_a_free_tag(self):
        for code in [401, 403, 429, 500]:
            error = urllib.error.HTTPError('https://ghcr.io', code, 'test', {}, None)
            with patch('urllib.request.urlopen', side_effect=error), self.assertRaises(RuntimeError):
                self.registry().manifest_digest('new-version')
        error = urllib.error.HTTPError('https://ghcr.io', 404, 'test', {}, None)
        with patch('urllib.request.urlopen', side_effect=error):
            self.assertIsNone(self.registry().manifest_digest('new-version'))

    def test_missing_digest_is_not_success(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.headers = {}
        with patch('urllib.request.urlopen', return_value=response), self.assertRaises(ValueError):
            self.registry().manifest_digest('new-version')

    def test_existing_image_and_release_are_never_replaced(self):
        github, registry = Mock(), Mock()
        registry.manifest_digest.return_value = 'sha256:' + 'a' * 64
        with self.assertRaises(ValueError):
            require_new_publication(registry, github, 'owner/repo', 'version')
        github.request.assert_not_called()
        registry.manifest_digest.return_value = None
        github.request.return_value = {'id': 1}
        with self.assertRaises(ValueError):
            require_new_publication(registry, github, 'owner/repo', 'version')

    def test_github_auth_error_is_not_absence(self):
        error = urllib.error.HTTPError('https://api.github.com', 403, 'test', {}, None)
        with patch('urllib.request.urlopen', side_effect=error), self.assertRaises(RuntimeError):
            GitHub('synthetic').request('/repos/owner/repo/releases/tags/build-x', missing_ok=True)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dist = self.root / 'dist'
        self.dist.mkdir()
        self.archive = self.dist / 'image.tar.gz'
        self.archive.write_bytes(b'synthetic verified image')
        lock_bytes = (json.dumps(LOCK) + '\n').encode()
        (self.dist / 'upstream.lock.json').write_bytes(lock_bytes)
        (self.root / 'upstream.lock.json').write_bytes(lock_bytes)
        version = build_version('v0.2.17', 2, ENV)
        self.record = {'checks_passed': True, 'archive_sha256': hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                       'source_repository': ENV['GITHUB_REPOSITORY'], 'source_revision': ENV['GITHUB_SHA'],
                       'version': version, 'image_reference': 'sub2api-subpath:' + version, 'image_id': 'sha256:' + 'b' * 64,
                       'build_run_id': ENV['GITHUB_RUN_ID'], 'build_run_attempt': '1', 'upstream_ref': 'v0.2.17',
                       'upstream_revision': LOCK['revision'], 'patch_revision': 2, 'lock_changed': False,
                       'images': LOCK['images'],
                       'lock_sha256': hashlib.sha256(lock_bytes).hexdigest(), 'base_lock_sha256': hashlib.sha256(lock_bytes).hexdigest()}
        (self.dist / 'build.json').write_text(json.dumps(self.record))
        for context in [patch.object(ci, 'ROOT', self.root), patch.object(ci, 'DIST', self.dist), patch.dict(os.environ, ENV, clear=True)]:
            context.start()
            self.addCleanup(context.stop)

    def test_existing_version_stops_before_docker_load_or_push(self):
        registry = Mock()
        registry.manifest_digest.return_value = 'sha256:' + 'a' * 64
        with patch.object(ci, 'Registry', return_value=registry), patch.object(ci, 'GitHub'), patch.object(ci, 'run') as run:
            with self.assertRaises(ValueError):
                ci.publish()
            run.assert_not_called()

    def test_success_publishes_only_unique_tag_and_creates_new_release(self):
        registry, github = Mock(), Mock()
        registry.manifest_digest.side_effect = [None, 'sha256:' + 'c' * 64]
        github.request.side_effect = [None, {'html_url': 'https://github.com/owner/repo/releases/tag/new'}]
        image = json.dumps([{'Id': self.record['image_id']}]).encode()
        with patch.object(ci, 'Registry', return_value=registry), patch.object(ci, 'GitHub', return_value=github), \
             patch.object(ci, 'run') as run, patch.object(ci.subprocess, 'check_output', return_value=image):
            ci.publish()
        pushes = [call.args[0] for call in run.call_args_list if call.args[0][:2] == ['docker', 'push']]
        self.assertEqual(pushes, [['docker', 'push', 'ghcr.io/celesrain/sub2api-subpath:' + self.record['version']]])
        create = github.request.call_args_list[-1]
        self.assertEqual(create.kwargs['data']['tag_name'], 'build-' + self.record['version'])
        self.assertEqual(create.kwargs['data']['make_latest'], 'false')
        self.assertTrue((self.dist / 'published.json').exists())

    def test_failed_build_has_no_publication_to_promote(self):
        with patch.object(ci, 'GitHub') as github, self.assertRaises(FileNotFoundError):
            ci.promote_lock()
        github.assert_not_called()

    def test_concurrent_main_update_does_not_write(self):
        candidate = {**LOCK, 'ref': 'v0.2.18', 'revision': '3' * 40, 'source_sha256': '4' * 64}
        data = (json.dumps(candidate) + '\n').encode()
        (self.dist / 'upstream.lock.json').write_bytes(data)
        receipt = {**self.record, 'lock_changed': True, 'lock_sha256': hashlib.sha256(data).hexdigest(),
                   'upstream_ref': candidate['ref'], 'upstream_revision': candidate['revision'], 'source_sha256': candidate['source_sha256']}
        (self.dist / 'published.json').write_text(json.dumps(receipt))
        github = Mock()
        github.request.return_value = {'object': {'sha': 'f' * 40}}
        with patch.object(ci, 'GitHub', return_value=github):
            ci.promote_lock()
        self.assertEqual(github.request.call_count, 1)
        self.assertNotIn('data', github.request.call_args.kwargs)

    def test_success_promotes_only_verified_lock_without_force(self):
        candidate = {**LOCK, 'ref': 'v0.2.18', 'revision': '3' * 40, 'source_sha256': '4' * 64}
        data = (json.dumps(candidate) + '\n').encode()
        (self.dist / 'upstream.lock.json').write_bytes(data)
        receipt = {**self.record, 'lock_changed': True, 'lock_sha256': hashlib.sha256(data).hexdigest(),
                   'upstream_ref': candidate['ref'], 'upstream_revision': candidate['revision'], 'source_sha256': candidate['source_sha256']}
        (self.dist / 'published.json').write_text(json.dumps(receipt))
        github = Mock()
        github.request.side_effect = [{'object': {'sha': ENV['GITHUB_SHA']}}, {'tree': {'sha': 'old-tree'}},
                                      {'sha': 'lock-blob'}, {'sha': 'new-tree'}, {'sha': 'new-commit'}, {}]
        with patch.object(ci, 'GitHub', return_value=github):
            ci.promote_lock()
        tree = github.request.call_args_list[3].kwargs['data']
        self.assertEqual(tree['tree'], [{'path': 'upstream.lock.json', 'mode': '100644', 'type': 'blob', 'sha': 'lock-blob'}])
        self.assertEqual(github.request.call_args.kwargs['data'], {'sha': 'new-commit', 'force': False})

    def test_promotion_rejects_unrelated_recipe_changes(self):
        candidate = {**LOCK, 'ref': 'v0.2.18', 'revision': '3' * 40, 'images': {**LOCK['images'], 'example': 'changed'}}
        data = (json.dumps(candidate) + '\n').encode()
        (self.dist / 'upstream.lock.json').write_bytes(data)
        receipt = {**self.record, 'lock_changed': True, 'lock_sha256': hashlib.sha256(data).hexdigest()}
        (self.dist / 'published.json').write_text(json.dumps(receipt))
        with patch.object(ci, 'GitHub') as github, self.assertRaises(AssertionError):
            ci.promote_lock()
        github.assert_not_called()


if __name__ == '__main__':
    unittest.main()
