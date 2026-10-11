"""Release API decisions without network access or real credentials."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('ci_release', Path(__file__).resolve().parents[1] / 'release.py')
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
        pr = {'merged_at': '2026-10-11T00:00:00Z', 'merge_commit_sha': SHA,
              'base': {'ref': 'main'},
              'head': {'ref': 'release/1.1.0', 'repo': {'full_name': 'fictional/misbot'}}}
        pr.update(changes)
        return pr

    def tag_api(self, prs=None, current=None):
        return self.api({'': {'default_branch': 'main'},
            '/commits/' + SHA + '/pulls?per_page=100': prs if prs is not None else [self.merged()],
            '/compare/' + SHA + '...main': {'status': 'identical'},
            '/git/ref/tags/1.1.0': current or release.APIError(404),
            '/git/tags': {'sha': OTHER}, '/git/refs': {}})

    def test_branch_and_version_must_match(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); (root / 'installer').mkdir()
            (root / 'installer/release.json').write_text(json.dumps({'version': '1.1.0'}))
            (root / 'installer/release-notes.md').write_text('# Misbot 1.1.0\n')
            self.assertEqual(release.release_version('release/1.1.0', root), '1.1.0')
            for branch in ('release/1.0.0', 'release/1.1.0;fake', 'release/v1.1.0', 'release/next'):
                with self.assertRaises(release.Error):
                    release.release_version(branch, root)
            (root / 'installer/release.json').write_text('{"version":"01.1.0"}')
            with self.assertRaises(release.Error):
                release.release_version('main', root)

    def test_open_pr_creates_one_and_reuses_existing(self):
        query = '/pulls?state=open&head=fictional%3Arelease%2F1.1.0&base=main'
        pr = {'html_url': 'https://github.com/fictional/misbot/pull/1'}
        for existing in ([], [pr]):
            api = self.api({'': {'default_branch': 'main'},
                '/git/ref/heads/release/1.1.0': {'object': {'sha': SHA}},
                query: existing, '/pulls': pr})
            self.assertEqual(release.open_pr(api, 'release/1.1.0', SHA, '1.1.0'), pr)
            mutations = [call for call in api.call.call_args_list if len(call.args) > 1]
            self.assertEqual(len(mutations), 0 if existing else 1)

    def test_stale_branch_cannot_open_pr(self):
        api = self.api({'': {'default_branch': 'main'},
                       '/git/ref/heads/release/1.1.0': {'object': {'sha': OTHER}}})
        with self.assertRaisesRegex(release.Error, 'advanced'):
            release.open_pr(api, 'release/1.1.0', SHA, '1.1.0')

    def test_merge_commit_is_tagged_with_annotation(self):
        api = self.tag_api()
        self.assertEqual(release.publish_tag(api, 'main', SHA, '1.1.0'), '1.1.0')
        api.call.assert_any_call('/git/tags', {'tag': '1.1.0', 'message': 'Misbot 1.1.0',
                                            'object': SHA, 'type': 'commit'})
        api.call.assert_any_call('/git/refs', {'ref': 'refs/tags/1.1.0', 'sha': OTHER})

    def test_direct_push_unmerged_fork_wrong_version_and_other_commit_do_not_tag(self):
        cases = [[], [self.merged(merged_at=None)], [self.merged(merge_commit_sha=OTHER)],
                 [self.merged(head={'ref': 'release/1.0.0', 'repo': {'full_name': 'fictional/misbot'}})],
                 [self.merged(head={'ref': 'release/1.1.0', 'repo': {'full_name': 'foreign/misbot'}})],
                 [self.merged(head={'ref': 'release/1.1.0', 'repo': None})]]
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
        api = self.api({'/git/ref/tags/1.1.0': {'object': {'type': 'tag', 'sha': OTHER}},
                       '/git/tags/' + OTHER: {'object': {'type': 'commit', 'sha': SHA}}})
        self.assertEqual(release.tag_target(api, '1.1.0'), SHA)

    def test_default_branch_rewrite_prevents_tag(self):
        api = self.tag_api()
        base_call = api.call.side_effect
        api.call.side_effect = lambda path, payload=None: ({'status': 'diverged'}
            if path.startswith('/compare/') else base_call(path, payload))
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
        with patch.object(api.opener, 'open', side_effect=urllib.error.HTTPError(
                'https://api.github.com', 403, 'FICTIONAL_PRIVATE_BODY', {}, io.BytesIO(b'FICTIONAL_PRIVATE_BODY'))):
            with self.assertRaises(release.APIError) as caught:
                api.call('')
        self.assertNotIn('FICTIONAL_TOKEN', str(caught.exception))
        self.assertNotIn('FICTIONAL_PRIVATE_BODY', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
