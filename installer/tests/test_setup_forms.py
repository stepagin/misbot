"""Transactional installation forms and independent external authentication DNS."""

import io
import os
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration_schema as schema
import preflight
import terminal_ui as ui
from terminal_views import TerminalView
import wizard_dns


class SetupFormsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.interface = ui.UI(
            self.base / 'target', draft=self.base / 'draft.json', view=TerminalView(plain=True)
        )
        self.interface.model = {'DOMAIN': 'example.org', 'MEDIA_MODE': 'local'}
        self.output = io.StringIO()
        self.stdout = patch('sys.stdout', self.output)
        self.stdout.start()
        self.addCleanup(self.stdout.stop)

    def answers(self, entries):
        values = iter(entries)
        self.prompts = []

        def reader(prompt, secret):
            self.prompts.append((prompt, secret))
            return next(values)

        self.interface.reader = reader

    def s3_values(self):
        return ['fictional-media', '', 'us-east-1', 'media/', 'FICTIONAL_ID', 'FICTIONAL_SECRET']

    def test_summary_s3_choice_collects_complete_hidden_credentials_before_validation(self):
        self.answers(['MEDIA_MODE', 's3', *self.s3_values(), 'сохранить и выйти'])
        with (
            patch(
                's3_diagnostics.diagnose',
                return_value={'passed': True, 'conclusion': 'ready', 'next_actions': []},
            ) as probe,
            self.assertRaises(ui.SaveExit),
        ):
            self.interface.setup_summary()
        probe.assert_called_once()
        saved = json.loads(self.interface.draft.read_text())['values']
        self.assertEqual(saved['MEDIA_MODE'], 's3')
        self.assertEqual(saved['S3_BUCKET'], 'fictional-media')
        self.assertEqual(saved['S3_SECRET_ACCESS_KEY'], 'FICTIONAL_SECRET')
        for name in ('S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY'):
            self.assertTrue(next(secret for prompt, secret in self.prompts if name in prompt))
        self.assertNotIn('FICTIONAL_SECRET', self.output.getvalue())
        self.assertEqual(list(self.base.glob('draft-s3-probe-recovery-*.env')), [])

    def test_back_from_each_s3_input_restores_previous_mode_and_profile(self):
        original = {'DOMAIN': 'example.org', 'MEDIA_MODE': 'local', 'S3_BUCKET': 'old-bucket'}
        for index in range(6):
            with self.subTest(field=index):
                self.interface.model = original.copy()
                self.answers(['s3', *self.s3_values()[:index], 'назад'])
                with patch('s3_diagnostics.diagnose') as probe, self.assertRaises(ui.Back):
                    self.interface.edit_setup_field('MEDIA_MODE')
                self.assertEqual(self.interface.model, original)
                self.assertEqual(json.loads(self.interface.draft.read_text())['values'], original)
                probe.assert_not_called()

    def test_failed_s3_probe_back_restores_profile_and_removes_temporary_secrets(self):
        original = self.interface.model.copy()
        self.answers(['s3', *self.s3_values(), 'назад'])
        with (
            patch(
                's3_diagnostics.diagnose',
                return_value={'passed': False, 'conclusion': 'denied', 'next_actions': []},
            ),
            self.assertRaises(ui.Back),
        ):
            self.interface.edit_setup_field('MEDIA_MODE')
        self.assertEqual(self.interface.model, original)
        self.assertEqual(list(self.base.glob('draft-s3-probe-recovery-*.env')), [])

    def test_pending_probe_cleanup_keeps_original_credentials_after_profile_correction(self):
        import s3_config

        corrected = self.s3_values()
        corrected[-1] = 'FICTIONAL_NEW_SECRET'
        self.answers(['s3', *self.s3_values(), '1', *corrected])
        results = [
            {
                'passed': False,
                'conclusion': 'cleanup denied',
                'next_actions': [],
                'cleanup_pending': True,
            },
            {'passed': True, 'conclusion': 'ready', 'next_actions': []},
        ]
        with patch('s3_diagnostics.diagnose', side_effect=results) as probe:
            self.interface.edit_setup_field('MEDIA_MODE')
        first, second = (call.args[0] for call in probe.call_args_list)
        self.assertNotEqual(first, second)
        self.assertEqual(s3_config.read_profile(first)['S3_SECRET_ACCESS_KEY'], 'FICTIONAL_SECRET')
        self.assertFalse(second.exists())
        self.assertEqual(self.interface.model['S3_SECRET_ACCESS_KEY'], 'FICTIONAL_NEW_SECRET')

    def test_standalone_profile_does_not_mutate_or_save_wizard_settings(self):
        original = self.interface.model.copy()
        self.answers(self.s3_values())
        values = self.interface.s3_profile({})
        self.assertEqual(values['S3_BUCKET'], 'fictional-media')
        self.assertEqual(self.interface.model, original)
        self.assertFalse(self.interface.draft.exists())

    def test_dns_back_from_apply_keeps_summary_and_never_applies(self):
        self.interface.step = 6
        self.answers(['применить', 'сохранить и выйти'])
        with patch.object(self.interface, 'check_dns_settings', side_effect=ui.Back()) as dns:
            with self.assertRaises(ui.SaveExit):
                self.interface.setup_summary()
        dns.assert_called_once()
        self.assertEqual(self.interface.step, 6)
        self.assertFalse(self.interface.root.exists())

    def test_clearing_either_telegram_credential_disables_complete_provider_and_proxy(self):
        for field in ('TELEGRAM_CLIENT_ID', 'TELEGRAM_CLIENT_SECRET'):
            with self.subTest(field=field):
                self.interface.model.update(
                    TELEGRAM_CLIENT_ID='FICTIONAL_ID',
                    TELEGRAM_CLIENT_SECRET='FICTIONAL_SECRET',
                    SOCKS5_CONFIG='socks5://proxy.example.org:1080',
                )
                self.answers(['/очистить'])
                self.interface.field(field)
                schema.validate(self.interface.model)
                for key in ('TELEGRAM_CLIENT_ID', 'TELEGRAM_CLIENT_SECRET', 'SOCKS5_CONFIG'):
                    self.assertEqual(self.interface.model[key], '')

    def test_provider_cancel_rolls_back_new_partial_credentials(self):
        original = self.interface.model.copy()
        self.answers(['да', 'FICTIONAL_ID', 'назад'])
        with self.assertRaises(ui.Back):
            self.interface.setup_provider('TELEGRAM')
        self.assertEqual(self.interface.model, original)

    def test_retry_prompt_accepts_navigation_aliases(self):
        for command, exception in (
            ('назад', ui.Back),
            (' b ', ui.Back),
            ('q', ui.SaveExit),
            ('сохранить и выйти', ui.SaveExit),
            ('отмена', ui.Cancel),
        ):
            with self.subTest(command=command):
                self.answers([command])
                with self.assertRaises(exception):
                    self.interface.answer(
                        'Повторить / назад / сохранить и выйти', ('повторить',), 'повторить'
                    )

    def test_mas_default_is_only_in_brackets_and_current_custom_value_wins(self):
        self.interface.model['MAS_DOMAIN'] = 'auth.example.net'
        self.answers([''])
        self.interface.field('MAS_DOMAIN')
        self.assertIn('[auth.example.net]', self.prompts[0][0])
        self.assertNotIn('пусто', self.output.getvalue())
        self.assertNotIn('mas.example.org', self.output.getvalue())
        self.assertEqual(self.interface.model['MAS_DOMAIN'], 'auth.example.net')

    def test_single_main_domain_a_avoids_ipv4_prompt(self):
        with (
            patch.object(preflight, 'resolve', return_value={'8.8.8.8'}),
            patch.object(self.interface, 'field') as field,
            patch.object(self.interface, 'wait_dns'),
            patch.object(preflight, 'dns_check'),
            patch.object(preflight, 'caa_check'),
            patch.object(preflight, 'network_check'),
        ):
            self.interface.check_dns_settings()
        field.assert_not_called()

    def test_saved_exit_includes_resume_show_and_reset_commands(self):
        self.answers(['сохранить и выйти'])
        with patch.object(self.interface, 'setup_step', side_effect=ui.SaveExit()):
            self.assertEqual(self.interface.wizard(), 0)
        for command in (' ui', 'config draft show', 'config draft reset --confirm discard-draft'):
            self.assertIn(command, self.output.getvalue())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux PTY required')
    def test_dns_shortcut_with_enter_does_not_submit_the_following_form(self):
        import select
        import termios

        master, slave = os.openpty()
        stream = os.fdopen(slave, 'r')
        self.addCleanup(stream.close)
        self.addCleanup(lambda: os.close(master))
        original = termios.tcgetattr(stream.fileno())
        with patch('sys.stdin', stream):
            with self.interface.dns_input_mode():
                os.write(master, b'b\n')
                self.assertEqual(self.interface.dns_wait_command(1), 'назад')
            self.assertFalse(select.select([stream], [], [], 0)[0])
        self.assertEqual(termios.tcgetattr(stream.fileno()), original)

    def test_reuse_removed_runtime_rejects_unexpected_retained_private_files(self):
        import command_wrapper
        import state_store

        root = self.interface.root
        root.mkdir(mode=0o700)
        state_store.write_json(
            root / '.misbot-empty.json', {'family': command_wrapper.FAMILY, 'format': 1}
        )
        state_store.write_json(root / 'release.json', {'family': command_wrapper.FAMILY})
        with patch('update.validate_manifest', return_value=({'release.json': 'fictional'}, None)):
            self.assertTrue(self.interface.reusable_removed_directory(root))
            state_store.write_text(root / 'private-retained.env', 'FICTIONAL_PRIVATE')
            self.assertFalse(self.interface.reusable_removed_directory(root))

    def test_external_mas_profile_requires_credentials_and_dedicated_host(self):
        values = {
            'DOMAIN': 'example.org',
            'MAS_MODE': 'external',
            'MAS_DOMAIN': 'auth.example.net',
            'MAS_SHARED_SECRET': 'FICTIONAL_SHARED',
            'MAS_CLIENT_ID': 'FICTIONAL_ID',
            'MAS_CLIENT_SECRET': 'FICTIONAL_SECRET',
        }
        model = schema.validate(values)
        self.assertEqual(schema.mas_api_base(model), 'https://auth.example.net')
        self.assertNotIn('auth.example.net', schema.public_hosts(model))
        for host in ('example.org', 'matrix.example.org', 'web.example.org'):
            with self.subTest(host=host), self.assertRaises(schema.InvalidConfiguration):
                schema.validate(dict(values, MAS_DOMAIN=host))
        for key in ('MAS_SHARED_SECRET', 'MAS_CLIENT_ID', 'MAS_CLIENT_SECRET'):
            with self.subTest(key=key), self.assertRaises(schema.InvalidConfiguration):
                schema.validate(dict(values, **{key: ''}))

    def test_external_authorization_does_not_ask_for_local_oauth_settings(self):
        self.interface.model['MAS_MODE'] = 'external'
        with patch.object(self.interface, 'setup_provider') as provider:
            self.interface.setup_step(4)
        provider.assert_not_called()
        self.assertIn('настраиваются на внешнем MAS', self.output.getvalue())

    def test_external_dns_keeps_its_own_address_and_accepts_its_ipv6(self):
        values = {'DOMAIN': 'example.org', 'MAS_MODE': 'external', 'MAS_DOMAIN': 'auth.example.net'}

        def resolve(host, family):
            if host == 'auth.example.net':
                return {'8.8.4.4'} if family == socket.AF_INET else {'2001:4860:4860::8888'}
            return {'8.8.8.8'} if family == socket.AF_INET else set()

        statuses = wizard_dns.scan(values, '8.8.8.8', resolver=resolve)
        self.assertTrue(all(status.passed for status in statuses))
        self.assertTrue(statuses[-1].external)
        self.assertNotIn('удалить AAAA', statuses[-1].message)
        with patch.object(preflight, 'resolve', side_effect=lambda h, f, **kw: resolve(h, f)):
            preflight.dns_check('example.org', '8.8.8.8', 'auth.example.net', 'external')
            with self.assertRaises(RuntimeError):
                preflight.dns_check(
                    'example.org', '8.8.8.8', 'auth.example.net', 'external', '8.8.8.8'
                )


if __name__ == '__main__':
    unittest.main()
