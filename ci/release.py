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
BUILD_VERSION = VERSION + r'(?:-build\.(0|[1-9][0-9]*))?'
VERSION_FILES = ('installer/release.json', 'installer/SHA256SUMS')


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
    if not isinstance(number, str) or not re.fullmatch(BUILD_VERSION, number):
        raise Error('release.json must contain major.minor.patch or major.minor.patch-build.N')
    base = number.split('-build.', 1)[0]
    if branch.startswith('release/') and branch != 'release/' + base:
        raise Error('Release branch must be release/' + base + ' to match release.json')
    notes = (root / 'installer/release-notes.md').read_text()
    if not notes.splitlines() or notes.splitlines()[0] != '# Misbot ' + base:
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
            'Build versions use `' + number + '-build.N`. After merging, GitHub Actions '
            'commits the stable version `' + number + '`, tags that commit, '
            'and publishes its verified installer and checksum.'
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


def publish_tag(api, branch, sha, number, original_sha=None):
    if not re.fullmatch(VERSION, number):
        raise Error('Only stable versions can be tagged')
    if not merged_release(api, branch, original_sha or sha, number):
        return None
    if original_sha and original_sha != sha:
        base = api.call('')['default_branch']
        comparison = api.call('/compare/' + sha + '...' + urllib.parse.quote(base, safe=''))
        if comparison['status'] not in ('identical', 'ahead'):
            raise Error('Prepared release commit is no longer on the GitHub default branch')
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


def prepare_version(root, sha, number):
    """Create the same local commit on every retry of the original event."""
    clean_checkout(root)
    if git(root, 'rev-parse', 'HEAD') != sha:
        raise Error('Checkout must match the original event commit')
    subprocess.run([sys.executable, 'installer/build.py', '--check'], cwd=root, check=True)
    path = root / VERSION_FILES[0]
    metadata = json.loads(path.read_text())
    if metadata['version'] == number:
        return sha
    metadata['version'] = number
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')
    subprocess.run([sys.executable, 'installer/build.py'], cwd=root, check=True)
    changed = set(git(root, 'diff', '--name-only').splitlines())
    if not changed <= set(VERSION_FILES):
        raise Error('Version preparation changed unexpected tracked files')
    git(root, 'add', '--', *VERSION_FILES)
    staged = set(git(root, 'diff', '--cached', '--name-only').splitlines())
    if not staged or not staged <= set(VERSION_FILES):
        raise Error('Version commit contains unexpected files')
    git(root, 'diff', '--cached', '--check')
    date = git(root, 'show', '-s', '--format=%cI', sha)
    environment = dict(os.environ)
    environment.update(
        GIT_AUTHOR_NAME='github-actions[bot]',
        GIT_COMMITTER_NAME='github-actions[bot]',
        GIT_AUTHOR_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
        GIT_COMMITTER_EMAIL='41898282+github-actions[bot]@users.noreply.github.com',
        GIT_AUTHOR_DATE=date,
        GIT_COMMITTER_DATE=date,
    )
    git(
        root,
        '-c',
        'commit.gpgsign=false',
        'commit',
        '-m',
        'chore: version ' + number,
        env=environment,
    )
    clean_checkout(root)
    return git(root, 'rev-parse', 'HEAD')


def prepare(api, branch, sha, run_number, event, ref_type, root=ROOT):
    number = release_version(branch if ref_type == 'branch' else '', root)
    base = number.split('-build.', 1)[0]
    state = dict(kind='check', branch=branch, original=sha, prepared=sha, version=number)
    clean_checkout(root)
    if git(root, 'rev-parse', 'HEAD') != sha:
        raise Error('Checkout must match the original event commit')
    if ref_type == 'tag':
        if number != base or branch.removeprefix('v') != number:
            raise Error('Release tag must match a stable version in release.json')
        state['kind'] = 'tag'
    elif event == 'push' and branch.startswith('release/'):
        if not re.fullmatch(r'[1-9][0-9]*', run_number):
            raise Error('GITHUB_RUN_NUMBER must be a positive integer')
        previous = int(number.split('-build.')[1]) if '-build.' in number else 0
        number = base + '-build.' + str(max(previous + 1, int(run_number)))
        state['kind'] = 'build'
    elif event == 'push' and branch == api.call('')['default_branch']:
        if merged_release(api, branch, sha, base):
            number = base
            state['kind'] = 'stable'
    if state['kind'] in ('build', 'stable'):
        state['prepared'] = prepare_version(root, sha, number)
        state['version'] = number
    return state


def push_prepared(root, branch, original, prepared, allow_descendant=False):
    ref = 'refs/heads/' + branch
    remote = git(root, 'ls-remote', '--refs', 'origin', ref).split()
    if not remote:
        raise Error('Release branch no longer exists')
    if remote[0] == prepared:
        return
    if remote[0] != original:
        if allow_descendant:
            # A stable retry may finish tagging an already-pushed commit after
            # newer work landed. Fetch and prove ancestry without changing the ref.
            git(root, 'fetch', '--no-tags', 'origin', ref)
            current = git(root, 'rev-parse', 'FETCH_HEAD')
            if git(root, 'merge-base', prepared, current) == prepared:
                return
        raise Error('Release branch advanced; refusing to overwrite newer commits')
    # A normal push also rejects a concurrent change after ls-remote.
    git(root, 'push', 'origin', prepared + ':' + ref)


def finalize(api, state, root=ROOT):
    clean_checkout(root)
    if git(root, 'rev-parse', 'HEAD') != state['prepared']:
        raise Error('Validated checkout differs from the prepared release commit')
    if release_version(root=root) != state['version']:
        raise Error('Validated release metadata differs from the prepared version')
    kind = state['kind']
    if kind == 'tag':
        return state['branch']
    if kind not in ('build', 'stable'):
        return None
    branch, original, prepared = (state[key] for key in ('branch', 'original', 'prepared'))
    if prepared != original and git(root, 'rev-parse', 'HEAD^') != original:
        raise Error('Prepared commit must be a direct child of the original event')
    base = state['version'].split('-build.', 1)[0]
    if kind == 'stable':
        if not merged_release(api, branch, original, base):
            raise Error('The original commit is no longer an authorized release merge')
        current = tag_target(api, base)
        if current is not None and current != prepared:
            raise Error('Release tag already points to another commit; it will not be moved')
    push_prepared(root, branch, original, prepared, allow_descendant=kind == 'stable')
    if kind == 'build':
        open_pr(api, branch, prepared, base)
        return None
    return publish_tag(api, branch, prepared, base, original_sha=original)


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
            os.environ.get('GITHUB_RUN_NUMBER', ''),
            os.environ.get('GITHUB_EVENT_NAME', ''),
            os.environ.get('GITHUB_REF_TYPE', ''),
        )
        state_path.write_text(json.dumps(state))
        print('Prepared Misbot version', state['version'])
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
