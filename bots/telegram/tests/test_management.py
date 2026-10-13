import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
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
                ('example.org', uid, f'MAS-USER-{uid}', int(time.time()) + 1000),
            )
        self.bridge.db.commit()

    def tearDown(self):
        self.bridge.db.close()
        self.tmp.cleanup()

    def call(self, action, uid=10, **extras):
        return self.bridge.handle({'action': action, 'server': 'example.org', 'uid': uid, **extras})

    def test_s3_prepare_checks_private_profile_before_creating_confirmation(self):
        self.bridge.config['servers']['example.org']['management_actions'].append('storage_connect')
        for content, mode in [
            (None, 0o600),
            ('SECRET_PRIVATE_VALUE', 0o600),
            ('S3_BUCKET=example-bucket', 0o644),
        ]:
            folder = self.root / '.misbot/s3'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / 'media.env'
            if content is not None:
                path.write_text(content)
                path.chmod(mode)
            with self.subTest(content=content), self.assertRaises(BotError) as raised:
                self.call('manage_prepare', operation='storage_connect')
            self.assertNotIn('SECRET_PRIVATE_VALUE', str(raised.exception))
            self.assertEqual(
                self.bridge.db.execute('SELECT count(*) FROM confirmations').fetchone()[0], 0
            )
        path.write_text(
            'S3_BUCKET=example-bucket\nS3_REGION=example-region\nS3_ENDPOINT_URL=https://objects.example.org\nS3_ACCESS_KEY_ID=fictional-access\nS3_SECRET_ACCESS_KEY=fictional-secret\nS3_PREFIX=media/'
        )
        path.chmod(0o600)
        result = self.call('manage_prepare', operation='storage_connect')
        self.assertTrue(result['confirmation'])

    def test_check_details_are_allowlisted_without_raw_errors(self):
        output = b'\n'.join(
            json.dumps(item).encode()
            for item in [
                {
                    'check': 'mas_health',
                    'passed': False,
                    'failure_code': 'timeout',
                    'error': 'SECRET',
                },
                {
                    'check': 'synapse',
                    'passed': False,
                    'failure_code': 'http_status',
                    'status': 503,
                    'expected': 200,
                },
                {'check': 'unknown', 'passed': False, 'failure_code': 'SECRET', 'detail': 'SECRET'},
            ]
        )
        with patch(
            'control.subprocess.run', return_value=subprocess.CompletedProcess([], 1, output)
        ):
            result = run_control(self.config, 'check')
        self.assertIn('отведённое время', result['checks'][0]['detail'])
        self.assertIn('HTTP 503', result['checks'][1]['detail'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_management_read_releases_lock_and_rechecks_authorization(self):
        started, release = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            started.set()
            if not release.wait(3):
                raise TimeoutError()
            return {'ok': True}

        with patch('bridge.run_control', side_effect=slow), ThreadPoolExecutor(2) as pool:
            result = pool.submit(self.call, 'manage_read', operation='status')
            try:
                self.assertTrue(started.wait(1))
                self.assertTrue(pool.submit(self.call, 'logout').result(timeout=1))
                self.assertIn('example.org', self.bridge.handle({'action': 'servers'}))
            finally:
                release.set()
            with self.assertRaises(BotError):
                result.result(timeout=1)

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
        queued = {'operation_id': 'SO-' + 'a' * 32, 'operation': 'backup', 'status': 'queued'}
        with (
            patch.object(self.bridge, 'management_runtime', return_value=self.root),
            patch('server_management.queue_operation', return_value=queued) as enqueue,
        ):
            job = self.call('manage_apply', confirmation=confirmation)
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args.kwargs['actor'], '@member:example.org')
        with self.assertRaises(BotError):
            self.call('manage_apply', confirmation=confirmation)
        with self.assertRaises(BotError):
            self.call('manage_job', uid=11, job=job['job'])
        finished = {**queued, 'status': 'complete', 'result': {'ok': True}}
        with (
            patch.object(self.bridge, 'management_runtime', return_value=self.root),
            patch('server_management.operation_status', return_value=finished),
        ):
            self.assertTrue(self.call('manage_job', job=job['job'])['result']['ok'])

    def test_expired_confirmation_and_write_without_confirmation_rejected(self):
        confirmation = self.call('manage_prepare', operation='start')['confirmation']
        self.bridge.db.execute('UPDATE confirmations SET expires=0')
        self.bridge.db.commit()
        with self.assertRaises(BotError):
            self.call('manage_apply', confirmation=confirmation)
        with self.assertRaises(BotError):
            self.call('manage_read', operation='start')

    def test_detached_job_survives_bridge_restart_and_is_not_replayed(self):
        first = self.call('manage_prepare', operation='backup')['confirmation']
        second = self.call('manage_prepare', operation='start')['confirmation']
        queued = {'operation_id': 'SO-' + 'b' * 32, 'operation': 'backup', 'status': 'queued'}
        with (
            patch.object(self.bridge, 'management_runtime', return_value=self.root),
            patch('server_management.queue_operation', side_effect=[queued, RuntimeError('busy')]),
        ):
            job = self.call('manage_apply', confirmation=first)
            with self.assertRaises(BotError):
                self.call('manage_apply', confirmation=second)
        self.bridge.db.close()
        self.bridge = Bridge(self.root / 'bot-config.json', FakeMAS)
        with (
            patch.object(self.bridge, 'management_runtime', return_value=self.root),
            patch(
                'server_management.operation_status', return_value={**queued, 'status': 'running'}
            ),
            patch('server_management.queue_operation') as enqueue,
        ):
            self.assertEqual(self.call('manage_job', job=job['job'])['state'], 'running')
            enqueue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
