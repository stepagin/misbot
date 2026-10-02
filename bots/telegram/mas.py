import threading
import time
from uuid import UUID
from urllib.parse import urljoin, urlsplit
from urllib.parse import quote

import requests

from common import BotError


class MAS:
    def __init__(self, config):
        self.config = config
        self.base = config['mas_url'].rstrip('/')
        self.token = None
        self.expires = 0
        self.lock = threading.RLock()

    def request(self, path, method='GET', data=None, params=None):
        url = urljoin(self.base + '/', path)
        if urlsplit(url).netloc != urlsplit(self.base).netloc:
            raise BotError('Ошибка адреса MAS.')
        with self.lock:
            if time.time() >= self.expires:
                r = requests.post(self.base + '/oauth2/token',
                    auth=(self.config['client_id'], self.config['client_secret']),
                    data={'grant_type': 'client_credentials', 'scope': 'urn:mas:admin'},
                    timeout=20, allow_redirects=False)
                if r.status_code != 200:
                    raise BotError('MAS не выдал доступ службе бота.')
                token = r.json()
                self.token = token['access_token']
                self.expires = time.time() + max(1, token.get('expires_in', 60) - 20)
            r = requests.request(method, url, params=params, json=data,
                headers={'Authorization': 'Bearer ' + self.token}, timeout=25,
                allow_redirects=False)
            if r.status_code == 401:
                self.expires = 0
            if r.status_code not in (200, 201, 204):
                raise BotError('MAS отклонил операцию (HTTP %s).' % r.status_code)
            return r.json() if r.content else None

    def api(self, path, **kwargs):
        return self.request('/api/admin/v1/' + path, **kwargs)

    def items(self, resource, filters=None):
        page = self.api(resource, params={'page[first]': 100, **(filters or {})})
        while True:
            yield from page.get('data') or []
            nxt = page.get('links', {}).get('next')
            if not nxt:
                return
            page = self.request(nxt)

    def active_user(self, user_id):
        user = self.api('users/' + user_id)['data']
        attr = user['attributes']
        if attr.get('deactivated_at') or attr.get('locked_at') or attr.get('legacy_guest'):
            raise BotError('Нужен действующий аккаунт Misbot. Этот аккаунт недоступен.')
        return user

    def administrator_user(self, user_id):
        user = self.active_user(user_id)
        attr = user['attributes']
        if not attr.get('admin', attr.get('can_request_admin', False)):
            raise BotError('Изменять федерацию может только администратор MAS. У этой учётной записи нет доступа.')
        return user

    def matrix_user(self, mxid, server_name):
        suffix = ':' + server_name
        if not isinstance(mxid, str) or len(mxid) > 255 or not mxid.startswith('@') or not mxid.endswith(suffix):
            raise BotError('Приглашения доступны пользователям этого сервера.')
        username = mxid[1:-len(suffix)]
        if not username or ':' in username or any(ord(c) < 32 for c in username):
            raise BotError('Недопустимый Matrix ID.')
        user = self.api('users/by-username/' + quote(username, safe=''))['data']
        return self.active_user(user['id'])

    def device_start(self):
        r = requests.post(self.base + '/oauth2/device',
            auth=(self.config['client_id'], self.config['client_secret']),
            data={'scope': 'openid'}, timeout=20, allow_redirects=False)
        if r.status_code != 200:
            raise BotError('MAS не разрешил подтверждение доступа (HTTP %s).' % r.status_code)
        return r.json()

    def device_token(self, code):
        r = requests.post(self.base + '/oauth2/token',
            auth=(self.config['client_id'], self.config['client_secret']),
            data={'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': code},
            timeout=20, allow_redirects=False)
        return r.status_code, r.json()

    def device_user(self, token):
        if 'openid' not in token.get('scope', '').split():
            raise BotError('Misbot не подтвердил вход.')
        r = requests.get(self.base + '/oauth2/userinfo',
            headers={'Authorization': 'Bearer ' + token['access_token']}, timeout=20, allow_redirects=False)
        if r.status_code != 200:
            raise BotError('MAS не подтвердил учётную запись.')
        subject = r.json()['sub']
        try:
            number = UUID(subject).int
        except ValueError:
            user_id = subject
        else:
            alphabet = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
            user_id = ''
            for _ in range(26):
                user_id = alphabet[number & 31] + user_id
                number >>= 5
        user = self.active_user(user_id)
        # Revoke the temporary OAuth token; only the local, expiring authorization remains.
        requests.post(self.base + '/oauth2/revoke',
            auth=(self.config['client_id'], self.config['client_secret']),
            data={'token': token['access_token'], 'token_type_hint': 'access_token'},
            timeout=15, allow_redirects=False)
        if token.get('refresh_token'):
            requests.post(self.base + '/oauth2/revoke',
                auth=(self.config['client_id'], self.config['client_secret']),
                data={'token': token['refresh_token'], 'token_type_hint': 'refresh_token'},
                timeout=15, allow_redirects=False)
        return user
