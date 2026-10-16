"""Usability flows retain trust, selected installation and backend confirmations."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import state_store
import terminal_navigation
import terminal_ui
import ui_actions


class UsabilityTests(unittest.TestCase):
    def interface(self, root, answers=()):
        root.mkdir(mode=0o700, exist_ok=True)
        state_store.write_json(root / 'installation.json', {'domain': 'example.org'})
        iterator = iter(answers)
        with patch('configuration.load', return_value={}):
            return terminal_ui.UI(
                root, draft=root.parent / 'draft.json', reader=lambda *_: next(iterator)
            )

    def test_configuration_recovery_is_reachable_before_normal_menu(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'server'
            interface = self.interface(root, ['1', 'да'])
            marker = root / 'configuration-transition.json'
            state_store.write_json(marker, {'format': 1})

            def run(path, **values):
                self.assertEqual(path, 'config recover')
                marker.unlink()
                return 0

            with (
                patch('command_wrapper.trusted_installation'),
                patch.object(interface, 'run', side_effect=run),
                patch.object(interface, 'use_active_configuration') as load,
                patch.object(interface, 'menu', return_value=0) as menu,
                patch('sys.stdout', new=io.StringIO()),
            ):
                self.assertEqual(terminal_ui.interaction(interface), 0)
            load.assert_called_once()
            menu.assert_called_once()

    def test_configuration_recovery_exit_preserves_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'server'
            interface = self.interface(root, ['0'])
            marker = root / 'configuration-transition.json'
            state_store.write_json(marker, {'format': 1})
            with patch.object(interface, 'run') as run, patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(interface.configuration_recovery(), 1)
            self.assertTrue(marker.exists())
            run.assert_not_called()

    def test_pending_actions_exclude_unrelated_recovery_and_hidden_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'server'
            interface = self.interface(root)
            state_store.write_json(root / 'federation-transition.json', {'format': 1})
            paths = {item.path for item in terminal_navigation.pending_actions(interface)}
            self.assertEqual(paths, {'federation recover', 'federation rollback'})
            self.assertNotIn('storage s3 cleanup', paths)

    def test_request_selection_transmits_opaque_id_without_typing_it(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['1'])
            rid = 'MB-' + '1' * 32

            def run(path, **values):
                self.assertEqual(path, 'federation requests')
                self.assertEqual(values, {'json': True})
                interface.operation_records = [
                    {
                        'requests': [
                            {
                                'id': rid,
                                'server': 'peer.example.org',
                                'direction': 'incoming',
                                'state': 'pending',
                            },
                            {
                                'id': 'MB-' + '2' * 32,
                                'server': 'old.example.org',
                                'direction': 'outgoing',
                                'state': 'pending',
                            },
                            {
                                'id': 'MB-' + '4' * 32,
                                'server': 'busy.example.org',
                                'direction': 'incoming',
                                'state': 'applying',
                            },
                            {
                                'id': 'MB-' + '5' * 32,
                                'server': 'applied.example.org',
                                'direction': 'incoming',
                                'state': 'pending',
                                'local_applied': True,
                            },
                        ]
                    }
                ]
                return 0

            with (
                patch.object(interface, 'run', side_effect=run),
                patch('sys.stdout', new=io.StringIO()),
            ):
                selected = terminal_navigation.operation_selection(
                    interface,
                    terminal_ui.cli_commands.BY_PATH['federation requests approve'],
                    'request',
                )
            self.assertEqual(selected, rid)

    def test_cancel_request_only_offers_unapplied_outgoing_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['1'])
            expected = 'MB-' + '2' * 32

            def run(*args, **kwargs):
                interface.operation_records = [
                    {
                        'requests': [
                            {
                                'id': 'MB-' + '1' * 32,
                                'server': 'incoming.example.org',
                                'direction': 'incoming',
                                'state': 'pending',
                            },
                            {
                                'id': expected,
                                'server': 'outgoing.example.org',
                                'direction': 'outgoing',
                                'state': 'pending',
                            },
                            {
                                'id': 'MB-' + '3' * 32,
                                'server': 'busy.example.org',
                                'direction': 'outgoing',
                                'state': 'pending',
                                'remote_applied': True,
                            },
                        ]
                    }
                ]
                return 0

            with (
                patch.object(interface, 'run', side_effect=run),
                patch('sys.stdout', new=io.StringIO()),
            ):
                selected = terminal_navigation.operation_selection(
                    interface,
                    terminal_ui.cli_commands.BY_PATH['federation requests cancel'],
                    'request',
                )
            self.assertEqual(selected, expected)

    def test_cleanup_selection_excludes_verified_and_foreign_storage(self):
        import configuration
        import external_backup

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'server'
            interface = self.interface(root, ['1'])
            (root / 'settings').mkdir(mode=0o700)
            values = {
                'S3_BUCKET': 'fictional-backups',
                'S3_ENDPOINT_URL': 'https://s3.example.org',
                'S3_REGION': 'example-1',
                'S3_PREFIX': 'backup/',
                'S3_ACCESS_KEY_ID': 'FICTIONAL_ID',
                'S3_SECRET_ACCESS_KEY': 'FICTIONAL_SECRET',
            }
            profile = root / 'settings/backup.env'
            state_store.write_text(
                profile,
                configuration.serialize(
                    {
                        **values,
                        'BACKUP_KEY_FILE': str(Path(folder) / 'backup.key'),
                    }
                ),
            )
            paths = []
            for number, verified in ((1, False), (2, True), (3, False)):
                name = '20261014T000000Z-' + str(number) * 32
                path = root / 'stages/external-backups' / name / 'catalog.json'
                path.parent.mkdir(parents=True, mode=0o700)
                storage = external_backup.storage_identity(values)
                if number == 3:
                    storage['S3_BUCKET'] = 'foreign-backups'
                state_store.write_json(
                    path,
                    {
                        'format': 1,
                        'upload_verified': verified,
                        'storage': storage,
                        'object_key': 'backup/' + name + '.matrix.enc',
                    },
                )
                paths.append(path)
            with patch('sys.stdout', new=io.StringIO()):
                selected = terminal_navigation.cleanup_arguments(interface, {})
            self.assertEqual(selected, {'config': str(profile), 'catalog': str(paths[0])})

    def test_admin_removal_does_not_offer_primary_or_initial_administrator(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['1'])

            def run(*args, **kwargs):
                interface.operation_records = [
                    {
                        'primary': '@owner:example.org',
                        'administrators': [
                            '@owner:example.org',
                            '@matrix-admin:example.org',
                            '@alice:example.org',
                        ],
                    }
                ]
                return 0

            with (
                patch.object(interface, 'run', side_effect=run),
                patch('sys.stdout', new=io.StringIO()),
            ):
                selected = terminal_navigation.operation_selection(
                    interface, terminal_ui.cli_commands.BY_PATH['admin access remove'], 'mxid'
                )
            self.assertEqual(selected, '@alice:example.org')

    def test_weekly_schedule_uses_day_time_and_timezone(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(
                Path(folder) / 'server', ['2', '5', '21:30', '3', 'Europe/Moscow']
            )
            with (
                patch.object(interface, 'calendar_value', side_effect=lambda v: v),
                patch('sys.stdout', new=io.StringIO()),
            ):
                result = interface.schedule_calendar()
            self.assertEqual(result, 'Fri *-*-* 21:30:00 Europe/Moscow')

    def test_saved_schedule_does_not_need_retained_manual_expression(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'server'
            interface = self.interface(root, ['s'])
            (root / 'config').mkdir(mode=0o700)
            state_store.write_json(
                root / 'config/backup-schedule.json', {'root': str(root), 'calendar': 'daily'}
            )
            with (
                patch.object(interface, 'calendar_value', side_effect=lambda v: v),
                patch('sys.stdout', new=io.StringIO()),
            ):
                self.assertEqual(interface.schedule_calendar(), 'daily')

    def test_unpacked_bundle_autodetection_rejects_wrapper_installed_and_writable_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'bundle'
            source.mkdir(mode=0o700)
            root = Path(folder) / 'server'
            self.assertIsNone(terminal_navigation.trusted_bundle(source, source))
            (source / 'command.json').write_text('{}')
            self.assertIsNone(terminal_navigation.trusted_bundle(source, root))
            (source / 'command.json').unlink()
            (source / 'SHA256SUMS').write_text('')
            with (
                patch('update.validate_manifest', return_value=({}, 'a' * 64)),
                patch('update.release'),
            ):
                self.assertEqual(terminal_navigation.trusted_bundle(source, root), source)
                source.chmod(0o777)
                self.assertIsNone(terminal_navigation.trusted_bundle(source, root))

    def test_reopening_checks_installation_before_executing_new_ui(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server')
            with (
                patch('command_wrapper.trusted_installation', side_effect=RuntimeError('unsafe')),
                patch('os.execv') as execute,
            ):
                with self.assertRaises(RuntimeError):
                    interface.reopen_updated()
            execute.assert_not_called()
            with (
                patch('command_wrapper.trusted_installation') as trust,
                patch('os.execv') as execute,
            ):
                interface.reopen_updated(plain=True)
            trust.assert_called_once_with(interface.root)
            self.assertEqual(
                execute.call_args.args[1][-4:],
                ['--directory', str(interface.root), 'ui', '--plain'],
            )

    def test_mxid_localpart_uses_selected_domain(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server')
            self.assertEqual(
                interface.operation_argument('mxid', 'alice', ''), '@alice:example.org'
            )

    def test_explicit_request_keeps_cli_intent_without_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['да'])
            rid = 'MB-' + '3' * 32
            interface.supplied_arguments = {'request': rid, 'json': False}
            with (
                patch.object(interface, 'run', return_value=0) as run,
                patch('sys.stdout', new=io.StringIO()),
            ):
                self.assertEqual(
                    interface.execute(ui_actions.Action('federation requests approve', 'Одобрить')),
                    0,
                )
            run.assert_called_once_with('federation requests approve', request=rid)

    def test_verified_bundle_is_used_and_success_requests_new_ui(self):
        for path in ('update apply', 'update recover'):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as folder:
                interface = self.interface(Path(folder) / 'server', ['да'])
                bundle = Path(folder) / 'bundle'
                with (
                    patch('terminal_navigation.trusted_bundle', return_value=bundle),
                    patch.object(interface, 'run', return_value=0) as run,
                    patch('sys.stdout', new=io.StringIO()),
                ):
                    with self.assertRaises(terminal_ui.SaveExit):
                        interface.execute(ui_actions.Action(path, 'Обновить'))
                run.assert_called_once_with(path, source=bundle)
                self.assertTrue(interface.restart_requested)

    def test_failed_update_does_not_restart_ui(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['да'])
            with (
                patch.object(interface, 'run', return_value=1),
                patch('sys.stdout', new=io.StringIO()),
            ):
                self.assertEqual(
                    interface.execute(ui_actions.Action('update latest', 'Обновить')), 1
                )
            self.assertFalse(interface.restart_requested)

    def test_large_invitation_list_keeps_all_rows_without_secret_or_cli_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server')
            invitations = [
                {
                    'state': 'valid',
                    'issuer': '@person' + str(i) + ':example.org',
                    'token': 'FICTIONAL_NOT_SHOWN',
                }
                for i in range(1200)
            ]
            with (
                patch('registration.manage', return_value={'invitations': invitations}) as backend,
                patch('sys.stdout', new=io.StringIO()) as output,
            ):
                self.assertEqual(interface.run('users invitation list'), 0)
                interface.show_result_lines('Приглашения')
            backend.assert_called_once_with(interface.root, 'list')
            self.assertEqual(len(interface.result_lines), 1200)
            self.assertIn('@person1199:example.org', output.getvalue())
            self.assertNotIn('FICTIONAL_NOT_SHOWN', output.getvalue())
            self.assertNotIn('используйте CLI', output.getvalue())

    def test_provider_apply_preview_masks_secret_and_cancel_keeps_draft(self):
        import configuration_schema

        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(
                Path(folder) / 'server',
                ['g', 'да', 'fictional-client', 'FICTIONAL_PRIVATE_SECRET', 'a', 'нет'],
            )
            model = {key: field.default for key, field in configuration_schema.FIELDS.items()}
            model['DOMAIN'] = 'example.org'
            with (
                patch('configuration.load', return_value=model),
                patch('configuration.apply_settings') as apply,
                patch('sys.stdout', new=io.StringIO()) as output,
            ):
                interface.settings_menu()
            apply.assert_not_called()
            self.assertNotIn('FICTIONAL_PRIVATE_SECRET', output.getvalue())
            saved = json.loads(interface.draft.read_text())['values']
            self.assertEqual(saved['GOOGLE_CLIENT_SECRET'], 'FICTIONAL_PRIVATE_SECRET')

    def test_s3_explicit_read_only_profile_and_purpose_are_preserved(self):
        import configuration

        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['да'])
            profile = Path(folder) / 'backup.env'
            state_store.write_text(
                profile,
                configuration.serialize(
                    {
                        'S3_BUCKET': 'fictional-backups',
                        'S3_ENDPOINT_URL': 'https://s3.example.org',
                        'S3_REGION': 'example-1',
                        'S3_PREFIX': 'backups/',
                        'S3_ACCESS_KEY_ID': 'FICTIONAL_ID',
                        'S3_SECRET_ACCESS_KEY': 'FICTIONAL_SECRET',
                    }
                ),
            )
            interface.supplied_arguments = {
                'config': str(profile),
                'purpose': 'backup',
                'read_only': True,
                'catalog_directory': '/root/catalogs',
            }
            with (
                patch.object(interface, 'run', return_value=0) as run,
                patch.object(interface, 's3_profile') as form,
                patch('sys.stdout', new=io.StringIO()) as output,
            ):
                self.assertEqual(
                    interface.execute_s3(ui_actions.Action('storage s3 check', 'Проверка S3')), 0
                )
            form.assert_not_called()
            run.assert_called_once_with(
                'storage s3 check',
                config=profile,
                purpose='backup',
                json=True,
                read_only=True,
                catalog_directory='/root/catalogs',
            )
            self.assertTrue(profile.exists())
            self.assertNotIn('FICTIONAL_SECRET', output.getvalue())
            self.assertIn('объект не создаётся', output.getvalue())

    def test_dependency_installation_is_bounded_and_uses_only_missing_packages(self):
        import terminal_dependencies
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['да'])
            with (
                patch('terminal_dependencies.missing_packages', side_effect=[['dnsutils'], []]),
                patch(
                    'platform.freedesktop_os_release',
                    return_value={'ID': 'debian', 'VERSION_ID': '12'},
                ),
                patch('os.geteuid', return_value=0),
                patch('sys.executable', '/usr/bin/python3'),
                patch('startup_process.run', return_value=SimpleNamespace(returncode=0)) as run,
                patch('sys.stdout', new=io.StringIO()),
            ):
                terminal_dependencies.ensure(interface)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.kwargs['timeout'], 900)
            self.assertEqual(
                run.call_args.args[0][-4:], ['install', '-y', '--no-install-recommends', 'dnsutils']
            )

    def test_declined_dependency_installation_does_not_run_apt(self):
        import terminal_dependencies

        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['нет'])
            with (
                patch('terminal_dependencies.missing_packages', return_value=['dnsutils']),
                patch(
                    'platform.freedesktop_os_release',
                    return_value={'ID': 'debian', 'VERSION_ID': '12'},
                ),
                patch('os.geteuid', return_value=0),
                patch('sys.executable', '/usr/bin/python3'),
                patch('startup_process.run') as run,
                patch('sys.stdout', new=io.StringIO()),
            ):
                with self.assertRaises(terminal_ui.Back):
                    terminal_dependencies.ensure(interface)
            run.assert_not_called()

    def test_activation_requires_confirmation_of_external_mas(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'server'
            interface = self.interface(root, ['да', 'нет'])
            state_store.write_json(
                root / 'installation.json',
                {'domain': 'example.org', 'restore_pending': True, 'mas_mode': 'external'},
            )
            with patch.object(interface, 'run') as run, patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(
                    interface.execute(ui_actions.Action('backup activate', 'Активировать')), 0
                )
            run.assert_not_called()
            self.assertTrue(interface.installation()['restore_pending'])

    def test_saved_s3_profile_can_be_reused_without_typing_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['1'])
            saved = {
                'S3_BUCKET': 'fictional-media',
                'S3_ENDPOINT_URL': 'https://s3.example.org',
                'S3_REGION': 'example-1',
                'S3_PREFIX': 'media/',
                'S3_ACCESS_KEY_ID': 'FICTIONAL_ID',
                'S3_SECRET_ACCESS_KEY': 'FICTIONAL_SECRET',
            }
            with patch('sys.stdout', new=io.StringIO()) as output:
                self.assertEqual(interface.s3_profile(saved), saved)
            self.assertNotIn('FICTIONAL_SECRET', output.getvalue())

    def test_dependency_failure_keeps_draft_and_returns_safe_retry_message(self):
        import terminal_dependencies

        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['да'])
            interface.model = {'DOMAIN': 'example.org'}
            interface.save()
            before = interface.draft.read_bytes()
            with (
                patch('terminal_dependencies.missing_packages', return_value=['dnsutils']),
                patch(
                    'platform.freedesktop_os_release',
                    return_value={'ID': 'debian', 'VERSION_ID': '12'},
                ),
                patch('os.geteuid', return_value=0),
                patch('sys.executable', '/usr/bin/python3'),
                patch(
                    'startup_process.run', side_effect=RuntimeError('FICTIONAL_PRIVATE_RESPONSE')
                ),
                patch('sys.stdout', new=io.StringIO()),
            ):
                with self.assertRaisesRegex(RuntimeError, 'повторите шаг') as raised:
                    terminal_dependencies.ensure(interface)
            self.assertNotIn('FICTIONAL_PRIVATE_RESPONSE', str(raised.exception))
            self.assertEqual(interface.draft.read_bytes(), before)

    def test_provider_partial_cancel_restores_pair(self):
        with tempfile.TemporaryDirectory() as folder:
            interface = self.interface(Path(folder) / 'server', ['да', 'new-client', 'отмена'])
            interface.model = {
                'DOMAIN': 'example.org',
                'GOOGLE_CLIENT_ID': 'old-client',
                'GOOGLE_CLIENT_SECRET': 'FICTIONAL_OLD_SECRET',
            }
            previous = dict(interface.model)
            with patch('sys.stdout', new=io.StringIO()):
                with self.assertRaises(terminal_ui.Cancel):
                    interface.setup_provider('GOOGLE')
            self.assertEqual(interface.model, previous)


if __name__ == '__main__':
    unittest.main()
