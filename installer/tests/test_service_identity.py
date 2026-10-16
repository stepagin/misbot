"""Portable regeneration preserves imported credentials and service identities."""

import hashlib
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE))
import configuration
import credentials
import lifecycle
import mas_admin
import service_identity


IMPORTED_ADMIN = {
    'username': 'saved-owner',
    'matrix_id': '@saved-owner:preserved.example.org',
    'mas_user_id': '01K5WZ00000000000000000009',
    'password_managed': False,
    'admin_url': 'https://admin.preserved.example.org',
    'web_url': 'https://web.preserved.example.org',
}


class ServiceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='misbot-identity-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'stack'
        result = subprocess.run(
            [
                sys.executable,
                str(BUNDLE / 'generate.py'),
                '--directory',
                str(self.root),
                '--domain',
                'preserved.example.org',
                '--initialize-env',
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.root / 'config/mas-defaults.yaml').write_text('{}\n')
        configuration.patch(self.root, {'PUBLIC_IP': '8.8.8.8'})

    def generate(self, *, ok=True):
        worker = """
import os, runpy, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import state_store
state_store.require_root = lambda *args: None
os.chown = lambda *args: None
bundle, root = sys.argv[1:]
sys.argv = ['generate.py', '--directory', root]
runpy.run_path(str(Path(bundle) / 'generate.py'), run_name='__main__')
"""
        result = subprocess.run(
            [sys.executable, '-c', worker, str(BUNDLE), str(self.root)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.root.rglob('*')
            if path.is_file()
        }

    def seed_import(self):
        path = self.root / 'installation.json'
        metadata = json.loads(path.read_text())
        metadata.pop('service_identity_sha256')
        metadata.pop('administrator_identity')
        metadata['service_identity'] = dict(
            service_identity.DEFAULTS,
            livekit_key='preserved-key',
            mas_signing_key={'kid': 'previous-signing', 'algorithm': 'RS256'},
            synapse_form_secret_enabled=False,
            synapse_registration_shared_secret_enabled=True,
        )
        path.write_text(json.dumps(metadata))
        # Fictional opaque credentials include a quote, backslash and Unicode;
        # they must remain byte-for-byte stable and form safe SQL literals.
        for name in (
            'db_synapse',
            'db_mas',
            'db_admin',
            'mas_shared',
            'rtc_as_token',
            'rtc_hs_token',
            'livekit_secret',
            'synapse_macaroon',
            'synapse_registration_shared_secret',
        ):
            value = "fictional-'\\-пароль-" + name
            if name not in ('livekit_secret', 'rtc_as_token', 'rtc_hs_token'):
                value = ' ' + value + ' '
            (self.root / 'secrets' / name).write_text(value + '\n')
        (self.root / 'secrets/synapse_form').unlink()
        client_path = self.root / 'secrets/referral-client.json'
        client = json.loads(client_path.read_text())
        client['client_id'] = '01K5WZ00000000000000000005'
        client_path.write_text(json.dumps(client))
        (self.root / 'secrets/admin-access.json').write_text(json.dumps(IMPORTED_ADMIN))

    def test_new_installation_keeps_default_identity_and_hex_generation(self):
        self.generate()
        metadata = json.loads((self.root / 'installation.json').read_text())
        self.assertEqual(metadata['service_identity'], service_identity.DEFAULTS)
        for name in service_identity.SECRETS:
            self.assertRegex((self.root / 'secrets' / name).read_text(), r'^[a-f0-9]{64}\n$')
        synapse = yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())
        self.assertIsInstance(synapse['form_secret'], str)
        self.assertNotIn('registration_shared_secret', synapse)
        mas = yaml.safe_load((self.root / 'config/mas.yaml').read_text())
        self.assertEqual(mas['secrets']['keys'], [{'key_file': '/config/mas-signing.pem'}])

    def test_imported_identity_and_opaque_secrets_survive_repeated_generation(self):
        self.seed_import()
        before = {
            name: value
            for name, value in self.snapshot().items()
            if name.startswith('secrets/') or name == 'config/mas-signing.pem'
        }
        for _ in range(2):
            self.generate()
            after = self.snapshot()
            self.assertEqual({name: after[name] for name in before}, before)
            synapse = yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())
            self.assertIsNone(synapse['form_secret'])
            self.assertEqual(
                synapse['macaroon_secret_key'], " fictional-'\\-пароль-synapse_macaroon "
            )
            self.assertEqual(
                synapse['registration_shared_secret'],
                " fictional-'\\-пароль-synapse_registration_shared_secret ",
            )
            mas = yaml.safe_load((self.root / 'config/mas.yaml').read_text())
            self.assertEqual(
                mas['secrets']['keys'],
                [
                    {
                        'key_file': '/config/mas-signing.pem',
                        'kid': 'previous-signing',
                        'algorithm': 'RS256',
                    }
                ],
            )
            self.assertEqual(mas['clients'][0]['client_id'], '01K5WZ00000000000000000005')
            livekit = yaml.safe_load((self.root / 'config/livekit.yaml').read_text())
            self.assertEqual(set(livekit['keys']), {'preserved-key'})
            compose = yaml.safe_load((self.root / 'compose.yaml').read_text())
            self.assertEqual(
                compose['services']['jwt']['environment']['LIVEKIT_KEY'], 'preserved-key'
            )
            sql = (self.root / 'config/init-db.sql').read_text()
            self.assertIn('SET standard_conforming_strings=on;', sql)
            self.assertIn(
                "CREATE ROLE synapse LOGIN PASSWORD ' fictional-''\\-пароль-db_synapse ';", sql
            )
            self.assertFalse((self.root / 'secrets/synapse_form').exists())

    def test_invalid_identity_or_secret_fails_before_runtime_changes(self):
        self.seed_import()
        self.generate()
        for mutation in (
            'identity',
            'missing',
            'control',
            'mas_encryption',
            'client',
            'rtc_space',
            'admin_domain',
        ):
            with self.subTest(mutation=mutation):
                if mutation == 'identity':
                    path = self.root / 'installation.json'
                    original = path.read_bytes()
                    value = json.loads(original)
                    value['service_identity']['livekit_key'] = 'replacement'
                    path.write_text(json.dumps(value))
                elif mutation == 'admin_domain':
                    path = self.root / 'secrets/admin-access.json'
                    original = path.read_bytes()
                    value = json.loads(original)
                    value['matrix_id'] = '@saved-owner:another.example.org'
                    path.write_text(json.dumps(value))
                elif mutation == 'client':
                    path = self.root / 'secrets/referral-client.json'
                    original = path.read_bytes()
                    value = json.loads(original)
                    value['client_id'] = 'not-a-ulid'
                    path.write_text(json.dumps(value))
                else:
                    path = (
                        self.root
                        / 'secrets'
                        / ('mas_encryption' if mutation == 'mas_encryption' else 'rtc_as_token')
                    )
                    original = path.read_bytes()
                    if mutation == 'missing':
                        path.unlink()
                    else:
                        path.write_text(
                            ' fictional-token '
                            if mutation == 'rtc_space'
                            else ('FICTIONAL_INVALID\x01' if mutation == 'control' else 'nonhex')
                        )
                before = self.snapshot()
                result = self.generate(ok=False)
                self.assertEqual(self.snapshot(), before)
                self.assertNotIn('FICTIONAL_INVALID', result.stdout + result.stderr)
                path.write_bytes(original)

    def test_native_update_and_restore_reject_bad_identity_before_publication(self):
        import update
        import restore

        self.seed_import()
        self.generate()
        archive = Path(self.temp.name) / 'fixture.tar.gz'
        archive.write_bytes(b'FICTIONAL_ARCHIVE')
        target = Path(self.temp.name) / 'restored'
        snapshot = Path(self.temp.name) / 'snapshot'
        snapshot.mkdir()

        def extract(path, destination, **kwargs):
            if path != archive:
                shutil.copytree(self.root, destination, dirs_exist_ok=True)

        for mutation in ('fingerprint', 'secret', 'rsa', 'admin'):
            with self.subTest(mutation=mutation):
                path = (
                    self.root
                    / {
                        'fingerprint': 'installation.json',
                        'secret': 'secrets/rtc_as_token',
                        'rsa': 'config/mas-signing.pem',
                        'admin': 'secrets/admin-access.json',
                    }[mutation]
                )
                original = path.read_bytes()
                if mutation == 'fingerprint':
                    metadata = json.loads(original)
                    metadata['service_identity_sha256'] = '0' * 64
                    path.write_text(json.dumps(metadata))
                elif mutation == 'rsa':
                    path.write_text('FICTIONAL_INVALID_KEY')
                else:
                    path.unlink()
                before = self.snapshot()
                metadata = json.loads((self.root / 'installation.json').read_text())
                metadata['databases_initialized'] = True
                with (
                    patch.object(update, 'validate_manifest', return_value=({}, 'revision')),
                    patch.object(update, 'release', return_value={'version': '1.2.0'}),
                    patch.object(update, 'state', return_value=metadata),
                    patch.object(update, 'admit_release'),
                    patch.object(update, 'snapshot') as save,
                    patch.object(update, 'verified_backup') as backup,
                    patch.object(update, 'copy_sources') as copy,
                ):
                    with self.assertRaises(RuntimeError):
                        update.perform_update(Path(self.temp.name) / 'source', self.root, 'apply')
                    save.assert_not_called()
                    backup.assert_not_called()
                    copy.assert_not_called()
                self.assertEqual(self.snapshot(), before)
                with (
                    patch.object(restore.backup_archive, 'extract', side_effect=extract),
                    patch.object(restore.backup_archive, 'verify_snapshot', return_value=snapshot),
                    patch.object(
                        restore.backup_archive,
                        'validate_authentication_boundary',
                        return_value=['synapse', 'mas'],
                    ),
                    patch.object(restore.update, 'copy_sources') as copy,
                    patch.object(restore.lifecycle, 'configure') as configure,
                ):
                    with self.assertRaises(RuntimeError):
                        restore._restore_locked(
                            archive,
                            target,
                            hashlib.sha256(archive.read_bytes()).hexdigest(),
                            '8.8.8.8',
                        )
                    copy.assert_not_called()
                    configure.assert_not_called()
                self.assertFalse(target.exists())
                self.assertEqual(self.snapshot(), before)
                path.write_bytes(original)
                path.chmod(0o600)

    def test_native_source_update_preserves_imported_private_identity(self):
        import update

        self.seed_import()
        self.generate()
        before = {
            name: value
            for name, value in self.snapshot().items()
            if name.startswith('secrets/') or name == 'config/mas-signing.pem'
        }
        source = Path(self.temp.name) / 'source'
        source.mkdir()
        (source / 'release.json').write_text('{"version":"1.2.0"}')
        (source / 'public-helper.py').write_text('# FICTIONAL_PUBLIC_REPLACEMENT')
        (source / 'SHA256SUMS').write_text('FICTIONAL_MANIFEST')
        entries = {name: source / name for name in ('release.json', 'public-helper.py')}
        metadata = json.loads((self.root / 'installation.json').read_text())
        metadata.update(
            databases_initialized=True,
            applied_release_version='1.2.0',
            applied_release_revision='revision',
        )
        with (
            patch.object(update, 'validate_manifest', return_value=(entries, 'revision')),
            patch.object(update, 'release', return_value={'version': '1.2.0'}),
            patch.object(update, 'state', return_value=metadata),
            patch.object(update, 'admit_release'),
            patch.object(update, 'component_plan', return_value=None),
            patch.object(update, 'run_data_migrations'),
            patch.object(update, 'update_resource_check'),
            patch.object(update, 'verified_backup'),
            patch.object(update, 'run_up', return_value=0),
            patch.object(update, 'refresh_command'),
        ):
            update.perform_update(source, self.root, 'apply')
        after = self.snapshot()
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(
            (self.root / 'public-helper.py').read_bytes(),
            (source / 'public-helper.py').read_bytes(),
        )
        self.assertFalse((self.root / 'update-journal.json').exists())

    def test_restore_publication_keeps_imported_keys_and_identity(self):
        import restore

        self.seed_import()
        self.generate()
        (self.root / 'versions.json').write_bytes((BUNDLE / 'versions.json').read_bytes())
        archive = Path(self.temp.name) / 'fixture.tar.gz'
        archive.write_bytes(b'FICTIONAL_ARCHIVE')
        snapshot = Path(self.temp.name) / 'snapshot'
        snapshot.mkdir()
        target = Path(self.temp.name) / 'restored'
        before = {
            name: value
            for name, value in self.snapshot().items()
            if name.startswith('secrets/') or name == 'config/mas-signing.pem'
        }

        def extract(path, destination, **kwargs):
            if path != archive:
                shutil.copytree(self.root, destination, dirs_exist_ok=True)

        with (
            patch.object(restore.backup_archive, 'extract', side_effect=extract),
            patch.object(restore.backup_archive, 'verify_snapshot', return_value=snapshot),
            patch.object(
                restore.backup_archive,
                'validate_authentication_boundary',
                return_value=['synapse', 'mas'],
            ),
            patch.object(restore.update, 'validate_manifest', return_value=({}, 'revision')),
            patch.object(restore.update, 'release', return_value={'version': '1.2.0'}),
            patch.object(restore.update, 'copy_sources'),
            patch.object(restore.referral, 'verify_archive'),
            patch('federation_runtime.verify_archive'),
            patch.object(restore.lifecycle, 'configure'),
            patch.object(restore.lifecycle, 'ownership_check'),
            patch.object(restore.os, 'chown'),
            patch.object(
                restore.lifecycle,
                'generate',
                side_effect=RuntimeError('FIXTURE_STOP_BEFORE_DOCKER'),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, 'FIXTURE_STOP_BEFORE_DOCKER'):
                restore._restore_locked(
                    archive, target, hashlib.sha256(archive.read_bytes()).hexdigest(), '8.8.8.8'
                )
        self.assertEqual(
            {name: hashlib.sha256((target / name).read_bytes()).hexdigest() for name in before},
            before,
        )
        source_metadata = json.loads((self.root / 'installation.json').read_text())
        target_metadata = json.loads((target / 'installation.json').read_text())
        for key in ('service_identity', 'service_identity_sha256', 'administrator_identity'):
            self.assertEqual(target_metadata[key], source_metadata[key])
        self.assertTrue(target_metadata['restore_pending'])

    def test_legacy_seeded_missing_administrator_fails_admission(self):
        metadata_path = self.root / 'installation.json'
        metadata = json.loads(metadata_path.read_text())
        metadata.pop('administrator_identity')
        metadata_path.write_text(json.dumps(metadata))
        (self.root / 'secrets/admin-access.json').unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'administrator credentials are missing'):
            service_identity.validate_installation(self.root, metadata)
        self.assertEqual(self.snapshot(), before)


class ImportedAdministratorTests(unittest.TestCase):
    def user(self, **changes):
        user = {
            'id': IMPORTED_ADMIN['mas_user_id'],
            'attributes': {
                'username': IMPORTED_ADMIN['username'],
                'admin': True,
            },
        }
        user['attributes'].update(changes)
        return user

    def test_bootstrap_of_imported_admin_only_inspects_and_retains_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / 'checkpoint.json'
            checkpoint.write_text('{"user_id":"UNRELATED_OLD_CHECKPOINT"}')
            before = checkpoint.read_bytes()
            for _ in range(2):
                request = Mock(return_value=(200, {'data': self.user()}))
                result = mas_admin.bootstrap(request, IMPORTED_ADMIN, checkpoint)
                self.assertEqual(result['user_id'], IMPORTED_ADMIN['mas_user_id'])
                request.assert_called_once_with('/api/admin/v1/users/by-username/saved-owner')
                self.assertEqual(checkpoint.read_bytes(), before)

    def test_imported_admin_changes_are_rejected_without_mutation(self):
        for attrs in (
            {'admin': False},
            {'locked_at': 'fixture'},
            {'deactivated_at': 'fixture'},
            {'username': 'other-owner'},
        ):
            with self.subTest(attrs=attrs):
                request = Mock(return_value=(200, {'data': self.user(**attrs)}))
                with self.assertRaises(RuntimeError):
                    mas_admin.inspect_administrator(request, IMPORTED_ADMIN)
                self.assertEqual(request.call_count, 1)
        user = self.user()
        user['id'] = '01K5WZ00000000000000000008'
        request = Mock(return_value=(200, {'data': user}))
        with self.assertRaises(RuntimeError):
            mas_admin.inspect_administrator(request, IMPORTED_ADMIN)
        self.assertEqual(request.call_count, 1)
        with self.assertRaises(RuntimeError):
            mas_admin.administrator_username(IMPORTED_ADMIN, domain='another.example.org')
        request = Mock()
        with self.assertRaises(RuntimeError):
            mas_admin.set_password(request, dict(IMPORTED_ADMIN, password='FICTIONAL'))
        request.assert_not_called()

    def test_lifecycle_uses_read_only_inspection_and_keeps_saved_admin(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'secrets').mkdir()
            path = root / 'secrets/admin-access.json'
            path.write_text(json.dumps(IMPORTED_ADMIN))
            path.chmod(0o600)
            (root / 'secrets/referral-client.json').write_text('{}')
            (root / 'mas_admin.py').write_text('PUBLIC_HELPER')
            before = path.read_bytes()
            with (
                patch.object(lifecycle, 'ROOT', root, create=True),
                patch.object(lifecycle, 'state', return_value={'domain': 'preserved.example.org'}),
                patch.object(lifecycle, 'postgres') as postgres,
                patch.object(lifecycle, 'emit'),
                patch.object(
                    lifecycle,
                    'dc',
                    return_value=json.dumps(
                        {
                            'user_id': IMPORTED_ADMIN['mas_user_id'],
                            'password_managed': False,
                        }
                    ),
                ) as dc,
            ):
                lifecycle.create_admin()
                postgres.assert_not_called()
                self.assertEqual(json.loads(dc.call_args.kwargs['stdin'])['action'], 'inspect')
                self.assertEqual(path.read_bytes(), before)

    def test_imported_password_rotation_is_rejected_before_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'secrets').mkdir()
            (root / 'secrets/admin-access.json').write_text(json.dumps(IMPORTED_ADMIN))
            input_path = root / 'input.json'
            input_path.write_text(json.dumps({'password': 'FICTIONAL_NEW_PASSWORD_123'}))
            with (
                patch.object(
                    credentials, 'private_json', side_effect=lambda p: json.loads(p.read_text())
                ),
                patch.object(lifecycle, 'state', return_value={'domain': 'preserved.example.org'}),
                patch.object(lifecycle, 'backup') as backup,
            ):
                with self.assertRaises(RuntimeError):
                    credentials.rotate(root, input_path)
                backup.assert_not_called()


class AdministratorConsumerTests(unittest.TestCase):
    def setUp(self):
        import state_store

        self.temp = tempfile.TemporaryDirectory(prefix='misbot-admin-consumers-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'secrets').mkdir(mode=0o700)
        (self.root / 'config').mkdir(mode=0o700)
        self.metadata = {'domain': 'preserved.example.org'}
        state_store.write_json(self.root / 'installation.json', self.metadata)
        state_store.write_json(self.root / 'secrets/admin-access.json', IMPORTED_ADMIN)

    def test_default_owner_invitation_and_bot_identity_use_saved_administrator(self):
        import federation_runtime
        import registration
        import state_store
        import welcome_settings

        self.assertEqual(welcome_settings.owner(self.root), IMPORTED_ADMIN['matrix_id'])
        saved = federation_runtime.identity(self.root, self.metadata)
        self.assertEqual(saved['administrator_mxid'], IMPORTED_ADMIN['matrix_id'])
        state_store.write_json(
            self.root / 'secrets/first-registration.json',
            {
                'domain': self.metadata['domain'],
                'format': 1,
                'resource': 'FICTIONAL_INVITATION',
            },
        )
        self.assertEqual(
            registration.invitation_issuers(self.root, self.metadata['domain']),
            {'FICTIONAL_INVITATION': IMPORTED_ADMIN['matrix_id']},
        )

    def test_explicit_admins_are_retained_and_initial_admin_is_protected(self):
        import admin_identity
        import federation_runtime
        import state_store
        import welcome_settings

        other = '@another:preserved.example.org'
        state_store.write_json(
            self.root / 'config/federation-bot.json',
            {
                'user_id': '@admin-panel:preserved.example.org',
                'administrator_mxids': [other],
            },
        )
        saved = federation_runtime.identity(self.root, self.metadata)
        self.assertEqual(saved['administrator_mxids'], [other])
        self.assertEqual(welcome_settings.owner(self.root), other)
        with patch.object(admin_identity, 'api') as api:
            with self.assertRaises(ValueError):
                admin_identity.access(self.root, 'remove', IMPORTED_ADMIN['matrix_id'])
            with self.assertRaises(ValueError):
                admin_identity.choose(self.root, IMPORTED_ADMIN['username'])
            api.assert_not_called()
        self.assertEqual(welcome_settings.administrators(self.root), [other])

    def test_terminal_protects_imported_owner_and_does_not_request_unmanaged_password(self):
        import cli_commands
        import terminal_navigation
        import terminal_ui

        ui = Mock()
        ui.root = self.root
        ui.installation.return_value = self.metadata
        ui.run.return_value = 0
        ui.operation_records = [
            {
                'primary': '@another:preserved.example.org',
                'administrators': [
                    '@another:preserved.example.org',
                    IMPORTED_ADMIN['matrix_id'],
                    '@extra:preserved.example.org',
                ],
            }
        ]
        with patch.object(terminal_navigation, 'choose_value', return_value='selected') as choose:
            terminal_navigation.operation_selection(
                ui, cli_commands.BY_PATH['admin access remove'], 'mxid'
            )
        self.assertEqual(
            choose.call_args.args[2],
            [('@extra:preserved.example.org', '@extra:preserved.example.org', '')],
        )
        ui.reset_mock()
        code = terminal_ui.UI.execute_password(ui, cli_commands.BY_PATH['credentials admin set'])
        self.assertEqual(code, 2)
        ui.run.assert_not_called()
        ui.input_value.assert_not_called()

    def test_cli_can_show_unmanaged_admin_status_without_bootstrap_identity_error(self):
        import io
        import registration

        class Terminal(io.StringIO):
            def isatty(self):
                return True

        output = Terminal()
        with (
            patch.object(
                sys, 'argv', ['registration.py', '--directory', str(self.root), 'admin-show']
            ),
            patch.object(sys, 'stdin', Terminal()),
            patch.object(sys, 'stdout', output),
            patch.object(registration.state_store, 'require_root'),
            patch('command_wrapper.trusted_installation'),
        ):
            self.assertEqual(registration.main(), 0)
        self.assertIn('MAS', output.getvalue())
        self.assertNotIn('Unexpected', output.getvalue())

    def test_saved_admin_identity_and_password_mode_cannot_be_replaced(self):
        import state_store

        original = dict(IMPORTED_ADMIN)
        for mode in (None, 0, 'false'):
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                mas_admin.administrator_username(dict(original, password_managed=mode))
        metadata = dict(
            self.metadata,
            administrator_identity={
                key: original[key] for key in ('username', 'matrix_id', 'mas_user_id')
            },
        )
        state_store.write_json(self.root / 'installation.json', metadata)
        state_store.write_json(
            self.root / 'secrets/admin-access.json',
            dict(original, mas_user_id='01K5WZ00000000000000000008'),
        )
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            service_identity.administrator(self.root)
