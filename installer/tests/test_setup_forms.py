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
                self.answers(['s3', *self.s3_values()[:index], 'назад', 'local'])
                with patch('s3_diagnostics.diagnose') as probe:
                    self.interface.edit_setup_field('MEDIA_MODE')
                self.assertEqual(self.interface.model, original)
                self.assertEqual(json.loads(self.interface.draft.read_text())['values'], original)
                probe.assert_not_called()

    def test_failed_s3_probe_back_restores_profile_and_removes_temporary_secrets(self):
        original = self.interface.model.copy()
        self.answers(['s3', *self.s3_values(), 'назад', 'local'])
        with (
            patch(
                's3_diagnostics.diagnose',
                return_value={'passed': False, 'conclusion': 'denied', 'next_actions': []},
            ),
        ):
            self.interface.edit_setup_field('MEDIA_MODE')
        self.assertEqual(self.interface.model, original)
        self.assertEqual(list(self.base.glob('draft-s3-probe-recovery-*.env')), [])

    def test_wizard_nested_media_cancel_returns_to_media_and_can_save_exit(self):
        for command in ('назад', 'отмена', '0'):
            with self.subTest(command=command):
                self.interface.step = 3
                original = self.interface.model.copy()
                self.answers(['s3', command, 'local', 'сохранить и выйти'])
                with patch('s3_diagnostics.diagnose') as probe:
                    self.assertEqual(self.interface.wizard(), 0)
                self.assertEqual(self.interface.step, 4)
                self.assertEqual(self.interface.model, original)
                self.assertEqual(sum('MEDIA_MODE' in prompt for prompt, _ in self.prompts), 2)
                self.assertFalse(any('MAS_MODE' in prompt for prompt, _ in self.prompts))
                probe.assert_not_called()

    def test_zero_at_every_s3_field_cancels_without_saving_or_probing(self):
        import ui_actions

        for index in range(6):
            with self.subTest(field=index):
                self.answers(['1', *self.s3_values()[:index], '0'])
                with patch.object(self.interface, 'run') as run:
                    self.assertEqual(
                        self.interface.execute_s3(ui_actions.Action('storage s3 check', 'S3')), 0
                    )
                    self.assertTrue(self.interface._result_waited)
                run.assert_not_called()
                self.assertFalse(self.interface.draft.exists())
                self.assertNotIn('S3_BUCKET', self.interface.model)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux PTY required')
    def test_small_fullscreen_s3_endpoint_zero_and_back_cancel_actual_input(self):
        import fcntl
        import pty
        import re
        import select
        import struct
        import subprocess
        import termios
        import time
        from terminal_views import Console

        if Console is None:
            self.skipTest('Rich required')
        script = self.base / 'pty_form.py'
        script.write_text("""import sys
sys.path.insert(0, sys.argv[1])
import terminal_ui, ui_actions
interface = terminal_ui.UI(sys.argv[2], draft=sys.argv[3])
with interface.view.screen():
    assert interface.execute_s3(ui_actions.Action('storage s3 check', 'Проверка S3')) == 0
    assert interface._result_waited
    # Only the current form is kept in the fullscreen page, never a stale menu.
    assert not any('Что проверить?' in str(part) for part in interface.view.page)
print('CANCELLED_S3')
""")
        for command in ('0', 'назад'):
            with self.subTest(command=command):
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 12, 44, 0, 0))
                env = {**os.environ, 'TERM': 'xterm-256color'}
                env.pop('NO_COLOR', None)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        str(script),
                        str(Path(ui.__file__).parent),
                        str(self.interface.root),
                        str(self.interface.draft),
                    ],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=env,
                    start_new_session=True,
                )
                os.close(slave)
                output = b''

                def until(marker):
                    nonlocal output
                    deadline = time.monotonic() + 8
                    while marker not in re.sub(
                        r'\x1b\[[0-?]*[ -/]*[@-~]', '', output.decode(errors='replace')
                    ):
                        if time.monotonic() >= deadline:
                            self.fail(output.decode(errors='replace'))
                        if select.select([master], [], [], 0.1)[0]:
                            try:
                                output += os.read(master, 65536)
                            except OSError:
                                self.fail(output.decode(errors='replace'))

                try:
                    until('Номер')
                    os.write(master, b'1\n')
                    until('S3_BUCKET')
                    os.write(master, b'fictional-media\n')
                    until('S3_ENDPOINT_URL')
                    # Resize during input, then send an actual navigation command.
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 10, 40, 0, 0))
                    os.write(master, (command + '\n').encode())
                    until('CANCELLED_S3')
                    child.wait(timeout=5)
                    self.assertEqual(child.returncode, 0, output.decode(errors='replace'))
                    self.assertFalse(self.interface.draft.exists())
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                    os.close(master)

    def test_telegram_form_checks_supplied_proxy_before_advancing(self):
        self.answers(['да', 'FICTIONAL_ID', 'FICTIONAL_SECRET', 'socks5://proxy.example.org:1080'])
        with patch('telegram_login_check.check') as check:
            self.interface.setup_provider('TELEGRAM')
        check.assert_called_once_with(self.interface.model)
        self.assertIn('эта проверка их не подтверждает', self.output.getvalue())
        self.assertNotIn('FICTIONAL_SECRET', self.output.getvalue())

    def test_telegram_failed_check_allows_correction_and_retry(self):
        from telegram_login_check import LoginCheckError

        self.answers(
            [
                'да',
                'FICTIONAL_ID',
                'FICTIONAL_SECRET',
                'socks5://proxy.example.org:1080',
                '1',
                'socks5://fixed.example.org:1080',
            ]
        )
        seen = []

        def check(values):
            seen.append(values['SOCKS5_CONFIG'])
            if len(seen) == 1:
                raise LoginCheckError('Проверка не пройдена.')

        with patch('telegram_login_check.check', side_effect=check):
            self.interface.setup_provider('TELEGRAM')
        self.assertEqual(
            seen, ['socks5://proxy.example.org:1080', 'socks5://fixed.example.org:1080']
        )

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
