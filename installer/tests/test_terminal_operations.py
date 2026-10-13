"""Regressions for operation results, removal scope and private interactive S3."""

import io
import json
import os
from pathlib import Path
import sys
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


class RemovalScopeTests(unittest.TestCase):
    def test_external_mas_removal_confirms_only_owned_synapse_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'domain': 'example.org'})
            prompts, rows = [], []

            def decide(label):
                prompts.append(label)
                return True

            def ask(label):
                if 'УДАЛИТЬ БАЗЫ' in label:
                    return 'УДАЛИТЬ БАЗЫ synapse'
                return 'УДАЛИТЬ СЕРВЕР example.org'

            selected = remove_server.plan(
                root,
                [],
                {'MAS_MODE': 'external', 'POSTGRES_MODE': 'external'},
                ask=ask,
                say=lambda *_: None,
                decide=decide,
                summary=lambda values: rows.extend(values),
            )
            self.assertTrue(selected['external_database'])
            self.assertIn('базы synapse и их роли', '\n'.join(prompts))
            self.assertNotIn('synapse/mas', '\n'.join(prompts))
            self.assertIn(
                (
                    'Внешний MAS: аккаунты, устройства, ключи и копии',
                    'Сохранить на внешнем сервере',
                ),
                rows,
            )

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
