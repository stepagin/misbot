import copy
from contextlib import contextmanager
import hashlib
import fcntl
import threading
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
import subprocess
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import federation
import federation_protocol as protocol
import federation_runtime
import federation_service
import federation_admin_api
import state_store


class FederationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.backends = {}
        self.allowed = {'root', '@admin:a.example', '@admin:b.example'}
        self.fail_apply = False
        for server in ('a.example', 'b.example'):
            root = self.root / server
            root.mkdir(mode=0o700)
            (root / 'data').mkdir(mode=0o700)
            (root / 'config').mkdir(mode=0o700)
            state_store.write_json(root / 'installation.json', {'domain': server})
            state_store.write_json(
                root / 'config/admin-panel-identity.json',
                {'user_id': '@admin-panel:' + server, 'administrator_mxid': '@admin:' + server},
            )
            state_store.write_text(
                root / 'settings.env',
                'DOMAIN='
                + server
                + '\nPUBLIC_IP=8.8.8.8\nACME_EMAIL=operator@example.org\nFEDERATION_SERVERS='
                + server
                + '\n',
            )
            federation.setup(root, {'domain': server})

            def apply(servers, r=root):
                if self.fail_apply:
                    raise protocol.ProtocolError('apply_failed')
                values = configuration.load(r)
                values['FEDERATION_SERVERS'] = ','.join(servers)
                configuration.write_model(r, values)

            self.backends[server] = federation.Backend(
                root,
                transport=self.transport,
                admin=lambda actor: actor in self.allowed,
                apply=apply,
                admission=False,
            )
        self.a = self.backends['a.example']
        self.b = self.backends['b.example']
        self.lose = False

    def tearDown(self):
        self.temp.cleanup()

    def transport(self, server, path, body=None):
        if path == '/.well-known/matrix/server':
            return {'m.server': server + ':443'}
        if path == '/_matrix/federation/v1/version':
            return {'server': {'name': 'fixture'}}
        if path == protocol.DISCOVERY:
            return self.backends[server].doc()
        value = self.backends[server].receive(body)
        if self.lose:
            self.lose = False
            raise protocol.ProtocolError('timeout')
        return value

    def invite(self):
        return self.a.operate('invite', 'b.example', actor='@admin:a.example')['id']

    def test_receive_discovery_holds_neither_writer_lock_nor_controller_mutex(self):
        body = {
            'action': 'invite',
            'request_id': 'MB-' + 'a' * 32,
            'expires_at': int(time.time()) + 100,
            'approved': True,
            'applied': False,
        }
        message = protocol.envelope(self.a.doc(), self.a.key, self.b.doc(), body)
        work = threading.Lock()
        original = self.b.transport

        def transport(server, path, body=None):
            self.assertTrue(work.acquire(blocking=False))
            work.release()
            # Independent flock descriptor detects a held lock even when the
            # operation_guard environment would allow reentrant child work.
            fd = os.open(
                state_store.operation_lock_path(self.b.root), os.O_CREAT | os.O_RDWR, 0o600
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return original(server, path, body)

        with patch.object(self.b, 'transport', side_effect=transport):
            reply = federation_service.receive_call(self.b, message, work)
        self.assertEqual(reply['payload']['body']['state'], 'pending')
        with self.assertRaisesRegex(protocol.ProtocolError, 'replay'):
            federation_service.receive_call(self.b, message, work)

    def test_receive_revalidates_concurrent_api_identity_pin_nonce_and_admission(self):
        original = self.b.load()
        body = {
            'action': 'invite',
            'request_id': 'MB-' + 'a' * 32,
            'expires_at': int(time.time()) + 100,
            'approved': True,
            'applied': False,
        }
        message = protocol.envelope(self.a.doc(), self.a.key, self.b.doc(), body)
        remote = self.a.doc()
        for change, code in (
            ('api', 'api_disabled'),
            ('own', 'identity_mismatch'),
            ('peer', 'peer_identity_changed'),
            ('nonce', 'replay'),
        ):
            self.b.save(original)

            def discover(server, path, body=None):
                data = self.b.load()
                if change == 'api':
                    data['api_enabled'] = False
                elif change == 'own':
                    data['instance_id'] = 'f' * 32
                elif change == 'peer':
                    data['peers'][server] = {'document': {**remote, 'key_id': 'f' * 64}}
                else:
                    data['nonces'][message['payload']['nonce']] = int(time.time())
                self.b.save(data)
                return remote

            with (
                self.subTest(change=change),
                patch.object(self.b, 'transport', side_effect=discover),
            ):
                with self.assertRaisesRegex(protocol.ProtocolError, code):
                    self.b.receive(message)
                self.assertEqual(self.b.load()['requests'], {})
        self.b.save(original)
        with patch.object(
            self.b, 'ready', side_effect=protocol.ProtocolError('installation_pending')
        ):
            with self.assertRaisesRegex(protocol.ProtocolError, 'installation_pending'):
                self.b.receive(message)
        self.assertEqual(self.b.load(), original)

    def test_receive_rechecks_signed_timestamp_after_verification_before_commit(self):
        body = {
            'action': 'invite',
            'request_id': 'MB-' + 'a' * 32,
            'expires_at': int(time.time()) + 1000,
            'approved': True,
            'applied': False,
        }
        message = protocol.envelope(self.a.doc(), self.a.key, self.b.doc(), body)
        before = self.b.path.read_bytes()

        @contextmanager
        def commit_guard():
            with patch.object(
                protocol.time, 'time', return_value=message['payload']['timestamp'] + 301
            ):
                yield

        with self.assertRaisesRegex(protocol.ProtocolError, 'expired_request'):
            self.b.receive(message, commit_guard=commit_guard)
        self.assertEqual(self.b.path.read_bytes(), before)

    def test_receive_malformed_input_never_discovers_or_locks(self):
        body = {
            'action': 'invite',
            'request_id': 'MB-' + 'a' * 32,
            'expires_at': int(time.time()) + 100,
            'approved': True,
            'applied': False,
        }
        valid = protocol.envelope(self.a.doc(), self.a.key, self.b.doc(), body)
        invalid_body = copy.deepcopy(valid)
        invalid_body['payload']['body']['approved'] = 'yes'
        invalid_nonce = copy.deepcopy(valid)
        invalid_nonce['payload']['nonce'] = []
        for message in ({}, {'payload': {'sender': 'a.example'}}, invalid_body, invalid_nonce):
            with (
                self.subTest(message=message),
                patch.object(self.b, 'transport') as transport,
                patch.object(self.b, 'guard') as guard,
            ):
                with self.assertRaises(protocol.ProtocolError):
                    self.b.receive(message)
                transport.assert_not_called()
                guard.assert_not_called()

    def test_handshake_requires_both_local_approvals(self):
        rid = self.invite()
        self.assertNotIn('a.example', self.b.servers())
        self.assertNotIn('b.example', self.a.servers())
        result = self.b.operate('approve', rid, actor='@admin:b.example')
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(self.a.operate('request-status', rid)['state'], 'connected')
        self.assertIn('b.example', self.a.servers())
        self.assertIn('a.example', self.b.servers())
        self.assertEqual(self.a.operate('retry', rid)['state'], 'connected')

    def test_lost_response_and_idempotency(self):
        self.lose = True
        first = self.a.operate('invite', 'b.example')
        rid = first['id']
        self.assertEqual(first['code'], 'timeout')
        self.assertEqual(len(self.b.load()['requests']), 1)
        with self.assertRaisesRegex(protocol.ProtocolError, 'pending_request_exists'):
            self.a.operate('invite', 'b.example')
        self.assertEqual(self.a.operate('retry', rid)['id'], rid)
        self.b.operate('approve', rid)
        self.a.operate('retry', rid)
        self.assertEqual(len(self.b.load()['requests']), 1)

    def test_partial_apply_revoked_admin_and_retry(self):
        rid = self.invite()
        self.allowed.remove('@admin:a.example')
        result = self.b.operate('approve', rid, actor='@admin:b.example')
        self.assertIn(result['state'], ('accepted', 'partially_applied'))
        self.assertNotIn('b.example', self.a.servers())
        self.allowed.add('@admin:a.example')
        self.b.operate('retry', rid)
        self.a.operate('retry', rid)
        self.assertEqual(self.a.operate('request-status', rid)['state'], 'connected')

    def test_reject_cancel_and_preexisting_policy_preserved(self):
        rid = self.invite()
        self.b.operate('reject', rid)
        self.assertEqual(self.a.operate('request-status', rid)['state'], 'rejected')
        with patch.object(federation.time, 'time', return_value=int(time.time()) + 61):
            rid = self.invite()
            self.a.operate('cancel', rid)
        self.assertEqual(self.b.operate('requests')['requests'][0]['state'], 'rejected')
        self.assertEqual(len(self.b.operate('requests')['requests']), 1)
        self.a.operate('add', 'b.example')
        with patch.object(federation.time, 'time', return_value=int(time.time()) + 122):
            rid = self.invite()
            self.assertEqual(self.b.operate('request-status', rid)['state'], 'pending')
            self.a.operate('cancel', rid)
        self.assertIn('b.example', self.a.servers())

    def test_simultaneous_invites_and_expiration(self):
        a = self.invite()
        with self.assertRaisesRegex(protocol.ProtocolError, 'pending_request_exists'):
            self.b.operate('invite', 'a.example')
        self.b.operate('approve', a)
        self.assertEqual(self.a.servers().count('b.example'), 1)
        self.assertEqual(self.a.operate('requests')['requests'], [])
        data = self.a.load()
        item = data['requests'][a]
        item.update(state='pending', local_approval=False, remote_approval=False, expires_at=0)
        item.update(local_applied=False, remote_applied=False)
        self.a.save(data)
        self.assertEqual(self.a.operate('retry', a)['state'], 'expired')

    def test_default_split_policy_is_available_without_policy_file(self):
        root = self.a.root
        (root / 'settings.env').unlink()
        model = configuration.schema.validate(
            {'DOMAIN': 'a.example', 'PUBLIC_IP': '', 'ACME_EMAIL': 'operator@example.org'}
        )
        configuration.stage_bundle(root / 'settings', model, None)
        self.assertFalse((root / 'settings/policy.env').exists())
        self.assertEqual(self.a.operate('servers')['servers'], ['misbot.ru'])
        # Inviting another peer must reach normal protocol validation, rather
        # than fail with KeyError because a default setting was not stored.
        self.assertEqual(self.a.operate('check', 'b.example')['input'], 'valid')

    def test_operations_committed_snapshot_is_available_while_writer_holds_real_lock(self):
        data = self.a.load()
        identifier = 'FO-' + 'a' * 32
        data['jobs'] = {identifier: {'state': 'running'}}
        self.a.save(data)
        before = self.a.path.read_bytes()
        script = (
            'import sys; sys.path.insert(0,sys.argv[1]); import state_store; '
            'guard=state_store.operation_guard(sys.argv[2]); guard.__enter__(); '
            'print("locked",flush=True); sys.stdin.read(); guard.__exit__(None,None,None)'
        )
        owner = subprocess.Popen(
            [
                sys.executable,
                '-B',
                '-c',
                script,
                str(Path(state_store.__file__).parent),
                str(self.a.root),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(owner.stdout.readline().strip(), 'locked')
            self.assertEqual(
                self.a.operate('operations'),
                {
                    'recovery_available': False,
                    'operations': [{'id': identifier, 'state': 'running', 'result': {}}],
                },
            )
            self.assertIsNone(owner.poll())
            with self.assertRaisesRegex(RuntimeError, 'Another installer operation'):
                self.a.operate('add', 'peer.example')
            self.assertEqual(self.a.path.read_bytes(), before)
        finally:
            owner.communicate(timeout=5)

    def test_operations_snapshot_retains_rpc_authorization_and_rejects_invalid_state(self):
        request = {
            'action': 'operations',
            'actor': '@admin:a.example',
            'event_id': '$operations-fixture',
        }
        before = self.a.path.read_bytes()
        with patch.object(
            self.a, 'guard', side_effect=AssertionError('read acquired writer guard')
        ):
            self.assertEqual(
                federation_service.bot_call(self.a, request, 10003),
                {'recovery_available': False, 'operations': []},
            )
        self.assertEqual(self.a.path.read_bytes(), before)
        with self.assertRaises(protocol.ProtocolError):
            federation_service.bot_call(self.a, dict(request, actor='@other:a.example'), 10003)
        data = self.a.load()
        data['jobs'] = {'FO-' + 'a' * 32: {'state': 'unsupported'}}
        self.a.save(data)
        with self.assertRaisesRegex(protocol.ProtocolError, 'invalid_operation_state'):
            self.a.operate('operations')

    def test_signature_replay_scope_size_and_unsupported(self):
        rid = 'MB-' + 'a' * 32
        body = {
            'action': 'invite',
            'request_id': rid,
            'expires_at': int(time.time()) + 100,
            'approved': True,
            'applied': False,
        }
        req = protocol.envelope(self.a.doc(), self.a.key, self.b.doc(), body)
        self.b.receive(req)
        with self.assertRaisesRegex(protocol.ProtocolError, 'replay'):
            self.b.receive(req)
        for change, code in [
            ({'protocol': 99}, 'unsupported_version'),
            ({'timestamp': 0}, 'expired_request'),
            ({'recipient': 'c.example'}, 'signature_identity'),
        ]:
            altered = copy.deepcopy(req)
            altered['payload'].update(change)
            altered['payload']['nonce'] = 'b' * 32
            with self.assertRaisesRegex(protocol.ProtocolError, code):
                self.b.receive(altered)
        invalid = protocol.envelope(self.a.doc(), self.a.key, self.b.doc(), body)
        invalid['signature'] = 'FICTIONAL'
        with self.assertRaisesRegex(protocol.ProtocolError, 'invalid_signature'):
            self.b.receive(invalid)
        forbidden = protocol.envelope(
            self.a.doc(), self.a.key, self.b.doc(), {**body, 'action': 'update'}
        )
        with self.assertRaisesRegex(protocol.ProtocolError, 'remote_operation_denied'):
            self.b.receive(forbidden)

    def test_seven_day_expiration_accepts_authenticated_clock_skew(self):
        now = int(time.time())
        rid = 'MB-' + 'c' * 32
        body = {
            'action': 'invite',
            'request_id': rid,
            'expires_at': now + 100 + 604800,
            'approved': True,
            'applied': False,
        }
        with patch.object(protocol.time, 'time', return_value=now + 100):
            request = protocol.envelope(self.a.doc(), self.a.key, self.b.doc(), body)
        self.b.receive(request)
        self.assertEqual(self.b.operate('request-status', rid)['state'], 'pending')

    def test_thirty_day_deadline_and_persistent_creation_limit(self):
        now = int(time.time())
        with patch.object(federation.time, 'time', return_value=now):
            rid = self.invite()
            self.assertEqual(self.a.load()['requests'][rid]['expires_at'], now + 30 * 86400)
            with self.assertRaisesRegex(protocol.ProtocolError, 'pending_request_exists'):
                self.a.operate('invite', 'b.example')
            self.a.operate('cancel', rid)
            with self.assertRaisesRegex(protocol.ProtocolError, 'invitation_rate_limit'):
                self.invite()
            # A newly constructed backend retains the limiter in state.json.
            fresh = federation.Backend(
                self.a.root, transport=self.transport, admin=lambda actor: True, admission=False
            )
            with self.assertRaisesRegex(protocol.ProtocolError, 'invitation_rate_limit'):
                fresh.operate('invite', 'b.example')
        with patch.object(federation.time, 'time', return_value=now + 60):
            new = self.invite()
        self.assertNotEqual(rid, new)

    def test_expiry_notifies_peer_and_never_removes_applied_policy(self):
        now = int(time.time())
        rid = self.invite()
        deadline = self.b.load()['requests'][rid]['expires_at']
        self.b.operate('api-disable')
        with patch.object(federation.time, 'time', return_value=deadline):
            self.b.maintain()
            item = self.b.load()['requests'][rid]
            self.assertEqual(item['state'], 'expired')
            self.assertTrue(item['delivery_pending'])
            self.b.operate('api-enable')
            self.b.maintain()
            self.assertNotIn('delivery_pending', self.b.load()['requests'][rid])
            self.assertEqual(self.a.operate('request-status', rid)['state'], 'expired')
            self.assertEqual(self.b.operate('requests')['requests'][0]['state'], 'expired')
        with patch.object(federation.time, 'time', return_value=now + 61):
            rid = self.invite()
            self.b.operate('approve', rid)
        with patch.object(federation.time, 'time', return_value=deadline + 86400):
            self.assertEqual(self.a.operate('retry', rid)['state'], 'connected')
            self.assertIn('b.example', self.a.servers())

    def test_approved_interrupted_saga_can_finish_after_invitation_deadline(self):
        rid = self.invite()
        deadline = self.a.load()['requests'][rid]['expires_at']
        self.allowed.remove('@admin:a.example')
        self.b.operate('approve', rid, actor='@admin:b.example')
        with patch.object(federation.time, 'time', return_value=deadline + 1):
            self.b.maintain()
            self.assertEqual(self.b.load()['requests'][rid]['state'], 'accepted')
            self.allowed.add('@admin:a.example')
            self.b.operate('retry', rid)
            self.assertEqual(self.a.operate('request-status', rid)['state'], 'connected')

    def test_new_invitation_cannot_change_identity_of_pending_peer(self):
        rid = self.invite()
        document = self.a.doc()
        document['instance_id'] = 'f' * 32
        original = self.b.transport

        def changed(server, path, body=None):
            return document if path == protocol.DISCOVERY else original(server, path, body)

        self.b.transport = changed
        with self.assertRaisesRegex(protocol.ProtocolError, 'peer_identity_changed'):
            self.b.peer(self.b.load(), 'a.example')
        self.assertEqual(self.b.load()['requests'][rid]['state'], 'pending')

    def test_incoming_new_id_spam_cannot_bypass_creation_interval(self):
        rid = self.invite()
        self.b.operate('reject', rid)
        message = protocol.envelope(
            self.a.doc(),
            self.a.key,
            self.b.doc(),
            {
                'action': 'invite',
                'request_id': 'MB-' + 'f' * 32,
                'expires_at': int(time.time()) + protocol.REQUEST_TTL,
                'approved': True,
                'applied': False,
            },
        )
        with self.assertRaisesRegex(protocol.ProtocolError, 'invitation_rate_limit'):
            self.b.receive(message)
        self.assertEqual(len(self.b.load()['requests']), 1)

    def test_delayed_rejection_and_unapplied_status_cannot_regress_connected_peer(self):
        rid = self.invite()
        self.b.operate('approve', rid)
        for action in ('status', 'rejected', 'cancelled'):
            delayed = protocol.envelope(
                self.b.doc(),
                self.b.key,
                self.a.doc(),
                {
                    'action': action,
                    'request_id': rid,
                    'expires_at': self.b.load()['requests'][rid]['expires_at'],
                    'approved': False,
                    'applied': False,
                },
            )
            self.assertEqual(self.a.receive(delayed)['payload']['body']['state'], 'connected')
        item = self.a.load()['requests'][rid]
        self.assertTrue(item['remote_applied'])
        self.assertEqual(self.a.operate('peers')['peers'][0]['state'], 'connected')
        self.assertEqual(self.a.operate('requests')['requests'], [])

    def test_clearing_rejections_retains_replay_tombstone_and_allows_new_request(self):
        now = int(time.time())
        rid = self.invite()
        self.b.operate('reject', rid)
        self.assertEqual(self.b.operate('clear-rejected')['count'], 1)
        self.assertEqual(self.b.operate('requests')['requests'], [])
        old = self.a.load()['requests'][rid]
        delayed = protocol.envelope(
            self.a.doc(),
            self.a.key,
            self.b.doc(),
            {
                'action': 'invite',
                'request_id': rid,
                'expires_at': old['expires_at'],
                'approved': True,
                'applied': False,
            },
        )
        self.assertEqual(self.b.receive(delayed)['payload']['body']['state'], 'rejected')
        self.assertEqual(self.b.operate('requests')['requests'], [])
        with patch.object(federation.time, 'time', return_value=now + 61):
            self.assertNotEqual(self.invite(), rid)
        self.assertEqual(len(self.b.operate('requests')['requests']), 1)

    def test_cancel_before_invite_delivery_hides_request_at_both_ends(self):
        original = self.a.transport

        def offline(server, path, body=None):
            if path == protocol.API:
                raise protocol.ProtocolError('timeout')
            return original(server, path, body)

        self.a.transport = offline
        rid = self.invite()
        old = self.a.load()['requests'][rid]
        self.assertEqual(self.b.load()['requests'], {})
        self.a.transport = original
        self.a.operate('cancel', rid)
        delayed = protocol.envelope(
            self.a.doc(),
            self.a.key,
            self.b.doc(),
            {
                'action': 'invite',
                'request_id': rid,
                'expires_at': old['expires_at'],
                'approved': True,
                'applied': False,
            },
        )
        self.assertEqual(self.b.receive(delayed)['payload']['body']['state'], 'cancelled')
        self.assertEqual(self.a.operate('requests')['requests'], [])
        self.assertEqual(self.b.operate('requests')['requests'], [])

    def test_rejection_delivery_retries_after_api_off_and_lost_response(self):
        rid = self.invite()
        self.a.operate('api-disable')
        result = self.b.operate('reject', rid)
        self.assertEqual(result['code'], 'api_disabled')
        self.assertTrue(self.b.load()['requests'][rid]['delivery_pending'])
        self.b.operate('clear-rejected', rid)
        self.a.operate('api-enable')
        self.lose = True
        self.b.maintain()
        self.assertTrue(self.b.load()['requests'][rid]['delivery_pending'])
        self.assertEqual(self.a.operate('request-status', rid)['state'], 'rejected')
        with patch.object(federation.time, 'time', return_value=int(time.time()) + 61):
            self.b.maintain()
        self.assertNotIn('delivery_pending', self.b.load()['requests'][rid])
        self.assertEqual(self.b.operate('requests')['requests'], [])

    def test_failed_first_delivery_cannot_starve_later_pending_results(self):
        original = self.transport
        now = int(time.time())

        def offline(server, path, body=None):
            if path == protocol.API:
                raise protocol.ProtocolError('timeout')
            return original(server, path, body)

        self.b.transport = offline
        with patch.object(federation.time, 'time', return_value=now):
            first = self.invite()
            self.b.operate('reject', first)
            self.a.operate('retry', first)
        with patch.object(federation.time, 'time', return_value=now + 61):
            second = self.invite()
            self.b.operate('reject', second)
        self.assertNotEqual(first, second)

        def first_remains_offline(server, path, body=None):
            if path == protocol.API and body['payload']['body']['request_id'] == first:
                raise protocol.ProtocolError('timeout')
            return original(server, path, body)

        self.b.transport = first_remains_offline
        with patch.object(federation.time, 'time', return_value=now + 120):
            self.b.maintain()
        with patch.object(federation.time, 'time', return_value=now + 180):
            self.b.maintain()
        requests = self.b.load()['requests']
        self.assertTrue(requests[first]['delivery_pending'])
        self.assertNotIn('delivery_pending', requests[second])
        self.assertEqual(self.a.operate('request-status', second)['state'], 'rejected')

    def test_api_setting_defaults_and_survives_setup_without_replacing_identity(self):
        saved = self.a.load()
        saved.pop('api_enabled', None)
        self.a.save(saved)
        key = self.a.key.read_bytes()
        self.assertEqual(self.a.operate('api-status'), {'enabled': True})
        self.a.operate('api-disable')
        federation.setup(self.a.root, {'domain': 'a.example', 'federation_seeded': True})
        self.assertEqual(self.a.operate('api-status'), {'enabled': False})
        self.assertEqual(self.a.key.read_bytes(), key)
        self.assertEqual(self.a.load()['instance_id'], saved['instance_id'])
        with self.assertRaisesRegex(protocol.ProtocolError, 'api_disabled'):
            self.invite()
        self.assertEqual(self.a.operate('servers')['servers'], ['a.example'])
        self.a.operate('api-enable')
        self.assertEqual(self.a.operate('api-enable')['status'], 'unchanged')

    def test_preexisting_allowlist_does_not_hide_invites_or_block_approval(self):
        self.a.operate('add', 'b.example')
        self.b.operate('add', 'a.example')
        rid = self.invite()
        self.assertEqual(self.b.operate('requests')['requests'][0]['id'], rid)
        self.assertEqual(self.a.operate('requests')['requests'][0]['id'], rid)
        self.assertTrue(self.a.operate('request-status', rid)['delivery_confirmed'])
        self.assertEqual(self.b.operate('approve', rid)['state'], 'connected')
        self.assertEqual(self.a.operate('request-status', rid)['state'], 'connected')
        self.assertEqual(self.a.servers().count('b.example'), 1)
        self.assertEqual(self.b.servers().count('a.example'), 1)

    def test_explicit_policy_removal_allows_new_agreement(self):
        now = int(time.time())
        first = self.invite()
        self.b.operate('approve', first)
        self.a.operate('remove', 'b.example')
        with patch.object(federation.time, 'time', return_value=now + 61):
            second = self.invite()
            self.assertEqual(self.b.operate('approve', second)['state'], 'connected')
        self.assertNotEqual(first, second)
        self.assertIn('b.example', self.a.servers())
        self.assertIn('a.example', self.b.servers())

    def test_rejection_allows_fresh_invite_and_preserves_notifications(self):
        now = int(time.time())
        first = self.invite()
        self.b.operate('reject', first)
        with patch.object(federation.time, 'time', return_value=now + 61):
            second = self.invite()
        self.assertNotEqual(first, second)
        listed = self.b.operate('requests')['requests']
        self.assertEqual(
            {item['id']: item['state'] for item in listed},
            {
                first: 'rejected',
                second: 'pending',
            },
        )
        with patch.object(federation.time, 'time', return_value=now + 61):
            self.b.operate('approve', second)
            notices = federation_service.bot_call(self.b, {'action': 'notifications'}, 10003)
        self.assertEqual(
            {item['id']: item['state'] for item in notices['requests']},
            {
                first: 'rejected',
                second: 'connected',
            },
        )

    def test_failed_invite_is_not_confirmed_and_maintenance_retries_delivery(self):
        original = self.a.transport
        with patch.object(
            self.a,
            'transport',
            side_effect=lambda server, path, body=None: original(server, path, body)
            if body is None
            else (_ for _ in ()).throw(protocol.ProtocolError('timeout')),
        ):
            first = self.a.operate('invite', 'b.example')
        self.assertFalse(first['delivery_confirmed'])
        self.assertTrue(first['delivery_pending'])
        self.assertEqual(self.b.load()['requests'], {})
        self.a.maintain()
        result = self.a.operate('request-status', first['id'])
        self.assertTrue(result['delivery_confirmed'])
        self.assertFalse(result.get('delivery_pending', False))
        self.assertEqual(len(self.b.load()['requests']), 1)

    def test_notification_pages_cover_history_below_rpc_byte_limit(self):
        rid = self.invite()
        data = self.a.load()
        template = data['requests'][rid]
        data['requests'] = {
            'MB-' + format(index, '032x'): dict(
                template, id='MB-' + format(index, '032x'), state='rejected'
            )
            for index in range(200)
        }
        data['jobs'] = {
            'FO-' + format(index, '032x'): {
                'state': 'complete',
                'action': 'reject',
                'actor': '@admin:a.example',
                'argument': 'MB-' + format(index, '032x'),
                'result': {'state': 'rejected', 'irrelevant_large_field': 'x' * 50000},
            }
            for index in range(200)
        }
        self.a.save(data)
        cursor = None
        seen = {'requests': [], 'operations': []}
        pages = 0
        while True:
            page = federation_service.bot_call(
                self.a,
                {
                    'action': 'notifications',
                    'argument': {'cursor': cursor} if cursor else None,
                },
                10003,
            )
            self.assertLess(len(json.dumps({'ok': True, 'result': page}).encode()), 32768)
            for collection in seen:
                seen[collection].extend(item['id'] for item in page[collection])
            pages += 1
            cursor = page['next_cursor']
            if cursor is None:
                break
            self.assertLess(pages, 30)
        for collection, values in seen.items():
            expected = data['requests' if collection == 'requests' else 'jobs']
            self.assertEqual(set(values), set(expected))
            self.assertEqual(len(values), len(expected))
        self.assertGreater(pages, 1)

    def test_unused_arguments_are_rejected_and_legacy_oversized_jobs_do_not_block_feed(self):
        actor = '@admin:a.example'
        for action in ('api-enable', 'api-disable', 'recover', 'rollback'):
            before = self.a.path.read_bytes()
            with self.assertRaisesRegex(protocol.ProtocolError, 'invalid_request'):
                federation_service.queue_job(
                    self.a,
                    {
                        'action': action,
                        'argument': 'я' * 4500,
                        'actor': actor,
                        'event_id': '$oversized-' + action,
                    },
                )
            self.assertEqual(self.a.path.read_bytes(), before)
        data = self.a.load()
        data['jobs'] = {
            'FO-' + 'f' * 32: {
                'action': 'api-disable',
                'argument': 'я' * 4500,
                'actor': actor,
                'state': 'complete',
                'result': {'enabled': False},
            }
        }
        self.a.save(data)
        before = self.a.path.read_bytes()
        page = federation_service.bot_call(self.a, {'action': 'notifications'}, 10003)
        self.assertIsNone(page['operations'][0]['argument'])
        self.assertEqual(page['operations'][0]['result'], {'enabled': False})
        self.assertLess(len(json.dumps(page).encode()), 32768)
        self.assertEqual(self.a.path.read_bytes(), before)

    def test_terminal_and_duplicate_intents_fail_before_creating_job(self):
        rid = self.invite()
        self.b.operate('reject', rid)
        for backend, action, actor in (
            (self.b, 'approve', '@admin:b.example'),
            (self.b, 'reject', '@admin:b.example'),
            (self.a, 'cancel', '@admin:a.example'),
        ):
            request = {'action': action, 'argument': rid, 'actor': actor, 'event_id': '$' + action}
            with self.subTest(action=action):
                before = backend.path.read_bytes()
                with self.assertRaisesRegex(protocol.ProtocolError, 'invalid_transition'):
                    federation_service.queue_job(backend, request)
                self.assertEqual(backend.path.read_bytes(), before)
                with self.assertRaisesRegex(protocol.ProtocolError, 'invalid_transition'):
                    federation_service.bot_call(
                        backend,
                        {
                            'action': 'validate',
                            'actor': actor,
                            'argument': {'action': action, 'argument': rid},
                        },
                        10003,
                    )
                self.assertEqual(backend.path.read_bytes(), before)

    def test_queue_reserves_request_and_execution_rechecks_remote_cancellation(self):
        rid = self.invite()
        request = {
            'action': 'approve',
            'argument': rid,
            'actor': '@admin:b.example',
            'event_id': '$approve-one',
        }
        first = federation_service.queue_job(self.b, request)
        self.assertEqual(federation_service.queue_job(self.b, request), first)
        for action in ('approve', 'reject'):
            with self.assertRaisesRegex(protocol.ProtocolError, 'request_in_progress'):
                federation_service.queue_job(self.b, dict(request, action=action, event_id='$two'))
        self.a.operate('cancel', rid)
        federation_service.job_step(self.b)
        job = self.b.load()['jobs'][first['operation_id']]
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['result']['code'], 'invalid_transition')
        self.assertNotIn('a.example', self.b.servers())
        self.assertEqual(len(self.b.load()['jobs']), 1)

    def test_duplicate_invite_rejected_before_queue_and_before_job_execution(self):
        request = {
            'action': 'invite',
            'argument': 'b.example',
            'actor': '@admin:a.example',
            'event_id': '$invite-one',
        }
        first = federation_service.queue_job(self.a, request)
        with self.assertRaisesRegex(protocol.ProtocolError, 'request_in_progress'):
            federation_service.queue_job(self.a, dict(request, event_id='$invite-two'))
        federation_service.job_step(self.a)
        self.assertEqual(self.a.load()['jobs'][first['operation_id']]['state'], 'complete')
        with self.assertRaisesRegex(protocol.ProtocolError, 'pending_request_exists'):
            federation_service.queue_job(self.a, dict(request, event_id='$invite-three'))
        self.assertEqual(len(self.a.load()['jobs']), 1)
        self.assertEqual(len(self.b.load()['requests']), 1)

    def test_legacy_peer_requires_upgrade_before_new_thirty_day_invitation(self):
        transport = self.a.transport

        def legacy(server, path, body=None):
            result = transport(server, path, body)
            if path == protocol.DISCOVERY:
                result.pop('request_ttl', None)
            return result

        self.a.transport = legacy
        with self.assertRaisesRegex(protocol.ProtocolError, 'peer_upgrade_required'):
            self.invite()
        self.assertEqual(self.a.load()['requests'], {})
        self.assertNotIn('last_invitation_at', self.a.load())

    def test_invitation_preflight_rejects_plain_matrix_and_unsupported_peers_before_post(self):
        document = self.b.doc()
        original = self.transport
        cases = [
            (protocol.ProtocolError('remote_http_404'), 'misbot_not_found'),
            (protocol.ProtocolError('invalid_json_response'), 'invalid_misbot_discovery'),
            ({'m.server': 'b.example:443'}, 'invalid_misbot_discovery'),
            ('<html>Matrix server</html>', 'invalid_misbot_discovery'),
            ({**document, 'protocol': 99}, 'unsupported_version'),
            ({**document, 'api_path': '/arbitrary-admin'}, 'invalid_misbot_discovery'),
            ({**document, 'public_key': None}, 'invalid_misbot_discovery'),
            ({**document, 'capabilities': {}}, 'invalid_misbot_discovery'),
            ({**document, 'capabilities': []}, 'invitation_unsupported'),
            ({k: v for k, v in document.items() if k != 'capabilities'}, 'peer_upgrade_required'),
            (protocol.ProtocolError('remote_api_disabled'), 'remote_api_disabled'),
        ]
        for discovery, code in cases:
            calls = []

            def transport(server, path, body=None):
                calls.append((path, body))
                if path == protocol.DISCOVERY:
                    if isinstance(discovery, Exception):
                        raise discovery
                    return discovery
                return original(server, path, body)

            with self.subTest(code=code, discovery_type=type(discovery).__name__):
                self.a.transport = transport
                preflight = self.a.operate('check', 'b.example')['invitation']
                self.assertFalse(preflight['ready'])
                self.assertFalse(preflight['supported'])
                self.assertEqual(preflight['code'], code)
                with self.assertRaisesRegex(protocol.ProtocolError, code):
                    self.invite()
                self.assertEqual(self.a.load()['requests'], {})
                self.assertNotIn('last_invitation_at', self.a.load())
                self.assertTrue(all(path != protocol.API and body is None for path, body in calls))

    def test_preflight_advertises_capability_without_creating_and_rechecks_before_invite(self):
        document = self.b.doc()
        self.assertIn(protocol.INVITATION_CAPABILITY, document['capabilities'])
        preflight = self.a.operate('check', 'b.example')['invitation']
        self.assertEqual(
            preflight,
            {
                'supported': True,
                'ready': True,
                'expires_after': 30 * 86400,
            },
        )
        self.assertEqual(self.a.load()['requests'], {})
        self.a.operate('api-disable')
        preflight = self.a.operate('check', 'b.example')['invitation']
        self.assertTrue(preflight['supported'])
        self.assertFalse(preflight['ready'])
        self.assertEqual(preflight['code'], 'api_disabled')
        self.a.operate('api-enable')
        document['capabilities'] = []
        original = self.transport
        posts = []

        def changed(server, path, body=None):
            if path == protocol.DISCOVERY:
                return document
            posts.append((path, body))
            return original(server, path, body)

        self.a.transport = changed
        with self.assertRaisesRegex(protocol.ProtocolError, 'invitation_unsupported'):
            self.invite()
        self.assertEqual(posts, [])
        self.assertEqual(self.a.load()['requests'], {})

    def test_crossed_offline_invites_converge_without_transferring_approval(self):
        original = self.transport

        def offline(server, path, body=None):
            if path == protocol.API:
                raise protocol.ProtocolError('timeout')
            return original(server, path, body)

        self.a.transport = offline
        self.b.transport = offline
        a = self.invite()
        b = self.b.operate('invite', 'a.example')['id']
        self.a.transport = original
        self.b.transport = original
        winner, loser = (self.a, self.b) if a < b else (self.b, self.a)
        winner_id, loser_id = (a, b) if a < b else (b, a)
        winner.operate('retry', winner_id)
        self.assertEqual(loser.operate('request-status', loser_id)['state'], 'cancelled')
        self.assertFalse(loser.load()['requests'][winner_id]['local_approval'])
        self.assertEqual(len(loser.operate('requests')['requests']), 1)
        loser.operate('approve', winner_id)
        self.assertEqual(winner.operate('request-status', winner_id)['state'], 'connected')

    def test_current_admin_local_uid_and_event_replay(self):
        for actor in (
            '@member:a.example',
            '@roomadmin:a.example',
            '@admin:foreign.example',
            '@blocked:a.example',
        ):
            with self.assertRaisesRegex(protocol.ProtocolError, 'admin_unconfirmed'):
                self.a.operate('add', 'c.example', actor)
        self.a.operate('add', 'c.example', '@admin:a.example', '$one')
        self.assertEqual(
            self.a.operate('remove', 'c.example', '@admin:a.example', '$one')['status'],
            'already_processed',
        )
        self.assertIn('c.example', self.a.servers())
        with self.assertRaises(protocol.ProtocolError):
            federation_service.bot_call(
                self.a, {'action': 'servers', 'actor': 'root', 'event_id': '$x'}, 10003
            )
        with self.assertRaises(protocol.ProtocolError):
            federation_service.bot_call(
                self.a, {'action': 'servers', 'actor': '@admin:a.example', 'event_id': '$x'}, 10002
            )

    def test_durable_bot_job_survives_bot_stop_and_rechecks_admin(self):
        request = {
            'action': 'add',
            'argument': 'c.example',
            'actor': '@admin:a.example',
            'event_id': '$queued',
        }
        ack = federation_service.bot_call(self.a, request, 10003)
        self.assertEqual(ack['status'], 'queued')
        self.assertNotIn('c.example', self.a.servers())
        self.assertEqual(ack['plan']['before'], ['a.example'])
        self.assertEqual(ack['plan']['after'], ['a.example', 'c.example'])
        self.assertEqual(
            federation_service.bot_call(self.a, request, 10003)['operation_id'], ack['operation_id']
        )
        federation_service.job_step(self.a)
        self.assertIn('c.example', self.a.servers())
        self.assertEqual(self.a.load()['jobs'][ack['operation_id']]['state'], 'complete')
        request.update(event_id='$revoked-before-worker', argument='d.example')
        ack = federation_service.bot_call(self.a, request, 10003)
        self.allowed.remove('@admin:a.example')
        federation_service.job_step(self.a)
        self.assertNotIn('d.example', self.a.servers())
        self.assertEqual(
            self.a.load()['jobs'][ack['operation_id']]['result']['code'], 'admin_unconfirmed'
        )

    def test_completed_event_replay_preserves_receipt_without_repeating_exchange(self):
        request = {
            'action': 'invite',
            'argument': 'b.example',
            'actor': '@admin:a.example',
            'event_id': '$lost-rpc-response',
        }
        accepted = federation_service.queue_job(self.a, request)
        federation_service.job_step(self.a)
        before = self.a.path.read_bytes()
        with patch.object(self.a, 'transport') as transport:
            replay = federation_service.queue_job(self.a, request)
        transport.assert_not_called()
        self.assertEqual(replay['operation_id'], accepted['operation_id'])
        self.assertEqual(replay['status'], 'complete')
        self.assertTrue(replay['delivery_confirmed'])
        self.assertEqual(replay['id'], next(iter(self.a.load()['requests'])))
        self.assertEqual(self.a.path.read_bytes(), before)
        self.assertEqual(len(self.a.load()['jobs']), 1)
        self.assertEqual(len(self.b.load()['requests']), 1)

    def test_queued_event_cannot_change_actor_or_action(self):
        request = {
            'action': 'add',
            'argument': 'c.example',
            'actor': '@admin:a.example',
            'event_id': '$fixture-event',
        }
        accepted = federation_service.queue_job(self.a, request)
        with self.assertRaisesRegex(protocol.ProtocolError, 'event_mismatch'):
            federation_service.queue_job(self.a, dict(request, argument='d.example'))
        with self.assertRaisesRegex(protocol.ProtocolError, 'local_authorization_required'):
            federation_service.queue_job(self.a, dict(request, actor='root'))
        self.assertEqual(self.a.load()['jobs'][accepted['operation_id']]['argument'], 'c.example')
        self.assertNotIn('c.example', self.a.servers())

    def test_removed_room_operations_are_denied_without_side_effects(self):
        before = {path: path.read_bytes() for path in self.a.root.rglob('*') if path.is_file()}
        for action in (
            'room-invitations',
            'room-accept',
            'room-reject',
            'room-leave',
            'room-invite',
            'auto-accept',
        ):
            request = {
                'action': action,
                'argument': '!fixture:a.example',
                'actor': '@admin:a.example',
                'event_id': '$' + action,
            }
            with (
                self.subTest(action=action),
                patch.object(self.a, 'ready', side_effect=AssertionError('backend access')),
            ):
                with self.assertRaisesRegex(protocol.ProtocolError, 'unknown_operation'):
                    self.a.operate(
                        action, request['argument'], request['actor'], request['event_id']
                    )
                with self.assertRaisesRegex(protocol.ProtocolError, 'local_authorization_required'):
                    federation_service.bot_call(self.a, request, 10003)
                with self.assertRaisesRegex(protocol.ProtocolError, 'local_authorization_required'):
                    federation_service.queue_job(self.a, request)
        self.assertEqual(
            before, {path: path.read_bytes() for path in self.a.root.rglob('*') if path.is_file()}
        )

    def test_previously_queued_room_operation_fails_after_update(self):
        jid = 'FO-' + 'a' * 32
        data = self.a.load()
        data['jobs'] = {
            jid: {
                'action': 'auto-accept',
                'argument': 'true',
                'actor': '@admin:a.example',
                'event_id': '$old-room-operation',
                'state': 'queued',
            }
        }
        self.a.save(data)
        federation_service.job_step(self.a)
        job = self.a.load()['jobs'][jid]
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['result']['code'], 'unknown_operation')
        self.assertFalse(self.a.auto_accept())

    def test_policy_plan_is_shown_before_actual_change(self):
        emitted = []
        self.a.admission = True
        with patch.object(
            federation.lifecycle,
            'emit',
            side_effect=lambda name, **values: emitted.append((name, values)),
        ):
            result = self.a.change('c.example', True, 'root')
        self.assertEqual(emitted[0][0], 'federation_policy_plan')
        self.assertEqual(emitted[0][1]['before'], ['a.example'])
        self.assertEqual(result['servers'], ['a.example', 'c.example'])

    def test_admin_revoked_during_checked_backup_prevents_publication(self):
        actor = '@admin:a.example'
        with (
            patch.object(
                federation.lifecycle, 'backup', side_effect=lambda: self.allowed.remove(actor)
            ),
            patch.object(federation.lifecycle, 'restore_check'),
        ):
            with self.assertRaisesRegex(protocol.ProtocolError, 'admin_unconfirmed'):
                self.a.apply_policy(['a.example', 'c.example'], actor=actor)
        self.assertFalse((self.a.root / 'federation-transition.json').exists())
        self.assertEqual(self.a.servers(), ['a.example'])

    def test_actual_admin_query_fail_closed_and_blocked_attributes(self):
        self.assertTrue(federation_admin_api.authorized({'admin': True}))
        for attr in (
            {'admin': False, 'power_level': 100},
            {'admin': True, 'locked_at': 'fictional'},
            {'admin': True, 'deactivated_at': 'fictional'},
            {'admin': True, 'legacy_guest': True},
        ):
            self.assertFalse(federation_admin_api.authorized(attr))
        self.a.admin = self.a.check_admin
        (self.a.root / 'secrets').mkdir(mode=0o700)
        state_store.write_json(
            self.a.root / 'secrets/referral-client.json',
            {'client_id': 'FICTIONAL_ID', 'client_secret': 'FICTIONAL_SECRET'},
        )
        (self.a.root / 'federation_admin_api.py').write_text('# fictional helper')
        with patch.object(
            federation.lifecycle, 'dc', side_effect=RuntimeError('FICTIONAL_SECRET_DO_NOT_ECHO')
        ):
            self.assertFalse(self.a.check_admin('@admin:a.example'))
            with self.assertRaisesRegex(protocol.ProtocolError, 'admin_unconfirmed'):
                self.a.operate('servers', actor='@admin:a.example')
        self.assertFalse(self.a.check_admin('@admin:foreign.example'))

    def test_staged_frozen_and_second_controller_owner_denied(self):
        live = federation.Backend(self.a.root)
        for flag in ('restore_pending', 'migration_frozen'):
            metadata = configuration.read_private_json(self.a.root / 'installation.json')
            metadata[flag] = True
            state_store.write_json(self.a.root / 'installation.json', metadata)
            with self.assertRaisesRegex(protocol.ProtocolError, 'installation_pending'):
                live.ready()
            metadata.pop(flag)
            state_store.write_json(self.a.root / 'installation.json', metadata)
        system = self.root / 'system'
        system.mkdir(mode=0o700)
        calls = []
        federation_runtime.controller(self.a.root, lambda args: calls.append(args), system=system)
        second = self.root / 'second'
        second.mkdir(mode=0o700)
        state_store.write_json(second / 'installation.json', {'domain': 'a.example'})
        with self.assertRaisesRegex(RuntimeError, 'Another installation owns'):
            federation_runtime.controller(second, lambda args: None, system=system)
        self.assertTrue(any('restart' in args for args in calls))
        with self.assertRaisesRegex(RuntimeError, 'Another installation owns'):
            federation_runtime.controller(
                second, lambda args: None, False, system=system, remove=True
            )
        federation_runtime.controller(self.a.root, lambda args: None, False, system=system)
        self.assertTrue((system / federation_runtime.unit_name(self.a.root)).exists())
        federation_runtime.controller(
            self.a.root, lambda args: None, False, system=system, remove=True
        )
        self.assertFalse((system / federation_runtime.unit_name(self.a.root)).exists())
        federation_runtime.controller(second, lambda args: None, system=system)

    def test_saved_state_never_regenerates_missing_identity_key(self):
        self.a.key.unlink()
        with self.assertRaisesRegex(RuntimeError, 'restore its backup'):
            federation.setup(self.a.root, {'domain': 'a.example'})
        self.assertFalse(self.a.key.exists())

    def test_policy_validation_repeat_auto_and_trust_pin(self):
        for value in (
            'https://example.org',
            'localhost',
            '127.0.0.1',
            'example.org/path',
            'example.org:443',
        ):
            with self.assertRaises(ValueError):
                self.a.operate('add', value)
        self.assertEqual(self.a.operate('add', 'a.example')['status'], 'unchanged')
        self.assertEqual(self.a.operate('remove', 'absent.example')['status'], 'unchanged')
        claim = json.dumps({'sender': '@admin-panel:b.example', 'room': '!fixture:b.example'})
        self.assertFalse(
            federation_service.bot_call(
                self.a, {'action': 'auto-invitation', 'argument': claim}, 10003
            )['allowed']
        )
        rid = self.invite()
        self.b.operate('approve', rid)
        model = configuration.load(self.a.root)
        model['FEDERATION_AUTO_ACCEPT'] = 'true'
        configuration.write_model(self.a.root, model)
        with patch(
            'federation_runtime.matrix_operation',
            return_value={
                'invitations': [{'room': '!fixture:b.example', 'sender': '@admin-panel:b.example'}]
            },
        ):
            self.assertTrue(
                federation_service.bot_call(
                    self.a, {'action': 'auto-invitation', 'argument': claim}, 10003
                )['allowed']
            )
        with patch('federation_runtime.matrix_operation', return_value={'invitations': []}):
            self.assertFalse(
                federation_service.bot_call(
                    self.a, {'action': 'auto-invitation', 'argument': claim}, 10003
                )['allowed']
            )
        self.assertFalse(
            federation_service.bot_call(
                self.a,
                {
                    'action': 'auto-invitation',
                    'argument': json.dumps(
                        {'sender': '@federation-bot:unknown.example', 'room': '!fixture:b.example'}
                    ),
                },
                10003,
            )['allowed']
        )
        old = self.b.doc()
        altered = {**old, 'instance_id': 'c' * 32}
        with patch.object(self.a, 'transport', lambda *a: altered):
            with self.assertRaisesRegex(protocol.ProtocolError, 'peer_identity_changed'):
                self.a.peer(self.a.load(), 'b.example')

    def test_network_failures_not_nonexistence_and_private_ip_refused(self):
        for code in ('dns_failed', 'tls_failed', 'timeout'):
            with patch.object(self.a, 'transport', side_effect=protocol.ProtocolError(code)):
                result = self.a.operate('check', 'b.example')
                self.assertEqual(result['code'], code)
                self.assertEqual(result['https'], 'unconfirmed')
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaisesRegex(protocol.ProtocolError, 'non_public_destination'):
                protocol.https('example.org', protocol.DISCOVERY)

    def test_https_discovery_distinguishes_api_off_and_malformed_response_without_network(self):
        cases = (
            (
                503,
                b'{"code":"api_disabled","detail":"FICTIONAL_PRIVATE_VALUE"}',
                'remote_api_disabled',
            ),
            (503, b'<html>busy</html>', 'remote_http_503'),
            (200, b'<html>Matrix</html>', 'invalid_misbot_discovery'),
            (404, b'{}', 'misbot_not_found'),
        )
        for status, body, code in cases:
            connection = Mock()
            connection.getresponse.return_value.status = status
            connection.getresponse.return_value.read.return_value = body
            with (
                self.subTest(status=status, code=code),
                patch.object(
                    protocol.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]
                ),
                patch.object(protocol.socket, 'socket'),
                patch.object(protocol.ssl, 'create_default_context'),
                patch.object(protocol.http.client, 'HTTPSConnection', return_value=connection),
            ):
                with self.assertRaisesRegex(protocol.ProtocolError, code) as error:
                    protocol.discover('b.example')
                self.assertNotIn('FICTIONAL_PRIVATE_VALUE', str(error.exception))
            self.assertEqual(connection.request.call_args.args[:2], ('GET', protocol.DISCOVERY))

    def test_backup_actual_extract_identity_request_and_keys(self):
        rid = self.invite()
        self.b.operate('approve', rid)
        original = self.a.key.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        archive = self.root / 'backup.tar.gz'
        metadata = configuration.read_private_json(self.a.root / 'installation.json')
        metadata['federation_seeded'] = True
        state_store.write_json(self.a.root / 'installation.json', metadata)
        with tarfile.open(archive, 'w:gz') as tar:
            for name in ('installation.json', 'data/federation'):
                tar.add(self.a.root / name, arcname=name)
        federation_runtime.verify_archive(archive)
        restored = self.root / 'restored'
        restored.mkdir(mode=0o700)
        with tarfile.open(archive) as tar:
            tar.extractall(restored, filter='data')
        self.assertEqual(
            hashlib.sha256((restored / 'data/federation/identity.pem').read_bytes()).hexdigest(),
            digest,
        )
        state = configuration.read_private_json(restored / 'data/federation/state.json')
        self.assertEqual(state['requests'][rid]['state'], 'connected')
        self.assertEqual(state['instance_id'], self.a.load()['instance_id'])
        self.assertEqual((restored / 'data/federation/identity.pem').stat().st_mode & 0o777, 0o600)

    def test_interrupted_policy_apply_recover_and_rollback(self):
        root = self.a.root
        (root / 'config').mkdir(mode=0o700, exist_ok=True)
        (root / 'public').mkdir(mode=0o700)
        state_store.write_text(
            root / 'config/homeserver.yaml',
            'server_name: a.example\nfederation_domain_whitelist: [a.example]\n',
        )
        state_store.write_json(
            root / 'public/web-config.json',
            {
                'room_directory': {'servers': ['a.example'], 'custom_policy': 'retained'},
                'brand': 'retained',
            },
        )
        key = self.a.key.read_bytes()
        with (
            state_store.operation_guard(root),
            patch.object(federation.lifecycle, 'backup'),
            patch.object(federation.lifecycle, 'restore_check'),
            patch.object(federation.lifecycle, 'dc', side_effect=RuntimeError('failure')),
        ):
            with self.assertRaises(RuntimeError):
                self.a.apply_policy(['a.example', 'b.example'])
        self.assertTrue((root / 'federation-transition.json').exists())
        self.assertEqual((root / 'public/web-config.json').stat().st_mode & 0o777, 0o644)
        with self.assertRaisesRegex(RuntimeError, 'federation recover'):
            state_store.assert_update_allowed(root)
        with (
            state_store.operation_guard(root),
            patch.object(federation.lifecycle, 'dc', return_value='') as dc,
            patch.object(federation.lifecycle, 'wait'),
        ):
            self.assertEqual(self.a.recover_policy()['status'], 'applied')
            self.assertEqual(dc.call_args.args[:4], ('up', '-d', '--no-deps', '--force-recreate'))
        self.assertFalse((root / 'federation-transition.json').exists())
        self.assertEqual(self.a.key.read_bytes(), key)
        self.assertEqual(
            json.loads((root / 'public/web-config.json').read_text())['room_directory'][
                'custom_policy'
            ],
            'retained',
        )
        with (
            state_store.operation_guard(root),
            patch.object(federation.lifecycle, 'backup'),
            patch.object(federation.lifecycle, 'restore_check'),
            patch.object(federation.lifecycle, 'dc', side_effect=RuntimeError('failure')),
        ):
            with self.assertRaises(RuntimeError):
                self.a.apply_policy(['a.example'])
        with (
            state_store.operation_guard(root),
            patch.object(federation.lifecycle, 'dc', return_value=''),
            patch.object(federation.lifecycle, 'wait'),
        ):
            self.assertEqual(self.a.recover_policy(True)['status'], 'rolled_back')
        self.assertIn('b.example', self.a.servers())
        self.assertEqual(self.a.key.read_bytes(), key)


if __name__ == '__main__':
    unittest.main()
