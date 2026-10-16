"""Saved defaults preserve explicit choices, credentials and recovery identity."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cli_commands
import command_wrapper
import configuration
import federation_admin_api
import misbot
import operation_inputs
import state_store
import storage_summary


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'stack'
        self.root.mkdir(mode=0o700)
        (self.root / 'settings').mkdir(mode=0o700)
        (self.root / 'config').mkdir(mode=0o700)
        state_store.write_json(self.root / 'installation.json', {'domain': 'example.org'})
        self.key = self.base / 'original.key'
        self.key.write_bytes(b'k' * 32)
        self.key.chmod(0o600)
        self.profile = self.root / 'settings/backup.env'
        self.values = {
            'S3_BUCKET': 'fictional-backups',
            'S3_REGION': 'us-east-1',
            'S3_ENDPOINT_URL': 'https://s3.example.org',
            'S3_ACCESS_KEY_ID': 'FICTIONAL_ACCESS',
            'S3_SECRET_ACCESS_KEY': 'FICTIONAL_SECRET',
            'S3_PREFIX': 'backups/',
            'BACKUP_KEY_FILE': str(self.key),
            'BACKUP_CALENDAR': '*-*-* 03:00:00 UTC',
        }
        state_store.write_text(self.profile, configuration.serialize(self.values))

    def test_saved_pair_and_defaults_preserve_the_original_key(self):
        self.assertEqual(operation_inputs.saved_backup(self.root), (self.profile, self.key))
        command = cli_commands.BY_PATH['backup external create']
        values = operation_inputs.defaults(self.root, command, {'config': None, 'key': None})
        self.assertEqual(values, {'config': str(self.profile), 'key': str(self.key)})
        self.key.unlink()
        with self.assertRaises(RuntimeError):
            operation_inputs.saved_backup(self.root)
        self.assertFalse(self.key.exists())

    def test_explicit_other_profile_never_gets_unrelated_saved_key(self):
        command = cli_commands.BY_PATH['backup external create']
        values = operation_inputs.defaults(
            self.root, command, {'config': '/root/another.env', 'key': None}
        )
        self.assertEqual(values, {'config': '/root/another.env', 'key': None})

    def test_symlink_key_refused_without_modification(self):
        link = self.base / 'key-link'
        link.symlink_to(self.key)
        self.values['BACKUP_KEY_FILE'] = str(link)
        state_store.write_text(self.profile, configuration.serialize(self.values))
        with self.assertRaises((RuntimeError, ValueError)):
            operation_inputs.saved_backup(self.root)
        self.assertEqual(self.key.read_bytes(), b'k' * 32)

    def test_explicit_values_are_not_overwritten(self):
        command = cli_commands.BY_PATH['backup schedule external']
        values = {'config': str(self.profile), 'key': '/root/explicit.key', 'calendar': 'weekly'}
        self.assertEqual(operation_inputs.defaults(self.root, command, values), values)

    def test_missing_saved_key_does_not_block_explicit_original_key_elsewhere(self):
        other = self.base / 'restored-original.key'
        self.key.rename(other)
        command = cli_commands.BY_PATH['backup external create']
        result = operation_inputs.defaults(self.root, command, {'config': None, 'key': str(other)})
        self.assertEqual(result, {'config': str(self.profile), 'key': str(other)})
        self.assertFalse(self.key.exists())

    def test_missing_key_defers_to_form_without_generating_any_file(self):
        self.key.unlink()
        command = cli_commands.BY_PATH['backup external create']
        self.assertEqual(
            operation_inputs.defaults(self.root, command, {'config': None, 'key': None}),
            {'config': None, 'key': None},
        )
        self.assertFalse(self.key.exists())

    def test_accounts_use_selected_compose_without_lifecycle_globals(self):
        import lifecycle

        (self.root / 'secrets').mkdir(mode=0o700)
        state_store.write_json(
            self.root / 'secrets/referral-client.json',
            {'client_id': 'fictional', 'client_secret': 'FICTIONAL_SECRET'},
        )
        (self.root / 'federation_admin_api.py').write_text('# test API')
        before = {key: getattr(lifecycle, key, None) for key in ('ROOT', 'COMPOSE')}
        with patch.object(
            operation_inputs.subprocess,
            'run',
            return_value=SimpleNamespace(
                returncode=0,
                stdout=json.dumps({'accounts': [{'username': 'alice', 'admin': False}]}),
            ),
        ) as run:
            result = operation_inputs.local_accounts(self.root)
        self.assertEqual(result[0]['mxid'], '@alice:example.org')
        args = run.call_args.args[0]
        self.assertIn(str(self.root / 'compose.yaml'), args)
        self.assertNotIn('FICTIONAL_SECRET', ' '.join(args))
        self.assertEqual(run.call_args.kwargs['timeout'], 45)
        self.assertEqual(before, {key: getattr(lifecycle, key, None) for key in before})

    def test_latest_failed_external_copy_is_not_reported_verified(self):
        for name, passed in (('20260101-old', True), ('20260102-new', False)):
            folder = self.root / 'stages/external-backups' / name
            folder.mkdir(parents=True, mode=0o700)
            state_store.write_json(folder / 'catalog.json', {'upload_verified': passed})
        self.assertFalse(storage_summary.summary(self.root)['last_backup_verified'])

    def test_calendar_from_foreign_installation_is_refused(self):
        state_store.write_json(
            self.root / 'config/backup-schedule.json', {'root': '/other', 'calendar': 'daily'}
        )
        with self.assertRaises(ValueError):
            operation_inputs.saved_calendar(self.root)

    def test_s3_purpose_not_guessed_and_native_profile_is_used(self):
        command = cli_commands.BY_PATH['storage s3 check']
        path = self.root / 'settings/media-s3.env'
        state_store.write_text(path, 'S3_BUCKET=fictional-media\n')
        self.assertIsNone(
            operation_inputs.defaults(self.root, command, {'config': None, 'purpose': None})[
                'config'
            ]
        )
        self.assertEqual(
            operation_inputs.defaults(self.root, command, {'config': None, 'purpose': 'media'})[
                'config'
            ],
            str(path),
        )

    def test_auto_argument_parser_and_admission_reject_foreign_backend(self):
        command, options, values = misbot.parse(
            ['backup', 'external', 'create', '--directory', str(self.root)]
        )
        self.assertIsNone(values['key'])
        with patch.object(
            command_wrapper, 'trusted_installation', side_effect=RuntimeError('foreign')
        ):
            with self.assertRaises(RuntimeError):
                misbot.plan(command, options, values)

    def test_missing_input_routes_to_admitted_helper(self):
        command, options, values = misbot.parse(
            ['backup', 'external', 'create', '--directory', str(self.root)]
        )
        with (
            patch.object(command_wrapper, 'trusted_installation'),
            patch.object(
                command_wrapper, 'regular', return_value=SimpleNamespace(st_uid=0, st_mode=0o100644)
            ),
        ):
            args = misbot.plan(command, options, values)
        self.assertEqual(args[1], str(self.root / 'operation_inputs.py'))
        self.assertIn('backup external create', args)
        self.assertNotIn('FICTIONAL_SECRET', ' '.join(args))

    def test_non_tty_missing_data_has_safe_actionable_error(self):
        with (
            patch('os.geteuid', return_value=0),
            patch.object(command_wrapper, 'trusted_installation'),
            patch.object(operation_inputs, 'defaults', side_effect=lambda r, c, v: v),
            patch.object(sys.stdin, 'isatty', return_value=False),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(
                operation_inputs.main(
                    ['--directory', str(self.root), '--command', 'credentials vault connect']
                ),
                2,
            )
        self.assertIn('--config', output.getvalue())
        self.assertNotIn('FICTIONAL_SECRET', output.getvalue())

    def test_native_storage_summary_is_specific_and_secret_free(self):
        state_store.write_json(
            self.root / 'installation.json',
            {
                'domain': 'example.org',
                'media_mode': 's3',
                'database_mode': 'external',
                'media_transition_pending': True,
            },
        )
        result = storage_summary.summary(self.root)
        self.assertTrue(result['media_s3'])
        self.assertTrue(result['media_pending'])
        self.assertEqual(result['database_mode'], 'external')
        self.assertEqual(result['manual_backup_destination'], 'local')
        self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))


class AccountListTests(unittest.TestCase):
    def test_bounded_pages_return_only_active_public_identities(self):
        calls = []

        def request(path):
            calls.append(path)
            if len(calls) == 1:
                return 200, {
                    'data': [
                        {
                            'attributes': {
                                'username': 'alice',
                                'admin': False,
                                'password': 'FICTIONAL_SECRET',
                            }
                        },
                        {'attributes': {'username': 'disabled', 'locked_at': 'today'}},
                    ],
                    'links': {
                        'next': 'https://auth.example.org/api/admin/v1/users?page[after]=cursor'
                    },
                }
            return 200, {'data': [{'attributes': {'username': 'owner', 'admin': True}}]}

        result = federation_admin_api.accounts(request)
        self.assertEqual(
            result['accounts'],
            [{'username': 'alice', 'admin': False}, {'username': 'owner', 'admin': True}],
        )
        self.assertTrue(all(p.startswith('/api/admin/v1/users?') for p in calls))
        self.assertNotIn('FICTIONAL_SECRET', json.dumps(result))

    def test_unexpected_pagination_endpoint_is_not_requested(self):
        calls = []

        def request(path):
            calls.append(path)
            return 200, {'data': [], 'links': {'next': 'https://foreign.example/secrets'}}

        with self.assertRaises(RuntimeError):
            federation_admin_api.accounts(request)
        self.assertEqual(len(calls), 1)

    def test_repeated_page_does_not_loop_or_return_partial_list(self):
        def request(path):
            return 200, {'data': [], 'links': {'next': '/api/admin/v1/users?page[first]=100'}}

        with self.assertRaises(RuntimeError):
            federation_admin_api.accounts(request)


if __name__ == '__main__':
    unittest.main()
