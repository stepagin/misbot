"""Private, fixed MAS admin-rights query. Never return credentials or user payloads."""

import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def authorized(attributes):
    return bool(attributes.get('admin', attributes.get('can_request_admin', False))) and not any(
        attributes.get(k) for k in ('locked_at', 'deactivated_at', 'legacy_guest')
    )


def account(request, username, action='inspect', admin=None, expected=None):
    status, response = request(
        '/api/admin/v1/users/by-username/' + urllib.parse.quote(username, safe='')
    )
    if status != 200:
        raise RuntimeError('Existing account not confirmed')
    user = response['data']
    attr = user['attributes']
    if (
        attr.get('username') != username
        or any(attr.get(k) for k in ('locked_at', 'deactivated_at', 'legacy_guest'))
        or not isinstance(user.get('id'), str)
        or not user['id']
        or (expected is not None and expected != user['id'])
    ):
        raise RuntimeError('Active account identity not confirmed')
    if action == 'set-admin':
        if type(admin) is not bool or expected is None:
            raise RuntimeError('Explicit privilege decision required')
        status, response = request(
            '/api/admin/v1/users/' + urllib.parse.quote(user['id'], safe='') + '/set-admin',
            {'admin': admin},
        )
        if status != 200 or response['data']['id'] != user['id']:
            raise RuntimeError('Privilege change not confirmed')
        attr = response['data']['attributes']
        privilege = attr.get('admin', attr.get('can_request_admin'))
        if (
            type(privilege) is not bool
            or privilege != admin
            or attr.get('username') != username
            or any(attr.get(k) for k in ('locked_at', 'deactivated_at', 'legacy_guest'))
        ):
            raise RuntimeError('Privilege change not confirmed')
    elif action != 'inspect':
        raise RuntimeError('Unknown account operation')
    return {'username': username, 'user_id': user['id'], 'admin': authorized(attr)}


def accounts(request):
    """List active identities, following only bounded same-endpoint pagination."""
    path = '/api/admin/v1/users?page[first]=100'
    seen = set()
    rows = {}
    for _ in range(100):
        if path in seen:
            raise RuntimeError('Repeated account page')
        seen.add(path)
        status, response = request(path)
        if (
            status != 200
            or not isinstance(response, dict)
            or not isinstance(response.get('data'), list)
        ):
            raise RuntimeError('Account list not confirmed')
        for user in response['data']:
            attr = user['attributes']
            name = attr.get('username')
            if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._=-]{0,254}', name):
                raise RuntimeError('Invalid account identity')
            if any(attr.get(k) for k in ('locked_at', 'deactivated_at', 'legacy_guest')):
                continue
            rows[name] = {'username': name, 'admin': authorized(attr)}
        following = response.get('links', {}).get('next')
        if not following:
            return {'accounts': sorted(rows.values(), key=lambda row: row['username'])}
        if not isinstance(following, str):
            raise RuntimeError('Invalid account page')
        parsed = urllib.parse.urlsplit(following)
        if parsed.path != '/api/admin/v1/users' or parsed.fragment:
            raise RuntimeError('Invalid account page')
        # Never follow a server-supplied authority with an administrative token.
        query = urllib.parse.parse_qsl(parsed.query, strict_parsing=True)
        if not query or any(not key.startswith('page[') for key, _ in query):
            raise RuntimeError('Invalid account cursor')
        path = parsed.path + '?' + urllib.parse.urlencode(query)
    raise RuntimeError('Account list exceeds page limit; narrow the account selection')


def main():
    payload = json.load(sys.stdin)
    client = payload['client']
    base = client.get('mas_url', 'http://mas:8080').rstrip('/')
    username = payload['username']

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    auth = base64.b64encode((client['client_id'] + ':' + client['client_secret']).encode()).decode()
    req = urllib.request.Request(
        base + '/oauth2/token',
        data=urllib.parse.urlencode(
            {'grant_type': 'client_credentials', 'scope': 'urn:mas:admin'}
        ).encode(),
        headers={'Authorization': 'Basic ' + auth},
    )
    with opener.open(req, timeout=10) as response:
        token = json.load(response)['access_token']

    def request(path, body=None):
        req = urllib.request.Request(
            base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'},
        )
        try:
            response = opener.open(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            return response.status, json.loads(content) if content else None

    if payload.get('action') == 'list':
        result = accounts(request)
    elif payload.get('action'):
        result = account(
            request, username, payload['action'], payload.get('admin'), payload.get('user_id')
        )
    else:
        try:
            result = {'authorized': account(request, username)['admin']}
        except (KeyError, RuntimeError, TypeError):
            result = {'authorized': False}
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"authorized": false}')
