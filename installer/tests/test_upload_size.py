"""Upload limits across configuration, generation and the optional SSH settings form."""

from contextlib import ExitStack
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

INSTALLER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INSTALLER))
import configuration
import configuration_schema as schema
import lifecycle
import state_store
import terminal_ui
from terminal_views import TerminalView


class UploadSizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'stack'

    def generate(self, *args, success=True):
        result = subprocess.run(
            [
                sys.executable,
                str(INSTALLER / 'generate.py'),
                '--directory',
                str(self.root),
                '--preview',
                *args,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return result

    def initialize(self):
        self.generate('--domain', 'fictional.example.org')

    def upload_bytes(self):
        return yaml.safe_load((self.root / 'config/homeserver.yaml').read_text())['max_upload_size']

    def runtime_and_secrets(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for area in ('config', 'secrets')
            for path in (self.root / area).rglob('*')
            if path.is_file()
        }

    def lifecycle_stubs(self, up=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        calls = []
        for name in ('configure', 'ownership_check', 'backup', 'restore_check', 'up'):

            def action(*args, step=name, **kwargs):
                calls.append(step)
                if step == 'up' and up:
                    up()

            stack.enter_context(patch.object(lifecycle, name, side_effect=action))
        return calls

    def test_default_is_exactly_256_mib_and_repeat_keeps_runtime_and_keys(self):
        self.initialize()
        self.assertEqual(configuration.load(self.root, True)['MAX_UPLOAD_SIZE_MIB'], '256')
        self.assertEqual(self.upload_bytes(), 268435456)
        before = self.runtime_and_secrets()
        self.generate()
        self.assertEqual(self.runtime_and_secrets(), before)

    def test_legacy_settings_without_field_get_new_default(self):
        self.initialize()
        path = self.root / 'config/homeserver.yaml'
        previous = yaml.safe_load(path.read_text())
        previous['max_upload_size'] = '100M'
        path.write_text(yaml.safe_dump(previous))
        keys = {p.name: p.read_bytes() for p in (self.root / 'secrets').iterdir()}
        shutil.rmtree(self.root / 'settings')
        state_store.write_text(self.root / 'settings.env', 'DOMAIN=fictional.example.org\n')
        self.generate()
        self.assertEqual(self.upload_bytes(), 268435456)
        self.assertEqual((self.root / 'settings.env').read_text(), 'DOMAIN=fictional.example.org\n')
        self.assertEqual({p.name: p.read_bytes() for p in (self.root / 'secrets').iterdir()}, keys)

    def test_explicit_limit_round_trips_and_applies_to_local_and_s3_media(self):
        for mode in ('local', 's3'):
            with self.subTest(media=mode):
                self.root = Path(self.temp.name) / mode
                self.root.mkdir(mode=0o700)
                state_store.write_json(
                    self.root / 'installation.json',
                    {
                        'domain': 'fictional.example.org',
                        'preview': True,
                        'config_format_version': 2,
                    },
                )
                model = {
                    'DOMAIN': 'fictional.example.org',
                    'MAX_UPLOAD_SIZE_MIB': '512',
                    'MEDIA_MODE': mode,
                }
                if mode == 's3':
                    model.update(
                        S3_BUCKET='fictional-media',
                        S3_REGION='fictional-1',
                        S3_PREFIX='media/',
                        S3_ACCESS_KEY_ID='FICTIONAL_ACCESS',
                        S3_SECRET_ACCESS_KEY='FICTIONAL_SECRET',
                    )
                configuration.write_model(self.root, model, preview=True)
                self.generate('--domain', 'fictional.example.org')
                before = self.runtime_and_secrets()
                self.assertEqual(self.upload_bytes(), 536870912)
                self.generate()
                self.assertEqual(self.runtime_and_secrets(), before)
                self.assertEqual(configuration.load(self.root, True)['MAX_UPLOAD_SIZE_MIB'], '512')

    def test_legacy_explicit_limit_survives_migration_and_repeat(self):
        self.root.mkdir(mode=0o700)
        state_store.write_text(
            self.root / 'settings.env', 'DOMAIN=fictional.example.org\nMAX_UPLOAD_SIZE_MIB=100\n'
        )
        state_store.write_json(
            self.root / 'installation.json',
            {'domain': 'fictional.example.org', 'preview': True, 'config_format_version': 1},
        )
        configuration.migrate(self.root)
        configuration.migrate(self.root)
        self.assertEqual(configuration.load(self.root, True)['MAX_UPLOAD_SIZE_MIB'], '100')
        self.assertEqual(
            (self.root / 'settings/policy.env').read_text().count('MAX_UPLOAD_SIZE_MIB='), 1
        )
        self.generate('--domain', 'fictional.example.org')
        self.assertEqual(self.upload_bytes(), 104857600)

    def test_validation_accepts_integer_bounds_and_rejects_ambiguous_input(self):
        for value in ('1', '256', '512', '65536'):
            with self.subTest(value=value):
                self.assertEqual(schema.field_value('MAX_UPLOAD_SIZE_MIB', value), value)
        for value in (
            '',
            '0',
            '-1',
            '65537',
            '256.0',
            '+256',
            '256M',
            '256 МиБ',
            '٢٥٦',
            'FICTIONAL_PRIVATE_VALUE',
            '1\n2',
            256,
            True,
        ):
            with self.subTest(value=value):
                with self.assertRaises(schema.InvalidConfiguration) as error:
                    schema.field_value('MAX_UPLOAD_SIZE_MIB', value)
                self.assertEqual(error.exception.field, 'MAX_UPLOAD_SIZE_MIB')
                self.assertNotIn('FICTIONAL_PRIVATE_VALUE', json.dumps(error.exception.result()))

    def test_invalid_limit_preserves_generated_files_and_secrets(self):
        self.initialize()
        before = self.runtime_and_secrets()
        policy = self.root / 'settings/policy.env'
        for value in ('0', '65537', '256M', 'FICTIONAL_PRIVATE_VALUE'):
            with self.subTest(value=value):
                state_store.write_text(policy, 'MAX_UPLOAD_SIZE_MIB=' + value + '\n')
                result = self.generate(success=False)
                self.assertIn('MAX_UPLOAD_SIZE_MIB', result.stderr)
                self.assertNotIn('FICTIONAL_PRIVATE_VALUE', result.stderr + result.stdout)
                self.assertEqual(self.runtime_and_secrets(), before)

    def test_settings_backend_checks_backup_before_publishing_and_generates_limit(self):
        self.initialize()
        keys = {path.name: path.read_bytes() for path in (self.root / 'secrets').iterdir()}
        calls = self.lifecycle_stubs(up=self.generate)
        configuration.apply_settings(self.root, {'MAX_UPLOAD_SIZE_MIB': '512'})
        self.assertEqual(calls, ['configure', 'ownership_check', 'backup', 'restore_check', 'up'])
        self.assertEqual(configuration.load(self.root, True)['MAX_UPLOAD_SIZE_MIB'], '512')
        self.assertEqual(self.upload_bytes(), 536870912)
        self.assertEqual(
            {path.name: path.read_bytes() for path in (self.root / 'secrets').iterdir()}, keys
        )

    def test_invalid_setting_stops_before_backup_or_runtime_actions(self):
        self.initialize()
        before = self.runtime_and_secrets()
        calls = self.lifecycle_stubs()
        with self.assertRaises(schema.InvalidConfiguration):
            configuration.apply_settings(self.root, {'MAX_UPLOAD_SIZE_MIB': '0'})
        self.assertEqual(calls, [])
        self.assertEqual(configuration.load(self.root, True)['MAX_UPLOAD_SIZE_MIB'], '256')
        self.assertEqual(self.runtime_and_secrets(), before)

    def test_backup_or_restore_check_failure_does_not_publish_limit(self):
        self.initialize()
        before = self.runtime_and_secrets()
        self.lifecycle_stubs()
        for step in ('backup', 'restore_check'):
            with (
                self.subTest(step=step),
                patch.object(lifecycle, step, side_effect=RuntimeError('Fictional failure')),
            ):
                with self.assertRaises(RuntimeError):
                    configuration.apply_settings(self.root, {'MAX_UPLOAD_SIZE_MIB': '512'})
                self.assertEqual(configuration.load(self.root, True)['MAX_UPLOAD_SIZE_MIB'], '256')
                self.assertEqual(self.runtime_and_secrets(), before)

    def test_failed_or_interrupted_apply_restores_previous_settings(self):
        self.initialize()
        before = self.runtime_and_secrets()
        self.lifecycle_stubs()
        for error in (RuntimeError('FICTIONAL_PRIVATE_FAILURE'), KeyboardInterrupt()):
            with (
                self.subTest(failure=type(error).__name__),
                patch.object(lifecycle, 'up', side_effect=error),
            ):
                with self.assertRaisesRegex(RuntimeError, 'Settings restored') as failure:
                    configuration.apply_settings(self.root, {'MAX_UPLOAD_SIZE_MIB': '512'})
                self.assertNotIn('FICTIONAL_PRIVATE_FAILURE', str(failure.exception))
                self.assertEqual(configuration.load(self.root, True)['MAX_UPLOAD_SIZE_MIB'], '256')
                self.assertEqual(self.runtime_and_secrets(), before)
                self.assertFalse((self.root / 'configuration-transition.json').exists())
        configuration.apply_settings(self.root, {'MAX_UPLOAD_SIZE_MIB': '512'})
        self.assertEqual(configuration.load(self.root, True)['MAX_UPLOAD_SIZE_MIB'], '512')

    def test_settings_form_retries_bad_input_and_only_applies_after_confirmation(self):
        self.root.mkdir(mode=0o700)
        state_store.write_json(self.root / 'installation.json', {'domain': 'fictional.example.org'})
        configuration.write_model(self.root, {'DOMAIN': 'fictional.example.org'})
        for answers, expected in (
            (['8', '0', '512', 'a', 'да'], {'MAX_UPLOAD_SIZE_MIB': '512'}),
            (['8', '512', 'a', 'нет'], None),
            (['8', '', 'a'], None),
        ):
            with self.subTest(answers=answers):
                entries = iter(answers)
                interface = terminal_ui.UI(
                    self.root,
                    draft=Path(self.temp.name) / 'draft.json',
                    reader=lambda *args: next(entries),
                    view=TerminalView(plain=True),
                )
                with (
                    patch('sys.stdout', new=io.StringIO()),
                    patch.object(configuration, 'apply_settings') as apply,
                ):
                    interface.settings_menu()
                if expected:
                    apply.assert_called_once_with(self.root, expected)
                else:
                    apply.assert_not_called()
                self.assertEqual(configuration.load(self.root, True)['MAX_UPLOAD_SIZE_MIB'], '256')

    def test_existing_compose_settings_do_not_touch_native_configuration(self):
        self.root.mkdir(mode=0o700)
        state_store.write_json(self.root / 'installation.json', {'backend': 'existing-compose'})
        with (
            patch.object(configuration, 'load') as load,
            patch.object(configuration, 'apply_settings') as apply,
        ):
            interface = terminal_ui.UI(
                self.root, draft=Path(self.temp.name) / 'draft.json', view=TerminalView(plain=True)
            )
            with patch('sys.stdout', new=io.StringIO()):
                interface.settings_menu()
        load.assert_not_called()
        apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
