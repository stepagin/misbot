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
                lifecycle.subprocess,
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


class PresentationTests(unittest.TestCase):
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
    def test_result_and_details_fit_fullscreen_without_repeating_backend(self):
        import os

        for width in (80, 40):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as folder:
                output = io.StringIO()
                with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
                    console = views.Console(
                        file=output, force_terminal=True, no_color=False, width=width, height=24
                    )
                view = views.TerminalView(console=console)
                answers = iter(['1', '>', '<', '0', '0'])

                def reader(*args):
                    self.assertLessEqual(view.used_lines, 22)
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
                self.assertIn('DNS: проверка пройдена', output.getvalue())
                self.assertIn('Страница', output.getvalue())
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
            stdout = None

            def __init__(self):
                self.calls, self.signals = 0, []

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
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(interface.run('check'), 130)
            self.assertEqual(child.returncode, 143)
            self.assertEqual(child.signals, [signal.SIGTERM])
            self.assertEqual(len(interface.check_results), 1)
            self.assertIn('прервана', diagnostics.check_summary(interface.check_results, 130))


if __name__ == '__main__':
    unittest.main()
