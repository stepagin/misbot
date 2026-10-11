"""Private provisioning response consumed by lifecycle.py, never logged."""

import json
import re
from pathlib import Path
import sys

import requests
from common import private_json
from mas import MAS

ROOT = Path('/config')
DEVICE = 'MATRIX-REFERRAL-01'
SCOPE = 'urn:matrix:client:api:* urn:matrix:client:device:' + DEVICE


def provision(root, api, federation=False, welcome=False, username=None):
    root = Path(root)
    domain = next(iter(json.loads((root / 'bot-config.json').read_text())['servers']))
    cfg = json.loads((root / 'bot-config.json').read_text())['servers'][domain]
    journal = root / (
        'welcome-session.json'
        if welcome
        else 'federation-session.json'
        if federation
        else 'session.json'
    )
    saved_name = (
        json.loads(journal.read_text())['config']['user_id'][1:].split(':')[0]
        if journal.exists()
        else None
    )
    username = (
        username
        or saved_name
        or ('server-guide' if welcome else 'admin-panel' if federation else 'referral-bot')
    )
    if not re.fullmatch(r'[a-z0-9][a-z0-9._=-]{0,63}', username) or (
        saved_name and saved_name != username
    ):
        raise RuntimeError('Bot tag is immutable after creation')
    display = (
        'Серверный помощник'
        if welcome
        else 'Панель администратора Misbot'
        if federation
        else 'Реферальный бот'
    )
    device = 'MATRIX-WELCOME-01' if welcome else 'MATRIX-FEDERATION-01' if federation else DEVICE
    scope = 'urn:matrix:client:api:* urn:matrix:client:device:' + device
    marker = root / (
        'managed-welcome-user.json'
        if welcome
        else 'managed-federation-user.json'
        if federation
        else 'managed-user.json'
    )
    if journal.exists():
        result = json.loads(journal.read_text())
        user = api.active_user(result['metadata']['mas_user_id'])
        session = api.api('personal-sessions/' + result['metadata']['personal_session_id'])['data']
        if set(session['attributes']['scope'].split()) != set(scope.split()):
            raise RuntimeError('Unexpected referral session scope; refusing replacement')
    else:
        existing = next(
            (u for u in api.items('users') if u['attributes']['username'] == username), None
        )
        if marker.exists():
            owned = json.loads(marker.read_text())
            if not existing or existing['id'] != owned['mas_user_id']:
                raise RuntimeError('Managed referral account differs; restore from backup')
            user = api.active_user(existing['id'])
        else:
            if existing:
                raise RuntimeError('Referral username already occupied; existing account preserved')
            user = api.api(
                'users', method='POST', data={'username': username, 'displayname': display}
            )['data']
            private_json(marker, {'mas_user_id': user['id'], 'domain': domain})
        attr = user['attributes']
        if attr['username'] != username or attr.get('admin', attr.get('can_request_admin', False)):
            raise RuntimeError('Referral account is not an ordinary bot account')
        session = api.api(
            'personal-sessions',
            method='POST',
            data={'actor_user_id': user['id'], 'human_name': display, 'scope': scope},
        )['data']
        result = {
            'config': {
                'user_id': '@' + username + ':' + domain,
                'server_name': domain,
                'device_id': device,
                'homeserver_url': cfg['synapse_url'],
                'access_token': session['attributes']['access_token'],
            },
            'metadata': {
                'mas_user_id': user['id'],
                'personal_session_id': session['id'],
                'user_id': '@' + username + ':' + domain,
                'device_id': device,
                'scope': scope,
            },
        }
        private_json(journal, result)
    attr = user['attributes']
    if attr['username'] != username or attr.get('admin', attr.get('can_request_admin', False)):
        raise RuntimeError('Referral account is not an ordinary bot account')
    if (
        result['config']['user_id'] != '@' + username + ':' + domain
        or result['config']['device_id'] != device
    ):
        raise RuntimeError('Referral account or device mismatch')
    policy = json.loads((root / 'bot-policy.json').read_text())['servers'][domain]
    result['config'].update(
        invitation_lifetime_seconds=policy['invitation_lifetime_seconds'],
        invitation_usage_limit=policy['invitation_usage_limit'],
    )
    return result


def main():
    configs = json.loads((ROOT / 'bot-config.json').read_text())['servers']
    api = MAS(next(iter(configs.values())))
    import argparse

    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--federation', action='store_true')
    group.add_argument('--welcome', action='store_true')
    parser.add_argument('--username')
    args = parser.parse_args()
    result = provision(ROOT, api, args.federation, args.welcome, args.username)
    config = result['config']
    r = requests.get(
        config['homeserver_url'] + '/_matrix/client/v3/account/whoami',
        headers={'Authorization': 'Bearer ' + config['access_token']},
        timeout=15,
    )
    if (
        r.status_code != 200
        or r.json().get('user_id') != config['user_id']
        or r.json().get('device_id') != config['device_id']
    ):
        raise RuntimeError(
            'Saved bot session is not accepted; restore or repair without resetting its device'
        )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Exceptions from SDK/network may contain response bodies or credentials.
        print('Referral provisioning failed; existing account/session preserved', file=sys.stderr)
        sys.exit(1)
