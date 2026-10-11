"""Real publication faults and shared runtime loading, with fictional data only."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration as config
import configuration_schema as schema
import state_store
import preflight


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'installation'
        self.root.mkdir(mode=0o700)
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'fictional.example.org', 'config_format_version': 1},
        )
        state_store.write_text(
            self.root / 'settings.env',
            'DOMAIN=fictional.example.org\nGOOGLE_CLIENT_ID=FICTIONAL_ID\nGOOGLE_CLIENT_SECRET="\'FICTIONAL_SECRET"\n',
        )
        self.key = self.root / 'secrets'
        self.key.mkdir(mode=0o700)
        state_store.write_text(self.key / 'identity.key', 'FICTIONAL_IMMUTABLE')

    def tearDown(self):
        self.temp.cleanup()

    def test_migrate_repeat_preserves_model_secrets_and_one_source(self):
        before = config.load(self.root, True)
        original = (self.root / 'settings.env').read_bytes()
        key = (self.key / 'identity.key').read_bytes()
        result = config.migrate(self.root)
        self.assertEqual(config.load(self.root, True), before)
        self.assertFalse((self.root / 'settings.env').exists())
        self.assertEqual((Path(result['checkpoint']) / 'settings.env').read_bytes(), original)
        self.assertEqual((self.key / 'identity.key').read_bytes(), key)
        self.assertEqual(config.migrate(self.root)['format'], 2)
        self.assertFalse((self.root / 'settings/database.env').exists())
        self.assertFalse((self.root / 'settings/backup.env').exists())
        self.assertEqual(preflight.settings(self.root)['GOOGLE_CLIENT_SECRET'], "'FICTIONAL_SECRET")
        for item in (self.root / 'settings').iterdir():
            self.assertEqual(item.stat().st_mode & 0o777, 0o600)

    def test_fault_after_first_file_blocks_all_loaders_and_forward_retry(self):
        original = state_store.write_text
        calls = []

        def fail(path, text, *args, **kwargs):
            if Path(path).parent == self.root / 'settings':
                calls.append(path)
                if len(calls) == 2:
                    raise OSError('fictional publication interruption')
            return original(path, text, *args, **kwargs)

        with patch.object(state_store, 'write_text', side_effect=fail):
            with self.assertRaises(OSError):
                config.migrate(self.root)
        with self.assertRaisesRegex(RuntimeError, 'config recover'):
            config.load(self.root)
        with self.assertRaisesRegex(RuntimeError, 'config recover'):
            state_store.assert_update_allowed(self.root)
        config.recover(self.root)
        self.assertEqual(config.load(self.root, True)['DOMAIN'], 'fictional.example.org')
        self.assertFalse((self.root / 'configuration-transition.json').exists())

    def test_unknown_duplicate_symlink_permissions_and_secret_errors(self):
        cases = (
            'DOMAIN=fictional.example.org\nUNKNOWN=FICTIONAL_SECRET\n',
            'DOMAIN=fictional.example.org\nDOMAIN=another.example.org\n',
            'GOOGLE_CLIENT_SECRET=FICTIONAL_SECRET\nDOMAIN=fictional.example.org\n',
        )
        for body in cases:
            state_store.write_text(self.root / 'settings.env', body)
            with self.assertRaises(schema.InvalidConfiguration) as error:
                config.migrate(self.root)
            self.assertNotIn('FICTIONAL_SECRET', str(error.exception))
            self.assertFalse((self.root / 'settings').exists())
        (self.root / 'settings.env').chmod(0o644)
        with self.assertRaises(schema.InvalidConfiguration):
            config.load(self.root)
        outside = self.root / 'outside.env'
        state_store.write_text(outside, 'DOMAIN=fictional.example.org\n')
        (self.root / 'settings.env').unlink()
        (self.root / 'settings.env').symlink_to(outside)
        with self.assertRaises(schema.InvalidConfiguration):
            config.load(self.root)

    def test_invalid_host_empty_enum_and_duplicates_are_rejected_without_values(self):
        for field, value in (
            ('POSTGRES_HOST', '-db.example.org'),
            ('POSTGRES_HOST', 'db..example.org'),
            ('MEDIA_MODE', ''),
            ('INVITATION_LIFETIME_SECONDS', '0'),
            ('S3_SECRET_ACCESS_KEY', 'FICTIONAL\nSECRET'),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(schema.InvalidConfiguration) as error:
                    schema.field_value(field, value)
                self.assertNotIn('FICTIONAL', str(error.exception))

    def test_split_backup_scope_and_recovery_key_location(self):
        import s3_config, external_backup

        config.migrate(self.root)
        profile = {
            'S3_BUCKET': 'fictional-backups',
            'S3_REGION': 'us-east-1',
            'S3_ENDPOINT_URL': 'https://s3.example.org',
            'S3_PREFIX': 'backup/example/',
            'S3_ACCESS_KEY_ID': 'FICTIONAL_ACCESS',
            'S3_SECRET_ACCESS_KEY': 'FICTIONAL_SECRET',
            'BACKUP_KEY_FILE': str(Path(self.temp.name) / 'recovery.key'),
            'BACKUP_CALENDAR': '',
        }
        config.write_model(self.root, config.load(self.root, True), profile)
        path = self.root / 'settings/backup.env'
        self.assertEqual(
            external_backup.settings(path), {key: profile[key] for key in s3_config.FIELDS}
        )
        with self.assertRaises(s3_config.ConfigError):
            s3_config.read_profile(path, purpose='media')
        profile['BACKUP_KEY_FILE'] = str(self.root / 'secrets/key')
        with self.assertRaises(schema.InvalidConfiguration):
            config.write_model(self.root, config.load(self.root, True), profile)

    def test_rollback_partial_publication_and_immutable_domain(self):
        before = (self.root / 'settings.env').read_bytes()
        original = state_store.write_text

        def fail(path, text, *args, **kwargs):
            if Path(path).parent == self.root / 'settings':
                raise OSError('fictional interruption')
            return original(path, text, *args, **kwargs)

        with patch.object(state_store, 'write_text', side_effect=fail):
            with self.assertRaises(OSError):
                config.migrate(self.root)
        # The prepared candidate can itself be damaged; rollback must use the verified original.
        journal = config.read_private_json(self.root / 'configuration-transition.json')
        state_store.write_text(
            self.root / 'stages/configuration' / journal['id'] / 'next/server.env',
            'UNKNOWN=FICTIONAL\n',
        )
        config.recover(self.root, rollback=True)
        self.assertEqual((self.root / 'settings.env').read_bytes(), before)
        with self.assertRaisesRegex(RuntimeError, 'immutable'):
            config.patch(self.root, {'DOMAIN': 'another.example.org'})
        self.assertEqual((self.root / 'settings.env').read_bytes(), before)

    def test_split_generation_uses_same_loader_and_does_not_rotate_identity(self):
        config.migrate(self.root)
        # Generate creates internal keys once; subsequent run preserves them.
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'fictional.example.org', 'config_format_version': 2, 'preview': True},
        )
        source = Path(__file__).resolve().parents[1]
        previous = None
        for _ in range(2):
            result = subprocess.run(
                [
                    sys.executable,
                    str(source / 'generate.py'),
                    '--directory',
                    str(self.root),
                    '--preview',
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            current = hashlib.sha256((self.key / 'identity.key').read_bytes()).hexdigest()
            if _:
                self.assertEqual(current, previous)
            previous = current
        self.assertFalse((self.root / 'settings.env').exists())
        self.assertEqual(
            json.loads((self.root / 'installation.json').read_text())['config_format_version'], 2
        )

    def test_legacy_custom_mas_issuer_is_preserved_through_config_migration(self):
        state_store.write_text(
            self.root / 'settings.env',
            'DOMAIN=fictional.example.org\nMAS_DOMAIN=auth.example.net\n',
        )
        (self.root / 'config').mkdir()
        state_store.write_text(
            self.root / 'config/mas.yaml', 'http:\n  issuer: https://auth.example.net/\n'
        )
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'fictional.example.org', 'databases_initialized': True},
        )
        config.migrate(self.root)
        self.assertEqual(config.load(self.root, True)['MAS_DOMAIN'], 'auth.example.net')
        original = (self.root / 'settings/server.env').read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'MAS issuer is immutable'):
            config.patch(self.root, {'MAS_DOMAIN': 'different.example.net'})
        self.assertEqual((self.root / 'settings/server.env').read_bytes(), original)
        self.assertFalse((self.root / 'configuration-transition.json').exists())

    def test_custom_auth_domain_cannot_be_homepage_redirect_or_other_service(self):
        with self.assertRaises(schema.InvalidConfiguration):
            schema.validate(
                {
                    'DOMAIN': 'fictional.example.org',
                    'MAS_DOMAIN': 'auth.example.net',
                    'HOMEPAGE_MODE': 'external',
                    'HOMEPAGE_URL': 'https://auth.example.net/',
                }
            )
        with self.assertRaises(schema.InvalidConfiguration):
            schema.validate(
                {'DOMAIN': 'fictional.example.org', 'MAS_DOMAIN': 'web.fictional.example.org'}
            )


if __name__ == '__main__':
    unittest.main()
