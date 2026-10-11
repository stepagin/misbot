"""Private MAS transport; stdin/stdout are captured, never written to operation logs."""

import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    payload = json.load(sys.stdin)
    client = payload['client']
    opener = urllib.request.build_opener(NoRedirect())
    basic = base64.b64encode(
        (client['client_id'] + ':' + client['client_secret']).encode()
    ).decode()
    request = urllib.request.Request(
        'http://mas:8080/oauth2/token',
        data=urllib.parse.urlencode(
            {'grant_type': 'client_credentials', 'scope': 'urn:mas:admin'}
        ).encode(),
        headers={'Authorization': 'Basic ' + basic},
    )
    with opener.open(request, timeout=20) as response:
        bearer = json.load(response)['access_token']
    path = payload['path']
    if not re.fullmatch(
        r'/api/admin/v1/user-registration-tokens(?:/[0-9A-HJKMNP-TV-Z]{26}(?:/revoke)?)?(?:\?[^\r\n]*)?',
        path,
    ):
        raise ValueError('Unsafe MAS resource path')
    body = payload.get('body')
    request = urllib.request.Request(
        'http://mas:8080' + path,
        method=payload.get('method', 'GET'),
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': 'Bearer ' + bearer, 'Content-Type': 'application/json'},
    )
    try:
        response = opener.open(request, timeout=20)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('Oversized MAS response')
        # Error bodies may contain secrets and are never returned.
        data = json.loads(raw) if response.status in (200, 201) and raw else None
        print(json.dumps({'status': response.status, 'body': data}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('MAS registration API unavailable; retry the saved operation', file=sys.stderr)
        sys.exit(1)
