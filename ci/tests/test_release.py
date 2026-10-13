"""Release API decisions without network access or real credentials."""

import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
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

    def test_build_version_uses_stable_branch_and_release_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'installer').mkdir()
            (root / 'installer/release-notes.md').write_text('# Misbot 1.2.0\n')
            for number in ('1.2.0', '1.2.0-build.0', '1.2.0-build.123'):
                (root / 'installer/release.json').write_text(json.dumps({'version': number}))
                self.assertEqual(release.release_version('release/1.2.0', root), number)
            for number in ('1.2.0-build.01', '1.2.0-rc.1', '1.2.0+build.1', '1.2.0-build.-1'):
                (root / 'installer/release.json').write_text(json.dumps({'version': number}))
                with self.assertRaises(release.Error):
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
    """Exercise preparation and retries against an isolated local Git remote."""

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
        (installer / 'release.json').write_text('{"version":"1.2.0-build.9","schema":1}\n')
        (installer / 'release-notes.md').write_text('# Misbot 1.2.0\n')
        (installer / 'build.py').write_text(
            'from pathlib import Path\n'
            'import hashlib, sys\n'
            'root = Path(__file__).parent\n'
            "digest = hashlib.sha256((root / 'release.json').read_bytes()).hexdigest()\n"
            "manifest = root / 'SHA256SUMS'\n"
            "if '--check' in sys.argv:\n"
            '    sys.exit(0 if manifest.read_text() == digest else 1)\n'
            'manifest.write_text(digest)\n'
        )
        subprocess.run([sys.executable, 'installer/build.py'], cwd=self.root, check=True)
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
        if path == '/git/ref/tags/1.2.0':
            if '1.2.0' not in self.tags:
                raise release.APIError(404)
            return {'object': {'type': 'commit', 'sha': self.tags['1.2.0']}}
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

    def prepare(self, branch='release/1.2.0', run='12', event='push', ref_type='branch'):
        return release.prepare(self.api, branch, self.sha, run, event, ref_type, self.root)

    def test_build_commit_increases_number_and_changes_only_metadata(self):
        state = self.prepare()
        self.assertEqual(state['version'], '1.2.0-build.12')
        self.assertEqual(self.remote_head(state['branch']), self.sha)
        self.assertEqual(
            set(
                release.git(
                    self.root, 'diff-tree', '--no-commit-id', '--name-only', '-r', state['prepared']
                ).splitlines()
            ),
            set(release.VERSION_FILES),
        )
        self.assertEqual(release.git(self.root, 'rev-parse', 'HEAD^'), self.sha)
        release.finalize(self.api, state, self.root)
        self.assertEqual(self.remote_head(state['branch']), state['prepared'])

    def test_number_is_monotonic_even_when_workflow_counter_is_lower(self):
        self.assertEqual(self.prepare(run='2')['version'], '1.2.0-build.10')

    def test_original_event_retry_recreates_and_reuses_same_commit(self):
        first = self.prepare()
        release.finalize(self.api, first, self.root)
        release.git(self.root, 'checkout', '--detach', self.sha)
        retry = self.prepare()
        self.assertEqual(retry, first)
        release.finalize(self.api, retry, self.root)
        self.assertEqual(self.remote_head(retry['branch']), first['prepared'])

    def test_merge_publishes_stable_child_and_retries_without_retagging(self):
        state = self.prepare(branch='main')
        self.assertEqual(state['version'], '1.2.0')
        self.assertNotEqual(state['prepared'], self.sha)
        self.assertEqual(release.finalize(self.api, state, self.root), '1.2.0')
        self.assertEqual(self.tags['1.2.0'], state['prepared'])
        self.assertEqual(self.remote_head('main'), state['prepared'])
        release.git(self.root, 'checkout', '--detach', self.sha)
        retry = self.prepare(branch='main')
        self.assertEqual(retry, state)
        self.api.reset_mock()
        self.assertEqual(release.finalize(self.api, retry, self.root), '1.2.0')
        self.assertFalse(any(len(call.args) > 1 for call in self.api.call.call_args_list))

    def test_retry_finishes_tag_after_branch_push_succeeded(self):
        state = self.prepare(branch='main')
        api_call = self.api_call

        def interrupted_call(path, payload=None):
            if path == '/git/tags':
                raise release.APIError(503)
            return api_call(path, payload)

        self.api.call.side_effect = interrupted_call
        with self.assertRaises(release.APIError):
            release.finalize(self.api, state, self.root)
        self.assertEqual(self.remote_head('main'), state['prepared'])
        self.assertEqual(self.tags, {})
        release.git(self.root, 'checkout', '--detach', self.sha)
        self.api.call.side_effect = api_call
        retry = self.prepare(branch='main')
        self.assertEqual(retry['prepared'], state['prepared'])
        self.assertEqual(release.finalize(self.api, retry, self.root), '1.2.0')
        self.assertEqual(self.tags['1.2.0'], state['prepared'])

    def test_stable_retry_tags_already_pushed_ancestor_without_rewinding_main(self):
        state = self.prepare(branch='main')
        release.push_prepared(self.root, 'main', self.sha, state['prepared'])
        newer = self.advance_remote('main')
        release.git(self.root, 'checkout', '--detach', self.sha)
        retry = self.prepare(branch='main')
        self.assertEqual(retry, state)
        self.assertEqual(release.finalize(self.api, retry, self.root), '1.2.0')
        self.assertEqual(self.tags['1.2.0'], state['prepared'])
        self.assertEqual(self.remote_head('main'), newer)

    def test_stable_preparation_cannot_overwrite_divergent_newer_main(self):
        state = self.prepare(branch='main')
        newer = self.advance_remote('main')
        with self.assertRaisesRegex(release.Error, 'advanced'):
            release.finalize(self.api, state, self.root)
        self.assertEqual(self.remote_head('main'), newer)
        self.assertEqual(self.tags, {})

    def test_changed_files_after_validation_cannot_be_pushed(self):
        state = self.prepare()
        (self.root / 'installer/release-notes.md').write_text('# Changed after checks\n')
        with self.assertRaisesRegex(release.Error, 'clean'):
            release.finalize(self.api, state, self.root)
        self.assertEqual(self.remote_head(state['branch']), self.sha)

    def test_unrelated_default_push_keeps_build_version_and_does_not_publish(self):
        self.prs = []
        state = self.prepare(branch='main')
        self.assertEqual(state['kind'], 'check')
        self.assertEqual(state['version'], '1.2.0-build.9')
        self.assertIsNone(release.finalize(self.api, state, self.root))
        self.assertEqual(self.remote_head('main'), self.sha)
        self.assertEqual(self.tags, {})

    def test_manual_run_preserves_build_number_and_does_not_push(self):
        state = self.prepare(event='workflow_dispatch')
        self.assertEqual(state['kind'], 'check')
        self.assertEqual(state['version'], '1.2.0-build.9')
        self.assertIsNone(release.finalize(self.api, state, self.root))
        self.assertEqual(self.remote_head(state['branch']), self.sha)

    def test_prerelease_tags_are_rejected_before_publication(self):
        for branch in ('1.2.0', 'v1.2.0', '1.2.0-build.9'):
            with self.subTest(branch=branch), self.assertRaisesRegex(release.Error, 'stable'):
                self.prepare(branch=branch, ref_type='tag')
        self.assertEqual(self.tags, {})

    def test_stable_and_v_prefixed_existing_tags_are_supported(self):
        prepared = release.prepare_version(self.root, self.sha, '1.2.0')
        for branch in ('1.2.0', 'v1.2.0'):
            state = release.prepare(self.api, branch, prepared, '12', 'push', 'tag', self.root)
            self.assertEqual(release.finalize(self.api, state, self.root), branch)
        with self.assertRaisesRegex(release.Error, 'stable'):
            release.prepare(self.api, '1.3.0', prepared, '12', 'push', 'tag', self.root)

    def test_existing_different_tag_blocks_before_default_branch_push(self):
        state = self.prepare(branch='main')
        self.tags['1.2.0'] = OTHER
        with self.assertRaisesRegex(release.Error, 'will not be moved'):
            release.finalize(self.api, state, self.root)
        self.assertEqual(self.remote_head('main'), self.sha)

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

    def test_concurrent_push_after_remote_check_fails_without_force(self):
        state = self.prepare()
        real_git = release.git
        advanced = []

        def racing_git(root, *args, **kwargs):
            if args[0] == 'push' and root == self.root:
                advanced.append(self.advance_remote(state['branch']))
            return real_git(root, *args, **kwargs)

        with patch.object(release, 'git', side_effect=racing_git):
            with self.assertRaisesRegex(release.Error, 'Git operation failed'):
                release.finalize(self.api, state, self.root)
        self.assertEqual(self.remote_head(state['branch']), advanced[0])

    def test_dirty_checkout_and_stale_manifest_are_rejected(self):
        path = self.root / 'installer/release.json'
        path.write_text('{"version":"1.2.0-build.10"}\n')
        with self.assertRaisesRegex(release.Error, 'clean'):
            self.prepare()
        release.git(self.root, 'add', '--', 'installer/release.json')
        release.git(self.root, 'commit', '-m', 'Stale manifest')
        self.sha = release.git(self.root, 'rev-parse', 'HEAD')
        with self.assertRaises(subprocess.CalledProcessError):
            self.prepare()

    def test_failed_hook_is_not_bypassed(self):
        hook = self.root / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 1\n')
        hook.chmod(0o700)
        with self.assertRaisesRegex(release.Error, 'Git operation failed'):
            self.prepare()
        self.assertEqual(release.git(self.root, 'rev-parse', 'HEAD'), self.sha)
        self.assertEqual(self.remote_head('release/1.2.0'), self.sha)


if __name__ == '__main__':
    unittest.main()
