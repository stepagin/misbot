#!/usr/bin/env python3
"""GitHub Actions automation for PRs and immutable release tags.

The built-in job token is read from the environment, never commands/logs.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'


class Error(RuntimeError):
    pass


class APIError(Error):
    def __init__(self, status):
        self.status = status
        super().__init__(
            'GitHub API returned HTTP ' + str(status) + '; check CI permissions privately'
        )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHub:
    def __init__(self, repository, token):
        if (
            not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or any(part in ('.', '..') for part in repository.split('/'))
            or not token
        ):
            raise Error('Configure GITHUB_REPOSITORY and the job GH_TOKEN')
        self.repository = repository
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))

    def call(self, path, payload=None, method=None):
        request = urllib.request.Request(
            'https://api.github.com/repos/' + self.repository + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            method=method or ('POST' if payload is not None else 'GET'),
            headers={
                'Authorization': 'Bearer ' + self.token,
                'Accept': 'application/vnd.github+json',
                'Content-Type': 'application/json',
                'X-GitHub-Api-Version': '2022-11-28',
                'User-Agent': 'Misbot-release-ci',
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise Error('GitHub response exceeds the allowed size')
                return json.loads(payload)
        except urllib.error.HTTPError as error:
            raise APIError(error.code) from None
        except (urllib.error.URLError, OSError, ValueError):
            raise Error(
                'GitHub request failed; retry the pipeline after checking connectivity'
            ) from None


def release_version(branch='', root=ROOT):
    number = json.loads((root / 'installer/release.json').read_text())['version']
    if not isinstance(number, str) or not re.fullmatch(VERSION, number):
        raise Error('release.json must contain the owner-selected stable major.minor.patch version')
    if branch.startswith('release/') and branch != 'release/' + number:
        raise Error('Release branch must be release/' + number + ' to match release.json')
    notes = (root / 'installer/release-notes.md').read_text()
    if not notes.splitlines() or notes.splitlines()[0] != '# Misbot ' + number:
        raise Error('Release notes must describe the version in release.json')
    return number


def commit_sha(value):
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise Error('GITHUB_SHA must be a full Git commit SHA')
    return value


def open_pr(api, branch, sha, number):
    if branch != 'release/' + number:
        raise Error('Only a matching release branch can create a release PR')
    base = api.call('')['default_branch']
    remote = api.call('/git/ref/heads/' + urllib.parse.quote(branch, safe='/'))
    if remote['object']['sha'] != sha:
        raise Error('Release branch advanced since this checkout; use its latest workflow run')
    owner = api.repository.split('/')[0]
    query = urllib.parse.urlencode({'state': 'open', 'head': owner + ':' + branch, 'base': base})
    existing = api.call('/pulls?' + query)
    if existing:
        result = existing[0]
    else:
        body = (
            'Release Misbot ' + number + '.\n\n'
            'Validation: installer, Matrix bots, Telegram, deployment, source manifest '
            'and deterministic release archive.\n\n'
            'Data migrations run automatically after a verified backup. '
            'Review compatibility and recovery before merging.\n\n'
            'After merging, GitHub Actions tags the validated merge commit as `' + number + '` '
            'and publishes its verified installer and checksum. The version in release.json '
            'is preserved unchanged.'
        )
        try:
            result = api.call(
                '/pulls',
                {'title': 'Release Misbot ' + number, 'head': branch, 'base': base, 'body': body},
            )
        except APIError as error:
            if error.status != 422:
                raise
            existing = api.call('/pulls?' + query)
            if not existing:
                raise
            result = existing[0]
    print('Release PR:', result['html_url'])
    return result


def tag_target(api, number):
    try:
        current = api.call('/git/ref/tags/' + number)['object']
    except APIError as error:
        if error.status == 404:
            return None
        raise
    # Accept both lightweight tags and our own annotated tags; bounded peeling.
    for _ in range(5):
        if current['type'] == 'commit':
            return current['sha']
        if current['type'] != 'tag':
            break
        current = api.call('/git/tags/' + current['sha'])['object']
    raise Error('Existing release tag does not resolve to a commit')


def merged_release(api, branch, sha, number):
    base = api.call('')['default_branch']
    if branch != base:
        raise Error('Release tags can only be created from the GitHub default branch')
    # This endpoint finds the PR for this exact merge/squash/rebase commit, so
    # unrelated default-branch pushes cannot release the version in the file.
    prs = api.call('/commits/' + sha + '/pulls?per_page=100')
    matching = [
        pr
        for pr in prs
        if pr.get('merged_at')
        and pr.get('merge_commit_sha') == sha
        and pr['base']['ref'] == base
        and pr['head']['ref'] == 'release/' + number
        and (pr['head'].get('repo') or {}).get('full_name', '').lower() == api.repository.lower()
    ]
    if not matching:
        print('No merged release PR at this commit; no release changes')
        return False
    # A queued job may run after another commit lands: allow ancestors,
    # but refuse a foreign/detached commit or rewritten default-branch history.
    comparison = api.call('/compare/' + sha + '...' + urllib.parse.quote(base, safe=''))
    if comparison['status'] not in ('identical', 'ahead'):
        raise Error('CI commit is no longer on the GitHub default branch')
    return True


def publish_tag(api, branch, sha, number):
    if not re.fullmatch(VERSION, number):
        raise Error('Only stable versions can be tagged')
    if not merged_release(api, branch, sha, number):
        return None
    current = tag_target(api, number)
    if current is not None:
        if current != sha:
            raise Error('Release tag already points to another commit; it will not be moved')
        print('Release tag already exists:', number)
        return number
    annotated = api.call(
        '/git/tags', {'tag': number, 'message': 'Misbot ' + number, 'object': sha, 'type': 'commit'}
    )
    try:
        api.call('/git/refs', {'ref': 'refs/tags/' + number, 'sha': annotated['sha']})
    except APIError as error:
        if error.status != 422 or tag_target(api, number) != sha:
            raise
    print('Release tag created:', number)
    return number


def git(root, *args, env=None):
    result = subprocess.run(
        ['git', *args], cwd=root, env=env, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise Error('Git operation failed (' + args[0] + '); check branch permissions and history')
    return result.stdout.strip()


def clean_checkout(root):
    if git(root, 'status', '--porcelain', '--untracked-files=no'):
        raise Error('Release automation requires a clean tracked checkout')


def prepare(api, branch, sha, event, ref_type, root=ROOT):
    number = release_version(branch if ref_type == 'branch' else '', root)
    state = dict(kind='check', branch=branch, original=sha, version=number)
    clean_checkout(root)
    if git(root, 'rev-parse', 'HEAD') != sha:
        raise Error('Checkout must match the original event commit')
    if ref_type == 'tag':
        if branch.removeprefix('v') != number:
            raise Error('Release tag must match the stable version in release.json')
        state['kind'] = 'tag'
    elif event == 'push' and branch.startswith('release/'):
        state['kind'] = 'pr'
    elif event == 'push' and branch == api.call('')['default_branch']:
        if merged_release(api, branch, sha, number):
            state['kind'] = 'release'
    return state


def finalize(api, state, root=ROOT):
    clean_checkout(root)
    sha = state['original']
    if git(root, 'rev-parse', 'HEAD') != sha:
        raise Error('Validated checkout differs from the original event commit')
    if release_version(root=root) != state['version']:
        raise Error('Validated release metadata differs from the original version')
    kind, branch, number = (state[key] for key in ('kind', 'branch', 'version'))
    if kind == 'tag':
        if tag_target(api, branch) != sha:
            raise Error('Release tag no longer points to the validated event commit')
        return branch
    if kind == 'pr':
        open_pr(api, branch, sha, number)
    elif kind == 'release':
        return publish_tag(api, branch, sha, number)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('validate', 'prepare', 'finalize'))
    args = parser.parse_args()
    branch = os.environ.get('GITHUB_HEAD_REF') or os.environ.get('GITHUB_REF_NAME', '')
    if args.action == 'validate':
        print('Validated Misbot version', release_version(branch))
        return
    api = GitHub(os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GH_TOKEN', ''))
    sha = commit_sha(os.environ.get('GITHUB_SHA', ''))
    state_path = ROOT / git(ROOT, 'rev-parse', '--git-path', 'misbot-release-state.json')
    if args.action == 'prepare':
        state = prepare(
            api,
            branch,
            sha,
            os.environ.get('GITHUB_EVENT_NAME', ''),
            os.environ.get('GITHUB_REF_TYPE', ''),
        )
        state_path.write_text(json.dumps(state))
        print('Validated Misbot version', state['version'])
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('version=' + state['version'] + '\n')
    else:
        state = json.loads(state_path.read_text())
        if state['original'] != sha or state['branch'] != branch:
            raise Error('Release state differs from the workflow event')
        tag = finalize(api, state)
        if tag and os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('release_tag=' + tag + '\n')


if __name__ == '__main__':
    try:
        main()
    except (Error, OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        message = (
            str(error)
            if isinstance(error, Error)
            else 'Check release metadata and protected CI variables'
        )
        print('Release automation failed:', message, file=sys.stderr)
        sys.exit(1)
