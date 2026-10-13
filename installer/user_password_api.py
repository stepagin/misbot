"""Set an existing ordinary MAS user's password; private payload only on stdin."""

import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def inspect_user(request, credentials):
    username = credentials.get('username')
    if (
        not isinstance(username, str)
        or not re.fullmatch(r'[a-z0-9][a-z0-9._=-]{0,254}', username)
        or username == 'matrix-admin'
    ):
        raise RuntimeError('Use an existing ordinary username without @ or domain')
    status, response = request(
        '/api/admin/v1/users/by-username/' + urllib.parse.quote(username, safe='')
    )
    if status != 200:
        raise RuntimeError('Existing user could not be confirmed; no account created')
    user = response['data']
    attributes = user['attributes']
    if (
        attributes.get('username') != username
        or attributes.get('locked_at')
        or attributes.get('deactivated_at')
        or attributes.get('admin')
        or attributes.get('can_request_admin')
    ):
        raise RuntimeError(
            'Password reset requires the same active ordinary user; no privileges changed'
        )
    user_id = user.get('id')
    if not isinstance(user_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', user_id):
        raise RuntimeError('User identity could not be confirmed')
    if credentials.get('user_id') is not None and credentials['user_id'] != user_id:
        raise RuntimeError('User identity changed; retain the private recovery checkpoint')
    return {'username': username, 'user_id': user_id}


def set_password(request, credentials):
    identity = inspect_user(request, credentials)
    password = credentials.get('password')
    if (
        not isinstance(password, str)
        or not 16 <= len(password) <= 1024
        or any(ord(c) < 32 for c in password)
    ):
        raise RuntimeError('Use a password of 16–1024 characters without control characters')
    if credentials.get('user_id') != identity['user_id']:
        raise RuntimeError('A confirmed user ID is required before changing a password')
    status, _ = request(
        '/api/admin/v1/users/' + identity['user_id'] + '/set-password', {'password': password}
    )
    if status != 204:
        raise RuntimeError('Password change was not confirmed; retain the private checkpoint')
    return identity


def main():
    payload = json.load(sys.stdin)
    if payload.get('action') not in ('inspect', 'password'):
        raise RuntimeError('Unknown user password operation')
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

    # Refuse redirects for both the token request and the authenticated API.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    basic = base64.b64encode(
        (client['client_id'] + ':' + client['client_secret']).encode()
    ).decode()
    request = urllib.request.Request(
        base + '/oauth2/token',
        data=urllib.parse.urlencode(
            {'grant_type': 'client_credentials', 'scope': 'urn:mas:admin'}
        ).encode(),
        headers={'Authorization': 'Basic ' + basic},
    )
    with opener.open(request, timeout=20) as response:
        token = json.load(response)['access_token']

    def send(path, body=None):
        request = urllib.request.Request(
            base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
        )
        try:
            response = opener.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read(1048577)
            if len(content) > 1048576:
                raise RuntimeError('Oversized MAS response')
            return response.status, json.loads(content) if content else None

    result = (inspect_user if payload['action'] == 'inspect' else set_password)(
        send, payload['credentials']
    )
    print(json.dumps(result))  # Public account identity only; never a token or password.


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(
            'User password API was not confirmed. Retain private input/checkpoints; repair MAS connectivity and retry the password operation.',
            file=sys.stderr,
        )
        raise SystemExit(1)
