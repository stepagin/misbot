"""Disposable MAS browser registration: stdin contains only fixture credentials.

Runs inside the isolated Synapse container; never against a public provider.
"""

import base64
import http.cookiejar
from html.parser import HTMLParser
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.current = {
                'action': attrs.get('action', ''),
                'method': attrs.get('method', 'GET'),
                'inputs': {},
            }
            self.forms.append(self.current)
        if tag in ('input', 'button') and self.current is not None and attrs.get('name'):
            self.current['inputs'][attrs['name']] = attrs.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


def main():
    payload = json.load(sys.stdin)
    client = payload['client']
    invitation = payload['token']

    def opener():
        jar = http.cookiejar.CookieJar()
        result = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(jar))
        result.fixture_jar = jar
        return result

    service = opener()

    def send(session, url, body=None, headers=None):
        parsed = urllib.parse.urlsplit(url)
        if parsed.hostname not in (None, 'mas', 'mas.bootstrap-test.invalid'):
            raise RuntimeError('Fixture attempted external connection')
        url = (
            'http://mas:8080' + (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
        )
        request = urllib.request.Request(url, data=body, headers=headers or {})
        try:
            response = session.open(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        # HTTP-only private Docker fixture, like oauth_internal.py; never runtime.
        for cookie in session.fixture_jar:
            cookie.secure = False
        with response:
            return response.status, response.headers, response.read().decode()

    basic = base64.b64encode(
        (client['client_id'] + ':' + client['client_secret']).encode()
    ).decode()
    status, _, raw = send(
        service,
        '/oauth2/token',
        urllib.parse.urlencode(
            {'grant_type': 'client_credentials', 'scope': 'urn:mas:admin'}
        ).encode(),
        {'Authorization': 'Basic ' + basic},
    )
    assert status == 200
    auth = {'Authorization': 'Bearer ' + json.loads(raw)['access_token']}

    def register(username):
        browser = opener()
        url = '/register'
        seen = set()
        password = secrets.token_urlsafe(36)
        for step in range(24):
            status, headers, body = send(browser, url)
            location = headers.get('Location')
            if location:
                if 'finish' in url:
                    confirmed, _, _ = send(
                        service, '/api/admin/v1/users/by-username/' + username, headers=auth
                    )
                    if confirmed == 200:
                        return True
                url = urllib.parse.urljoin(url, location)
                continue
            forms = Forms(body).forms
            print(
                json.dumps(
                    {
                        'check': 'fixture_registration_step',
                        'step': step,
                        'status': status,
                        'forms': len(forms),
                        'redirect': bool(location),
                    }
                ),
                flush=True,
            )
            selected = next((form for form in forms if form['method'].upper() == 'POST'), None)
            if selected is None:
                return False
            values = dict(selected['inputs'])
            # Names contain no credential values; useful fixture evidence only.
            print(
                json.dumps(
                    {
                        'check': 'fixture_registration_step',
                        'step': step,
                        'status': status,
                        'fields': sorted(values),
                    }
                ),
                flush=True,
            )
            signature = (urllib.parse.urlsplit(url).path, tuple(sorted(values)))
            if signature in seen:
                return False
            seen.add(signature)
            for key in values:
                if 'token' in key and key not in ('csrf_token', 'csrf'):
                    values[key] = invitation
                elif key == 'username':
                    values[key] = username
                elif key == 'display_name':
                    values[key] = 'Fictional Stage 3'
                elif 'password' in key:
                    values[key] = password
                elif key in ('terms', 'terms_and_conditions', 'accept_terms'):
                    values[key] = 'on'
            target = urllib.parse.urljoin(url, selected['action'])
            status, headers, body = send(
                browser,
                target,
                urllib.parse.urlencode(values).encode(),
                {'Content-Type': 'application/x-www-form-urlencoded'},
            )
            if not headers.get('Location'):
                markers = [
                    word
                    for word in (
                        'password',
                        'weak',
                        'username',
                        'already exists',
                        'registration token',
                        'csrf',
                        'error',
                    )
                    if word in body.lower()
                ]
                print(
                    json.dumps(
                        {
                            'check': 'fixture_registration_step',
                            'post_status': status,
                            'safe_markers': markers,
                        }
                    ),
                    flush=True,
                )
                return False
            url = urllib.parse.urljoin(target, headers['Location'])
        return False

    assert register('stage3-first-user'), 'first fixture registration failed'
    status, _, raw = send(
        service, '/api/admin/v1/users/by-username/stage3-first-user', headers=auth
    )
    assert status == 200
    attr = json.loads(raw)['data']['attributes']
    assert not attr.get('admin', attr.get('can_request_admin', False))
    assert not register('stage3-second-user'), 'token allowed a second registration'
    status, _, _ = send(service, '/api/admin/v1/users/by-username/stage3-second-user', headers=auth)
    assert status == 404
    print(
        json.dumps(
            {
                'check': 'first_invitation_browser_registration',
                'passed': True,
                'ordinary_user': True,
                'second_registration_denied': True,
            }
        ),
        flush=True,
    )


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import traceback

        print(
            json.dumps(
                {
                    'check': 'fixture_registration_error',
                    'error_type': type(error).__name__,
                    'frames': [
                        {'function': frame.name, 'line': frame.lineno}
                        for frame in traceback.extract_tb(error.__traceback__)[-3:]
                    ],
                }
            ),
            flush=True,
        )
        print(
            'Disposable registration assertion failed; inspect protected fixture state',
            file=sys.stderr,
        )
        sys.exit(1)
