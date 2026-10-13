"""Installation input and recovery scenarios with fictional services and real drafts."""

import io
import itertools
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import configuration_schema as schema
import preflight
import state_store
import terminal_ui
from terminal_views import TerminalView


class InstallationWizardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'Matrix Test'
        self.interface = terminal_ui.UI(
            directory=self.root, draft=self.base / 'draft.json', view=TerminalView(plain=True)
        )
        self.output = io.StringIO()
        self.addCleanup(patch.stopall)
        patch('sys.stdout', self.output).start()

    def answers(self, values):
        entries = iter(values)
        self.interface.reader = lambda *args: next(entries)

    def saved(self):
        return json.loads(self.interface.draft.read_text())

    def test_ascii_domain_keeps_name_without_extra_confirmation(self):
        self.answers(['  FICTIONAL.EXAMPLE.ORG  '])
        self.interface.field('DOMAIN')
        self.assertEqual(self.saved()['values']['DOMAIN'], 'fictional.example.org')
        self.assertNotIn('Использовать именно', self.output.getvalue())
        self.assertFalse(self.root.exists())

    def test_default_draft_follows_selected_directory_and_resumes(self):
        with patch.object(Path, 'home', return_value=self.base):
            interface = terminal_ui.UI(self.base / 'initial', view=TerminalView(plain=True))
            original = interface.draft
            values = iter(['fictional.example.org', str(self.root)])
            interface.reader = lambda *args: next(values)
            interface.setup_step(0)
            interface.step = 1
            interface.save()
            resumed = terminal_ui.UI(self.root, view=TerminalView(plain=True))
            self.assertEqual(resumed.step, 1)
            self.assertEqual(resumed.model['DOMAIN'], 'fictional.example.org')
            self.assertEqual(resumed.draft, interface.draft)
            self.assertFalse(original.exists())
            self.assertEqual(interface.draft.stat().st_mode & 0o777, 0o600)
            self.assertFalse(self.root.exists())

    def test_directory_choice_preserves_another_existing_draft(self):
        with patch.object(Path, 'home', return_value=self.base):
            other = terminal_ui.UI(self.root, view=TerminalView(plain=True))
            other.model = {
                'DOMAIN': 'other.example.org',
                'GOOGLE_CLIENT_SECRET': 'FICTIONAL_PRIVATE',
            }
            other.step = 4
            other.save()
            before = other.draft.read_bytes()
            target = self.base / 'another'
            interface = terminal_ui.UI(self.base / 'initial', view=TerminalView(plain=True))
            values = iter(['fictional.example.org', str(self.root), str(target)])
            interface.reader = lambda *args: next(values)
            interface.setup_step(0)
            self.assertEqual(other.draft.read_bytes(), before)
            self.assertEqual(interface.root, target)
            self.assertEqual(terminal_ui.UI(target).model['DOMAIN'], 'fictional.example.org')
            self.assertNotIn('FICTIONAL_PRIVATE', self.output.getvalue())

    def test_explicit_draft_path_is_preserved_after_directory_choice(self):
        previous = self.interface.draft
        self.answers(['fictional.example.org', str(self.root)])
        self.interface.setup_step(0)
        self.assertEqual(self.interface.draft, previous)
        self.assertEqual(self.saved()['directory'], str(self.root))

    def test_legacy_draft_with_different_directory_migrates_on_save(self):
        with patch.object(Path, 'home', return_value=self.base):
            initial = self.base / 'initial'
            legacy = terminal_ui.UI(initial, view=TerminalView(plain=True))
            legacy.root = self.root
            legacy.explicit_draft = True
            legacy.model = {
                'DOMAIN': 'fictional.example.org',
                'GOOGLE_CLIENT_SECRET': 'FICTIONAL_PRIVATE',
            }
            legacy.step = 2
            legacy.save()
            resumed = terminal_ui.UI(initial, view=TerminalView(plain=True))
            self.assertEqual(resumed.root, self.root)
            resumed.save()
            final = terminal_ui.UI(self.root, view=TerminalView(plain=True))
            self.assertEqual(final.step, 2)
            self.assertEqual(final.model, legacy.model)
            self.assertFalse(legacy.draft.exists())

    def test_failed_draft_relocation_preserves_previous_file(self):
        with patch.object(Path, 'home', return_value=self.base):
            interface = terminal_ui.UI(self.base / 'initial', view=TerminalView(plain=True))
            interface.model = {'DOMAIN': 'fictional.example.org'}
            interface.save()
            previous = interface.draft
            before = previous.read_bytes()
            interface.root = self.root
            with patch.object(
                state_store, 'write_json', side_effect=OSError('fictional write failure')
            ):
                with self.assertRaises(OSError):
                    interface.save()
            self.assertEqual(interface.draft, previous)
            self.assertEqual(previous.read_bytes(), before)
            self.assertFalse(interface.draft_path(self.root).exists())

    def test_mixed_script_domain_is_shown_and_rejected_before_save(self):
        self.interface.model = {'DOMAIN': 'previous.example.org'}
        self.interface.save()
        original = self.interface.draft.read_bytes()
        values = iter(['ьшfictional.example.org', '', 'fictional.example.org'])

        def reader(prompt, secret):
            if 'Использовать именно' in prompt:
                self.assertEqual(self.interface.draft.read_bytes(), original)
            return next(values)

        self.interface.reader = reader
        self.interface.field('DOMAIN')
        self.assertEqual(self.saved()['values']['DOMAIN'], 'fictional.example.org')
        self.assertIn('ьшfictional.example.org', self.output.getvalue())
        self.assertIn('Адрес для DNS: xn--', self.output.getvalue())

    def test_unicode_and_punycode_domains_require_explicit_confirmation(self):
        for index, value in enumerate(
            ('пример.example.org', 'пример.example.org'.encode('idna').decode('ascii'))
        ):
            with self.subTest(value=value):
                self.interface = terminal_ui.UI(
                    self.root,
                    draft=self.base / ('idn-' + str(index) + '.json'),
                    view=TerminalView(plain=True),
                )
                self.answers([value, 'да'])
                self.interface.field('DOMAIN')
                self.assertEqual(
                    self.saved()['values']['DOMAIN'],
                    'пример.example.org'.encode('idna').decode('ascii'),
                )

    def test_old_draft_idn_is_confirmed_before_dns_and_confirmation_survives_resume(self):
        domain = 'пример.example.org'.encode('idna').decode('ascii')
        state_store.write_json(
            self.interface.draft,
            {
                'format': 1,
                'directory': str(self.root),
                'step': 2,
                'values': {'DOMAIN': domain},
                'backup': {},
            },
        )
        interface = terminal_ui.UI(
            self.root, draft=self.interface.draft, view=TerminalView(plain=True)
        )
        interface.reader = lambda *args: 'да'
        interface.check_domain_inputs()
        self.assertIn(domain, json.loads(interface.draft.read_text())['confirmed_domains'])
        resumed = terminal_ui.UI(self.root, draft=self.interface.draft)
        resumed.reader = lambda *args: self.fail('confirmed domain asked twice')
        resumed.check_domain_inputs()
        self.assertEqual(resumed.model['DOMAIN'], domain)
        self.assertFalse(self.root.exists())

    def test_old_draft_idn_can_be_corrected_without_running_any_backend(self):
        self.interface.model = {'DOMAIN': 'пример.example.org'.encode('idna').decode('ascii')}
        self.answers(['нет', 'fictional.example.org'])
        with patch.object(self.interface, 'run') as run:
            self.interface.check_domain_inputs()
        run.assert_not_called()
        self.assertEqual(self.saved()['values']['DOMAIN'], 'fictional.example.org')

    def test_malformed_punycode_and_invisible_characters_are_retried(self):
        self.answers(['xn--a.example.org', 'fi\u200bctional.example.org', 'fictional.example.org'])
        self.interface.field('DOMAIN')
        self.assertEqual(self.saved()['values']['DOMAIN'], 'fictional.example.org')
        self.assertNotIn('fi\u200bctional', self.output.getvalue())

    def test_invalid_fields_are_retried_without_saving_invalid_or_secret_values(self):
        for key, invalid, valid in (
            ('PUBLIC_IP', '127.0.0.1', '8.8.8.8'),
            ('PUBLIC_IP', '2001:db8::1', '8.8.8.8'),
            ('ACME_EMAIL', 'operator without address', 'operator@example.org'),
            ('INVITATION_LIFETIME_SECONDS', '0', '604800'),
            ('INVITATION_USAGE_LIMIT', '-1', '1'),
            ('POSTGRES_PORT', '65536', '5432'),
            (
                'SOCKS5_CONFIG',
                'https://FICTIONAL_PRIVATE@proxy.example.org',
                'socks5://proxy.example.org:1080',
            ),
            (
                'HOMEPAGE_URL',
                'https://user:FICTIONAL_PRIVATE@example.org',
                'https://external.example.org/',
            ),
            ('GOOGLE_CLIENT_SECRET', 'FICTIONAL_PRIVATE\nunsafe', 'FICTIONAL_OK'),
        ):
            with self.subTest(key=key):
                self.interface.model = {'DOMAIN': 'fictional.example.org'}
                self.interface.save()
                original = self.interface.draft.read_bytes()
                values = iter([invalid, valid])

                def reader(*args):
                    self.assertEqual(self.interface.draft.read_bytes(), original)
                    return next(values)

                self.interface.reader = reader
                self.interface.field(key)
                self.assertEqual(self.saved()['values'][key], valid)
        self.assertNotIn('FICTIONAL_PRIVATE', self.output.getvalue())

    def test_mas_domain_reserved_for_another_service_is_retried(self):
        self.interface.model = {'DOMAIN': 'fictional.example.org'}
        self.answers(['web.fictional.example.org', 'auth.example.org'])
        self.interface.field('MAS_DOMAIN')
        self.assertEqual(self.saved()['values']['MAS_DOMAIN'], 'auth.example.org')

    def test_installation_path_retries_file_traversal_links_and_unrelated_directory(self):
        file = self.base / 'file'
        file.write_text('FICTIONAL_KEEP')
        alias = self.base / 'alias'
        alias.symlink_to(self.base, target_is_directory=True)
        unrelated = self.base / 'unrelated'
        unrelated.mkdir()
        (unrelated / 'keep').write_text('FICTIONAL_KEEP')
        self.answers(
            [
                'fictional.example.org',
                'relative',
                '/',
                str(file),
                str(alias / 'target'),
                str(self.base) + '/../target',
                str(self.base / 'Новый сервер'),
                str(unrelated),
                str(self.root),
            ]
        )
        self.interface.setup_step(0)
        self.assertEqual(self.saved()['directory'], str(self.root))
        self.assertFalse(self.root.exists())
        self.assertEqual(file.read_text(), 'FICTIONAL_KEEP')
        self.assertEqual((unrelated / 'keep').read_text(), 'FICTIONAL_KEEP')

    def test_unknown_answers_do_not_silently_skip_oauth_or_backups(self):
        self.answers(['perhaps', ' ДА ', 'unexpected', 'отложить'])
        self.assertEqual(self.interface.answer('Provider?', ('да', 'нет'), 'нет'), 'да')
        self.assertEqual(
            self.interface.answer('Backup?', ('да', 'отложить'), 'отложить'), 'отложить'
        )

    def test_declining_previously_selected_oauth_and_backup_really_defers_them(self):
        self.interface.model = {
            'DOMAIN': 'fictional.example.org',
            'GOOGLE_CLIENT_ID': 'FICTIONAL_ID',
            'GOOGLE_CLIENT_SECRET': 'FICTIONAL_SECRET',
            'TELEGRAM_CLIENT_ID': 'FICTIONAL_ID',
            'TELEGRAM_CLIENT_SECRET': 'FICTIONAL_SECRET',
        }
        self.interface.backup = {'S3_BUCKET': 'fictional-backup'}
        self.answers(['нет', 'нет', 'отложить'])
        self.interface.setup_step(4)
        self.interface.setup_step(5)
        saved = self.saved()
        self.assertEqual(saved['backup'], {})
        for provider in ('GOOGLE', 'TELEGRAM'):
            self.assertEqual(saved['values'][provider + '_CLIENT_ID'], '')
            self.assertEqual(saved['values'][provider + '_CLIENT_SECRET'], '')
        self.assertFalse(self.root.exists())

    def test_optional_endpoint_and_calendar_can_be_cleared_after_saving(self):
        self.interface.model = {
            'DOMAIN': 'fictional.example.org',
            'S3_ENDPOINT_URL': 'https://storage.example.org',
        }
        self.answers(['/очистить'])
        self.interface.s3_field('S3_ENDPOINT_URL', self.interface.model)
        self.assertEqual(self.saved()['values']['S3_ENDPOINT_URL'], '')
        with patch.object(terminal_ui.subprocess, 'run') as run:
            self.assertEqual(self.interface.calendar_value('/очистить'), '')
        run.assert_not_called()

    def test_s3_form_checks_fields_before_probe_and_preserves_literal_secret(self):
        self.interface.model = {'DOMAIN': 'fictional.example.org', 'MEDIA_MODE': 's3'}
        self.answers(
            [
                'BAD',
                'fictional-media',
                'http://storage.example.org',
                '',
                'region with spaces',
                'fictional-1',
                '../media/',
                'media/',
                'FICTIONAL_ACCESS',
                'FICTIONAL_PRIVATE\nunsafe',
                "FICTIONAL_$literal'",
            ]
        )
        with patch(
            's3_diagnostics.diagnose',
            return_value={'passed': True, 'conclusion': 'ready', 'next_actions': []},
        ) as probe:
            self.interface.s3_form('media')
        probe.assert_called_once()
        saved = self.saved()['values']
        self.assertEqual(saved['S3_BUCKET'], 'fictional-media')
        self.assertEqual(saved['S3_SECRET_ACCESS_KEY'], "FICTIONAL_$literal'")
        self.assertEqual(self.interface.draft.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('FICTIONAL_PRIVATE', self.output.getvalue())
        self.assertNotIn('FICTIONAL_$literal', self.output.getvalue())
        self.assertFalse(self.root.exists())

    def test_failed_s3_probe_can_be_deferred_without_repeating_on_invalid_answer(self):
        self.interface.model = {'DOMAIN': 'fictional.example.org', 'MEDIA_MODE': 's3'}
        self.answers(
            [
                'fictional-media',
                '',
                'fictional-1',
                'media/',
                'FICTIONAL_ACCESS',
                'FICTIONAL_SECRET',
                'unexpected',
                '3',
            ]
        )
        with patch(
            's3_diagnostics.diagnose',
            return_value={'passed': False, 'conclusion': 'denied', 'next_actions': []},
        ) as probe:
            self.interface.s3_form('media')
        probe.assert_called_once()
        self.assertEqual(self.saved()['values']['MEDIA_MODE'], 'local')

    def test_backup_key_rejects_inside_installation_and_invalid_existing_key(self):
        bad = self.base / 'bad.key'
        bad.write_bytes(b'FICTIONAL')
        good = self.base / 'good.key'
        good.write_bytes(b'F' * 32)
        good.chmod(0o600)
        for value in (str(self.root / 'key'), str(bad), str(self.base / 'missing' / 'key')):
            with (
                self.subTest(value=value),
                self.assertRaises((ValueError, schema.InvalidConfiguration)),
            ):
                self.interface.backup_key(value)
        self.assertEqual(self.interface.backup_key(str(good)), str(good))
        self.assertEqual(
            self.interface.backup_key(str(self.base / 'new.key')), str(self.base / 'new.key')
        )
        self.assertFalse((self.base / 'new.key').exists())

    def test_calendar_checks_syntax_and_systemd_without_backup_dependencies(self):
        with patch.object(
            terminal_ui.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)
        ) as run:
            self.assertEqual(self.interface.calendar_value(''), '')
            run.assert_not_called()
            self.assertEqual(self.interface.calendar_value('daily'), 'daily')
            self.assertEqual(run.call_args.args[0], ['systemd-analyze', 'calendar', 'daily'])
            self.assertEqual(run.call_args.kwargs['timeout'], 10)
        with patch.object(
            terminal_ui.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)
        ):
            with self.assertRaisesRegex(ValueError, 'Неверное расписание'):
                self.interface.calendar_value('not a calendar')
        with patch.object(
            terminal_ui.subprocess, 'run', side_effect=subprocess.TimeoutExpired([], 10)
        ):
            with self.assertRaisesRegex(ValueError, 'Не удалось проверить'):
                self.interface.calendar_value('daily')
        for value in ('daily%H', 'daily\\x', 'x' * 129):
            with self.subTest(value=value), patch.object(terminal_ui.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    self.interface.calendar_value(value)
                run.assert_not_called()

    def test_dns_failure_back_returns_to_domain_and_saves_position(self):
        self.interface.model = {'DOMAIN': 'wrong.example.org'}
        self.interface.step = 2
        self.answers(
            ['', '', '', 'назад', 'fictional.example.org', str(self.root), 'сохранить и выйти']
        )
        with (
            patch.object(preflight, 'resolve', return_value=set()),
            patch.object(
                preflight, 'system_check', side_effect=RuntimeError('Fictional system check retry')
            ),
            patch.object(self.interface, 'run') as run,
        ):
            self.assertEqual(self.interface.wizard(), 0)
        run.assert_not_called()
        saved = self.saved()
        self.assertEqual(saved['step'], 1)
        self.assertEqual(saved['values']['DOMAIN'], 'fictional.example.org')
        self.assertNotIn('sudo misbot status', self.output.getvalue())
        self.assertFalse(self.root.exists())
        resumed = terminal_ui.UI(directory=self.root, draft=self.interface.draft)
        self.assertEqual(resumed.step, 1)
        self.assertEqual(resumed.model['DOMAIN'], 'fictional.example.org')

    def test_cancel_and_save_after_failure_keep_draft_without_creating_installation(self):
        for answer in ('отмена', 'сохранить и выйти'):
            with self.subTest(answer=answer):
                self.interface.step = 1
                self.interface.model = {'DOMAIN': 'fictional.example.org'}
                self.answers([answer])
                with patch.object(
                    preflight, 'system_check', side_effect=RuntimeError('Fictional check failure')
                ):
                    self.assertEqual(self.interface.wizard(), 0)
                self.assertEqual(self.saved()['step'], 1)
                self.assertFalse(self.root.exists())

    def test_unknown_failure_answer_does_not_retry_the_step(self):
        self.interface.step = 1
        self.interface.model = {'DOMAIN': 'fictional.example.org'}
        with patch.object(
            self.interface,
            'setup_step',
            side_effect=[RuntimeError('Fictional failure'), terminal_ui.SaveExit()],
        ) as step:
            answers = iter(['FICTIONAL_UNEXPECTED', 'повторить'])

            def reader(*args):
                self.assertEqual(step.call_count, 1)
                return next(answers)

            self.interface.reader = reader
            self.assertEqual(self.interface.wizard(), 0)
        self.assertEqual(step.call_count, 2)
        self.assertEqual(self.saved()['step'], 1)
        self.assertNotIn('FICTIONAL_UNEXPECTED', self.output.getvalue())

    def test_back_at_every_wizard_step_saves_editable_previous_step(self):
        for index in range(10):
            with self.subTest(step=index):
                self.interface.step = index
                self.interface.model = {'DOMAIN': 'fictional.example.org'}
                with (
                    patch.object(
                        self.interface,
                        'setup_step',
                        side_effect=[terminal_ui.Back(), terminal_ui.SaveExit()],
                    ) as step,
                    patch.object(self.interface, 'run') as run,
                ):
                    self.assertEqual(self.interface.wizard(), 0)
                previous = 0 if index == 2 else max(0, index - 1)
                self.assertEqual([item.args[0] for item in step.call_args_list], [index, previous])
                self.assertEqual(self.saved()['step'], previous)
                run.assert_not_called()
                self.assertFalse(self.root.exists())

    def test_cancel_and_save_at_every_wizard_step_preserve_draft_and_data(self):
        for index, interruption in itertools.product(
            range(10), (terminal_ui.Cancel, terminal_ui.SaveExit)
        ):
            with self.subTest(step=index, interruption=interruption.__name__):
                self.interface.step = index
                self.interface.model = {'DOMAIN': 'fictional.example.org'}
                with (
                    patch.object(self.interface, 'setup_step', side_effect=interruption()),
                    patch.object(self.interface, 'run') as run,
                ):
                    expected = 130 if interruption is terminal_ui.Cancel else 0
                    self.assertEqual(self.interface.wizard(), expected)
                self.assertEqual(self.saved()['step'], index)
                self.assertEqual(self.saved()['values'], self.interface.model)
                run.assert_not_called()
                self.assertFalse(self.root.exists())

    def test_invalid_restore_arguments_are_retried_before_confirmation_or_backend(self):
        self.answers(
            [
                str(self.root),
                '/root/fictional.tar.gz',
                'wrong checksum',
                'a' * 64,
                '127.0.0.1',
                '8.8.8.8',
                'нет',
            ]
        )
        import ui_actions

        with patch.object(self.interface, 'run') as run:
            self.assertEqual(
                self.interface.execute(ui_actions.Action('backup restore', 'Restore')), 0
            )
        run.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_operation_arguments_reject_invalid_values_without_echoing_them(self):
        for key, value, backend in (
            ('sha256', 'FICTIONAL_PRIVATE', 'restore'),
            ('public-ip', 'FICTIONAL_PRIVATE', 'restore'),
            ('server', 'https://FICTIONAL_PRIVATE@example.org', 'federation-add'),
            ('purpose', 'FICTIONAL_PRIVATE', 's3-check'),
            ('confirm', 'FICTIONAL_PRIVATE', 'restore-activate'),
            ('request', 'FICTIONAL_PRIVATE', 'federation-retry'),
            ('object', '../FICTIONAL_PRIVATE', 'backup-fetch'),
            ('input', '/root/FICTIONAL_PRIVATE/../password.json', 'user-password'),
        ):
            with (
                self.subTest(key=key),
                self.assertRaises((ValueError, schema.InvalidConfiguration)) as error,
            ):
                self.interface.operation_argument(key, value, backend)
            self.assertNotIn('FICTIONAL_PRIVATE', str(error.exception))

    def test_new_local_installation_runs_shared_backend_in_order_and_repeat_keeps_identity(self):
        self.answers(
            [
                'fictional.example.org',
                str(self.root),
                '',
                '',
                '8.8.8.8',
                '',
                'нет',
                'нет',
                'отложить',
                'применить',
                '0',
            ]
        )
        calls = []

        def run(path, **args):
            calls.append(path)
            if path == 'config init':
                self.root.mkdir(mode=0o700)
                state_store.write_json(
                    self.root / 'installation.json',
                    {
                        'domain': args['domain'],
                        'config_format_version': 2,
                    },
                )
                configuration.write_model(self.root, {'DOMAIN': args['domain']})
                (self.root / 'secrets').mkdir(mode=0o700)
                state_store.write_text(self.root / 'secrets/identity', 'FICTIONAL_IDENTITY')
            if path == 'start':
                state_store.write_json(
                    self.root / 'installation.json',
                    {
                        'domain': 'fictional.example.org',
                        'config_format_version': 2,
                        'databases_initialized': True,
                    },
                )
            return 0

        with (
            patch('shutil.which', return_value='/usr/bin/fictional'),
            patch.object(preflight, 'system_check'),
            patch.object(preflight, 'dns_check'),
            patch.object(self.interface, 'wait_dns'),
            patch.object(preflight, 'caa_check'),
            patch.object(preflight, 'network_check'),
            patch.object(self.interface, 'run', side_effect=run),
            patch(
                'registration.manage',
                return_value={
                    'state': 'valid',
                    'expires_at': '2030-01-01',
                    'token': 'FICTIONAL_HIDDEN_TOKEN',
                },
            ),
        ):
            self.assertEqual(self.interface.wizard(), 0)
        self.assertEqual(calls, ['config init', 'start', 'check'])
        self.assertNotIn('FICTIONAL_HIDDEN_TOKEN', self.output.getvalue())
        self.assertEqual(configuration.load(self.root, True)['DOMAIN'], 'fictional.example.org')
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        resumed = terminal_ui.UI(self.root, draft=self.interface.draft)
        with patch.object(resumed, 'run') as repeat:
            self.assertEqual(resumed.wizard(), 0)
        repeat.assert_not_called()
        self.assertEqual(
            before, {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        )

    def test_real_telegram_check_with_formatted_view_survives_draft_resume_summary_apply(self):
        from terminal_views import Console

        if Console is None:
            self.skipTest('Rich is optional')
        from rich.console import Console as RichConsole

        view = TerminalView(
            console=RichConsole(file=self.output, force_terminal=True, width=72, height=20)
        )
        interface = terminal_ui.UI(self.root, draft=self.base / 'telegram.json', view=view)
        interface.model = schema.validate(
            {'DOMAIN': 'fictional.example.org', 'PUBLIC_IP': '8.8.8.8'}
        )
        interface.dns_verified = interface.dns_values()
        interface.step = 4
        answers = iter(['нет', 'да', 'FICTIONAL_ID', 'FICTIONAL_SECRET', ''])
        interface.reader = lambda *_: next(answers)
        response = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    'issuer': 'https://oauth.telegram.org',
                    'token_endpoint': 'https://oauth.telegram.org/token',
                }
            ),
            '',
        )
        with (
            patch.dict(os.environ, {'TERM': 'xterm'}, clear=True),
            view.screen(),
            patch('telegram_login_check.subprocess.run', return_value=response) as http,
        ):
            interface.setup_step(4)
            self.assertEqual(http.call_count, 3)
            self.assertNotIn('FICTIONAL_SECRET', str(http.call_args_list))
            saved = interface.draft.read_bytes()
            resumed = terminal_ui.UI(self.root, draft=interface.draft, view=view)
            self.assertEqual(resumed.model['TELEGRAM_CLIENT_SECRET'], 'FICTIONAL_SECRET')
            self.assertEqual(resumed.draft.read_bytes(), saved)
            answers = iter(['нет', 'да', '', '', '', 'отложить', 'применить'])
            resumed.reader = lambda *_: next(answers)
            resumed.setup_step(4)
            resumed.setup_step(5)
            resumed.setup_step(6)
            calls = []

            def run(path, **args):
                calls.append(path)
                if path == 'config init':
                    self.root.mkdir(mode=0o700)
                    state_store.write_json(
                        self.root / 'installation.json',
                        {'domain': args['domain'], 'config_format_version': 2},
                    )
                return 0

            with patch.object(resumed, 'run', side_effect=run):
                resumed.setup_step(7)
            self.assertEqual(calls, ['config init', 'start'])
            self.assertEqual(
                configuration.load(self.root, True)['TELEGRAM_CLIENT_SECRET'], 'FICTIONAL_SECRET'
            )
        self.assertNotIn('FICTIONAL_SECRET', self.output.getvalue())

    def test_wizard_media_oauth_and_backup_combinations_publish_only_valid_settings(self):
        combinations = itertools.product(
            ('local', 's3'),
            ((), ('GOOGLE',), ('TELEGRAM',), ('GOOGLE', 'TELEGRAM')),
            ('defer', 'manual', 'schedule'),
        )
        for index, (media, providers, backup) in enumerate(combinations):
            with self.subTest(media=media, providers=providers, backup=backup):
                root = self.base / str(index) / 'target'
                key = self.base / ('backup-' + str(index) + '.key')
                interface = terminal_ui.UI(
                    root,
                    draft=self.base / ('draft-' + str(index) + '.json'),
                    view=TerminalView(plain=True),
                )
                answers = ['fictional.example.org', str(root), '', '', '8.8.8.8', media]
                profile = [
                    'fictional-media',
                    '',
                    'fictional-1',
                    'media/',
                    'FICTIONAL_ACCESS',
                    'FICTIONAL_SECRET',
                ]
                if media == 's3':
                    answers.extend(profile)
                for provider in ('GOOGLE', 'TELEGRAM'):
                    answers.append('да' if provider in providers else 'нет')
                    if provider in providers:
                        answers.extend(['FICTIONAL_ID', 'FICTIONAL_SECRET'])
                        if provider == 'TELEGRAM':
                            answers.append('socks5://proxy.example.org:1080')
                answers.append('отложить' if backup == 'defer' else 'да')
                if backup != 'defer':
                    answers.extend(
                        profile[:3]
                        + ['backups/']
                        + profile[4:]
                        + [str(key), 'daily' if backup == 'schedule' else '']
                    )
                answers.extend(['применить', '0'])
                entries = iter(answers)
                interface.reader = lambda *args: next(entries)
                calls = []

                def run(path, **args):
                    calls.append(path)
                    if path == 'config init':
                        root.mkdir(parents=True, mode=0o700)
                        state_store.write_json(
                            root / 'installation.json',
                            {
                                'domain': args['domain'],
                                'config_format_version': 2,
                            },
                        )
                        configuration.write_model(root, {'DOMAIN': args['domain']})
                    if path == 'start':
                        state_store.write_json(
                            root / 'installation.json',
                            {
                                'domain': 'fictional.example.org',
                                'config_format_version': 2,
                                'databases_initialized': True,
                            },
                        )
                    if path == 'backup key create':
                        Path(args['key']).write_bytes(b'F' * 32)
                        Path(args['key']).chmod(0o600)
                    return 0

                with (
                    patch('shutil.which', return_value='/usr/bin/fictional'),
                    patch(
                        'telegram_login_check.subprocess.run',
                        return_value=subprocess.CompletedProcess(
                            [],
                            0,
                            json.dumps(
                                {
                                    'issuer': 'https://oauth.telegram.org',
                                    'token_endpoint': 'https://oauth.telegram.org/token',
                                }
                            ),
                            '',
                        ),
                    ),
                    patch.object(preflight, 'system_check'),
                    patch.object(preflight, 'dns_check'),
                    patch.object(interface, 'wait_dns'),
                    patch.object(preflight, 'caa_check'),
                    patch.object(preflight, 'network_check'),
                    patch.object(interface, 'run', side_effect=run),
                    patch.object(interface, 'calendar_value', side_effect=lambda value: value),
                    patch(
                        's3_diagnostics.diagnose',
                        return_value={'passed': True, 'conclusion': 'ready', 'next_actions': []},
                    ),
                    patch(
                        'registration.manage',
                        return_value={'state': 'valid', 'expires_at': '2030-01-01'},
                    ),
                ):
                    self.assertEqual(interface.wizard(), 0)
                model = configuration.load(root, True)
                self.assertEqual(model['MEDIA_MODE'], media)
                self.assertEqual(model['MAX_UPLOAD_SIZE_MIB'], '256')
                self.assertNotIn('MAX_UPLOAD_SIZE_MIB —', self.output.getvalue())
                for provider in ('GOOGLE', 'TELEGRAM'):
                    self.assertEqual(
                        bool(model[provider + '_CLIENT_SECRET']), provider in providers
                    )
                self.assertEqual(calls[:3], ['config init', 'start', 'check'])
                self.assertEqual(calls.count('start'), 1)
                if backup == 'defer':
                    self.assertEqual(calls[3:], [])
                    self.assertFalse((root / 'settings/backup.env').exists())
                else:
                    expected = (
                        'backup schedule external'
                        if backup == 'schedule'
                        else 'backup external create'
                    )
                    self.assertEqual(calls[3:], ['backup key create', expected])
                    self.assertEqual(configuration.backup_profile(root)['S3_PREFIX'], 'backups/')
                self.assertNotIn('FICTIONAL_SECRET', self.output.getvalue())

    def test_failed_init_start_and_check_do_not_claim_completion_or_repeat_init(self):
        for failed in ('config init', 'start', 'check'):
            with self.subTest(failed=failed):
                root = self.base / failed.replace(' ', '-')
                interface = terminal_ui.UI(
                    root, draft=self.base / (root.name + '.json'), view=TerminalView(plain=True)
                )
                interface.model = schema.validate(
                    {'DOMAIN': 'fictional.example.org', 'PUBLIC_IP': '8.8.8.8'}
                )
                interface.step = 7
                interface.reader = lambda *args: 'сохранить и выйти'
                calls = []

                def run(path, **args):
                    calls.append(path)
                    if path == failed:
                        return 130
                    if path == 'config init':
                        root.mkdir(mode=0o700)
                        state_store.write_json(
                            root / 'installation.json',
                            {'domain': 'fictional.example.org', 'config_format_version': 2},
                        )
                    if path == 'start':
                        state_store.write_json(
                            root / 'installation.json',
                            {
                                'domain': 'fictional.example.org',
                                'config_format_version': 2,
                                'databases_initialized': True,
                            },
                        )
                    return 0

                with (
                    patch.object(interface, 'run', side_effect=run),
                    patch.object(interface, 'finish') as finish,
                ):
                    self.assertEqual(interface.wizard(), 0)
                finish.assert_not_called()
                self.assertEqual(calls.count('config init'), 1)
                saved = json.loads(interface.draft.read_text())
                self.assertEqual(saved['step'], 8 if failed == 'check' else 7)
                if failed == 'config init':
                    self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
