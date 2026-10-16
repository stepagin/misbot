"""Bootstrap MAS through its private API. Credentials arrive only on stdin."""

import base64
import json
import os
import re
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def administrator_username(credentials, domain=None):
    username = credentials.get('username')
    if not isinstance(username, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._=-]{0,254}', username):
        raise RuntimeError('Invalid saved administrator username')
    if 'password_managed' in credentials and type(credentials['password_managed']) is not bool:
        raise RuntimeError('Invalid administrator password management flag')
    imported = credentials.get('password_managed') is False
    if not imported and username != 'matrix-admin':
        raise RuntimeError('Unexpected bootstrap administrator username')
    matrix_id = credentials.get('matrix_id')
    if imported and (
        not isinstance(credentials.get('mas_user_id'), str)
        or not re.fullmatch(r'[0-7][0-9A-HJKMNP-TV-Z]{25}', credentials['mas_user_id'])
        or 'password' in credentials
        or not isinstance(matrix_id, str)
    ):
        raise RuntimeError(
            'Imported administrator requires a saved identity without a managed password'
        )
    if matrix_id is not None and (
        not isinstance(matrix_id, str)
        or not matrix_id.startswith('@' + username + ':')
        or not matrix_id.split(':', 1)[-1]
        or (domain is not None and matrix_id != '@' + username + ':' + domain)
    ):
        raise RuntimeError('Saved administrator Matrix identity does not match this installation')
    return username


def inspect_administrator(request, credentials):
    username = administrator_username(credentials)
    status, response = request('/api/admin/v1/users/by-username/' + username)
    if status != 200:
        raise RuntimeError('Saved MAS administrator is unavailable')
    user = response['data']
    attributes = user['attributes']
    if (
        (credentials.get('mas_user_id') is not None and credentials['mas_user_id'] != user['id'])
        or (attributes.get('username', username) != username)
        or not attributes.get('admin', attributes.get('can_request_admin', False))
        or attributes.get('locked_at')
        or attributes.get('deactivated_at')
    ):
        raise RuntimeError('Saved MAS administrator identity or active privileges changed')
    return {'user_id': user['id'], 'password_managed': False}


def bootstrap(request, credentials, checkpoint, retain_checkpoint=False):
    username = administrator_username(credentials)
    if credentials.get('password_managed') is False:
        return inspect_administrator(request, credentials)
    path = '/api/admin/v1/users/by-username/' + username
    status, response = request(path)
    if status == 404:
        status, response = request('/api/admin/v1/users', {'username': username})
        if status != 201:
            raise RuntimeError(
                'Administrator creation failed; retry install.sh up with existing secrets'
            )
        user = response['data']
        # A lost create response has no ownership proof: refuse to adopt that account.
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        with checkpoint.open('x') as handle:
            os.chmod(checkpoint, 0o600)
            json.dump({'user_id': user['id']}, handle)
            handle.flush()
            os.fsync(handle.fileno())
    elif status == 200:
        user = response['data']
    else:
        raise RuntimeError('Cannot inspect administrator; retry after repairing MAS connectivity')
    attributes = user['attributes']
    if attributes.get('locked_at') or attributes.get('deactivated_at'):
        raise RuntimeError(
            'Bootstrap administrator is inactive; restore access without replacing the account'
        )
    owned = (
        (json.loads(checkpoint.read_text()).get('user_id') == user['id'])
        if checkpoint.exists()
        else False
    )
    if not owned:
        if attributes.get('admin', attributes.get('can_request_admin', False)):
            return {'user_id': user['id'], 'password_managed': False}
        raise RuntimeError(
            'Existing non-admin account has no bootstrap ownership checkpoint; refusing to overwrite it'
        )
    # Repeat the same saved password after an interrupted request; never generate a replacement.
    status, _ = request(
        '/api/admin/v1/users/' + user['id'] + '/set-password', {'password': credentials['password']}
    )
    if status != 204:
        raise RuntimeError(
            'Administrator password was not confirmed; preserve secrets and retry install.sh up'
        )
    status, response = request('/api/admin/v1/users/' + user['id'] + '/set-admin', {'admin': True})
    if status != 200 or not response['data']['attributes'].get('admin', False):
        raise RuntimeError('Administrator privileges were not confirmed; retry install.sh up')
    if not retain_checkpoint:
        checkpoint.unlink()
    return {'user_id': user['id'], 'password_managed': True}


def set_password(request, credentials):
    username = administrator_username(credentials)
    if credentials.get('password_managed') is False:
        raise RuntimeError('Imported administrator password is not managed by Misbot')
    status, response = request('/api/admin/v1/users/by-username/' + username)
    if status != 200:
        raise RuntimeError('Administrator is unavailable')
    user = response['data']
    if credentials.get('mas_user_id') is not None and credentials['mas_user_id'] != user['id']:
        raise RuntimeError('Saved administrator identity changed; no password changed')
    attributes = user['attributes']
    if (
        not attributes.get('admin', attributes.get('can_request_admin', False))
        or attributes.get('locked_at')
        or attributes.get('deactivated_at')
    ):
        raise RuntimeError('Administrator must be active and retain admin privileges')
    status, _ = request(
        '/api/admin/v1/users/' + user['id'] + '/set-password', {'password': credentials['password']}
    )
    if status != 204:
        raise RuntimeError('Password change was not confirmed')


def verify_state(request, users):
    import re

    if not isinstance(users, list) or not 1 <= len(users) <= 4:
        raise RuntimeError('Saved external MAS identities are required')
    for saved in users:
        username = saved['username']
        if not re.fullmatch(r'[a-z0-9][a-z0-9._=-]{0,254}', username):
            raise RuntimeError('Invalid saved MAS username')
        status, response = request('/api/admin/v1/users/by-username/' + username)
        if status != 200:
            raise RuntimeError('Saved external MAS user is unavailable')
        user = response['data']
        attributes = user['attributes']
        if (
            user['id'] != saved['user_id']
            or attributes.get('username') != username
            or attributes.get('locked_at')
            or attributes.get('deactivated_at')
            or bool(attributes.get('admin', attributes.get('can_request_admin', False)))
            != saved['admin']
        ):
            raise RuntimeError(
                'External MAS account identity changed; do not create replacement accounts'
            )
        if saved.get('session_id'):
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', saved['session_id']):
                raise RuntimeError('Invalid saved MAS session')
            status, response = request('/api/admin/v1/personal-sessions/' + saved['session_id'])
            if status != 200:
                raise RuntimeError('Saved external MAS device session is unavailable')
            session = response['data']
            attributes = session['attributes']
            if (
                session.get('id') != saved['session_id']
                or session.get('type') != 'personal-session'
                or attributes.get('actor_user_id') != saved['user_id']
                or set(attributes.get('scope', '').split()) != set(saved['scope'].split())
                or attributes.get('finished_at')
                or attributes.get('revoked_at')
            ):
                raise RuntimeError('Saved external MAS device session changed or expired')
    return {'passed': True, 'accounts_and_sessions': len(users)}


def main():
    payload = json.load(sys.stdin)
    client = payload['client']
    base = client.get('mas_url', 'http://mas:8080')
    parsed = urllib.parse.urlsplit(base)
    if base != 'http://mas:8080' and (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ('', '/')
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError('Unsafe MAS API address')
    base = base.rstrip('/')
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    basic = base64.b64encode(
        (client['client_id'] + ':' + client['client_secret']).encode()
    ).decode()

    def send(path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            base + path,
            data=data,
            headers={'Content-Type': 'application/json', **(headers or {})},
        )
        try:
            response = opener.open(req, timeout=20)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            content = response.read(1048577)
            if len(content) > 1048576:
                raise RuntimeError('Oversized MAS response')
            return response.status, json.loads(content) if content else None

    req = urllib.request.Request(
        base + '/oauth2/token',
        data=urllib.parse.urlencode(
            {'grant_type': 'client_credentials', 'scope': 'urn:mas:admin'}
        ).encode(),
        headers={'Authorization': 'Basic ' + basic},
    )
    with opener.open(req, timeout=20) as response:
        token = json.loads(response.read(1048577))['access_token']

    def request(path, body=None):
        return send(path, body, {'Authorization': 'Bearer ' + token})

    if payload.get('action') == 'probe':
        # A token alone does not prove access to the required administrative API.
        status, _ = request('/api/admin/v1/users?page[first]=1')
        if status != 200:
            raise RuntimeError('MAS administrative access was not confirmed')
        discovery = urllib.request.Request(base + '/.well-known/openid-configuration')
        with opener.open(discovery, timeout=20) as response:
            body = response.read(1048577)
        if len(body) > 1048576:
            raise RuntimeError('Oversized MAS discovery')
        document = json.loads(body)
        if any(
            document.get(key) != base + suffix
            for key, suffix in (
                ('issuer', '/'),
                ('token_endpoint', '/oauth2/token'),
                ('authorization_endpoint', '/authorize'),
            )
        ):
            raise RuntimeError('External MAS discovery does not match the configured issuer')
        print(json.dumps({'passed': True}))
    elif payload.get('action') == 'verify-state':
        print(json.dumps(verify_state(request, payload['users'])))
    elif payload.get('action') == 'inspect':
        print(json.dumps(inspect_administrator(request, payload['credentials'])))
    elif payload.get('action', 'bootstrap') == 'password':
        set_password(request, payload['credentials'])
    elif payload.get('action', 'bootstrap') == 'bootstrap':
        result = bootstrap(
            request,
            payload['credentials'],
            Path('/data/bootstrap-admin.json'),
            retain_checkpoint=base != 'http://mas:8080',
        )
        if base != 'http://mas:8080':
            print(json.dumps(result))
    else:
        raise RuntimeError('Unknown administrator operation')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Responses and exceptions may contain credentials; never print them.
        print(
            'MAS administrator setup failed. Keep existing data and secrets; retry install.sh up. '
            'An existing non-admin account without an ownership checkpoint requires private administrator review.',
            file=sys.stderr,
        )
        sys.exit(1)
