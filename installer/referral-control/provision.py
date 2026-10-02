"""Private provisioning response consumed by lifecycle.py, never logged."""
import json
from pathlib import Path
import sys

import requests
from common import private_json
from mas import MAS

ROOT = Path('/config')
DEVICE = 'MATRIX-REFERRAL-01'
SCOPE = 'urn:matrix:client:api:* urn:matrix:client:device:' + DEVICE


def provision(root, api):
    root = Path(root)
    domain = next(iter(json.loads((root / 'bot-config.json').read_text())['servers']))
    cfg = json.loads((root / 'bot-config.json').read_text())['servers'][domain]
    journal = root / 'session.json'
    marker = root / 'managed-user.json'
    if journal.exists():
        result = json.loads(journal.read_text())
        user = api.active_user(result['metadata']['mas_user_id'])
        session = api.api('personal-sessions/' + result['metadata']['personal_session_id'])['data']
        if set(session['attributes']['scope'].split()) != set(SCOPE.split()):
            raise RuntimeError('Unexpected referral session scope; refusing replacement')
    else:
        existing = next((u for u in api.items('users') if u['attributes']['username'] == 'referral-bot'), None)
        if marker.exists():
            owned = json.loads(marker.read_text())
            if not existing or existing['id'] != owned['mas_user_id']:
                raise RuntimeError('Managed referral account differs; restore from backup')
            user = api.active_user(existing['id'])
        else:
            if existing:
                raise RuntimeError('Referral username already occupied; existing account preserved')
            user = api.api('users', method='POST', data={'username': 'referral-bot', 'displayname': 'Реферальный бот'})['data']
            private_json(marker, {'mas_user_id': user['id'], 'domain': domain})
        attr = user['attributes']
        if attr['username'] != 'referral-bot' or attr.get('admin', attr.get('can_request_admin', False)):
            raise RuntimeError('Referral account is not an ordinary bot account')
        session = api.api('personal-sessions', method='POST', data={'actor_user_id': user['id'],
            'human_name': 'Реферальный бот', 'scope': SCOPE})['data']
        result = {'config': {'user_id': '@referral-bot:' + domain, 'server_name': domain,
            'device_id': DEVICE, 'homeserver_url': cfg['synapse_url'],
            'access_token': session['attributes']['access_token']},
            'metadata': {'mas_user_id': user['id'], 'personal_session_id': session['id'],
                'user_id': '@referral-bot:' + domain, 'device_id': DEVICE, 'scope': SCOPE}}
        private_json(journal, result)
    attr = user['attributes']
    if attr['username'] != 'referral-bot' or attr.get('admin', attr.get('can_request_admin', False)):
        raise RuntimeError('Referral account is not an ordinary bot account')
    if result['config']['user_id'] != '@referral-bot:' + domain or result['config']['device_id'] != DEVICE:
        raise RuntimeError('Referral account or device mismatch')
    policy = json.loads((root / 'bot-policy.json').read_text())['servers'][domain]
    result['config'].update(invitation_lifetime_seconds=policy['invitation_lifetime_seconds'],
                            invitation_usage_limit=policy['invitation_usage_limit'])
    return result


def main():
    configs = json.loads((ROOT / 'bot-config.json').read_text())['servers']
    api = MAS(next(iter(configs.values())))
    result = provision(ROOT, api)
    config = result['config']
    r = requests.get(config['homeserver_url'] + '/_matrix/client/v3/account/whoami',
        headers={'Authorization': 'Bearer ' + config['access_token']}, timeout=15)
    if r.status_code != 200 or r.json().get('user_id') != config['user_id'] or r.json().get('device_id') != DEVICE:
        raise RuntimeError('Saved bot session is not accepted; restore or repair without resetting its device')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Exceptions from SDK/network may contain response bodies or credentials.
        print('Referral provisioning failed; existing account/session preserved', file=sys.stderr)
        sys.exit(1)
