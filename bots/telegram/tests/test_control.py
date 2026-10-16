import json
from pathlib import Path
import sys
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import Bridge, check_peer
from common import BotError, domain
from mas import MAS


class FakeMAS:
    def __init__(self, cfg):
        self.tokens = {}
        self.admin = True
        self.active = True
        self.device_result = (400, {'error': 'authorization_pending'})

    def api(self, path, method='GET', data=None):
        if method == 'POST' and path == 'user-registration-tokens':
            resource = str(len(self.tokens) + 1)
            item = {
                'id': resource,
                'attributes': {
                    'token': data['token'],
                    'usage_limit': data['usage_limit'],
                    'expires_at': data['expires_at'],
                    'times_used': 0,
                    'valid': True,
                    'revoked_at': None,
                },
            }
            self.tokens[resource] = item
            return {'data': item}
        resource = path.split('/')[1]
        item = self.tokens[resource]
        if path.endswith('/revoke'):
            item['attributes'].update(valid=False, revoked_at='now')
        return {'data': item}

    def items(self, resource):
        yield from self.tokens.values()

    def active_user(self, uid):
        if not self.active:
            raise BotError('inactive')
        return {'id': uid, 'attributes': {'username': 'member', 'admin': self.admin}}

    def matrix_user(self, mxid, server_name):
        if (
            not isinstance(mxid, str)
            or not mxid.startswith('@')
            or not mxid.endswith(':' + server_name)
        ):
            raise BotError('not local')
        return self.active_user('MAS-MATRIX-USER')

    def device_start(self):
        return {
            'device_code': 'private-device-code',
            'user_code': 'HUMAN-CODE',
            'verification_uri': 'https://mas.example/link',
            'expires_in': 600,
            'interval': 5,
        }

    def device_token(self, code):
        return self.device_result

    def device_user(self, token):
        return self.active_user('MAS-USER')


class ControlTests(unittest.TestCase):
    def test_snapshot_preserves_live_running_job_and_uses_fixed_private_output(self):
        self.bridge.db.execute('PRAGMA journal_mode=WAL')
        self.bridge.db.execute(
            'INSERT INTO management_jobs VALUES (?,?,?,?,?,?)',
            ('live-job', 'misbot.ru', 10, 'backup', 'running', None),
        )
        self.bridge.db.commit()
        response = self.bridge.handle({'action': 'backup_snapshot'})
        saved = self.root / 'data/backup.sqlite3'
        import hashlib

        self.assertEqual(response, {'sha256': hashlib.sha256(saved.read_bytes()).hexdigest()})
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(saved) as connection:
            self.assertEqual(
                connection.execute(
                    'SELECT state FROM management_jobs WHERE id=?', ('live-job',)
                ).fetchone()[0],
                'running',
            )
        self.assertEqual(
            self.bridge.db.execute(
                'SELECT state FROM management_jobs WHERE id=?', ('live-job',)
            ).fetchone()[0],
            'running',
        )
        with self.assertRaises(BotError):
            self.bridge.handle({'action': 'backup_snapshot', 'output': '/tmp/forbidden'})

    def test_root_snapshot_normalizes_group_despite_controller_worker_group(self):
        with patch('bridge.os.geteuid', return_value=0), patch('bridge.os.fchown') as chown:
            self.bridge.handle({'action': 'backup_snapshot'})
        self.assertEqual(chown.call_count, 1)
        self.assertEqual(chown.call_args.args[1:], (0, 0))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir()
        (self.root / 'config').mkdir()
        (self.root / 'backups').mkdir()
        (self.root / 'bot-config.json').write_text(
            json.dumps(
                {
                    'servers': {
                        'misbot.ru': {
                            'stack_root': str(self.root),
                            'synapse_url': 'https://matrix.example',
                        }
                    }
                }
            )
        )
        (self.root / 'bot-policy.json').write_text(
            json.dumps(
                {
                    'servers': {
                        'misbot.ru': {
                            'invitation_lifetime_seconds': 604800,
                            'invitation_usage_limit': 1,
                            'registration_required': True,
                            'federation_servers': ['misbot.ru'],
                        }
                    }
                }
            )
        )
        self.bridge = Bridge(self.root / 'bot-config.json', FakeMAS)
        self.api = self.bridge.apis['misbot.ru']

    def tearDown(self):
        self.bridge.db.close()
        self.tmp.cleanup()

    def call(self, action, uid=10, **extras):
        return self.bridge.handle({'action': action, 'server': 'misbot.ru', 'uid': uid, **extras})

    def authorize(self, uid=10, user_id=None):
        self.bridge.db.execute(
            'INSERT INTO authorizations VALUES (?,?,?,?)',
            (
                'misbot.ru',
                uid,
                user_id or ('MAS-USER' if uid == 10 else f'MAS-USER-{uid}'),
                int(time.time()) + 3600,
            ),
        )
        self.bridge.db.commit()

    def sign_in(self, uid=10, user_id='MAS-USER'):
        self.call('auth_start', uid=uid)
        self.bridge.db.execute('UPDATE auth_requests SET next_poll=0')
        self.api.device_result = (200, {'scope': 'openid'})
        with patch.object(self.api, 'device_user', return_value=self.api.active_user(user_id)):
            self.bridge.poll_auths()

    def test_slow_mas_does_not_block_readiness_or_logout_and_revalidates(self):
        self.authorize()
        started, release = threading.Event(), threading.Event()
        original = self.api.active_user

        def slow(user_id):
            started.set()
            if not release.wait(3):
                raise TimeoutError()
            return original(user_id)

        with patch.object(self.api, 'active_user', side_effect=slow), ThreadPoolExecutor(2) as pool:
            result = pool.submit(self.call, 'identity')
            try:
                self.assertTrue(started.wait(1))
                ready = pool.submit(self.bridge.handle, {'action': 'servers'})
                self.assertIn('misbot.ru', ready.result(timeout=1))
                logout = pool.submit(self.call, 'logout')
                self.assertTrue(logout.result(timeout=1))
            finally:
                release.set()
            self.assertFalse(result.result(timeout=1)['authenticated'])

    def test_inflight_oauth_result_cannot_restore_logged_out_authorization(self):
        self.call('auth_start')
        self.bridge.db.execute('UPDATE auth_requests SET next_poll=0')
        self.bridge.db.commit()
        started, release = threading.Event(), threading.Event()

        def token(code):
            started.set()
            if not release.wait(3):
                raise TimeoutError()
            return 200, {'scope': 'openid'}

        with (
            patch.object(self.api, 'device_token', side_effect=token),
            ThreadPoolExecutor(2) as pool,
        ):
            result = pool.submit(self.bridge.poll_auths)
            try:
                self.assertTrue(started.wait(1))
                self.assertTrue(pool.submit(self.call, 'logout').result(timeout=1))
                self.assertIn('misbot.ru', self.bridge.handle({'action': 'servers'}))
            finally:
                release.set()
            result.result(timeout=1)
        self.assertFalse(self.call('identity')['authenticated'])

    def test_matrix_history_http_releases_state_lock(self):
        request = {'server': 'misbot.ru', 'mxid': '@member:misbot.ru'}
        self.bridge.handle({'action': 'matrix_invite', 'request_id': 'matrix-history', **request})
        self.authorize()
        started, release = threading.Event(), threading.Event()
        original = self.api.api

        def slow(path, **kwargs):
            started.set()
            if not release.wait(3):
                raise TimeoutError()
            return original(path, **kwargs)

        with patch.object(self.api, 'api', side_effect=slow), ThreadPoolExecutor(2) as pool:
            result = pool.submit(self.bridge.handle, {'action': 'matrix_tokens', **request})
            try:
                self.assertTrue(started.wait(1))
                self.assertTrue(pool.submit(self.call, 'logout').result(timeout=1))
            finally:
                release.set()
            self.assertEqual(len(result.result(timeout=1)['tokens']), 1)

    def test_control_configuration_binds_exactly_one_server_before_database_changes(self):
        original = json.loads((self.root / 'bot-config.json').read_text())
        for servers in ({}, {'misbot.ru': {}, 'other.example': {}}, []):
            with self.subTest(servers=servers):
                (self.root / 'bot-config.json').write_text(json.dumps({'servers': servers}))
                with self.assertRaisesRegex(BotError, 'ровно к одному серверу'):
                    Bridge(self.root / 'bot-config.json', FakeMAS)
        (self.root / 'bot-config.json').write_text(json.dumps(original))
        policy = json.loads((self.root / 'bot-policy.json').read_text())
        policy['servers']['other.example'] = policy['servers']['misbot.ru']
        (self.root / 'bot-policy.json').write_text(json.dumps(policy))
        with self.assertRaisesRegex(BotError, 'только к его серверу'):
            Bridge(self.root / 'bot-config.json', FakeMAS)

    def test_new_telegram_login_moves_history_and_revokes_previous_account(self):
        self.sign_in()
        invitation = self.call('invite', request_id='before-switch')
        confirmation = self.call('fed_prepare', operation='add', domain='other.example')[
            'confirmation'
        ]
        self.bridge.db.execute(
            'INSERT INTO management_jobs VALUES (?,?,?,?,?,?)',
            ('old-job', 'misbot.ru', 10, 'backup', 'complete', '{}'),
        )
        self.bridge.db.execute(
            'INSERT INTO telegram_federation_jobs VALUES (?,?,?)', ('old-fed-job', 'misbot.ru', 10)
        )
        self.bridge.db.execute(
            'INSERT INTO telegram_notifications (id,server,uid,message) VALUES (?,?,?,?)',
            ('notice', 'misbot.ru', 10, 'private notice'),
        )
        self.bridge.db.commit()
        self.call('auth_start', uid=11)
        self.assertTrue(self.call('identity')['authenticated'])
        self.sign_in(uid=11)
        self.assertFalse(self.call('identity')['authenticated'])
        self.assertTrue(self.call('identity', uid=11)['authenticated'])
        self.assertEqual(self.call('tokens', uid=11)['tokens'], [invitation])
        for table in ('management_jobs', 'telegram_federation_jobs'):
            self.assertEqual(self.bridge.db.execute(f'SELECT uid FROM {table}').fetchone()[0], 11)
        self.assertFalse(self.bridge.db.execute('SELECT 1 FROM telegram_notifications').fetchone())
        self.assertFalse(
            self.bridge.db.execute(
                'SELECT 1 FROM confirmations WHERE id=?', (confirmation,)
            ).fetchone()
        )
        with self.assertRaises(BotError):
            self.call('fed_apply', uid=11, confirmation=confirmation)
        self.assertFalse(
            self.call('revoke', uid=11, resource=invitation['id'])['attributes']['valid']
        )

    def test_logout_then_new_telegram_account_preserves_mas_ownership_after_restart(self):
        self.sign_in()
        invitation = self.call('invite', request_id='before-logout')
        self.call('logout')
        self.bridge.db.close()
        self.bridge = Bridge(self.root / 'bot-config.json', lambda _: self.api)
        self.sign_in(uid=11)
        self.assertEqual(self.call('tokens', uid=11)['tokens'], [invitation])
        self.assertFalse(self.call('identity')['authenticated'])

    def test_another_mas_identity_does_not_inherit_private_history_of_same_telegram_uid(self):
        self.sign_in()
        invitation = self.call('invite', request_id='first-identity')
        self.sign_in(user_id='ANOTHER-MAS-USER')
        self.assertEqual(self.call('tokens')['tokens'], [])
        with self.assertRaises(BotError):
            self.call('revoke', resource=invitation['id'])
        self.sign_in(uid=11)
        self.assertEqual(self.call('tokens', uid=11)['tokens'], [invitation])
        self.assertTrue(self.call('identity')['authenticated'])

    def test_superseded_pending_login_is_not_polled_after_account_move(self):
        self.sign_in()
        self.call('auth_start', uid=11)
        self.call('auth_start', uid=10)
        self.bridge.db.execute('UPDATE auth_requests SET next_poll=0')
        with patch.object(self.api, 'device_token', wraps=self.api.device_token) as poll:
            self.bridge.poll_auths()
        self.assertEqual(poll.call_count, 1)
        self.assertFalse(self.call('identity')['authenticated'])
        self.assertTrue(self.call('identity', uid=11)['authenticated'])

    def test_legacy_duplicate_mas_logins_merge_into_latest_binding_without_history_leak(self):
        self.authorize()
        self.authorize(11, user_id='MAS-USER')
        self.bridge.db.execute('UPDATE authorizations SET expires=expires+100 WHERE uid=11')
        self.bridge.db.commit()
        invitation = self.call('invite', request_id='legacy-owner')
        self.bridge.db.close()
        self.bridge = Bridge(self.root / 'bot-config.json', lambda _: self.api)
        self.assertFalse(self.call('identity')['authenticated'])
        self.assertEqual(self.call('tokens', uid=11)['tokens'], [invitation])
        self.sign_in(user_id='ANOTHER-MAS-USER')
        self.assertEqual(self.call('tokens')['tokens'], [])
        self.assertEqual(self.call('tokens', uid=11)['tokens'], [invitation])

    def test_legacy_logged_out_history_is_retained_without_granting_unknown_ownership(self):
        self.authorize()
        invitation = self.call('invite', request_id='legacy-logged-out')
        self.call('logout')
        self.bridge.db.execute('DROP TABLE telegram_accounts')
        self.bridge.db.commit()
        self.bridge.db.close()
        self.bridge = Bridge(self.root / 'bot-config.json', lambda _: self.api)
        self.sign_in(user_id='ANOTHER-MAS-USER')
        self.assertEqual(self.call('tokens')['tokens'], [])
        retained = self.bridge.db.execute(
            'SELECT uid,resource FROM invitations WHERE resource=?', (invitation['id'],)
        ).fetchone()
        self.assertTrue(retained['uid'].startswith('retired:'))
        self.assertEqual(retained['resource'], invitation['id'])

    def test_legacy_foreign_pending_grant_does_not_block_local_login(self):
        self.call('auth_start')
        self.bridge.db.execute(
            'INSERT INTO auth_requests SELECT ?,?,11,device_code,expires,interval,0,NULL,'
            'user_code,verification_uri FROM auth_requests LIMIT 1',
            ('foreign-request', 'other.example'),
        )
        self.bridge.db.execute('UPDATE auth_requests SET next_poll=0')
        self.api.device_result = (200, {'scope': 'openid'})
        with patch.object(self.api, 'device_token', wraps=self.api.device_token) as poll:
            self.bridge.poll_auths()
        self.assertEqual(poll.call_count, 1)
        self.assertTrue(self.call('identity')['authenticated'])
        self.assertTrue(
            self.bridge.db.execute(
                'SELECT 1 FROM auth_requests WHERE request=?', ('foreign-request',)
            ).fetchone()
        )

    def test_invitation_policy_unlimited_count_and_idempotent_delivery(self):
        from datetime import datetime

        self.authorize()
        before = time.time()
        first = self.call('invite', request_id='update:1')
        self.assertEqual(first, self.call('invite', request_id='update:1'))
        for i in range(2, 12):
            self.call('invite', request_id='update:' + str(i), usage_limit=99, expires_at=None)
        self.assertEqual(len(self.api.tokens), 11)
        self.assertEqual(first['attributes']['usage_limit'], 1)
        expiry = datetime.fromisoformat(first['attributes']['expires_at']).timestamp()
        self.assertLess(abs(expiry - before - 604800), 2)

    def test_token_ownership_and_revoke(self):
        self.authorize()
        self.authorize(11)
        token = self.call('invite', request_id='update:1')
        with self.assertRaises(BotError):
            self.call('revoke', uid=11, resource=token['id'])
        with self.assertRaises(BotError):
            self.call('invite', uid=11, request_id='update:1')
        self.assertEqual(self.call('tokens', uid=11)['tokens'], [])
        self.assertFalse(self.call('revoke', resource=token['id'])['attributes']['valid'])
        self.assertFalse(self.call('revoke', resource=token['id'])['attributes']['valid'])

    def test_matrix_filter_before_pagination_keeps_used_even_after_expiry(self):
        def call(action, **kwargs):
            return self.bridge.handle(
                {
                    'action': 'matrix_' + action,
                    'server': 'misbot.ru',
                    'mxid': '@member:misbot.ru',
                    **kwargs,
                }
            )

        used = call('invite', request_id='matrix:used')
        used['attributes'].update(expires_at='2020-01-01T00:00:00Z', times_used=1, valid=False)
        active = [call('invite', request_id='matrix:active:' + str(i)) for i in range(6)]
        for i in range(8):
            hidden = call('invite', request_id='matrix:hidden:' + str(i))
            if i % 2:
                hidden['attributes'].update(expires_at='2020-01-01T00:00:00Z', valid=False)
            else:
                call('revoke', resource=hidden['id'])
        first = call('tokens')
        self.assertEqual(
            [x['id'] for x in first['tokens']], [x['id'] for x in reversed(active[1:])]
        )
        self.assertTrue(first['has_more'])
        last = call('tokens', offset=5)
        self.assertEqual([x['id'] for x in last['tokens']], [active[0]['id'], used['id']])
        self.assertFalse(last['has_more'])
        with self.assertRaises(BotError):
            call('tokens', limit=21)

    def test_lost_post_response_reuses_same_token(self):
        self.authorize()
        original = self.api.api

        def fail_after_post(path, **kwargs):
            result = original(path, **kwargs)
            if kwargs.get('method') == 'POST':
                raise TimeoutError('response lost')
            return result

        self.api.api = fail_after_post
        with self.assertRaises(TimeoutError):
            self.call('invite', request_id='update:1')
        self.api.api = original
        self.assertEqual(len(self.call('tokens')['tokens']), 1)
        self.call('invite', request_id='update:1')
        self.assertEqual(len(self.api.tokens), 1)

    def test_admin_role_rechecked_and_logout(self):
        with self.assertRaises(BotError):
            self.call('federation')
        self.authorize()
        self.assertEqual(self.call('federation'), ['misbot.ru'])
        self.api.admin = False
        with self.assertRaises(BotError):
            self.call('fed_prepare', operation='add', domain='matrix.org')
        self.api.admin = True
        self.call('logout')
        with self.assertRaises(BotError):
            self.call('federation')

    def test_device_authorization_does_not_require_telegram_link(self):
        auth = self.call('auth_start')
        self.assertNotIn('device_code', auth)
        self.assertEqual(auth['user_code'], 'HUMAN-CODE')
        self.bridge.db.execute('UPDATE auth_requests SET next_poll=0')
        self.api.device_result = (200, {'scope': 'openid'})
        self.bridge.poll_auths()
        self.assertEqual(self.call('federation'), ['misbot.ru'])

    def test_non_admin_device_grant_allows_invitations_but_not_federation(self):
        self.call('auth_start')
        self.bridge.db.execute('UPDATE auth_requests SET next_poll=0')
        self.api.admin = False
        self.api.device_result = (200, {'scope': 'openid'})
        self.bridge.poll_auths()
        self.assertEqual(
            self.bridge.db.execute('SELECT COUNT(*) FROM authorizations').fetchone()[0], 1
        )
        self.assertTrue(self.call('identity')['authenticated'])
        self.assertFalse(self.call('identity')['admin'])
        self.assertTrue(self.call('invite', request_id='member:1')['attributes']['valid'])
        with self.assertRaises(BotError):
            self.call('federation')

    def test_invitation_operations_require_current_active_account(self):
        for action in ('invite', 'tokens', 'revoke'):
            with self.subTest(action=action), self.assertRaises(BotError):
                self.call(action, request_id='guest:1', resource='1')
        self.assertEqual(self.api.tokens, {})
        self.authorize()
        self.api.active = False
        self.assertFalse(self.call('identity')['authenticated'])
        with self.assertRaises(BotError):
            self.call('invite', request_id='deleted:1')
        self.api.active = True
        self.bridge.db.execute('UPDATE authorizations SET expires=0')
        with self.assertRaises(BotError):
            self.call('tokens')
        self.call('logout')
        self.assertFalse(self.call('identity')['authenticated'])

    def test_pending_login_is_reused_until_expired(self):
        with patch.object(self.api, 'device_start', wraps=self.api.device_start) as start:
            first = self.call('auth_start')
            self.assertEqual(first['user_code'], self.call('auth_start')['user_code'])
            self.assertEqual(start.call_count, 1)
            self.bridge.db.execute('UPDATE auth_requests SET expires=0')
            self.call('auth_start')
            self.assertEqual(start.call_count, 2)

    def test_old_database_is_migrated_without_losing_authorizations(self):
        self.authorize()
        self.bridge.db.close()
        db = sqlite3.connect(self.root / 'data/bot-control.sqlite3')
        db.execute('DROP TABLE auth_requests')
        db.execute(
            'CREATE TABLE auth_requests (request TEXT PRIMARY KEY, server TEXT, uid INTEGER, '
            'device_code TEXT, expires INTEGER, interval INTEGER, next_poll INTEGER, error TEXT)'
        )
        db.execute('DROP TABLE invitations')
        db.execute(
            'CREATE TABLE invitations (server TEXT, uid INTEGER, request TEXT UNIQUE, code TEXT UNIQUE, '
            'expires INTEGER, resource TEXT, PRIMARY KEY(server, uid, code))'
        )
        db.execute(
            'INSERT INTO invitations VALUES (?,?,?,?,?,?)',
            ('misbot.ru', 10, 'old', 'MIS-old', 123, 'old-resource'),
        )
        db.commit()
        db.close()
        self.bridge = Bridge(self.root / 'bot-config.json', FakeMAS)
        self.assertTrue(self.call('identity')['authenticated'])
        self.assertEqual(len(list(self.bridge.db.execute('PRAGMA table_info(auth_requests)'))), 10)
        old = self.bridge.db.execute(
            'SELECT code, inviter_name, created_at FROM invitations WHERE request=?', ('old',)
        ).fetchone()
        self.assertEqual(
            (old['code'], old['inviter_name'], old['created_at']), ('MIS-old', None, None)
        )

    def test_mas_openid_grant_and_inactive_user_checks(self):
        api = MAS({'mas_url': 'https://mas.example', 'client_id': 'test', 'client_secret': 'test'})
        with patch('mas.requests.post') as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {}
            api.device_start()
            self.assertEqual(post.call_args.kwargs['data'], {'scope': 'openid'})
        for flag in ('deactivated_at', 'locked_at', 'legacy_guest'):
            with (
                self.subTest(flag=flag),
                patch.object(api, 'api', return_value={'data': {'attributes': {flag: True}}}),
                self.assertRaises(BotError),
            ):
                api.active_user('user')

    def test_matrix_invitations_require_active_local_account_and_owner(self):
        def matrix(action, mxid='@member:misbot.ru', **extra):
            return self.bridge.handle(
                {'action': 'matrix_' + action, 'server': 'misbot.ru', 'mxid': mxid, **extra}
            )

        token = matrix('invite', request_id='matrix:$event')
        saved = self.bridge.db.execute(
            'SELECT uid, inviter_name, created_at FROM invitations WHERE resource=?', (token['id'],)
        ).fetchone()
        self.assertEqual((saved['uid'], saved['inviter_name']), ('@member:misbot.ru', 'member'))
        self.assertGreater(saved['created_at'], 0)
        self.assertEqual(token, matrix('invite', request_id='matrix:$event'))
        self.assertEqual(len(matrix('tokens')['tokens']), 1)
        with self.assertRaises(BotError):
            matrix('revoke', mxid='@other:misbot.ru', resource=token['id'])
        with self.assertRaises(BotError):
            matrix('invite', mxid='@remote:matrix.primango.ru', request_id='matrix:$remote')
        self.api.active = False
        with self.assertRaises(BotError):
            matrix('tokens')
        self.api.active = True
        self.assertFalse(matrix('revoke', resource=token['id'])['attributes']['valid'])
        with self.assertRaises(BotError):
            matrix('federation')

    def test_invitation_history_and_inviter_name_survive_reopen(self):
        self.authorize()
        first = self.call('invite', request_id='telegram:first')
        self.api.tokens[first['id']]['attributes']['revoked_at'] = 'now'
        self.bridge.db.close()
        self.bridge = Bridge(self.root / 'bot-config.json', lambda config: self.api)
        row = self.bridge.db.execute(
            'SELECT inviter_name, created_at, code FROM invitations WHERE request=?',
            ('telegram:first',),
        ).fetchone()
        self.assertEqual(row['inviter_name'], 'member')
        self.assertGreater(row['created_at'], 0)
        self.assertEqual(row['code'], first['attributes']['token'])

    def test_matrix_and_telegram_ownership_cannot_collide(self):
        self.authorize()
        telegram = self.call('invite', request_id='telegram:own')
        request = {
            'action': 'matrix_revoke',
            'server': 'misbot.ru',
            'mxid': '@10:misbot.ru',
            'resource': telegram['id'],
        }
        with self.assertRaises(BotError):
            self.bridge.handle(request)

    def test_real_mas_matrix_identity_cannot_redirect_or_use_foreign_account(self):
        api = MAS({'mas_url': 'https://mas.example', 'client_id': 'test', 'client_secret': 'test'})
        for value in [
            None,
            '@user:foreign.example',
            '@:misbot.ru',
            '@x:y:misbot.ru',
            '@x\n:misbot.ru',
        ]:
            with self.subTest(value=value), self.assertRaises(BotError):
                api.matrix_user(value, 'misbot.ru')
        with (
            patch.object(api, 'api', return_value={'data': {'id': 'MAS-USER'}}) as request,
            patch.object(api, 'active_user') as active,
        ):
            api.matrix_user('@name/slash:misbot.ru', 'misbot.ru')
            request.assert_called_once_with('users/by-username/name%2Fslash')
            active.assert_called_once_with('MAS-USER')

    def test_confirmation_bound_to_user_and_single_use(self):
        self.authorize()
        self.authorize(11)
        proposal = self.call('fed_prepare', operation='add', domain='matrix.org')
        with self.assertRaises(BotError):
            self.call('fed_apply', uid=11, confirmation=proposal['confirmation'])
        with patch.object(self.bridge, 'apply_federation') as apply:
            self.call('fed_apply', confirmation=proposal['confirmation'])
            apply.assert_called_once_with('misbot.ru', ['matrix.org', 'misbot.ru'])
        with self.assertRaises(BotError):
            self.call('fed_apply', confirmation=proposal['confirmation'])
        with self.assertRaises(BotError):
            self.call('fed_prepare', operation='remove', domain='misbot.ru')

    def test_federation_rollback_preserves_configuration(self):
        hs = self.root / 'config/homeserver.yaml'
        web = self.root / 'config/web-config.json'
        hs.write_text('server_name: misbot.ru\nfederation_domain_whitelist: [misbot.ru]\n')
        web.write_text('{"room_directory":{"servers":["misbot.ru"]},"brand":"unchanged"}')
        saved = {p: p.read_bytes() for p in [hs, web, self.root / 'bot-policy.json']}
        with patch.object(self.bridge, 'restart_healthy', side_effect=[RuntimeError(), None]):
            with self.assertRaises(BotError):
                self.bridge.apply_federation('misbot.ru', ['misbot.ru', 'matrix.org'])
        self.assertEqual(saved, {p: p.read_bytes() for p in saved})

    def test_untrusted_arguments_rejected(self):
        self.authorize()
        for value in [
            'https://matrix.org',
            '../config',
            'matrix.org:8448',
            '*.matrix.org',
            '127.0.0.1',
            'evil\n.org',
            '',
            None,
        ]:
            with self.subTest(value=value), self.assertRaises(BotError):
                self.call('fed_prepare', operation='add', domain=value)
        with self.assertRaises(BotError):
            self.call('run_command', command='arbitrary')
        with self.assertRaises(BotError):
            self.bridge.handle(
                {'action': 'invite', 'server': 'unmanaged.example', 'uid': 10, 'request_id': 'x'}
            )
        self.assertEqual(domain('пример.рф'), 'xn--e1afmkfd.xn--p1ai')


class IntegrationTests(unittest.TestCase):
    def test_snapshot_rpc_requires_root_not_worker_or_matrix_uid(self):
        check_peer(0, 'backup_snapshot')
        for uid in (10001, 10002, 10003):
            with self.subTest(uid=uid), self.assertRaises(BotError):
                check_peer(uid, 'backup_snapshot')

    def test_matrix_process_cannot_use_telegram_admin_channel(self):
        for action in [
            'servers',
            'matrix_identity',
            'matrix_invite',
            'matrix_tokens',
            'matrix_revoke',
        ]:
            check_peer(10002, action)
        for action in ['auth_start', 'federation', 'fed_prepare', 'fed_apply', 'invite', 'logout']:
            with self.subTest(action=action), self.assertRaises(BotError):
                check_peer(10002, action)
        check_peer(10001, 'federation')
        with self.assertRaises(BotError):
            check_peer(10001, 'matrix_tokens')
        with self.assertRaises(BotError):
            check_peer(10003, 'servers')
        check_peer(0, 'matrix_invite')


if __name__ == '__main__':
    unittest.main()
