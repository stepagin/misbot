"""Release API decisions without network access or real credentials."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location(
    'ci_release', Path(__file__).resolve().parents[1] / 'release.py'
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
SHA = 'a' * 40
OTHER = 'b' * 40


class ReleaseTests(unittest.TestCase):
    def api(self, values):
        api = Mock(repository='fictional/misbot')

        def call(path, payload=None):
            value = values[path]
            if isinstance(value, BaseException):
                raise value
            return value

        api.call.side_effect = call
        return api

    def merged(self, **changes):
        pr = {
            'merged_at': '2026-10-11T00:00:00Z',
            'merge_commit_sha': SHA,
            'base': {'ref': 'main'},
            'head': {'ref': 'release/1.1.0', 'repo': {'full_name': 'fictional/misbot'}},
        }
        pr.update(changes)
        return pr

    def tag_api(self, prs=None, current=None):
        return self.api(
            {
                '': {'default_branch': 'main'},
                '/commits/' + SHA + '/pulls?per_page=100': prs
                if prs is not None
                else [self.merged()],
                '/compare/' + SHA + '...main': {'status': 'identical'},
                '/git/ref/tags/1.1.0': current or release.APIError(404),
                '/git/tags': {'sha': OTHER},
                '/git/refs': {},
            }
        )

    def test_branch_and_version_must_match(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'installer').mkdir()
            (root / 'installer/release.json').write_text(json.dumps({'version': '1.1.0'}))
            (root / 'installer/release-notes.md').write_text('# Misbot 1.1.0\n')
            self.assertEqual(release.release_version('release/1.1.0', root), '1.1.0')
            for branch in ('release/1.0.0', 'release/1.1.0;fake', 'release/v1.1.0', 'release/next'):
                with self.assertRaises(release.Error):
                    release.release_version(branch, root)
            (root / 'installer/release.json').write_text('{"version":"01.1.0"}')
            with self.assertRaises(release.Error):
                release.release_version('main', root)

    def test_ci_rejects_build_and_prerelease_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'installer').mkdir()
            (root / 'installer/release-notes.md').write_text('# Misbot 1.2.0\n')
            for number in (
                '1.2.0-build.0',
                '1.2.0-build.123',
                '1.2.0-build.01',
                '1.2.0-rc.1',
                '1.2.0+build.1',
                '1.2.0-build.-1',
            ):
                with self.subTest(number=number):
                    (root / 'installer/release.json').write_text(json.dumps({'version': number}))
                    with self.assertRaisesRegex(release.Error, 'stable'):
                        release.release_version('release/1.2.0', root)

    def test_open_pr_creates_one_and_reuses_existing(self):
        query = '/pulls?state=open&head=fictional%3Arelease%2F1.1.0&base=main'
        pr = {'html_url': 'https://github.com/fictional/misbot/pull/1'}
        for existing in ([], [pr]):
            api = self.api(
                {
                    '': {'default_branch': 'main'},
                    '/git/ref/heads/release/1.1.0': {'object': {'sha': SHA}},
                    query: existing,
                    '/pulls': pr,
                }
            )
            self.assertEqual(release.open_pr(api, 'release/1.1.0', SHA, '1.1.0'), pr)
            mutations = [call for call in api.call.call_args_list if len(call.args) > 1]
            self.assertEqual(len(mutations), 0 if existing else 1)

    def test_stale_branch_cannot_open_pr(self):
        api = self.api(
            {
                '': {'default_branch': 'main'},
                '/git/ref/heads/release/1.1.0': {'object': {'sha': OTHER}},
            }
        )
        with self.assertRaisesRegex(release.Error, 'advanced'):
            release.open_pr(api, 'release/1.1.0', SHA, '1.1.0')

    def test_merge_commit_is_tagged_with_annotation(self):
        api = self.tag_api()
        self.assertEqual(release.publish_tag(api, 'main', SHA, '1.1.0'), '1.1.0')
        api.call.assert_any_call(
            '/git/tags',
            {'tag': '1.1.0', 'message': 'Misbot 1.1.0', 'object': SHA, 'type': 'commit'},
        )
        api.call.assert_any_call('/git/refs', {'ref': 'refs/tags/1.1.0', 'sha': OTHER})

    def test_direct_push_unmerged_fork_wrong_version_and_other_commit_do_not_tag(self):
        cases = [
            [],
            [self.merged(merged_at=None)],
            [self.merged(merge_commit_sha=OTHER)],
            [self.merged(head={'ref': 'release/1.0.0', 'repo': {'full_name': 'fictional/misbot'}})],
            [self.merged(head={'ref': 'release/1.1.0', 'repo': {'full_name': 'foreign/misbot'}})],
            [self.merged(head={'ref': 'release/1.1.0', 'repo': None})],
        ]
        for prs in cases:
            with self.subTest(prs=prs):
                api = self.tag_api(prs)
                self.assertIsNone(release.publish_tag(api, 'main', SHA, '1.1.0'))
                self.assertFalse(any(len(call.args) > 1 for call in api.call.call_args_list))

    def test_existing_tag_is_reused_and_never_moved(self):
        for target in (SHA, OTHER):
            with self.subTest(target=target):
                api = self.tag_api(current={'object': {'type': 'commit', 'sha': target}})
                if target == SHA:
                    self.assertEqual(release.publish_tag(api, 'main', SHA, '1.1.0'), '1.1.0')
                else:
                    with self.assertRaisesRegex(release.Error, 'will not be moved'):
                        release.publish_tag(api, 'main', SHA, '1.1.0')
                self.assertFalse(any(len(call.args) > 1 for call in api.call.call_args_list))

    def test_annotated_tag_resolves_to_commit(self):
        api = self.api(
            {
                '/git/ref/tags/1.1.0': {'object': {'type': 'tag', 'sha': OTHER}},
                '/git/tags/' + OTHER: {'object': {'type': 'commit', 'sha': SHA}},
            }
        )
        self.assertEqual(release.tag_target(api, '1.1.0'), SHA)

    def test_default_branch_rewrite_prevents_tag(self):
        api = self.tag_api()
        base_call = api.call.side_effect
        api.call.side_effect = lambda path, payload=None: (
            {'status': 'diverged'} if path.startswith('/compare/') else base_call(path, payload)
        )
        with self.assertRaisesRegex(release.Error, 'no longer'):
            release.publish_tag(api, 'main', SHA, '1.1.0')

    def test_repository_and_sha_cannot_inject_urls_or_output(self):
        for repository in ('../other', 'fictional/misbot?token=x', 'https://example.org/repo'):
            with self.assertRaises(release.Error):
                release.GitHub(repository, 'FICTIONAL_TOKEN')
        for sha in ('main', SHA + '\nrelease_tag=fake'):
            with self.assertRaises(release.Error):
                release.commit_sha(sha)

    def test_api_errors_do_not_echo_token_or_response_body(self):
        import io
        import urllib.error

        api = release.GitHub('fictional/misbot', 'FICTIONAL_TOKEN')
        with patch.object(
            api.opener,
            'open',
            side_effect=urllib.error.HTTPError(
                'https://api.github.com',
                403,
                'FICTIONAL_PRIVATE_BODY',
                {},
                io.BytesIO(b'FICTIONAL_PRIVATE_BODY'),
            ),
        ):
            with self.assertRaises(release.APIError) as caught:
                api.call('')
        self.assertNotIn('FICTIONAL_TOKEN', str(caught.exception))
        self.assertNotIn('FICTIONAL_PRIVATE_BODY', str(caught.exception))


class ReleaseGitTests(unittest.TestCase):
    """Prove release checks preserve source files and local/remote Git history."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='misbot-release-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'checkout'
        self.remote = Path(temporary.name) / 'remote.git'
        self.root.mkdir()
        release.git(self.root, 'init', '--initial-branch=main')
        release.git(self.root, 'config', 'user.name', 'Release Test')
        release.git(self.root, 'config', 'user.email', 'test@example.invalid')
        installer = self.root / 'installer'
        installer.mkdir()
        (installer / 'release.json').write_text('{"version":"1.2.0","schema":1}\n')
        (installer / 'release-notes.md').write_text('# Misbot 1.2.0\n')
        release.git(self.root, 'add', '--', 'installer')
        release.git(self.root, 'commit', '-m', 'Initial fixture')
        self.sha = release.git(self.root, 'rev-parse', 'HEAD')
        release.git(self.root, 'init', '--bare', str(self.remote))
        release.git(self.root, 'remote', 'add', 'origin', str(self.remote))
        release.git(self.root, 'push', 'origin', 'HEAD:refs/heads/main')
        release.git(self.root, 'push', 'origin', 'HEAD:refs/heads/release/1.2.0')
        self.api = Mock(repository='fictional/misbot')
        self.tags = {}
        self.prs = [
            {
                'merged_at': '2026-10-13T00:00:00Z',
                'merge_commit_sha': self.sha,
                'base': {'ref': 'main'},
                'head': {'ref': 'release/1.2.0', 'repo': {'full_name': 'fictional/misbot'}},
            }
        ]
        self.api.call.side_effect = self.api_call

    def api_call(self, path, payload=None):
        if path == '':
            return {'default_branch': 'main'}
        if path.startswith('/commits/'):
            self.assertEqual(path, '/commits/' + self.sha + '/pulls?per_page=100')
            return self.prs
        if path.startswith('/compare/'):
            return {'status': 'ahead'}
        if path.startswith('/git/ref/tags/'):
            tag = path.removeprefix('/git/ref/tags/')
            if tag not in self.tags:
                raise release.APIError(404)
            return {'object': {'type': 'commit', 'sha': self.tags[tag]}}
        if path == '/git/tags':
            self.pending_tag = payload['object']
            return {'sha': OTHER}
        if path == '/git/refs':
            self.tags['1.2.0'] = self.pending_tag
            return {}
        if path.startswith('/git/ref/heads/'):
            branch = path.removeprefix('/git/ref/heads/')
            return {'object': {'sha': self.remote_head(branch)}}
        if path.startswith('/pulls?'):
            return [{'html_url': 'https://github.com/fictional/misbot/pull/1'}]
        raise AssertionError('Unexpected API request ' + path)

    def remote_head(self, branch):
        return release.git(
            self.root, 'ls-remote', '--refs', 'origin', 'refs/heads/' + branch
        ).split()[0]

    def prepare(self, branch='release/1.2.0', event='push', ref_type='branch'):
        return release.prepare(self.api, branch, self.sha, event, ref_type, self.root)

    def assert_unchanged(self):
        self.assertEqual(release.git(self.root, 'rev-parse', 'HEAD'), self.sha)
        self.assertEqual(release.git(self.root, 'status', '--porcelain'), '')
        self.assertEqual(self.remote_head('main'), self.sha)
        self.assertEqual(self.remote_head('release/1.2.0'), self.sha)
        metadata = json.loads((self.root / 'installer/release.json').read_text())
        self.assertEqual(metadata, {'version': '1.2.0', 'schema': 1})

    def test_release_branch_opens_pr_without_changing_version_or_history(self):
        state = self.prepare()
        self.assertEqual(state['kind'], 'pr')
        self.assertEqual(state['version'], '1.2.0')
        self.assertIsNone(release.finalize(self.api, state, self.root))
        self.assert_unchanged()
        self.assertEqual(self.tags, {})
        self.assertTrue(
            any(call.args[0].startswith('/pulls?') for call in self.api.call.call_args_list)
        )

    def test_branch_retry_reuses_same_commit_and_version(self):
        first = self.prepare()
        release.finalize(self.api, first, self.root)
        retry = self.prepare()
        self.assertEqual(retry, first)
        release.finalize(self.api, retry, self.root)
        self.assert_unchanged()

    def test_merge_tags_exact_validated_commit_and_retries_without_retagging(self):
        state = self.prepare(branch='main')
        self.assertEqual(state['kind'], 'release')
        self.assertEqual(state['version'], '1.2.0')
        self.assertEqual(release.finalize(self.api, state, self.root), '1.2.0')
        self.assertEqual(self.tags['1.2.0'], self.sha)
        retry = self.prepare(branch='main')
        self.assertEqual(retry, state)
        self.api.reset_mock()
        self.assertEqual(release.finalize(self.api, retry, self.root), '1.2.0')
        self.assertFalse(any(len(call.args) > 1 for call in self.api.call.call_args_list))
        self.assert_unchanged()

    def test_interrupted_tag_publication_can_retry_without_source_changes(self):
        state = self.prepare(branch='main')
        api_call = self.api_call

        def interrupted_call(path, payload=None):
            if path == '/git/tags':
                raise release.APIError(503)
            return api_call(path, payload)

        self.api.call.side_effect = interrupted_call
        with self.assertRaises(release.APIError):
            release.finalize(self.api, state, self.root)
        self.assert_unchanged()
        self.assertEqual(self.tags, {})
        self.api.call.side_effect = api_call
        retry = self.prepare(branch='main')
        self.assertEqual(retry, state)
        self.assertEqual(release.finalize(self.api, retry, self.root), '1.2.0')
        self.assertEqual(self.tags['1.2.0'], self.sha)
        self.assert_unchanged()

    def test_queued_release_tags_ancestor_without_rewinding_main(self):
        state = self.prepare(branch='main')
        newer = self.advance_remote('main')
        self.assertEqual(release.finalize(self.api, state, self.root), '1.2.0')
        self.assertEqual(self.tags['1.2.0'], self.sha)
        self.assertEqual(self.remote_head('main'), newer)

    def test_changed_files_after_validation_cannot_be_published(self):
        state = self.prepare()
        (self.root / 'installer/release-notes.md').write_text('# Changed after checks\n')
        with self.assertRaisesRegex(release.Error, 'clean'):
            release.finalize(self.api, state, self.root)
        self.assertEqual(self.remote_head(state['branch']), self.sha)

    def test_checkout_change_after_validation_cannot_be_published(self):
        state = self.prepare(branch='main')
        release.git(self.root, 'commit', '--allow-empty', '-m', 'Different checkout')
        with self.assertRaisesRegex(release.Error, 'original event commit'):
            release.finalize(self.api, state, self.root)
        self.assertEqual(self.tags, {})

    def test_unrelated_default_push_does_not_publish(self):
        self.prs = []
        state = self.prepare(branch='main')
        self.assertEqual(state['kind'], 'check')
        self.assertIsNone(release.finalize(self.api, state, self.root))
        self.assert_unchanged()
        self.assertEqual(self.tags, {})

    def test_manual_branch_run_does_not_publish(self):
        for branch in ('release/1.2.0', 'main'):
            state = self.prepare(branch=branch, event='workflow_dispatch')
            self.assertEqual(state['kind'], 'check')
            self.assertIsNone(release.finalize(self.api, state, self.root))
            self.assert_unchanged()
            self.assertEqual(self.tags, {})
        self.api.call.assert_not_called()

    def test_prerelease_and_mismatched_tags_are_rejected(self):
        for branch in ('1.2.0-build.9', 'v1.2.0-build.9', '1.3.0'):
            with self.subTest(branch=branch), self.assertRaisesRegex(release.Error, 'stable'):
                self.prepare(branch=branch, ref_type='tag')
        self.assertEqual(self.tags, {})
        self.assert_unchanged()

    def test_stable_and_v_prefixed_existing_tags_are_supported(self):
        for branch in ('1.2.0', 'v1.2.0'):
            self.tags[branch] = self.sha
            state = self.prepare(branch=branch, ref_type='tag')
            self.assertEqual(release.finalize(self.api, state, self.root), branch)
            self.assert_unchanged()

    def test_tag_changed_since_validation_blocks_publication(self):
        state = self.prepare(branch='1.2.0', ref_type='tag')
        for target in (OTHER, None):
            self.tags['1.2.0'] = target
            with self.assertRaisesRegex(release.Error, 'validated event commit'):
                release.finalize(self.api, state, self.root)
        self.assert_unchanged()

    def test_existing_different_tag_blocks_without_changing_default_branch(self):
        state = self.prepare(branch='main')
        self.tags['1.2.0'] = OTHER
        with self.assertRaisesRegex(release.Error, 'will not be moved'):
            release.finalize(self.api, state, self.root)
        self.assert_unchanged()

    def advance_remote(self, branch):
        other = self.root.parent / 'concurrent'
        release.git(self.root, 'clone', '--branch', branch, str(self.remote), str(other))
        release.git(other, 'config', 'user.name', 'Other Test')
        release.git(other, 'config', 'user.email', 'other@example.invalid')
        (other / 'new.txt').write_text('New work\n')
        release.git(other, 'add', '--', 'new.txt')
        release.git(other, 'commit', '-m', 'Concurrent work')
        release.git(other, 'push', 'origin', 'HEAD:refs/heads/' + branch)
        return release.git(other, 'rev-parse', 'HEAD')

    def test_newer_branch_commit_is_never_overwritten(self):
        state = self.prepare()
        newer = self.advance_remote(state['branch'])
        with self.assertRaisesRegex(release.Error, 'advanced'):
            release.finalize(self.api, state, self.root)
        self.assertEqual(self.remote_head(state['branch']), newer)

    def test_dirty_checkout_is_rejected(self):
        (self.root / 'installer/release.json').write_text('{"version":"1.2.0","schema":2}\n')
        with self.assertRaisesRegex(release.Error, 'clean'):
            self.prepare(branch='main')

    def test_preparation_does_not_commit_even_when_hook_would_fail(self):
        hook = self.root / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o700)
        state = self.prepare()
        self.assertIsNone(release.finalize(self.api, state, self.root))
        self.assert_unchanged()


if __name__ == '__main__':
    unittest.main()
