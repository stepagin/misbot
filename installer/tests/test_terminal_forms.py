"""Private UI forms preserve destinations, trusted digests and recovery material."""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import state_store
import terminal_forms as forms
import ui_actions

PROFILE = {
    'S3_BUCKET': 'fictional-private-bucket',
    'S3_ENDPOINT_URL': 'https://s3.example.org',
    'S3_REGION': 'us-east-1',
    'S3_PREFIX': 'backups/',
    'S3_ACCESS_KEY_ID': 'FICTIONAL_ACCESS',
    'S3_SECRET_ACCESS_KEY': 'FICTIONAL_SECRET',
}


class FormsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'installation'
        self.root.mkdir(mode=0o700)
        self.drafts = self.base / 'drafts'
        self.drafts.mkdir(mode=0o700)
        self.ui = SimpleNamespace(
            root=self.root,
            draft=self.drafts / 'draft.json',
            model={},
            backup={},
            view=Mock(),
            supplied_arguments={},
            operation_records=[],
            installation=Mock(return_value={'domain': 'example.org', 'backend': 'portable'}),
            adopted=Mock(return_value=False),
            s3_profile=Mock(return_value=dict(PROFILE)),
            input_value=Mock(),
            backup_key=lambda value: value,
            confirm=Mock(return_value=True),
            choose=Mock(return_value='1'),
            schedule_calendar=Mock(return_value='daily'),
            run=Mock(return_value=0),
            show_operation_result=Mock(),
            operation_argument=lambda key, value, backend: value,
        )
        self.key = self.base / 'recovery.key'
        self.key.write_bytes(b'k' * 32)
        self.key.chmod(0o600)
        self.profile = self.base / 'profile.env'
        state_store.write_text(self.profile, configuration.serialize(PROFILE))

    def item(self, path):
        return ui_actions.Action(path, 'Операция')

    def test_private_profile_is_removed_after_backend_failure(self):
        with self.assertRaises(RuntimeError):
            with forms.profile_file(self.ui, PROFILE) as path:
                saved = path
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                self.assertIn('FICTIONAL_SECRET', path.read_text())
                raise RuntimeError('fictional interruption')
        self.assertFalse(saved.exists())

    def test_saved_backup_needs_no_repeated_profile_or_key_input(self):
        with patch('operation_inputs.saved_backup', return_value=(self.profile, self.key)):
            code = forms.execute(self.ui, self.item('backup external create'))
        self.assertEqual(code, 0)
        self.ui.input_value.assert_not_called()
        self.ui.s3_profile.assert_not_called()
        self.ui.run.assert_called_once_with(
            'backup external create', config=self.profile, key=self.key
        )
        self.assertNotIn('FICTIONAL_SECRET', repr(self.ui.view.mock_calls))

    def test_explicit_backup_inputs_win_over_saved_defaults(self):
        other = self.base / 'other.env'
        state_store.write_text(
            other, configuration.serialize({**PROFILE, 'S3_BUCKET': 'other-bucket'})
        )
        self.ui.supplied_arguments = {
            'config': str(other),
            'key': str(self.key),
            'calendar': 'weekly',
        }
        with patch(
            'operation_inputs.saved_backup', return_value=(self.profile, self.base / 'other.key')
        ):
            forms.execute(self.ui, self.item('backup schedule external'))
        self.ui.schedule_calendar.assert_not_called()
        self.ui.run.assert_called_once_with(
            'backup schedule external', config=other, key=self.key, calendar='weekly'
        )

    def test_first_backup_cancel_creates_no_key_profile_or_backend_call(self):
        self.ui.confirm.return_value = False
        self.ui.input_value.return_value = str(self.base / 'new.key')
        with patch('operation_inputs.saved_backup', return_value=None):
            forms.execute(self.ui, self.item('backup external create'))
        self.ui.run.assert_not_called()
        self.assertFalse((self.base / 'new.key').exists())
        self.assertFalse((self.root / 'settings').exists())

    def test_first_backup_persists_profile_before_starting_timer(self):
        new_key = self.base / 'new.key'
        self.ui.input_value.return_value = str(new_key)
        events = []
        self.ui.run.side_effect = lambda path, **args: events.append(path) or 0
        with (
            patch('operation_inputs.saved_backup', return_value=None),
            patch('operation_inputs.saved_calendar', return_value=''),
            patch.object(
                forms,
                'save_backup',
                side_effect=lambda *args: events.append('persist') or self.profile,
            ),
        ):
            forms.execute(self.ui, self.item('backup schedule external'))
        self.assertEqual(events, ['backup key create', 'persist', 'backup schedule external'])

    def test_fetch_never_creates_replacement_key(self):
        self.ui.input_value.return_value = str(self.base / 'lost.key')
        with (
            patch('operation_inputs.saved_backup', return_value=None),
            self.assertRaises(ValueError),
        ):
            forms.backup_values(self.ui, allow_create=False)
        self.ui.run.assert_not_called()

    def test_s3_rotation_keeps_destination_and_hides_both_new_credentials(self):
        self.ui.input_value.side_effect = ['NEW_FICTIONAL_ACCESS', 'NEW_FICTIONAL_SECRET']
        captured = {}

        def run(path, **args):
            captured.update(forms.s3_config.read_profile(args['config']))
            return 0

        self.ui.run.side_effect = run
        with patch('configuration.load', return_value=dict(PROFILE)):
            forms.execute(self.ui, self.item('credentials s3 rotate'))
        for key in PROFILE.keys() - {'S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY'}:
            self.assertEqual(captured[key], PROFILE[key])
        self.assertEqual(captured['S3_SECRET_ACCESS_KEY'], 'NEW_FICTIONAL_SECRET')
        self.assertTrue(all(call.kwargs['secret'] for call in self.ui.input_value.call_args_list))
        self.assertNotIn('NEW_FICTIONAL_SECRET', repr(self.ui.view.mock_calls))
        self.assertFalse(list(self.drafts.glob('.operation-*')))

    def test_postgres_form_uses_strict_tls_and_only_backend_fields(self):
        self.ui.input_value.side_effect = [
            'db.example.org',
            '5432',
            'postgres',
            'FICTIONAL_PASSWORD',
            '',
        ]
        captured = {}

        def run(path, **args):
            import database

            captured.update(database.settings(args['config']))
            return 0

        self.ui.run.side_effect = run
        forms.execute(self.ui, self.item('storage database connect'))
        self.assertEqual(captured['POSTGRES_SSL_MODE'], 'verify-full')
        self.assertEqual(captured['POSTGRES_ADMIN_PASSWORD'], 'FICTIONAL_PASSWORD')
        self.assertNotIn('FICTIONAL_PASSWORD', repr(self.ui.view.mock_calls))
        self.assertFalse(list(self.drafts.glob('.operation-*')))

    def vault_inputs(self):
        self.ui.input_value.side_effect = [
            'https://vault.example.org',
            'matrix',
            'misbot/example',
            '',
            'FICTIONAL_ROLE',
            'FICTIONAL_SECRET_ID',
        ]
        (self.root / 'config').mkdir(mode=0o700)

    def test_vault_retains_auth_files_if_saved_connection_exists_after_failed_sync(self):
        self.vault_inputs()

        def run(path, **args):
            state_store.write_text(
                self.root / 'config/vault-connection.env', Path(args['config']).read_text()
            )
            return 1

        self.ui.run.side_effect = run
        code = forms.execute(self.ui, self.item('credentials vault connect'))
        import vault_cache

        saved = vault_cache.connection(self.root / 'config/vault-connection.env')
        self.assertEqual(code, 1)
        for name in ('VAULT_ROLE_ID_FILE', 'VAULT_SECRET_ID_FILE'):
            path = Path(saved[name])
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.drafts.glob('.operation-*')))
        self.assertNotIn('FICTIONAL_SECRET_ID', repr(self.ui.view.mock_calls))

    def test_vault_removes_unused_auth_when_connect_fails_before_saving(self):
        self.vault_inputs()
        self.ui.run.return_value = 1
        forms.execute(self.ui, self.item('credentials vault connect'))
        self.assertEqual(list((self.root / 'config/vault-auth').iterdir()), [])

    def test_vault_existing_connect_reuses_exact_auth_paths(self):
        self.vault_inputs()
        state = {
            'VAULT_ADDR': 'https://vault.example.org',
            'VAULT_KV_MOUNT': 'matrix',
            'VAULT_SECRET_PATH': 'existing',
            'VAULT_ROLE_ID_FILE': str(self.base / 'role'),
            'VAULT_SECRET_ID_FILE': str(self.base / 'secret'),
        }
        saved = self.root / 'config/vault-connection.env'
        state_store.write_text(saved, configuration.serialize(state))
        forms.execute(self.ui, self.item('credentials vault connect'))
        self.ui.input_value.assert_not_called()
        self.ui.run.assert_called_once_with('credentials vault connect', config=saved)

    def test_explicit_profile_bypasses_form_without_rewriting_file(self):
        self.ui.supplied_arguments = {'config': str(self.profile)}
        before = self.profile.read_bytes()
        forms.execute(self.ui, self.item('credentials s3 rotate'))
        self.ui.input_value.assert_not_called()
        self.ui.run.assert_called_once_with('credentials s3 rotate', config=self.profile)
        self.assertEqual(self.profile.read_bytes(), before)

    def test_untrusted_archive_never_provides_its_own_checksum(self):
        archive = self.base / 'untrusted.tar.gz'
        archive.write_bytes(b'UNTRUSTED')
        self.ui.supplied_arguments = {'archive': str(archive), 'public-ip': '203.0.113.20'}
        self.ui.input_value.return_value = 'a' * 64
        args = forms.collect_restore(self.ui, 'restore')
        self.assertEqual(args['sha256'], 'a' * 64)
        self.assertEqual(self.ui.input_value.call_count, 1)
        self.assertIn('исходного сервера', self.ui.input_value.call_args.args[0])

    def test_receipt_from_trusted_backend_supplies_digest_without_reading_archive(self):
        archive = self.base / 'local.tar.gz'
        archive.write_bytes(b'fictional archive')
        self.ui.operation_records = [
            {'check': 'consistent_backup', 'archive': str(archive), 'sha256': 'b' * 64}
        ]
        forms.remember_backup_results(self.ui, 'backup create')
        self.ui.supplied_arguments = {'archive': str(archive), 'public-ip': '203.0.113.20'}
        args = forms.collect_restore(self.ui, 'restore')
        self.assertEqual(args['sha256'], 'b' * 64)
        self.ui.input_value.assert_not_called()

    def test_foreign_or_symlink_receipts_are_ignored(self):
        folder = self.drafts / 'backup-receipts'
        folder.mkdir(mode=0o700)
        state_store.write_json(
            folder / 'foreign.json',
            {
                'format': 1,
                'directory': '/other',
                'archive': '/other/archive.tar.gz',
                'sha256': 'c' * 64,
            },
        )
        target = self.base / 'unowned.json'
        state_store.write_json(
            target,
            {
                'format': 1,
                'directory': str(self.root),
                'archive': '/other/archive.tar.gz',
                'sha256': 'd' * 64,
            },
        )
        (folder / 'link.json').symlink_to(target)
        self.assertEqual(forms.receipts(self.ui), [])

    def test_restore_preserves_all_explicit_inputs(self):
        self.ui.supplied_arguments = {
            'archive': '/explicit/backup.tar.gz',
            'sha256': 'e' * 64,
            'public-ip': '203.0.113.20',
        }
        args = forms.collect_restore(self.ui, 'restore')
        self.assertEqual(args, self.ui.supplied_arguments)
        self.ui.input_value.assert_not_called()
        self.ui.choose.assert_not_called()

    def test_fetch_preserves_explicit_output_object_digest(self):
        self.ui.supplied_arguments = {
            'config': str(self.profile),
            'key': str(self.key),
            'object': 'backups/old.enc',
            'sha256': 'a' * 64,
            'output': str(self.base / 'chosen.tar.gz'),
        }
        with patch('operation_inputs.saved_backup', return_value=None):
            forms.execute(self.ui, self.item('backup fetch'))
        self.ui.input_value.assert_not_called()
        self.ui.run.assert_called_once_with(
            'backup fetch',
            config=self.profile,
            key=self.key,
            object='backups/old.enc',
            sha256='a' * 64,
            output=self.base / 'chosen.tar.gz',
        )

    def test_feature_resume_uses_exact_saved_plan_without_retyping(self):
        folder = self.root / '.misbot/features'
        folder.mkdir(mode=0o700, parents=True)
        plan = {
            'format': 1,
            'primary_domain': 'site.example.org',
            'admin_domain': 'admin.example.org',
            'telegram_runtime': {'directory': '/usr/local/lib/fictional'},
        }
        state_store.write_json(folder / 'enable.json', {'format': 1, 'plan': plan})
        captured = {}
        self.ui.run.side_effect = (
            lambda path, **args: captured.update(json.loads(Path(args['config']).read_text())) or 0
        )
        forms.execute(self.ui, self.item('features enable'))
        self.ui.input_value.assert_not_called()
        self.assertEqual(captured, plan)

    def test_missing_saved_key_creates_new_path_only_after_explicit_choice(self):
        folder = self.root / 'settings'
        folder.mkdir(mode=0o700)
        lost = self.base / 'lost.key'
        state_store.write_text(
            folder / 'backup.env',
            configuration.serialize({**PROFILE, 'BACKUP_KEY_FILE': str(lost)}),
        )
        self.ui.choose.return_value = '2'
        values, profile, key, fresh = forms.backup_values(self.ui)
        self.assertEqual(values, PROFILE)
        self.assertTrue(fresh)
        self.assertIsNone(profile)
        self.assertNotEqual(key, lost)
        self.assertFalse(key.exists())
        self.ui.run.assert_not_called()
        self.assertIn('не расшифрует старые', repr(self.ui.view.mock_calls))

    def test_explicit_different_profile_does_not_reuse_saved_key(self):
        self.ui.supplied_arguments = {'config': str(self.profile)}
        self.ui.input_value.return_value = str(self.key)
        with patch('operation_inputs.saved_backup') as discovery:
            _, _, key, _ = forms.backup_values(self.ui)
        discovery.assert_not_called()
        self.assertEqual(self.ui.input_value.call_count, 1)
        self.assertEqual(key, self.key)

    def test_fetch_explicit_original_key_works_when_saved_key_path_is_missing(self):
        state_store.write_json(self.root / 'installation.json', {'domain': 'example.org'})
        folder = self.root / 'settings'
        folder.mkdir(mode=0o700)
        state_store.write_text(
            folder / 'backup.env',
            configuration.serialize({**PROFILE, 'BACKUP_KEY_FILE': str(self.base / 'lost.key')}),
        )
        self.ui.supplied_arguments = {'key': str(self.key)}
        values, profile, key, fresh = forms.backup_values(self.ui, allow_create=False)
        self.assertEqual(values, PROFILE)
        self.assertEqual(profile, folder / 'backup.env')
        self.assertEqual(key, self.key)
        self.assertFalse(fresh)
        self.ui.input_value.assert_not_called()

    def test_unverified_external_catalog_is_not_a_trusted_receipt(self):
        folder = self.root / 'stages/external-backups/fictional'
        folder.mkdir(mode=0o700, parents=True)
        state_store.write_json(
            folder / 'catalog.json',
            {
                'format': 1,
                'upload_verified': False,
                'source_archive': '/tmp/unverified.tar.gz',
                'archive_sha256': 'a' * 64,
                'object_key': 'backups/unverified.enc',
                'ciphertext_sha256': 'b' * 64,
            },
        )
        self.assertEqual(forms.receipts(self.ui), [])

    def test_vault_auth_keeps_destination_and_asks_only_replacement_credentials(self):
        self.vault_inputs()
        state = {
            'VAULT_ADDR': 'https://vault.example.org',
            'VAULT_KV_MOUNT': 'matrix',
            'VAULT_SECRET_PATH': 'existing',
            'VAULT_ROLE_ID_FILE': str(self.base / 'old-role'),
            'VAULT_SECRET_ID_FILE': str(self.base / 'old-secret'),
        }
        saved = self.root / 'config/vault-connection.env'
        state_store.write_text(saved, configuration.serialize(state))
        self.ui.input_value.side_effect = ['', 'NEW_FICTIONAL_ROLE', 'NEW_FICTIONAL_SECRET']
        captured = {}

        def run(path, **args):
            import vault_cache

            captured.update(vault_cache.connection(args['config']))
            return 1

        self.ui.run.side_effect = run
        forms.execute(self.ui, self.item('credentials vault auth'))
        for key in ('VAULT_ADDR', 'VAULT_KV_MOUNT', 'VAULT_SECRET_PATH'):
            self.assertEqual(captured[key], state[key])
        self.assertEqual(self.ui.input_value.call_count, 3)
        self.assertFalse(Path(captured['VAULT_ROLE_ID_FILE']).exists())
        self.assertEqual(saved.read_text(), configuration.serialize(state))

    def test_public_ip_suggestion_requires_unique_global_host_address(self):
        result = SimpleNamespace(
            stdout=json.dumps([{'addr_info': [{'local': '10.0.0.2'}, {'local': '8.8.8.8'}]}])
        )
        with patch('terminal_forms.subprocess.run', return_value=result):
            self.assertEqual(forms.public_ip_suggestion(), '8.8.8.8')
        result.stdout = json.dumps([{'addr_info': [{'local': '8.8.8.8'}, {'local': '1.1.1.1'}]}])
        with patch('terminal_forms.subprocess.run', return_value=result):
            self.assertEqual(forms.public_ip_suggestion(), '')


if __name__ == '__main__':
    unittest.main()
