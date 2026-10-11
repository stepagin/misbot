"""Bootstrap MAS through its private API. Credentials arrive only on stdin."""

import base64
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def bootstrap(request, credentials, checkpoint):
    username = credentials['username']
    if username != 'matrix-admin':
        raise RuntimeError('Unexpected bootstrap administrator username')
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
            return
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
    checkpoint.unlink()


def set_password(request, credentials):
    if credentials.get('username') != 'matrix-admin':
        raise RuntimeError('Unexpected administrator username')
    status, response = request('/api/admin/v1/users/by-username/matrix-admin')
    if status != 200:
        raise RuntimeError('Administrator is unavailable')
    user = response['data']
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


def main():
    payload = json.load(sys.stdin)
    client = payload['client']
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    basic = base64.b64encode(
        (client['client_id'] + ':' + client['client_secret']).encode()
    ).decode()

    def send(path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            'http://mas:8080' + path,
            data=data,
            headers={'Content-Type': 'application/json', **(headers or {})},
        )
        try:
            response = opener.open(req, timeout=20)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            content = response.read()
            return response.status, json.loads(content) if content else None

    req = urllib.request.Request(
        'http://mas:8080/oauth2/token',
        data=urllib.parse.urlencode(
            {'grant_type': 'client_credentials', 'scope': 'urn:mas:admin'}
        ).encode(),
        headers={'Authorization': 'Basic ' + basic},
    )
    with opener.open(req, timeout=20) as response:
        token = json.load(response)['access_token']

    def request(path, body=None):
        return send(path, body, {'Authorization': 'Bearer ' + token})

    if payload.get('action', 'bootstrap') == 'password':
        set_password(request, payload['credentials'])
    elif payload.get('action', 'bootstrap') == 'bootstrap':
        bootstrap(request, payload['credentials'], Path('/data/bootstrap-admin.json'))
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
