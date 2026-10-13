"""Regressions for operation results, removal scope and private interactive S3."""

import io
import json
import os
import errno
import fcntl
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import remove_server
import state_store
import terminal_process
import terminal_ui as ui
from terminal_views import TerminalView
import terminal_views
import ui_actions
import wizard_state

PROFILE = {
    'S3_BUCKET': 'fictional-bucket',
    'S3_ENDPOINT_URL': 'https://s3.example.org',
    'S3_REGION': 'example-1',
    'S3_PREFIX': 'example-media/',
    'S3_ACCESS_KEY_ID': 'FICTIONAL_ACCESS',
    'S3_SECRET_ACCESS_KEY': 'FICTIONAL_SECRET',
}


class OperationTests(unittest.TestCase):
    def interface(self, root, reader=lambda *_: '', **kwargs):
        return ui.UI(
            root,
            draft=root.parent / 'draft.json',
            reader=reader,
            view=TerminalView(plain=True),
            **kwargs,
        )

    def test_busy_error_inside_json_gives_clear_guidance(self):
        messages = terminal_process.present_output(
            'start',
            json.dumps(
                {
                    'check': 'failed',
                    'error_type': 'RuntimeError',
                    'message': 'Another installer operation is running for this directory. private details',
                }
            ),
        )
        self.assertIn('другая операция', messages[0])
        self.assertNotIn('private details', '\n'.join(messages))

    def test_unconfirmed_records_do_not_assert_success_and_keep_followup(self):
        messages = terminal_process.present_output(
            'backup create',
            json.dumps(
                {
                    'check': 'unknown_backend_event',
                    'pending': True,
                    'next_action': 'sudo misbot backup recover',
                }
            ),
        )
        shown = '\n'.join(messages)
        self.assertNotIn('Выполнено', shown)
        self.assertNotIn('Подтверждено', shown)
        self.assertIn('требует продолжения', shown)
        self.assertIn('sudo misbot backup recover', shown)
        self.assertNotIn('внешнего MAS', shown)

    def test_external_mas_backup_and_restore_warning_remains_visible(self):
        for authentication in ('external-MAS-not-included', 'external-MAS-not-restored'):
            with self.subTest(authentication=authentication):
                shown = '\n'.join(
                    terminal_process.present_output(
                        'backup create',
                        json.dumps(
                            {
                                'check': 'consistent_backup',
                                'authentication': authentication,
                                'next_action': 'Keep the corresponding external snapshot',
                            }
                        ),
                    )
                )
                self.assertIn('Внимание:', shown)
                self.assertIn('пользователи, устройства, ключи', shown)
                self.assertIn('Keep the corresponding external snapshot', shown)

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_backup_result_page_replays_archive_warning_and_final_record_after_clear(self):
        from rich.console import Group

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / 'target'
            root.mkdir(mode=0o700)
            state_store.write_json(root / 'installation.json', {'domain': 'example.org'})
            records = [
                {
                    'check': 'consistent_backup',
                    'archive': '/opt/example/backups/matrix.tar.gz',
                    'sha256': 'a' * 64,
                    'authentication': 'external-MAS-not-included',
                    'next_action': 'Сохраните внешний MAS отдельно.',
                },
                {
                    'check': 'backup_finished',
                    'passed': True,
                    'next_action': 'sudo misbot backup verify',
                    'sdk_body': 'FICTIONAL_RAW_SDK_SECRET',
                },
            ]
            (base / 'misbot.py').write_text(
                'print(' + repr('\n'.join(json.dumps(record) for record in records)) + ')'
            )
            output = io.StringIO()
            console = terminal_views.Console(file=output, force_terminal=True, width=100, height=40)
            view = TerminalView(console=console)
            answers = iter(['да', ''])
            with patch('configuration.load', return_value={'DOMAIN': 'example.org'}):
                interface = ui.UI(
                    root,
                    source=base,
                    draft=base / 'draft.json',
                    view=view,
                    reader=lambda *_: next(answers),
                )
            with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), view.screen():
                self.assertEqual(
                    interface.execute(ui_actions.Action('backup create', 'Создать копию')), 0
                )
                frame = '\n'.join(
                    ''.join(segment.text for segment in line)
                    for line in console.render_lines(Group(*view.snapshot()), pad=False)
                )
            self.assertIn('Архив копии: /opt/example/backups/matrix.tar.gz', frame)
            self.assertIn('Внимание: пользователи, устройства, ключи', frame)
            self.assertIn('sudo misbot backup verify', frame)
            self.assertIn('Подтверждено:', frame)
            self.assertNotIn('FICTIONAL_RAW_SDK_SECRET', frame)
            self.assertEqual(interface.operation_records, [])

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_failure_reason_remains_on_final_operation_page(self):
        from rich.console import Group

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / 'target'
            root.mkdir(mode=0o700)
            state_store.write_json(root / 'installation.json', {'domain': 'example.org'})
            record = {
                'check': 'failed',
                'error_type': 'RuntimeError',
                'message': 'Another installer operation is running for this directory. private owner details',
            }
            (base / 'misbot.py').write_text(
                'print(' + repr(json.dumps(record)) + '); raise SystemExit(1)'
            )
            output = io.StringIO()
            console = terminal_views.Console(file=output, force_terminal=True, width=100, height=40)
            view = TerminalView(console=console)
            answers = iter(['да', ''])
            with patch('configuration.load', return_value={'DOMAIN': 'example.org'}):
                interface = ui.UI(
                    root,
                    source=base,
                    draft=base / 'draft.json',
                    view=view,
                    reader=lambda *_: next(answers),
                )
            with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), view.screen():
                self.assertEqual(
                    interface.execute(ui_actions.Action('start', 'Запустить сервер')), 1
                )
                frame = '\n'.join(
                    ''.join(segment.text for segment in line)
                    for line in console.render_lines(Group(*view.snapshot()), pad=False)
                )
            self.assertIn('другая операция', frame)
            self.assertIn('Операция не завершена:', frame)
            self.assertNotIn('private owner details', frame)

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_plain_admin_guidance_remains_on_final_page_without_sdk_dump(self):
        from rich.console import Group

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / 'target'
            root.mkdir(mode=0o700)
            state_store.write_json(root / 'installation.json', {'domain': 'example.org'})
            (base / 'misbot.py').write_text(
                "print('Проверьте имя зарегистрированного аккаунта.')\n"
                'print(\'[{"token":"FICTIONAL_RAW_SDK_SECRET"}]\')\n'
                "print('Traceback FICTIONAL_INTERNAL_TRACE')\n"
                "print('Authorization: Bearer FICTIONAL_RAW_SDK_SECRET')\n"
                'raise SystemExit(1)\n'
            )
            console = terminal_views.Console(
                file=io.StringIO(), force_terminal=True, width=100, height=40
            )
            view = TerminalView(console=console)
            answers = iter(['@personal:example.org', 'да', ''])
            frames = []

            def reader(prompt, *_):
                if prompt == 'Enter — вернуться к меню: ':
                    frames.append(
                        '\n'.join(
                            ''.join(segment.text for segment in line)
                            for line in console.render_lines(Group(*view.snapshot()), pad=False)
                        )
                    )
                return next(answers)

            with patch('configuration.load', return_value={'DOMAIN': 'example.org'}):
                interface = ui.UI(
                    root, source=base, draft=base / 'draft.json', view=view, reader=reader
                )
            with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), view.screen():
                self.assertEqual(
                    interface.execute(ui_actions.Action('admin access add', 'Добавить')), 1
                )
            self.assertEqual(len(frames), 1)
            self.assertIn('Проверьте имя зарегистрированного аккаунта.', frames[0])
            self.assertNotIn('FICTIONAL_RAW_SDK_SECRET', frames[0])
            self.assertNotIn('FICTIONAL_INTERNAL_TRACE', frames[0])
            self.assertNotIn('FICTIONAL_RAW_SDK_SECRET', repr(interface.result_messages))
            self.assertEqual(interface.operation_records, [])

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_restore_continuation_is_visible_before_final_acknowledgement(self):
        from rich.console import Group
        import shlex

        for path in ('backup restore', 'backup restore-local'):
            for mode in ('local', 'external'):
                with self.subTest(path=path, mode=mode), tempfile.TemporaryDirectory() as folder:
                    base = Path(folder)
                    root = base / 'selected'
                    target = base / 'restored target'
                    root.mkdir(mode=0o700)
                    state_store.write_json(root / 'installation.json', {'domain': 'example.org'})
                    (base / 'misbot.py').write_text(
                        'import json,os,sys\nfrom pathlib import Path\n'
                        "root=Path(sys.argv[sys.argv.index('--directory')+1]);root.mkdir(mode=0o700)\n"
                        "metadata=root/'installation.json'\n"
                        'metadata.write_text(json.dumps(' + repr({'mas_mode': mode}) + '))\n'
                        'os.chmod(metadata,0o600)\n'
                        'print(json.dumps({"check":"restore_staged"}))\n'
                    )
                    output = io.StringIO()
                    console = terminal_views.Console(
                        file=output, force_terminal=True, width=240, height=40
                    )
                    view = TerminalView(console=console)
                    answers = iter(
                        [str(target), '/opt/example/backup.tar.gz', 'a' * 64, '8.8.8.8', 'да', '']
                    )
                    final_frames = []

                    def reader(prompt, *_):
                        if prompt == 'Enter — вернуться к меню: ':
                            final_frames.append(
                                '\n'.join(
                                    ''.join(segment.text for segment in line)
                                    for line in console.render_lines(
                                        Group(*view.snapshot()), pad=False
                                    )
                                )
                            )
                        return next(answers)

                    with patch('configuration.load', return_value={'DOMAIN': 'example.org'}):
                        interface = ui.UI(
                            root, source=base, draft=base / 'draft.json', view=view, reader=reader
                        )
                    with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), view.screen():
                        self.assertEqual(
                            interface.execute(ui_actions.Action(path, 'Восстановить')), 0
                        )
                    self.assertEqual(len(final_frames), 1)
                    frame = final_frames[0]
                    expected = (
                        'sudo misbot --directory '
                        + shlex.quote(str(target))
                        + ' backup activate --confirm source-stopped'
                    )
                    self.assertIn(expected, frame)
                    self.assertIn('остановите источник и переключите DNS', frame)
                    self.assertEqual(
                        '--external-mas-confirm preserved-state' in frame, mode == 'external'
                    )
                    self.assertEqual(interface.root, root)

    def test_external_mas_activation_preserves_both_confirmations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'target'
            root.mkdir(mode=0o700)
            state_store.write_json(
                root / 'installation.json',
                {
                    'domain': 'example.org',
                    'restore_pending': True,
                    'mas_mode': 'external',
                },
            )
            answers = iter(['source-stopped', 'wrong', 'preserved-state', 'да'])
            with patch('configuration.load', return_value={}):
                interface = self.interface(root, reader=lambda *_: next(answers))
            with (
                patch.object(interface, 'run', return_value=0) as run,
                patch('sys.stdout', new=io.StringIO()) as output,
            ):
                self.assertEqual(
                    interface.execute(ui_actions.Action('backup activate', 'Activate')), 0
                )
            run.assert_called_once_with(
                'backup activate', confirm='source-stopped', external_mas_confirm='preserved-state'
            )
            self.assertIn('не содержит пользователей', output.getvalue())

    def test_status_shows_stopped_services_and_incomplete_start_without_json(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = [
                {
                    'check': 'installation_status',
                    'installation_complete': False,
                    'operation_running': True,
                    'message': 'FICTIONAL_PRIVATE_VALUE',
                },
                {'check': 'service', 'name': 'synapse', 'state': 'exited', 'exit_code': 0},
                {'check': 'service', 'name': 'mas', 'state': 'exited', 'exit_code': 137},
            ]
            (root / 'misbot.py').write_text(
                'print(' + repr('\n'.join(json.dumps(row) for row in records)) + ')'
            )
            interface = self.interface(root / 'target', source=root)
            with patch('sys.stdout', new=io.StringIO()) as output:
                code = interface.run('status')
                interface.status_result(code)
            shown = output.getvalue()
            self.assertIn('Сервер остановлен', shown)
            self.assertIn('Установка не завершена', shown)
            self.assertIn('Выполняется операция', shown)
            self.assertIn('Остановлена (код 137)', shown)
            self.assertNotIn('FICTIONAL_PRIVATE_VALUE', shown)
            self.assertNotIn('"check"', shown)

    def test_status_includes_missing_compose_services_and_excludes_profiles(self):
        import lifecycle
        from terminal_diagnostics import parse_checks

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                patch.object(lifecycle, 'ROOT', root, create=True),
                patch.object(lifecycle, 'state', return_value={'applied_release_version': '1.2.0'}),
                patch.object(
                    lifecycle,
                    'service_rows',
                    return_value=[{'Service': 'synapse', 'State': 'running', 'Health': 'healthy'}],
                ),
                patch.object(
                    lifecycle,
                    'dc',
                    return_value=json.dumps(
                        {
                            'services': {
                                'synapse': {},
                                'mas': {},
                                'postgres-client': {'profiles': ['tools']},
                            }
                        }
                    ),
                ),
                patch.object(lifecycle, 'emit') as emit,
                patch.dict(os.environ, {'MISBOT_TERMINAL_UI': '1'}),
            ):
                lifecycle.status()
            services = [call.kwargs for call in emit.call_args_list if call.args == ('service',)]
            self.assertEqual({row['name'] for row in services}, {'synapse', 'mas'})
            missing = next(row for row in services if row['name'] == 'mas')
            self.assertEqual(missing['state'], 'missing')
            results = parse_checks(json.dumps(dict(check='service', **missing)))
            self.assertIn('Не создана', results[0].message)
            self.assertFalse(results[0].passed)

    def test_availability_lists_all_checks_without_details_button(self):
        from terminal_diagnostics import parse_checks

        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'target')
            interface.check_results = parse_checks(
                '\n'.join(
                    json.dumps(row)
                    for row in [
                        {'check': 'dns', 'passed': True},
                        {
                            'check': 'homepage_routes',
                            'passed': False,
                            'failure_code': 'connection_refused',
                        },
                        {'check': 'mas_health', 'passed': False, 'failure_code': 'timeout'},
                    ]
                )
            )
            with patch('sys.stdout', new=io.StringIO()) as output:
                interface.check_result(1)
            shown = output.getvalue()
            for label in ('DNS', 'Главная страница и маршруты', 'Служба входа'):
                self.assertIn(label, shown)
            self.assertNotIn('Показать подробности', shown)
            self.assertIn('✓ DNS', shown)
            self.assertIn('× Служба входа', shown)

    def test_missing_installation_menu_keeps_unavailable_actions_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            answers = iter(['1', '1', '0', '0'])
            interface = self.interface(Path(folder) / 'target', reader=lambda *_: next(answers))
            with (
                patch.object(interface, 'run') as run,
                patch('sys.stdout', new=io.StringIO()) as output,
            ):
                self.assertEqual(interface.menu(), 0)
            run.assert_not_called()
            self.assertIn('Недоступно:', output.getvalue())
            self.assertIn('Показать состояние', output.getvalue())

    def test_welcome_menu_choice_never_starts_wizard(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'target', reader=lambda *_: '4')
            with (
                patch.object(interface, 'menu', return_value=0) as menu,
                patch.object(interface, 'wizard') as wizard,
                patch('sys.stdout', new=io.StringIO()),
            ):
                self.assertEqual(interface.welcome(), 0)
            menu.assert_called_once()
            wizard.assert_not_called()

    def test_removed_marker_never_resumes_old_installation_draft(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'target'
            root.mkdir(mode=0o700)
            state_store.write_json(root / '.misbot-empty.json', {'format': 1})
            interface = self.interface(root)
            interface.step = 8
            interface.model = {'DOMAIN': 'example.org'}
            interface.save()
            with (
                patch.object(interface, 'welcome', return_value=0) as welcome,
                patch.object(interface, 'wizard') as wizard,
            ):
                self.assertEqual(ui.interaction(interface), 0)
            welcome.assert_called_once()
            wizard.assert_not_called()

    def test_probe_profile_is_private_and_removed_after_backend_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            answers = iter(['1', 'да'])
            seen = []
            interface = self.interface(Path(folder) / 'target', reader=lambda *_: next(answers))

            def run(path, **args):
                self.assertEqual(path, 'storage s3 check')
                profile = Path(args['config'])
                self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
                self.assertEqual(profile.parent.stat().st_mode & 0o777, 0o700)
                self.assertIn('FICTIONAL_SECRET', profile.read_text())
                seen.append(profile)
                return 130

            with (
                patch.object(interface, 's3_profile', return_value=PROFILE, create=True),
                patch.object(interface, 'run', side_effect=run),
                patch('sys.stdout', new=io.StringIO()) as output,
            ):
                self.assertEqual(
                    interface.execute_s3(ui_actions.Action('storage s3 check', 'S3')), 130
                )
            self.assertFalse(seen[0].exists())
            self.assertFalse(seen[0].parent.exists())
            self.assertNotIn('FICTIONAL_SECRET', output.getvalue())
            self.assertFalse(interface.draft.exists())

    def test_s3_input_cancellation_does_not_mutate_or_run_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'target')
            interface.model = {'MEDIA_MODE': 'local'}
            with (
                patch.object(interface, 's3_profile', side_effect=ui.Back, create=True),
                patch.object(interface, 'run') as run,
                patch('sys.stdout', new=io.StringIO()),
                self.assertRaises(ui.Back),
            ):
                interface.execute_s3(ui_actions.Action('storage s3 connect', 'S3'))
            run.assert_not_called()
            self.assertEqual(interface.model, {'MEDIA_MODE': 'local'})
            self.assertFalse(interface.draft.exists())

    def test_real_interruption_stops_descendant_holding_installation_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'target'
            ready, done = root / 'ready', root / 'done'
            installer = str(Path(state_store.__file__).parent)
            grandchild = root / 'worker.py'
            grandchild.write_text(
                'import signal,sys,time\nfrom pathlib import Path\n'
                'sys.path.insert(0,' + repr(installer) + ')\nimport state_store\n'
                'def stop(*args):\n time.sleep(1.2)\n Path('
                + repr(str(done))
                + ').touch()\n raise SystemExit(143)\n'
                'signal.signal(signal.SIGTERM,stop)\n'
                'with state_store.operation_guard(Path(' + repr(str(target)) + ')):\n'
                ' Path(' + repr(str(ready)) + ').touch()\n time.sleep(20)\n'
            )
            (root / 'misbot.py').write_text(
                'import subprocess,sys,time\nsubprocess.Popen([sys.executable,'
                + repr(str(grandchild))
                + '])\ntime.sleep(20)\n'
            )
            original = terminal_process.read_output
            first = True

            def interrupt_once(child, emit):
                nonlocal first
                if first:
                    first = False
                    deadline = time.monotonic() + 3
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists())
                    self.assertTrue(state_store.operation_running(target))
                    raise KeyboardInterrupt()
                return original(child, emit)

            interface = self.interface(target, source=root)
            with (
                patch.object(terminal_process, 'read_output', side_effect=interrupt_once),
                patch('sys.stdout', new=io.StringIO()),
            ):
                self.assertEqual(interface.run('start'), 130)
            self.assertTrue(done.exists())
            self.assertFalse(state_store.operation_running(target))


@unittest.skipUnless(
    sys.platform.startswith('linux')
    and terminal_views.Console
    and hasattr(TerminalView, 'external_terminal'),
    'Linux PTY, Rich and integrated terminal mode guard required',
)
class SensitiveDisplayTests(unittest.TestCase):
    def receive(self, master, expected, process, output=b''):
        deadline = time.monotonic() + 5
        while expected not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output += os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
        self.assertIn(expected, output)
        return output

    def test_foreground_secret_stays_visible_until_enter_without_saved_history(self):
        for command in ('credentials admin show', 'users invitation show'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as folder:
                base = Path(folder)
                (base / 'misbot.py').write_text("print('FICTIONAL_VISIBLE_CREDENTIAL',flush=True)")
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 80, 0, 0))
                script = """
import fcntl,os,sys,termios
from pathlib import Path
os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
sys.path.insert(0,sys.argv[1])
from terminal_views import TerminalView
from rich.console import Group
sys.path.insert(0,sys.argv[2])
import terminal_ui as ui,configuration,state_store,ui_actions
base=Path(sys.argv[3]);root=base/'target';root.mkdir(mode=0o700)
state_store.write_json(root/'installation.json',{'domain':'example.org'})
configuration.load=lambda *args,**kw:{'DOMAIN':'example.org'}
settings=termios.tcgetattr(0);view=TerminalView()
interface=ui.UI(root,source=base,draft=base/'draft.json',view=view)
with view.screen():
 interface.execute(ui_actions.Action(sys.argv[4],'Показать секрет'))
 content=''.join(segment.text for line in view.console.render_lines(Group(*view.page),pad=False) for segment in line)
 print('NO_SECRET_HISTORY:'+str('FICTIONAL_VISIBLE_CREDENTIAL' not in content),flush=True)
 print('NO_SECRET_RECORDS:'+str(interface.operation_records==[] and interface.result_messages==[] and interface.result_notices==[]),flush=True)
print('RESTORED:'+str(settings==termios.tcgetattr(0)),flush=True)
"""
                process = subprocess.Popen(
                    [
                        sys.executable,
                        '-B',
                        '-c',
                        script,
                        str(Path(terminal_views.__file__).parent),
                        str(Path(ui.__file__).parent),
                        str(base),
                        command,
                    ],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env={
                        key: value
                        for key, value in dict(os.environ, TERM='xterm').items()
                        if key != 'NO_COLOR'
                    },
                )
                os.close(slave)
                try:
                    output = self.receive(master, 'Выполнить операцию?'.encode(), process)
                    os.write(master, 'да\n'.encode())
                    output = self.receive(master, b'Enter', process, output)
                    self.assertIn(b'FICTIONAL_VISIBLE_CREDENTIAL', output)
                    time.sleep(0.15)
                    self.assertIsNone(process.poll())
                    clear = max(output.rfind(b'\x1b[2J\x1b[H'), output.rfind(b'\x1b[H\x1b[2J'))
                    self.assertIn(b'FICTIONAL_VISIBLE_CREDENTIAL', output[clear:])
                    os.write(master, b'\n')
                    output = self.receive(master, b'RESTORED:True', process, output)
                    self.assertIn(b'NO_SECRET_HISTORY:True', output)
                    self.assertIn(b'NO_SECRET_RECORDS:True', output)
                    self.assertEqual(process.wait(timeout=5), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    os.close(master)


class RemovalScopeTests(unittest.TestCase):
    def test_removal_ui_completion_keeps_draft_commands_and_waits(self):
        import argparse

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'target'
            root.mkdir(mode=0o700)
            interface = ui.UI(root, draft=root.parent / 'draft.json', view=TerminalView(plain=True))
            with (
                patch.object(ui, 'UI', return_value=interface),
                patch.object(remove_server, 'discover', return_value=({}, [])),
                patch.object(
                    remove_server,
                    'plan',
                    return_value={'local': dict.fromkeys(remove_server.LOCAL, True)},
                ),
                patch.object(remove_server, 'execute'),
                patch.object(interface, 'pause') as pause,
                patch('sys.stdout', new=io.StringIO()) as output,
            ):
                remove_server.interactive_main(
                    argparse.Namespace(directory=root, backup_config=None)
                )
            shown = output.getvalue().rsplit('Удаление завершено', 1)[1]
            self.assertIn('config draft reset --confirm discard-draft', shown)
            self.assertIn('config draft show', shown)
            self.assertIn(' ui', shown)
            pause.assert_called_once_with('Enter — вернуться в Misbot')

    def test_plan_prompts_for_media_and_external_backups_with_visible_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'domain': 'example.org'})
            targets = [
                {
                    'kind': kind,
                    'profile': dict(PROFILE, S3_PREFIX=prefix),
                    'identity': remove_server.s3_identity(dict(PROFILE, S3_PREFIX=prefix)),
                }
                for kind, prefix in (('media', 'example-media/'), ('backup', 'example-backups/'))
            ]
            prompts, output = [], []

            def decide(label):
                prompts.append(label)
                return True

            def ask(label):
                if 'УДАЛИТЬ S3 ' in label:
                    return 'УДАЛИТЬ S3 ' + label.split('УДАЛИТЬ S3 ', 1)[1].strip()
                return 'УДАЛИТЬ СЕРВЕР example.org'

            record = remove_server.plan(
                root, targets, {}, ask=ask, say=output.append, decide=decide
            )
            self.assertEqual(len(record['s3']), 2)
            self.assertEqual(len([label for label in prompts if 'каталог S3' in label]), 2)
            self.assertIn('Медиа S3:', '\n'.join(output))
            self.assertIn('Копии S3:', '\n'.join(output))
            self.assertNotIn('FICTIONAL_SECRET', '\n'.join(output))
            self.assertFalse((root / 'remove-journal.json').exists())

    def test_draft_cleanup_removes_only_this_installation_private_material(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            root = home / 'server'
            foreign = home / 'foreign-server'
            with patch.object(Path, 'home', return_value=home):
                current = wizard_state.draft_path(root)
                configuration.directory(current.parent)
                saved = {
                    'format': 1,
                    'directory': str(root),
                    'step': 8,
                    'values': {'S3_SECRET_ACCESS_KEY': 'FICTIONAL_SECRET'},
                    'backup': {},
                }
                state_store.write_json(current, saved)
                previous = configuration.directory(current.parent / 'previous')
                old = previous / 'old.json'
                kept = previous / 'foreign.json'
                state_store.write_json(old, saved)
                state_store.write_json(kept, dict(saved, directory=str(foreign)))
                sidecar = current.with_name(current.stem + '-media.env')
                state_store.write_text(sidecar, 'FICTIONAL_SECRET')
                foreign_draft = wizard_state.draft_path(foreign)
                state_store.write_json(foreign_draft, dict(saved, directory=str(foreign)))
                wizard_state.purge_installation_drafts(root)
            self.assertFalse(current.exists())
            self.assertFalse(old.exists())
            self.assertFalse(sidecar.exists())
            self.assertTrue(kept.exists())
            self.assertTrue(foreign_draft.exists())


if __name__ == '__main__':
    unittest.main()
