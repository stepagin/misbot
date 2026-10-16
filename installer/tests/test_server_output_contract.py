import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server_management as management
import state_store
import storage_summary


class SafeServerOutputTests(unittest.TestCase):
    def response(self, *records, code=0):
        return subprocess.CompletedProcess(
            [], code, '\n'.join(json.dumps(x) for x in records).encode()
        )

    def test_missing_storage_information_is_unknown_and_untrusted_messages_are_dropped(self):
        result = management.sanitized(
            'storage',
            self.response(
                {
                    'check': 'storage_status',
                    'passed': False,
                    'media_s3': False,
                    'message': 'SDK SECRET=password',
                    'next_action': 'curl SECRET',
                },
                code=1,
            ),
        )
        self.assertFalse(result['ok'])
        self.assertIs(result['storage']['media_s3'], False)
        self.assertIsNone(result['storage']['backup_s3'])
        self.assertIsNone(result['storage']['restore_verified'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_portable_backup_preserves_scope_and_only_artifact_basename(self):
        result = management.sanitized(
            'backup',
            self.response(
                {
                    'check': 'consistent_backup',
                    'archive': '/private/matrix-20261014.tar.gz',
                    'authentication': 'external-MAS-not-included',
                    'next_action': 'SECRET backend text',
                    'credentials': 'SECRET',
                }
            ),
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['backup']['destination'], 'local')
        self.assertEqual(result['backup']['artifact'], 'matrix-20261014.tar.gz')
        self.assertEqual(result['backup']['authentication'], 'external-MAS-not-included')
        self.assertNotIn('/private', json.dumps(result))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('restore_verified', result['backup'])

    def test_adopted_remote_backup_has_stronger_verified_contract(self):
        result = management.sanitized(
            'backup',
            self.response(
                {
                    'check': 'existing_backup',
                    'passed': True,
                    'archive': 's3',
                    'verified': True,
                    'encrypted_bytes': 4096,
                }
            ),
        )
        self.assertTrue(result['backup']['restore_verified'])
        self.assertTrue(result['backup']['upload_verified'])
        self.assertEqual(result['backup']['destination'], 'external')

    def test_failed_final_record_never_means_success_with_exit_zero(self):
        result = management.sanitized(
            'backup',
            self.response(
                {'check': 'existing_backup', 'phase': 'dumping'},
                {'check': 'existing_backup', 'passed': False, 'message': 'SECRET'},
            ),
        )
        self.assertFalse(result['ok'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_restore_scope_lists_only_confirmed_databases(self):
        result = management.sanitized(
            'backup_verify',
            self.response(
                {'check': 'temporary_database_restore', 'database': 'synapse', 'passed': True},
                {'check': 'temporary_database_restore', 'database': 'mas', 'passed': False},
            ),
        )
        self.assertFalse(result['ok'])
        self.assertEqual(result['backup']['verified_databases'], ['synapse'])
        self.assertNotIn('restore_verified', result['backup'])

    def test_schedule_safe_metadata_not_raw_backend_payload(self):
        result = management.sanitized(
            'backup_schedule',
            self.response(
                {
                    'check': 'backup_schedule_status',
                    'configured': True,
                    'enabled': False,
                    'message': 'SECRET',
                    'last_verified': {
                        'completed_utc': '2026-10-14T00:00:00Z',
                        'two_database_restores': True,
                    },
                },
                {
                    'check': 'backup_schedule_status',
                    'resource': 'timer',
                    'NextElapseUSecRealtime': 'Wed 2026-10-14 03:00:00 UTC',
                },
            ),
        )
        self.assertEqual(result['schedule']['next_run'], 'Wed 2026-10-14 03:00:00 UTC')
        self.assertIs(result['schedule']['enabled'], False)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_backup_preview_distinguishes_backends_without_exposing_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(
                root / 'installation.json', {'backend': 'portable', 'mas_mode': 'external'}
            )
            self.assertEqual(
                management.backup_preview(root),
                {'destination': 'local', 'authentication': 'external-MAS-not-included'},
            )
            state_store.write_json(root / 'installation.json', {'backend': 'existing-compose'})
            (root / '.misbot/s3').mkdir(parents=True)
            state_store.write_json(root / '.misbot/s3/backup-active.json', {'SECRET': 'SECRET'})
            self.assertEqual(management.backup_preview(root)['destination'], 'external')

    def test_native_summary_does_not_invent_restore_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'media_mode': 'local'})
            (root / 'stages/external-backups/20261014T000000Z-a').mkdir(parents=True)
            state_store.write_json(
                root / 'stages/external-backups/20261014T000000Z-a/catalog.json',
                {'upload_verified': True},
            )
            with patch('storage_summary.state_store.operation_guard'):
                result = storage_summary.summary(root)
            self.assertTrue(result['upload_verified'])
            self.assertIsNone(result['restore_verified'])
            self.assertIsNone(result['last_restore_verified_at'])

    def test_empty_or_unrelated_output_cannot_confirm_backup(self):
        for operation in ('backup', 'backup_verify'):
            for response in (
                self.response(),
                self.response({'check': 'unrelated', 'passed': True}),
            ):
                with self.subTest(operation=operation):
                    self.assertFalse(management.sanitized(operation, response)['ok'])

    def test_partial_and_explicit_failure_are_not_success(self):
        for kind, operation, fields in (
            ('server_components', 'components', {'enabled': True}),
            ('consistent_backup', 'backup', {'archive': '/private/matrix-20261014.tar.gz'}),
            ('storage_status', 'storage', {'media_s3': True}),
        ):
            for failure in (
                {'passed': False},
                {'success': False},
                {'partial': True},
                {'status': 'partial'},
                {'state': 'failed'},
            ):
                with self.subTest(kind=kind, failure=failure):
                    result = management.sanitized(
                        operation,
                        self.response({'check': kind, **fields, **failure, 'message': 'SECRET'}),
                    )
                    self.assertFalse(result['ok'])
                    self.assertNotIn('SECRET', json.dumps(result))

    def test_schedule_default_and_current_running_result_are_not_last_success(self):
        for execution, active in ((False, 'inactive'), (True, 'active'), (True, 'activating')):
            result = management.sanitized(
                'backup_schedule',
                self.response(
                    {
                        'check': 'backup_schedule_status',
                        'resource': 'service',
                        'Result': 'success',
                        'execution_metadata_available': execution,
                        'ActiveState': active,
                    }
                ),
            )
            self.assertNotIn('last_run_status', result['schedule'])
        result = management.sanitized(
            'backup_schedule',
            self.response(
                {
                    'check': 'backup_schedule_status',
                    'resource': 'service',
                    'Result': 'success',
                    'execution_metadata_available': True,
                    'ActiveState': 'inactive',
                }
            ),
        )
        self.assertEqual(result['schedule']['last_run_status'], 'success')

    def test_invalid_calendar_timestamp_is_not_a_verification_date(self):
        self.assertFalse(management.utc_timestamp('2026-99-99T99:99:99Z'))
        result = management.sanitized(
            'storage',
            self.response(
                {'check': 'storage_status', 'last_restore_verified_at': '2026-99-99T99:99:99Z'}
            ),
        )
        self.assertNotIn('last_restore_verified_at', result['storage'])

    def test_native_latest_failure_does_not_reuse_older_upload_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'media_mode': 'local'})
            for stamp, verified in (('20261013T000000Z-a', True), ('20261014T000000Z-a', False)):
                target = root / 'stages/external-backups' / stamp
                target.mkdir(parents=True)
                state_store.write_json(target / 'catalog.json', {'upload_verified': verified})
            with patch('storage_summary.state_store.operation_guard'):
                self.assertIs(storage_summary.summary(root)['upload_verified'], False)
            target = root / 'stages/external-backups/20261014T000000Z-b'
            target.mkdir()
            state_store.write_json(target / 'catalog.json', {'upload_verified': True})
            with patch('storage_summary.state_store.operation_guard'):
                self.assertIsNone(storage_summary.summary(root)['upload_verified'])
