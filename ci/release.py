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
        raise Error('release.json must contain a stable major.minor.patch version')
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
            'Merging this PR creates tag `' + number + '` on the merge commit. '
            'GitHub Actions then publishes the verified installer and checksum.'
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


def publish_tag(api, branch, sha, number):
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
        print('No merged release PR at this commit; no tag created')
        return None
    # A queued job may run after another commit lands: allow ancestors,
    # but refuse a foreign/detached commit or rewritten default-branch history.
    comparison = api.call('/compare/' + sha + '...' + urllib.parse.quote(base, safe=''))
    if comparison['status'] not in ('identical', 'ahead'):
        raise Error('CI commit is no longer on the GitHub default branch')
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('validate', 'open-pr', 'tag'))
    args = parser.parse_args()
    branch = os.environ.get('GITHUB_HEAD_REF') or os.environ.get('GITHUB_REF_NAME', '')
    number = release_version(branch)
    if args.action == 'validate':
        print('Validated Misbot version', number)
        return
    api = GitHub(os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GH_TOKEN', ''))
    sha = commit_sha(os.environ.get('GITHUB_SHA', ''))
    if args.action == 'open-pr':
        open_pr(api, branch, sha, number)
    else:
        tag = publish_tag(api, branch, sha, number)
        if tag and os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('release_tag=' + tag + '\n')


if __name__ == '__main__':
    try:
        main()
    except (Error, OSError, ValueError, KeyError) as error:
        message = (
            str(error)
            if isinstance(error, Error)
            else 'Check release metadata and protected CI variables'
        )
        print('Release automation failed:', message, file=sys.stderr)
        sys.exit(1)
