"""Fixed membership operations for the saved bot, with current invitation checks."""

import json
import re
import secrets
import sys
import urllib.parse
import urllib.request


def main():
    p = json.load(sys.stdin)
    cfg = p['session']
    action = p['action']
    arg = p['argument']

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None

    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))

    def call(path, body=None):
        req = urllib.request.Request(
            'http://synapse:8008/_matrix/client/v3/' + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                'Authorization': 'Bearer ' + cfg['access_token'],
                'Content-Type': 'application/json',
            },
        )
        with opener.open(req, timeout=15) as r:
            b = r.read(1048577)
            if len(b) > 1048576:
                raise ValueError()
            return json.loads(b)

    if call('account/whoami')['user_id'] != cfg['user_id']:
        raise ValueError()
    # Minimal filter: no decrypted messages or unrelated user data.
    # Synapse caches initial /sync responses by their literal filter. A fresh,
    # valid exclusion of our unique nonexistent room prevents an older snapshot
    # from hiding a new invite or authorizing an already revoked invitation.
    excluded = '!misbot-sync-' + secrets.token_hex(16) + ':' + cfg['user_id'].split(':', 1)[1]
    filt = json.dumps(
        {
            'room': {
                'not_rooms': [excluded],
                'timeline': {'limit': 0},
                'state': {'types': ['m.room.member']},
                'account_data': {'types': []},
                'ephemeral': {'types': []},
            },
            'presence': {'types': []},
            'account_data': {'types': []},
        }
    )
    invites = call('sync?timeout=0&filter=' + urllib.parse.quote(filt))['rooms'].get('invite', {})

    def sender(room):
        events = invites[room]['invite_state']['events']
        return next(
            (
                e['sender']
                for e in events
                if e.get('type') == 'm.room.member'
                and e.get('state_key') == cfg['user_id']
                and e['content'].get('membership') == 'invite'
            ),
            None,
        )

    if action == 'room-invitations':
        result = {'invitations': [{'room': r, 'sender': sender(r)} for r in invites]}
    else:
        if action == 'room-invite':
            arg = json.loads(arg)
            room = arg['room']
            user = arg['mxid']
            if not re.fullmatch(r'@federation-bot:[a-z0-9.-]+', user):
                raise ValueError()
        else:
            room = arg
        if not isinstance(room, str) or not re.fullmatch(r'![^\s/]{1,254}', room):
            raise ValueError()
        if action in ('room-accept', 'room-reject') and room not in invites:
            result = {'status': 'no_current_invitation'}
        else:
            if action == 'room-accept':
                call('join/' + urllib.parse.quote(room, safe=''), {})
            elif action in ('room-reject', 'room-leave'):
                call('rooms/' + urllib.parse.quote(room, safe='') + '/leave', {})
            elif action == 'room-invite':
                call('rooms/' + urllib.parse.quote(room, safe='') + '/invite', {'user_id': user})
            else:
                raise ValueError()
            result = {'status': 'applied', 'room': room}
    print(json.dumps({'ok': True, 'result': result}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"ok":false,"code":"matrix_invitation_or_policy_unconfirmed"}')
