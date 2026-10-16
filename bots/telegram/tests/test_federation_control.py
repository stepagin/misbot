"""Telegram transport contracts over the shared federation backend."""

from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'installer'))
from bridge import Bridge
from common import BotError
from test_control import FakeMAS


REQUEST = 'MB-' + 'a' * 32
OPERATION = 'FO-' + 'b' * 32


class BackendFixture:
    def __init__(self):
        self.data = {'requests': {}, 'jobs': {}}
        self.calls = []
        self.invitation = {'supported': True, 'ready': True, 'expires_after': 2592000}
        self.ready_calls = []

    def guard(self):
        return nullcontext()

    def ready(self, allowed=False):
        self.ready_calls.append(allowed)

    def load(self):
        return self.data

    def requests(self, data):
        return [
            item
            for item in data['requests'].values()
            if item['state'] in ('pending', 'accepted', 'partially_applied', 'rejected', 'expired')
            and not item.get('cleared')
        ]

    def operate(self, action, argument=None, actor='root'):
        self.calls.append((action, argument, actor))
        if action == 'servers':
            return {'servers': ['example.org']}
        if action == 'requests':
            return {'requests': self.requests(self.data)}
        if action == 'api-status':
            return {'enabled': True}
        if action == 'check':
            return {'invitation': self.invitation}
        if action == 'request-status':
            return self.data['requests'][argument]
        raise AssertionError(action)


class FederationControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir()
        (self.root / 'bot-config.json').write_text(
            json.dumps(
                {
                    'servers': {
                        'example.org': {
                            'stack_root': str(self.root),
                            'federation_enabled': True,
                            'federation_backend': 'misbot',
                            'management_actions': ['status'],
                        }
                    }
                }
            )
        )
        (self.root / 'bot-policy.json').write_text(
            json.dumps(
                {
                    'servers': {
                        'example.org': {
                            'invitation_lifetime_seconds': 604800,
                            'invitation_usage_limit': 1,
                            'registration_required': True,
                            'federation_servers': ['example.org'],
                        }
                    }
                }
            )
        )
        self.bridge = Bridge(self.root / 'bot-config.json', FakeMAS)
        for uid in (10, 11):
            self.bridge.db.execute(
                'INSERT INTO authorizations VALUES (?,?,?,?)',
                ('example.org', uid, f'MAS-USER-{uid}', int(time.time()) + 1000),
            )
        self.bridge.db.commit()
        self.backend = BackendFixture()
        self.backend_patch = patch.object(
            self.bridge, 'federation_backend', return_value=self.backend
        )
        self.backend_patch.start()
        self.addCleanup(self.backend_patch.stop)

    def tearDown(self):
        self.bridge.db.close()
        self.tmp.cleanup()

    def call(self, action, uid=10, **values):
        return self.bridge.handle({'action': action, 'server': 'example.org', 'uid': uid, **values})

    def request(self, direction='incoming', state='pending'):
        item = {
            'id': REQUEST,
            'server': 'other.example',
            'direction': direction,
            'state': state,
            'expires_at': int(time.time()) + 2592000,
        }
        self.backend.data['requests'][REQUEST] = item
        return item

    def queued(self, confirmation):
        service = SimpleNamespace(
            queue_job=lambda *_: {'operation_id': OPERATION, 'status': 'queued'}
        )
        with patch.dict(sys.modules, {'federation_service': service}):
            with patch.object(service, 'queue_job', wraps=service.queue_job) as queue:
                result = self.call('fed_apply', confirmation=confirmation)
                return result, queue.call_args.args[1]

    def test_reads_and_prepares_recheck_current_role(self):
        self.request()
        self.assertEqual(self.call('fed_read', operation='requests')['requests'][0]['id'], REQUEST)
        self.bridge.apis['example.org'].admin = False
        for action in ('fed_read', 'fed_prepare', 'fed_apply'):
            with self.subTest(action=action), self.assertRaises(BotError):
                self.call(action, operation='api-status')

    def test_remote_capability_failure_creates_no_confirmation_or_operation(self):
        self.backend.invitation = {'supported': False, 'ready': False, 'code': 'misbot_not_found'}
        with self.assertRaisesRegex(BotError, 'не публикует Misbot API'):
            self.call('fed_prepare', operation='invite', target='other.example')
        self.assertEqual(
            self.bridge.db.execute('SELECT count(*) FROM confirmations').fetchone()[0], 0
        )
        self.assertEqual(
            self.bridge.db.execute('SELECT count(*) FROM telegram_federation_jobs').fetchone()[0], 0
        )
        self.assertIn(('check', 'other.example', '@member:example.org'), self.backend.calls)

    def test_invite_confirmation_is_bound_to_account_and_consumed_once(self):
        token = self.call('fed_prepare', operation='invite', target='other.example')['confirmation']
        with self.assertRaises(BotError):
            self.call('fed_apply', uid=11, confirmation=token)
        result, payload = self.queued(token)
        self.assertEqual(result['operation_id'], OPERATION)
        self.assertEqual(payload['action'], 'invite')
        self.assertEqual(payload['argument'], 'other.example')
        self.assertEqual(payload['actor'], '@member:example.org')
        with self.assertRaises(BotError):
            self.call('fed_apply', confirmation=token)

    def test_api_toggle_and_clear_use_local_confirmed_backend_actions(self):
        self.assertTrue(self.call('fed_read', operation='api-status')['enabled'])
        for operation, action in (('api-disable', 'api-disable'), ('clear', 'clear-rejected')):
            prepared = self.call('fed_prepare', operation=operation, target='')
            _, payload = self.queued(prepared['confirmation'])
            self.assertEqual(payload['action'], action)
            self.assertIsNone(payload['argument'])

    def test_retry_preserves_request_identity_and_requires_confirmation(self):
        self.request(direction='outgoing', state='partially_applied')
        prepared = self.call('fed_prepare', operation='retry', target=REQUEST)
        self.assertIn('other.example', prepared['summary'])
        _, payload = self.queued(prepared['confirmation'])
        self.assertEqual((payload['action'], payload['argument']), ('retry', REQUEST))

    def test_job_result_is_owner_scoped_and_excludes_private_fields(self):
        self.bridge.db.execute(
            'INSERT INTO telegram_federation_jobs VALUES (?,?,?)', (OPERATION, 'example.org', 10)
        )
        self.backend.data['jobs'][OPERATION] = {
            'state': 'complete',
            'action': 'invite',
            'result': {
                'state': 'pending',
                'safe_error': 'timeout',
                'private_key': 'FICTIONAL_SECRET',
            },
        }
        self.bridge.db.commit()
        result = self.call('fed_read', operation='operation', target=OPERATION)
        self.assertEqual(result['result']['safe_error'], 'timeout')
        self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))
        with self.assertRaises(BotError):
            self.call('fed_read', uid=11, operation='operation', target=OPERATION)

    def test_notifications_are_durable_per_account_and_acknowledge_independently(self):
        self.request()
        first = self.call('federation_notifications')['notifications']
        self.assertEqual({item['uid'] for item in first}, {10, 11})
        notice = next(item for item in first if item['uid'] == 10)
        self.call('federation_notifications_ack', id=notice['id'])
        remaining = self.call('federation_notifications')['notifications']
        self.assertEqual([item['uid'] for item in remaining], [11])
        self.assertTrue(self.backend.ready_calls and all(self.backend.ready_calls))
        self.bridge.db.close()
        self.bridge = Bridge(self.root / 'bot-config.json', FakeMAS)
        with patch.object(self.bridge, 'federation_backend', return_value=self.backend):
            self.assertEqual(
                [item['uid'] for item in self.call('federation_notifications')['notifications']],
                [11],
            )

    def test_revoked_or_cleared_request_is_not_delivered_late(self):
        item = self.request()
        self.assertEqual(len(self.call('federation_notifications')['notifications']), 2)
        item['state'] = 'cancelled'
        self.assertEqual(self.call('federation_notifications')['notifications'], [])
        rows = self.bridge.db.execute('SELECT state FROM telegram_notifications').fetchall()
        self.assertTrue(all(row['state'] == 'dismissed' for row in rows))

    def test_notification_feed_revalidates_earlier_recipient_after_later_network_call(self):
        self.request()
        api = self.bridge.apis['example.org']
        original = api.active_user

        def revoke_earlier(user_id):
            if user_id == 'MAS-USER-11':
                self.call('logout', uid=10)
            return original(user_id)

        with patch.object(api, 'active_user', side_effect=revoke_earlier):
            result = self.call('federation_notifications')['notifications']
        self.assertEqual([item['uid'] for item in result], [11])

    def test_notifications_never_deliver_to_revoked_administrator(self):
        self.request()
        self.bridge.apis['example.org'].admin = False
        self.assertEqual(self.call('federation_notifications')['notifications'], [])

    def test_push_subscription_survives_login_expiry_without_granting_commands(self):
        self.request()
        self.bridge.db.execute('UPDATE authorizations SET expires=0 WHERE uid=10')
        self.bridge.db.commit()
        with self.assertRaises(BotError):
            self.call('manage_read', operation='status')
        notices = self.call('federation_notifications')['notifications']
        own = next(item for item in notices if item['uid'] == 10)
        self.call('federation_notifications_ack', id=own['id'])
        self.call('logout')
        remaining = self.call('federation_notifications')['notifications']
        self.assertTrue(all(item['uid'] != 10 for item in remaining))


if __name__ == '__main__':
    unittest.main()
