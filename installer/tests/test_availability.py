"""Protocol errors explain the failed stage without leaking remote data."""

from contextlib import redirect_stdout
import errno
import io
import json
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration
import lifecycle
import state_store
import terminal_diagnostics as diagnostics
import terminal_ui as ui
import terminal_views as views
import verify


class ProtocolTests(unittest.TestCase):
    def test_configuration_busy_or_invalid_is_not_a_dns_failure(self):
        for error in (state_store.OperationBusy('busy'), RuntimeError('invalid config')):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(lifecycle, 'ROOT', Path('/fictional'), create=True),
                patch.object(lifecycle, 'state', return_value={}),
                patch.object(lifecycle.referral, 'validate_saved'),
                patch.object(lifecycle.preflight, 'settings', side_effect=error),
                patch.object(lifecycle.preflight, 'dns_check') as dns,
                patch.object(lifecycle, 'emit') as emit,
                self.assertRaises(type(error)),
            ):
                lifecycle.dns_check()
            dns.assert_not_called()
            emit.assert_not_called()

    def test_resource_presentation_preserves_unknown_and_rejects_unsafe_values(self):
        self.assertIn('CPU: нет данных', diagnostics.resource_messages({}))
        messages = diagnostics.resource_messages(
            {
                'cpu_used_percent': 25,
                'memory_used_bytes': 1024**3,
                'memory_total_bytes': 2 * 1024**3,
                'swap_total_bytes': 0,
                'swap_used_bytes': 0,
                'disk_total_bytes': 'PRIVATE_MARKER',
                'uptime_seconds': 90000,
            }
        )
        self.assertIn('CPU: 25 %', messages)
        self.assertIn('RAM: 1.0 / 2.0 ГиБ (50 %)', messages)
        self.assertIn('Swap: не используется', messages)
        self.assertIn('Диск установки: нет данных', messages)
        self.assertNotIn('PRIVATE_MARKER', str(messages))

    def test_failure_codes_unwrap_network_errors_without_printing_secrets(self):
        errors = (
            (ConnectionRefusedError(errno.ECONNREFUSED, 'PRIVATE_MARKER'), 'connection_refused'),
            (TimeoutError('PRIVATE_MARKER'), 'timeout'),
            (socket.gaierror('PRIVATE_MARKER'), 'dns_resolution'),
            (ssl.SSLCertVerificationError('PRIVATE_MARKER'), 'tls_certificate'),
            (ssl.SSLError('PRIVATE_MARKER'), 'tls_connection'),
            (OSError(errno.ENETUNREACH, 'PRIVATE_MARKER'), 'network_unreachable'),
            (ValueError('PRIVATE_MARKER'), 'unexpected_response'),
            (RuntimeError('PRIVATE_MARKER'), 'check_failed'),
        )
        for error, code in errors:
            for wrapped in (error, verify.urllib.error.URLError(error)):
                with self.subTest(code=code):
                    record = verify.failed_record('synapse', wrapped)
                    self.assertEqual(record['failure_code'], code)
                    self.assertNotIn('PRIVATE_MARKER', json.dumps(record))

    def test_homepage_temporary_http_failure_has_safe_status(self):
        record = verify.failed_record('homepage_routes', verify.homepage.ServiceUnavailable(503))
        self.assertEqual(record['failure_code'], 'http_status')
        self.assertEqual(record['status'], 503)

    def test_readiness_uses_same_deadline_for_docker_and_public_probe(self):
        deadlines = []

        def run(command, **kwargs):
            deadlines.append(lifecycle.startup_process.DEADLINE.get())
            if command[0] == 'docker':
                return subprocess.CompletedProcess(
                    [], 0, json.dumps({'Service': 'synapse', 'State': 'running'}), ''
                )
            return subprocess.CompletedProcess(
                [], 0, json.dumps({'check': 'synapse', 'passed': True}), ''
            )

        with (
            patch.object(lifecycle, 'ROOT', Path('/opt/fictional-stack'), create=True),
            patch.object(lifecycle, 'COMPOSE', ['docker', 'compose'], create=True),
            patch.object(lifecycle, 'emit'),
            patch.object(lifecycle, 'dns_check'),
            patch.object(lifecycle.startup_process, 'run', side_effect=run),
            patch.object(
                lifecycle.subprocess, 'run', side_effect=AssertionError('unbounded command')
            ),
            patch.object(lifecycle, 'expected_services', return_value={'synapse'}),
            patch.object(lifecycle, 'state', return_value={}),
            patch.object(lifecycle.referral, 'validate_saved'),
            patch('federation_runtime.validate'),
        ):
            lifecycle.check()
        self.assertEqual(len(deadlines), 2)
        self.assertIsNotNone(deadlines[0])
        self.assertEqual(deadlines[0], deadlines[1])

    def test_expired_readiness_budget_never_runs_another_probe(self):
        with (
            patch.object(lifecycle.startup_process, 'run') as run,
            patch.object(lifecycle, 'service_rows') as rows,
            self.assertRaisesRegex(RuntimeError, 'timed out'),
        ):
            lifecycle.check_services(time.monotonic() - 1)
        run.assert_not_called()
        rows.assert_not_called()

    def test_public_checks_keep_expected_401_404_and_validate_discovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'domain': 'example.org'})
            requests = []

            def response(request, timeout):
                url = request.full_url
                requests.append(url)
                body, status = {}, 200
                if 'openid-configuration' in url or 'auth_metadata' in url:
                    body = {
                        'issuer': 'https://mas.example.org/',
                        'token_endpoint': 'https://mas.example.org/oauth2/token',
                        'authorization_endpoint': 'https://mas.example.org/authorize',
                    }
                elif '/.well-known/matrix/client' in url:
                    body = {
                        'm.homeserver': {'base_url': 'https://matrix.example.org'},
                        'org.matrix.msc2965.authentication': {'issuer': 'https://mas.example.org/'},
                    }
                elif '/.well-known/matrix/server' in url:
                    body = {'m.server': 'matrix.example.org:443'}
                elif '/client/v3/login' in url:
                    status = 404
                elif '/users' in url:
                    status = 401
                if status != 200:
                    raise verify.urllib.error.HTTPError(
                        url, status, 'PRIVATE_MARKER', {}, io.BytesIO(b'{}')
                    )
                result = unittest.mock.MagicMock()
                result.__enter__.return_value = result
                result.code, result.read.return_value = status, json.dumps(body).encode()
                return result

            tls = unittest.mock.MagicMock()
            tls.wrap_socket.return_value.__enter__.return_value.version.return_value = 'TLSv1.3'
            output = io.StringIO()
            with (
                patch.object(configuration, 'load', return_value={'DOMAIN': 'example.org'}),
                patch.object(configuration, 'assert_auth_identity'),
                patch.object(verify.homepage, 'probe', return_value={}),
                patch.object(verify.urllib.request, 'urlopen', side_effect=response),
                patch.object(verify.socket, 'create_connection'),
                patch.object(verify.ssl, 'create_default_context', return_value=tls),
                redirect_stdout(output),
            ):
                self.assertEqual(verify.main(['--directory', folder]), 0)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(requests), 12)
            self.assertEqual(records[-1]['failures'], 0)
            for name, status in (
                ('legacy_login_disabled', 404),
                ('synapse_admin_protected', 401),
                ('mas_admin_protected', 401),
            ):
                record = next(item for item in records if item['check'] == name)
                self.assertTrue(record['passed'])
                self.assertEqual(record['status'], status)

    def test_failed_network_checks_report_all_routes_and_leave_metadata_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state_store.write_json(root / 'installation.json', {'domain': 'example.org'})
            original = (root / 'installation.json').read_bytes()
            refused = verify.urllib.error.URLError(ConnectionRefusedError('PRIVATE_MARKER'))
            output = io.StringIO()
            with (
                patch.object(configuration, 'load', return_value={'DOMAIN': 'example.org'}),
                patch.object(configuration, 'assert_auth_identity'),
                patch.object(verify.homepage, 'probe', side_effect=refused),
                patch.object(verify.urllib.request, 'urlopen', side_effect=refused),
                patch.object(
                    verify.socket,
                    'create_connection',
                    side_effect=ConnectionRefusedError('PRIVATE_MARKER'),
                ),
                redirect_stdout(output),
            ):
                for _ in range(2):
                    self.assertEqual(verify.main(['--directory', folder]), 1)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(records), 30)
            self.assertEqual(records[14]['failures'], 14)
            self.assertTrue(
                all(record['failure_code'] == 'connection_refused' for record in records[:14])
            )
            self.assertEqual((root / 'installation.json').read_bytes(), original)
            self.assertNotIn('PRIVATE_MARKER', output.getvalue())

    def test_lifecycle_keeps_network_cause_and_lists_services_on_public_failure(self):
        raw = json.dumps(
            {
                'check': 'synapse',
                'passed': False,
                'error_type': 'URLError',
                'failure_code': 'connection_refused',
                'message': 'PRIVATE_MARKER',
            }
        )
        rows = [{'Service': 'federation-bot', 'State': 'restarting', 'Health': ''}]
        output = io.StringIO()
        with (
            patch.object(lifecycle, 'ROOT', Path('/opt/fictional-stack'), create=True),
            patch.object(lifecycle, 'log', Path('/dev/null'), create=True),
            patch.object(lifecycle, 'dns_check'),
            patch.object(
                lifecycle.startup_process,
                'run',
                return_value=subprocess.CompletedProcess([], 1, raw, 'PRIVATE_MARKER'),
            ),
            patch.object(lifecycle, 'service_rows', return_value=rows),
            patch.object(lifecycle, 'external_database', return_value=False),
            patch.object(lifecycle, 'state', return_value={'federation_seeded': True}),
            redirect_stdout(output),
        ):
            with self.assertRaisesRegex(RuntimeError, 'Public checks failed'):
                lifecycle.check()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(records[0]['failure_code'], 'connection_refused')
        inventory = records[-1]
        self.assertFalse(inventory['passed'])
        self.assertIn('tls-router', inventory['missing'])
        self.assertIn('caddy', inventory['missing'])
        self.assertNotIn('PRIVATE_MARKER', output.getvalue())

    def test_readiness_polls_transient_https_and_service_starting_until_success(self):
        failed = json.dumps(
            {'check': 'synapse', 'passed': False, 'failure_code': 'connection_refused'}
        )
        passed = json.dumps({'check': 'synapse', 'passed': True})
        rows = [{'Service': 'synapse', 'State': 'running', 'Health': 'healthy'}]
        with (
            patch.object(lifecycle, 'ROOT', Path('/opt/fictional-stack'), create=True),
            patch.object(lifecycle, 'emit') as emit,
            patch.object(lifecycle, 'dns_check'),
            patch.object(
                lifecycle.startup_process,
                'run',
                side_effect=[
                    subprocess.CompletedProcess([], 1, failed, ''),
                    subprocess.CompletedProcess([], 0, passed, ''),
                ],
            ) as run,
            patch.object(lifecycle, 'service_rows', return_value=rows),
            patch.object(lifecycle, 'expected_services', return_value={'synapse'}),
            patch.object(lifecycle, 'state', return_value={}),
            patch.object(lifecycle.referral, 'validate_saved'),
            patch('federation_runtime.validate'),
            patch.object(lifecycle.time, 'sleep'),
        ):
            lifecycle.check()
        self.assertEqual(run.call_count, 2)
        self.assertTrue(any(call.args == ('waiting_for_service',) for call in emit.call_args_list))
        emit.assert_called_with('public_checks', passed=True)

    def test_readiness_never_retries_bad_identity_certificate_or_http_auth(self):
        for record in (
            {'failure_code': 'unexpected_response'},
            {'failure_code': 'tls_certificate'},
            {'failure_code': 'http_status', 'status': 401},
            {'failure_code': 'dns_resolution'},
        ):
            raw = json.dumps({'check': 'synapse', 'passed': False, **record})
            with (
                self.subTest(record=record),
                patch.object(lifecycle, 'ROOT', Path('/opt/fictional-stack'), create=True),
                patch.object(lifecycle, 'emit'),
                patch.object(lifecycle, 'dns_check'),
                patch.object(
                    lifecycle.startup_process,
                    'run',
                    return_value=subprocess.CompletedProcess([], 1, raw, ''),
                ) as run,
                patch.object(
                    lifecycle,
                    'service_rows',
                    return_value=[{'Service': 'synapse', 'State': 'running'}],
                ),
                patch.object(lifecycle, 'expected_services', return_value={'synapse'}),
                patch.object(lifecycle.time, 'sleep') as sleep,
            ):
                with self.assertRaisesRegex(RuntimeError, 'Public checks failed'):
                    lifecycle.check()
            run.assert_called_once()
            sleep.assert_not_called()


class PresentationTests(unittest.TestCase):
    def test_long_progress_preserves_dns_and_pending_status_with_final_service_state(self):
        for command in ('check', 'status'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as folder:
                source = Path(folder)
                records = [
                    {'check': 'dns', 'passed': True},
                    {
                        'check': 'installation_status',
                        'operation_running': False,
                        'installation_complete': True,
                        'admin_access_recovery_pending': True,
                    },
                ]
                for index in range(400):
                    records.extend(
                        [
                            {
                                'check': 'command_progress',
                                'command': 'docker',
                                'elapsed_seconds': index,
                            },
                            {
                                'check': 'synapse',
                                'passed': False,
                                'failure_code': 'connection_refused',
                            },
                            {'check': 'service', 'name': 'synapse', 'state': 'restarting'},
                            {'check': 'fictional_unknown_' + str(index), 'passed': False},
                        ]
                    )
                records.extend(
                    [
                        {'check': 'synapse', 'passed': True},
                        {
                            'check': 'service',
                            'name': 'synapse',
                            'state': 'running',
                            'health': 'healthy',
                        },
                        {'check': 'service_inventory', 'passed': True},
                    ]
                )
                (source / 'misbot.py').write_text(
                    'import json\n'
                    + 'records='
                    + repr(records)
                    + '\n'
                    + 'for record in records: print(json.dumps(record))\n'
                )
                interface = ui.UI(
                    source,
                    source=source,
                    draft=source / 'draft',
                    view=views.TerminalView(plain=True),
                )
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(interface.run(command), 0)
                results = {result.key: result for result in interface.check_results}
                self.assertEqual(
                    set(results), {'dns', 'synapse', 'service:synapse', 'service_inventory'}
                )
                self.assertTrue(all(result.passed for result in results.values()))
                if command == 'status':
                    self.assertIn(
                        'Изменение доступа администраторов', '\n'.join(interface.result_lines)
                    )

    def test_retry_output_displays_only_final_check_and_service_state(self):
        for final_passed in (True, False):
            with self.subTest(final_passed=final_passed), tempfile.TemporaryDirectory() as folder:
                source = Path(folder)
                records = [
                    {'check': 'dns', 'passed': True},
                    {'check': 'synapse', 'passed': False, 'failure_code': 'connection_refused'},
                    {'check': 'service', 'name': 'synapse', 'state': 'restarting'},
                    {'check': 'service_inventory', 'passed': False},
                    {'check': 'synapse', 'passed': True},
                    {
                        'check': 'service',
                        'name': 'synapse',
                        'state': 'running',
                        'health': 'healthy',
                    },
                    {'check': 'service_inventory', 'passed': True},
                ]
                if not final_passed:
                    records += [
                        {'check': 'synapse', 'passed': False, 'failure_code': 'timeout'},
                        {'check': 'service', 'name': 'synapse', 'state': 'exited', 'health': ''},
                        {'check': 'service_inventory', 'passed': False},
                    ]
                code = 0 if final_passed else 1
                (source / 'misbot.py').write_text(
                    'import sys\n'
                    + ''.join('print(' + repr(json.dumps(record)) + ')\n' for record in records)
                    + 'sys.exit('
                    + str(code)
                    + ')\n'
                )
                output = io.StringIO()
                interface = ui.UI(
                    source,
                    source=source,
                    draft=source / 'draft',
                    view=views.TerminalView(plain=True),
                )
                with redirect_stdout(output):
                    observed = interface.run('check')
                    interface.check_result(observed)
                self.assertEqual(observed, code)
                results = {result.key: result for result in interface.check_results}
                self.assertEqual(len(interface.check_results), 4)
                self.assertTrue(results['dns'].passed)
                for key in ('synapse', 'service:synapse', 'service_inventory'):
                    self.assertEqual(results[key].passed, final_passed)
                self.assertEqual(
                    diagnostics.check_summary(interface.check_results, code),
                    'Успешно: 2. Не пройдено: 0.'
                    if final_passed
                    else 'Успешно: 1. Не пройдено: 1.',
                )
                self.assertNotIn('TCP 443 отклонено', output.getvalue())
                self.assertNotIn('Перезапускается', output.getvalue())
                self.assertIn(
                    'Доступность подтверждена.' if final_passed else 'Доступность не подтверждена.',
                    output.getvalue(),
                )

    def test_safe_new_and_legacy_messages_keep_dns_success_and_http_expectations(self):
        output = '\n'.join(
            json.dumps(item)
            for item in [
                {'check': 'dns', 'passed': True},
                {'check': 'synapse', 'passed': False, 'failure_code': 'connection_refused'},
                {
                    'check': 'web',
                    'passed': False,
                    'error_type': 'URLError',
                    'message': 'PRIVATE_MARKER',
                },
                {'check': 'turn_tls', 'passed': False, 'error_type': 'ConnectionRefusedError'},
                {'check': 'mas_health', 'passed': False, 'status': 502, 'expected': 200},
                {'check': 'discovery', 'passed': False, 'status': 200, 'expected': 200},
                {
                    'check': 'service_inventory',
                    'passed': False,
                    'missing': ['tls-router', 'PRIVATE_MARKER'],
                },
                {'check': 'service', 'name': 'federation-bot', 'state': 'restarting'},
            ]
        )
        results = diagnostics.parse_checks(output)
        self.assertTrue(results[0].passed)
        self.assertIn('TCP 443', results[1].message)
        self.assertIn('не уточнена', results[2].message)
        self.assertIn('502', results[4].message)
        self.assertIn('Содержимое', results[5].message)
        self.assertIn('HAProxy', results[6].message)
        self.assertEqual(results[7].message, 'Перезапускается')
        self.assertEqual(diagnostics.check_summary(results, 1), 'Успешно: 1. Не пройдено: 5.')
        self.assertEqual(results[0].key, 'dns')
        self.assertNotIn('PRIVATE_MARKER', repr(results))

    def test_unknown_malformed_records_and_untrusted_fields_never_escape(self):
        raw = ['PRIVATE_MARKER', 'null', '[]']
        raw += [
            json.dumps(item)
            for item in [
                {'check': ['PRIVATE_MARKER'], 'passed': False},
                {'check': 'PRIVATE_MARKER', 'passed': False},
                {'check': 'synapse', 'passed': 'false'},
                {'check': 'service', 'name': ['PRIVATE_MARKER']},
                {'check': 'service', 'name': 'synapse', 'state': {}},
                {'check': 'web', 'passed': False, 'error_type': {}},
                {'check': 'admin', 'passed': False, 'status': 'PRIVATE_MARKER', 'expected': 200},
            ]
        ]
        results = diagnostics.parse_checks('\n'.join(raw))
        self.assertEqual(len(results), 3)
        self.assertNotIn('PRIVATE_MARKER', repr(results))

    @unittest.skipIf(views.Console is None, 'Rich is optional')
    def test_direct_results_fit_fullscreen_without_repeating_backend(self):
        import os

        for width in (80, 40):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as folder:
                output = io.StringIO()
                with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
                    console = views.Console(
                        file=output, force_terminal=True, no_color=False, width=width, height=24
                    )
                view = views.TerminalView(console=console)
                answers = iter(['>', '<', ''])
                pages = []

                def reader(*args):
                    self.assertLessEqual(view.used_lines, 22)
                    pages.append(output.getvalue())
                    return next(answers)

                interface = ui.UI(folder, draft=Path(folder) / 'draft', view=view, reader=reader)
                raw = [{'check': 'dns', 'passed': True}]
                raw += [
                    {'check': name, 'passed': False, 'failure_code': 'connection_refused'}
                    for name in diagnostics.CHECKS
                    if name not in ('dns', 'service_inventory', 'existing_checks')
                ]
                raw += [
                    {
                        'check': 'service_inventory',
                        'passed': False,
                        'missing': ['jwt', 'livekit', 'web', 'admin', 'tls-router'],
                    }
                ]
                interface.check_results = diagnostics.parse_checks(
                    '\n'.join(json.dumps(record) for record in raw)
                )
                with (
                    patch.dict(os.environ, {'TERM': 'xterm'}, clear=True),
                    view.screen(),
                    patch.object(interface, 'run') as run,
                ):
                    interface.check_result(1)
                    run.assert_not_called()
                self.assertIn('DNS', pages[0])
                self.assertIn('✓', pages[0])
                self.assertIn('×', output.getvalue())
                self.assertIn('Страница', output.getvalue())
                self.assertIn('TCP 443', output.getvalue())
                self.assertEqual(len(pages), 3)
                self.assertNotIn('Показать подробности', output.getvalue())
                self.assertNotIn('sudo misbot', output.getvalue())

    def test_check_capture_keeps_exit_codes_drops_raw_output_and_does_not_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)
            (source / 'misbot.py').write_text(
                'import json,sys\nprint(json.dumps({"check":"dns","passed":True}))\nprint("PRIVATE_MARKER", file=sys.stderr)\nsys.exit(1)\n'
            )
            interface = ui.UI(source / 'target', source=source, draft=source / 'draft')
            output = io.StringIO()
            with redirect_stdout(output):
                for _ in range(2):
                    self.assertEqual(interface.run('check'), 1)
                    interface.check_result(1)
            self.assertNotIn('PRIVATE_MARKER', output.getvalue())
            self.assertNotIn('sudo misbot', output.getvalue())
            self.assertEqual(len(interface.check_results), 1)
            self.assertIn('Показать состояние', output.getvalue())

    def test_interrupted_captured_check_drains_child_before_returning(self):
        class Child:
            returncode = None

            def __init__(self):
                self.calls, self.signals = 0, []
                self.stdout = io.BytesIO()

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt()
                if self.calls == 2:
                    raise state_store.OperationInterrupted()
                self.returncode = 143
                return '{"check":"dns","passed":true}', None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def send_signal(self, number):
                self.signals.append(number)

        with tempfile.TemporaryDirectory() as folder:
            interface = ui.UI(folder, draft=Path(folder) / 'draft')
            child = Child()

            def drain(child, emit):
                output, _ = child.communicate(timeout=1)
                emit(output)
                return child.returncode

            with (
                patch.object(ui.subprocess, 'Popen', return_value=child),
                patch('terminal_process.read_output', side_effect=drain),
                patch('terminal_process.read_interrupted_output', side_effect=drain),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(interface.run('check'), 130)
            self.assertEqual(child.returncode, 143)
            self.assertEqual(child.calls, 3)
            self.assertTrue(child.stdout.closed)
            self.assertEqual(child.signals, [signal.SIGTERM])
            self.assertEqual(len(interface.check_results), 1)
            self.assertIn('прервана', diagnostics.check_summary(interface.check_results, 130))


if __name__ == '__main__':
    unittest.main()
