import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import Bridge
from common import BotError
from control import run_control
from test_control import FakeMAS


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir()
        self.config = {
            'stack_root': str(self.root),
            'federation_enabled': False,
            'management_actions': ['status', 'check', 'start', 'backup'],
        }
        (self.root / 'bot-config.json').write_text(
            json.dumps({'servers': {'example.org': self.config}})
        )
        (self.root / 'bot-policy.json').write_text(
            json.dumps(
                {
                    'servers': {
                        'example.org': {
                            'invitation_lifetime_seconds': 604800,
                            'invitation_usage_limit': 1,
                            'registration_required': False,
                            'federation_servers': None,
                        }
                    }
                }
            )
        )
        (self.root / 'installation.json').write_text('{"backend":"existing-compose"}')
        self.bridge = Bridge(self.root / 'bot-config.json', FakeMAS)
        self.api = self.bridge.apis['example.org']
        for uid in (10, 11):
            self.bridge.db.execute(
                'INSERT INTO authorizations VALUES (?,?,?,?)',
                ('example.org', uid, 'MAS-USER', int(time.time()) + 1000),
            )
        self.bridge.db.commit()

    def tearDown(self):
        self.bridge.db.close()
        self.tmp.cleanup()

    def call(self, action, uid=10, **extras):
        return self.bridge.handle({'action': action, 'server': 'example.org', 'uid': uid, **extras})

    def test_current_mas_admin_role_required_for_reads_and_writes(self):
        self.api.admin = False
        with patch('bridge.run_control') as backend:
            for action in ('manage_read', 'manage_prepare', 'manage_apply', 'manage_job'):
                with self.subTest(action=action), self.assertRaises(BotError):
                    self.call(action, operation='status')
            backend.assert_not_called()

    def test_disabled_federation_never_reads_or_writes_legacy_files(self):
        with patch.object(self.bridge, 'apply_federation') as legacy:
            for action in ('federation', 'fed_prepare', 'fed_apply'):
                with self.assertRaises(BotError):
                    self.call(action, operation='add', domain='other.example')
            legacy.assert_not_called()

    def test_adopted_federation_queues_current_admin_once_without_legacy_writes(self):
        self.bridge.config['servers']['example.org'].update(
            federation_enabled=True, federation_backend='misbot'
        )
        backend = SimpleNamespace(operate=lambda *a, **k: {'servers': ['example.org']})
        service = SimpleNamespace(
            queue_job=lambda b, r: {'operation_id': 'FO-fixture', 'status': 'queued'}
        )
        with (
            patch.object(self.bridge, 'federation_backend', return_value=backend),
            patch.object(self.bridge, 'apply_federation') as legacy,
            patch.dict(sys.modules, {'federation_service': service}),
        ):
            token = self.call('fed_prepare', operation='add', domain='other.example')[
                'confirmation'
            ]
            self.api.admin = False
            with self.assertRaises(BotError):
                self.call('fed_apply', confirmation=token)
            self.api.admin = True
            with patch.object(service, 'queue_job', wraps=service.queue_job) as queued:
                result = self.call('fed_apply', confirmation=token)
                self.assertEqual(result['status'], 'queued')
                payload = queued.call_args.args[1]
                self.assertEqual(payload['actor'], '@member:example.org')
                self.assertEqual(payload['argument'], 'other.example')
                with self.assertRaises(BotError):
                    self.call('fed_apply', confirmation=token)
                queued.assert_called_once()
            legacy.assert_not_called()

    def test_component_output_never_includes_private_fields(self):
        self.config['management_actions'].append('components')
        body = json.dumps(
            {
                'check': 'existing_features',
                'enabled': True,
                'admin': 'admin.example.org',
                'homepage': 'example.org',
                'secret': 'FICTIONAL_SECRET',
                'directory': 'PRIVATE',
            }
        ).encode()
        with patch('control.subprocess.run', return_value=subprocess.CompletedProcess([], 0, body)):
            result = run_control(self.config, 'components')
        self.assertEqual(result['components']['admin'], 'admin.example.org')
        self.assertTrue(result['ok'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))

    def test_fixed_cli_arguments_and_output_allowlist(self):
        output = json.dumps(
            {
                'check': 'existing_status',
                'directory': '/PRIVATE',
                'secret': 'FICTIONAL_SECRET',
                'backup_pending': False,
                'containers': [
                    {
                        'name': 'matrix-synapse',
                        'running': True,
                        'health': 'healthy',
                        'image_id': 'PRIVATE',
                    },
                    {'name': 'https://private.example/path'},
                ],
            }
        )
        with patch(
            'control.subprocess.run',
            return_value=subprocess.CompletedProcess([], 0, output.encode()),
        ) as run:
            result = run_control(self.config, 'status')
        self.assertEqual(
            run.call_args.args[0],
            [
                '/usr/bin/python3',
                str(self.root / 'misbot.py'),
                '--directory',
                str(self.root),
                'status',
            ],
        )
        self.assertTrue(result['ok'])
        self.assertEqual(
            result['containers'], [{'name': 'matrix-synapse', 'running': True, 'healthy': True}]
        )
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))
        for operation in ('stop', 'remove', 'start;id'):
            with self.assertRaises(BotError):
                run_control(self.config, operation)

    def test_missing_native_or_symlink_installation_rejected(self):
        with patch('control.subprocess.run') as run:
            (self.root / 'installation.json').write_text('{"backend":"native"}')
            with self.assertRaises(BotError):
                run_control(self.config, 'start')
            run.assert_not_called()

    def test_storage_output_and_connect_arguments_are_fixed(self):
        self.config['management_actions'] += ['storage', 'storage_connect', 'backup_verify', 'stop']
        output = json.dumps(
            {
                'check': 'existing_storage_status',
                'media_s3': True,
                'backup_s3': True,
                'last_backup_verified': True,
                'secret': 'FICTIONAL_SECRET',
                'storage': {'private': 'PRIVATE'},
            }
        ).encode()
        with patch(
            'control.subprocess.run', return_value=subprocess.CompletedProcess([], 0, output)
        ) as run:
            result = run_control(self.config, 'storage')
            self.assertTrue(result['storage']['media_s3'])
            self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))
            run_control(self.config, 'storage_connect')
            self.assertEqual(
                run.call_args.args[0][-5:],
                ['storage', 's3', 'connect', '--config', str(self.root / '.misbot/s3/media.env')],
            )
        for operation in ('stop', 'backup_verify', 'storage_connect'):
            with self.subTest(operation=operation), self.assertRaises(BotError):
                self.call('manage_read', operation=operation)

    def test_timeout_and_unrecognized_output_do_not_claim_success(self):
        with patch(
            'control.subprocess.run', side_effect=subprocess.TimeoutExpired('FICTIONAL_SECRET', 85)
        ):
            self.assertFalse(run_control(self.config, 'check')['ok'])
        with patch(
            'control.subprocess.run',
            return_value=subprocess.CompletedProcess([], 0, b'FICTIONAL_SECRET'),
        ):
            self.assertFalse(run_control(self.config, 'status')['ok'])

    def test_confirmation_bound_to_user_role_and_once_only(self):
        confirmation = self.call('manage_prepare', operation='backup')['confirmation']
        with self.assertRaises(BotError):
            self.call('manage_apply', uid=11, confirmation=confirmation)
        self.api.admin = False
        with self.assertRaises(BotError):
            self.call('manage_apply', confirmation=confirmation)
        self.api.admin = True
        with patch('bridge.threading.Thread') as thread:
            job = self.call('manage_apply', confirmation=confirmation)
            thread.return_value.start.assert_called_once()
        with self.assertRaises(BotError):
            self.call('manage_apply', confirmation=confirmation)
        with self.assertRaises(BotError):
            self.call('manage_job', uid=11, job=job['job'])
        self.assertEqual(self.call('manage_job', job=job['job'])['state'], 'running')
        with patch('bridge.run_control', return_value={'operation': 'backup', 'ok': True}):
            self.bridge.management_job(job['job'], self.config, 'backup')
        self.assertTrue(self.call('manage_job', job=job['job'])['result']['ok'])

    def test_expired_confirmation_and_write_without_confirmation_rejected(self):
        confirmation = self.call('manage_prepare', operation='start')['confirmation']
        self.bridge.db.execute('UPDATE confirmations SET expires=0')
        self.bridge.db.commit()
        with self.assertRaises(BotError):
            self.call('manage_apply', confirmation=confirmation)
        with self.assertRaises(BotError):
            self.call('manage_read', operation='start')

    def test_one_active_job_and_no_replay_after_service_restart(self):
        first = self.call('manage_prepare', operation='backup')['confirmation']
        second = self.call('manage_prepare', operation='start')['confirmation']
        with patch('bridge.threading.Thread'):
            job = self.call('manage_apply', confirmation=first)
            with self.assertRaises(BotError):
                self.call('manage_apply', confirmation=second)
        self.bridge.db.close()
        self.bridge = Bridge(self.root / 'bot-config.json', FakeMAS)
        self.assertEqual(self.call('manage_job', job=job['job'])['state'], 'interrupted')


if __name__ == '__main__':
    unittest.main()
