import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

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
        self.assertEqual(self.a.operate('invite', 'b.example')['id'], rid)
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
        self.a.operate('add', 'b.example')
        rid = self.invite()
        self.b.operate('reject', rid)
        self.assertEqual(self.a.operate('request-status', rid)['state'], 'rejected')
        self.assertIn('b.example', self.a.servers())
        rid = self.invite()
        self.b.operate('approve', rid)
        self.a.operate('cancel', rid)
        self.assertIn('b.example', self.a.servers())
        self.assertIn('a.example', self.b.servers())

    def test_simultaneous_invites_and_expiration(self):
        a = self.invite()
        b = self.b.operate('invite', 'a.example')['id']
        self.assertNotEqual(a, b)
        self.a.operate('approve', b)
        self.b.operate('approve', a)
        self.assertEqual(self.a.servers().count('b.example'), 1)
        data = self.a.load()
        data['requests'][a]['state'] = 'pending'
        data['requests'][a]['expires_at'] = 0
        self.a.save(data)
        self.assertEqual(self.a.operate('retry', a)['state'], 'expired')

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
