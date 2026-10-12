"""Exercise actual child output and animation together, including silent waits."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import terminal_ui
import terminal_views
from terminal_process import read_output, present_output, MAX_LINE


class OutputTests(unittest.TestCase):
    def test_back_from_summary_field_returns_to_summary(self):
        answers = iter(['1', 'назад', ''])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'target'
            interface = terminal_ui.UI(
                root, draft=Path(folder) / 'draft.json', reader=lambda *_: next(answers)
            )
            interface.model = {'DOMAIN': 'example.org'}
            with (
                patch('sys.stdout', new=io.StringIO()),
                patch.object(interface, 'setup_step') as step,
                self.assertRaises(terminal_ui.SaveExit),
            ):
                interface.setup_summary()
            step.assert_not_called()
            self.assertEqual(interface.model['DOMAIN'], 'example.org')

    def test_changed_domain_in_summary_is_checked_before_apply(self):
        answers = iter(['1', 'new.example.org', 'применить'])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'target'
            interface = terminal_ui.UI(
                root, draft=Path(folder) / 'draft.json', reader=lambda *_: next(answers)
            )
            interface.model = {'DOMAIN': 'example.org'}
            interface.dns_verified = interface.dns_values()
            with (
                patch('sys.stdout', new=io.StringIO()),
                patch.object(interface, 'check_dns_settings') as check,
                patch.object(interface, 'run') as run,
            ):
                interface.setup_summary()
            check.assert_called_once_with()
            run.assert_not_called()
            self.assertEqual(interface.model['DOMAIN'], 'new.example.org')
            self.assertFalse(root.exists())

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_large_invitation_numbers_are_not_truncated(self):
        output = io.StringIO()
        console = terminal_views.Console(file=output, width=80)
        view = terminal_views.TerminalView(console=console)
        view.options('Приглашения', [('1000', 'Приглашение', '')])
        self.assertIn('1000', output.getvalue())

    def test_invitation_list_and_errors_have_readable_safe_output(self):
        messages = present_output(
            'users invitation list',
            json.dumps(
                {
                    'invitations': [
                        {
                            'id': 'INV-1',
                            'issuer': '@example:example.org',
                            'state': 'used',
                            'expires_at': '2030-01-01',
                            'token': 'FICTIONAL_NOT_FOR_UI',
                        }
                    ]
                }
            ),
        )
        self.assertIn('использовано', messages[0])
        self.assertIn('@example:example.org', messages[0])
        self.assertNotIn('FICTIONAL_NOT_FOR_UI', str(messages))
        messages = present_output(
            'federation operations',
            'Another installer operation is running for this directory. Technical entry: ...',
        )
        self.assertIn('другая операция', messages[0])
        self.assertIn('Меню:', messages[1])

    def test_each_menu_has_unique_actions_and_created_bot_has_no_rename(self):
        import ui_actions
        import state_store

        for _, actions in ui_actions.MENUS.values():
            self.assertEqual(len(actions), len({action.path for action in actions}))
            self.assertEqual(len(actions), len({action.title for action in actions}))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(
                root / 'installation.json',
                {'domain': 'example.org', 'federation_provisioned': True},
            )
            with patch('configuration.load', return_value={}):
                interface = terminal_ui.UI(root, draft=root / 'draft.json')
            self.assertFalse(interface.supported('admin bot set'))
            self.assertTrue(interface.supported('admin bot show'))

    def test_pending_installation_displays_saved_bot_name_and_can_return_to_default(self):
        import configuration
        import state_store

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'target'
            root.mkdir(mode=0o700)
            (root / 'config').mkdir()
            state_store.write_json(
                root / 'installation.json',
                {
                    'domain': 'example.org',
                    'databases_initialized': False,
                },
            )
            state_store.write_json(
                root / 'config/admin-panel-identity.json',
                {
                    'user_id': '@control-panel:example.org',
                },
            )
            with patch.object(configuration, 'load', return_value={'DOMAIN': 'example.org'}):
                interface = terminal_ui.UI(root, draft=Path(folder) / 'draft.json')
            self.assertEqual(interface.admin_tag, 'control-panel')
            interface.admin_tag = 'admin-panel'
            with (
                patch.object(configuration, 'write_model'),
                patch.object(interface, 'run', return_value=0) as run,
            ):
                interface.setup_step(7)
            self.assertEqual(run.call_args_list[0].args, ('admin bot set',))
            self.assertEqual(run.call_args_list[0].kwargs, {'tag': 'admin-panel'})
            self.assertEqual(run.call_args_list[1].args, ('start',))

    def test_streams_partial_unicode_and_bounds_unterminated_lines(self):
        script = (
            "import os,time; os.write(1,'Привет'.encode()[:3]); time.sleep(.05); "
            "os.write(1,'Привет'.encode()[3:]+b'\\n'); "
            "os.write(1,b'x'*20000+b'\\nlast'); raise SystemExit(23)"
        )
        child = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE)
        lines = []
        try:
            self.assertEqual(read_output(child, lines.append), 23)
        finally:
            child.stdout.close()
        self.assertEqual(lines[0], 'Привет')
        self.assertEqual(lines[-1], 'last')
        self.assertEqual(''.join(lines[1:-1]), 'x' * 20000)
        self.assertTrue(all(len(line) <= MAX_LINE for line in lines))

    def test_complete_large_invitation_record_is_presented_without_raw_json(self):
        items = [
            {'id': 'INV-' + str(i), 'issuer': '@example:example.org', 'state': 'valid'}
            for i in range(100)
        ]
        script = 'print(' + repr(json.dumps({'invitations': items})) + ')'
        child = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE)
        messages = []
        try:
            read_output(
                child, lambda line: messages.extend(present_output('users invitation list', line))
            )
        finally:
            child.stdout.close()
        self.assertEqual(len(messages), 100)
        self.assertTrue(all('действует' in message for message in messages))
        self.assertFalse(any('"invitations"' in message for message in messages))

    def test_detached_child_does_not_hold_completed_command_output_forever(self):
        script = "import subprocess; subprocess.Popen(['sleep','2']); raise SystemExit(23)"
        child = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE)
        started = time.monotonic()
        try:
            self.assertEqual(read_output(child, lambda line: None), 23)
        finally:
            child.stdout.close()
        self.assertLess(time.monotonic() - started, 1.9)

    def test_plain_backend_output_is_visible_once_and_exit_code_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            (source / 'misbot.py').write_text(
                "print('Настройки панели: @admin-panel:example.org'); raise SystemExit(23)"
            )
            interface = terminal_ui.UI(
                source=source,
                draft=source / 'draft.json',
                view=terminal_views.TerminalView(plain=True),
            )
            with patch('sys.stdout', new=io.StringIO()) as output:
                self.assertEqual(interface.run('admin bot show'), 23)
            self.assertEqual(output.getvalue().count('Настройки панели:'), 1)

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_long_final_error_remains_visible_in_small_terminal(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
            console = terminal_views.Console(file=output, force_terminal=True, width=40, height=18)
            view = terminal_views.TerminalView(console=console)
            with view.screen():
                with view.busy('Операция') as progress:
                    progress.log('Причина отказа: ' + 'x' * 3000)
                frame = view.snapshot()
                rendered = '\n'.join(str(part) for part in frame)
                self.assertIn('Причина отказа:', rendered)
                self.assertIn('сокращена', rendered)
                self.assertLessEqual(view.used_lines, 18)

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_animation_continues_without_backend_output_and_logs_follow_heading(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
            console = terminal_views.Console(
                file=output, force_terminal=True, width=80, height=24, no_color=False
            )
        view = terminal_views.TerminalView(console=console)
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), view.screen():
            with view.busy('Загрузка сервера') as progress:
                progress.log('Служба запускается')
                first = len(output.getvalue())
                time.sleep(0.4)
                self.assertGreater(len(output.getvalue()), first)
                for index in range(200):
                    progress.log('Шаг ' + str(index))
                frame = console.render_lines(progress, pad=False)
                self.assertLessEqual(len(frame), 24)
                rendered = '\n'.join(''.join(segment.text for segment in line) for line in frame)
                self.assertLess(rendered.index('Загрузка сервера'), rendered.index('Шаг 199'))
                self.assertNotIn('Шаг 0\n', rendered)
            self.assertLessEqual(view.used_lines, 24)
        self.assertIn('\x1b[?25h', output.getvalue())


if __name__ == '__main__':
    unittest.main()
