"""The terminal explains policy changes without changing the machine contract."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import federation
import misbot
import terminal_output
import terminal_process


class FederationOutputTests(unittest.TestCase):
    def test_policy_change_progress_and_result_hide_internal_restore_details(self):
        records = [
            {'check': 'federation_policy_plan'},
            *[
                {
                    'check': 'command_progress',
                    'command': 'docker',
                    'elapsed_seconds': 0,
                    'timeout_seconds': 120,
                }
            ]
            * 20,
            {
                'check': 'consistent_backup',
                'archive': '/opt/example/backups/copy.tar.gz',
                'sha256': 'a' * 64,
            },
            {
                'check': 'restored_one_time_keys',
                'passed': True,
                'database': 'restorecheck_private_123',
                'remaining': 0,
            },
            {
                'status': 'applied',
                'servers': ['example.org', 'other.example.org'],
                'federation_exchange': 'not_verified',
                'raw_sdk': 'FICTIONAL_PRIVATE_VALUE',
            },
        ]
        output = io.StringIO()
        with redirect_stdout(output), terminal_output.human_output('federation add'):
            for record in records:
                print(json.dumps(record))
        shown = output.getvalue()
        self.assertIn('Список разрешённых серверов обновлён', shown)
        self.assertIn('Misbot API другого сервера не нужен', shown)
        self.assertIn('ещё не проверен', shown)
        self.assertIn('/opt/example/backups/copy.tar.gz', shown)
        for private in ('restorecheck', 'FICTIONAL_PRIVATE_VALUE', 'elapsed_seconds', 'Docker: 0'):
            self.assertNotIn(private, shown)
        self.assertLess(len(shown.splitlines()), 12)

    def test_human_main_does_not_change_backend_or_exit_code(self):
        args = SimpleNamespace(
            directory=Path('/opt/example'), action='add', argument='other.example.org'
        )
        backend = SimpleNamespace(
            operate=lambda *_: {'status': 'applied', 'servers': ['other.example.org']}
        )
        for enabled in (False, True):
            output = io.StringIO()
            with (
                redirect_stdout(output),
                terminal_output.human_output('federation add', enabled),
                patch.object(federation.state_store, 'require_root'),
                patch('command_wrapper.trusted_installation'),
                patch.object(federation, 'backend', return_value=backend),
            ):
                self.assertEqual(federation.execute_cli(args), 0)
            if enabled:
                self.assertIn('Список разрешённых', output.getvalue())
            else:
                self.assertEqual(json.loads(output.getvalue())['status'], 'applied')

    def test_router_defaults_to_human_only_on_terminal_json_is_explicit(self):
        for tty, json_mode in ((True, False), (True, True), (False, False)):
            argv = ['federation', 'add', '--server', 'other.example.org']
            if json_mode:
                argv.append('--json')
            with (
                patch.object(misbot.os, 'geteuid', return_value=0),
                patch.object(sys.stdout, 'isatty', return_value=tty),
                patch.object(
                    misbot, 'plan', return_value=['/usr/bin/python3', '/opt/example/federation.py']
                ),
                patch.object(misbot.os, 'execv') as execute,
            ):
                misbot.main(argv)
            self.assertEqual('--human' in execute.call_args.args[1], tty and not json_mode)

    def test_no_misbot_api_is_not_reported_as_failed_matrix_federation(self):
        shown = '\n'.join(
            terminal_process.present_output(
                'federation check',
                json.dumps(
                    {
                        'server': 'other.example.org',
                        'matrix_https': 'available',
                        'policy': True,
                        'code': 'misbot_not_found',
                        'invitation': {'ready': False},
                    }
                ),
            )
        )
        self.assertIn('Matrix по HTTPS: доступен', shown)
        self.assertIn('Misbot API отсутствует', shown)
        self.assertIn('локальный список', shown)

    def test_rendering_does_not_expose_terminal_escape_or_truncated_records(self):
        output = io.StringIO()
        writer = terminal_output.HumanOutput('federation servers', output)
        writer.write(json.dumps({'servers': ['example.org\x1b[31m']}))
        writer.write('\n')
        writer.write('{"secret":"' + 'x' * (terminal_process.MAX_LINE + 1))
        writer.write('private"}\n')
        writer.finish()
        self.assertNotIn('\x1b', output.getvalue())
        self.assertNotIn('private', output.getvalue())

    def test_receipt_is_reported_only_with_signed_confirmation_and_time_is_moscow(self):
        original = {
            'id': 'MB-example',
            'server': 'other.example.org',
            'state': 'pending',
            'direction': 'outgoing',
            'expires_at': 0,
        }
        for confirmation in (None, False, True):
            record = dict(original)
            if confirmation is not None:
                record['delivery_confirmed'] = confirmation
            shown = '\n'.join(
                terminal_process.present_output('federation invite', json.dumps(record))
            )
            self.assertIn('01.01.1970 03:00 МСК', shown)
            if confirmation is True:
                self.assertIn('Получение заявки другим сервером подтверждено.', shown)
            else:
                self.assertIn('Получение заявки другим сервером пока не подтверждено.', shown)
        self.assertNotIn('delivery_confirmed', original)

    def test_operation_recovery_and_request_direction_remain_actionable(self):
        shown = '\n'.join(
            terminal_process.present_output(
                'federation operations',
                json.dumps(
                    {
                        'operations': [
                            {
                                'id': 'FO-example',
                                'state': 'interrupted',
                                'result': {
                                    'next_action': 'sudo misbot federation recover',
                                    'raw_sdk': 'PRIVATE_BODY',
                                },
                            }
                        ],
                        'recovery_available': True,
                    }
                ),
            )
        )
        self.assertIn('sudo misbot federation recover', shown)
        self.assertNotIn('PRIVATE_BODY', shown)
        for direction, label in (('incoming', 'Входящая'), ('outgoing', 'Исходящая')):
            shown = '\n'.join(
                terminal_process.present_output(
                    'federation requests',
                    json.dumps(
                        {
                            'requests': [
                                {
                                    'id': 'MB-example',
                                    'server': 'other.example.org',
                                    'state': 'partially_applied',
                                    'direction': direction,
                                    'expires_at': 1800000000,
                                    'local_applied': True,
                                    'remote_applied': False,
                                }
                            ],
                        }
                    ),
                )
            )
            self.assertIn(label, shown)
            self.assertIn('Срок: до', shown)
            self.assertIn('на этом сервере — да, на другом — не подтверждено', shown)


if __name__ == '__main__':
    unittest.main()
