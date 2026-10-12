"""DNS status, ten-second retries and cancellation without runtime changes."""

import io
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import configuration_schema as schema
import terminal_ui as ui
from terminal_views import TerminalView
import wizard_dns
import preflight


class DNSStatusTests(unittest.TestCase):
    def test_individual_dns_conditions_have_distinct_actions(self):
        cases = (
            ({'8.8.8.8'}, set(), True, 'готово'),
            (set(), set(), False, 'создать A'),
            ({'8.8.4.4'}, set(), False, 'исправить A'),
            ({'8.8.8.8', '8.8.4.4'}, set(), False, 'исправить A'),
            ({'8.8.8.8'}, {'2001:db8::1'}, False, 'удалить AAAA'),
        )
        for addresses, ipv6, passed, action in cases:
            with self.subTest(action=action, addresses=addresses):

                def resolve(_host, family):
                    return addresses if family == socket.AF_INET else ipv6

                status = wizard_dns.probe('example.org', '8.8.8.8', resolve)
                self.assertEqual(status.passed, passed)
                self.assertIn(action, status.message)

    def test_resolver_errors_are_safe_and_not_reported_as_missing_records(self):
        for error in (RuntimeError('FICTIONAL_PRIVATE'), OSError('FICTIONAL_PRIVATE')):
            with self.subTest(error=type(error).__name__):
                status = wizard_dns.probe(
                    'example.org', '8.8.8.8', lambda *_: (_ for _ in ()).throw(error)
                )
                self.assertTrue(status.query_failed)
                self.assertFalse(status.passed)
                self.assertNotIn('FICTIONAL_PRIVATE', status.message)
                self.assertNotIn('создать', status.message)

    def test_scan_checks_custom_mas_and_all_hosts_in_source_order(self):
        values = {'DOMAIN': 'example.org', 'MAS_DOMAIN': 'auth.example.net'}
        calls = []

        def resolver(host, family):
            calls.append((host, family))
            return {'8.8.8.8'} if family == socket.AF_INET else set()

        statuses = wizard_dns.scan(values, '8.8.8.8', resolver=resolver)
        self.assertEqual([status.host for status in statuses], schema.public_hosts(values))
        self.assertTrue(all(status.passed for status in statuses))
        self.assertEqual(len(calls), 14)
        self.assertIn(('auth.example.net', socket.AF_INET), calls)
        self.assertNotIn(('mas.example.org', socket.AF_INET), calls)

    def test_nxdomain_and_temporary_resolver_failure_are_distinguished(self):
        with patch.object(
            socket,
            'getaddrinfo',
            side_effect=socket.gaierror(socket.EAI_NONAME, 'FICTIONAL_PRIVATE'),
        ):
            self.assertEqual(preflight.resolve('example.org', socket.AF_INET, strict=True), set())
            self.assertIn('создать A', wizard_dns.probe('example.org', '8.8.8.8').message)
        with patch.object(
            socket,
            'getaddrinfo',
            side_effect=socket.gaierror(socket.EAI_AGAIN, 'FICTIONAL_PRIVATE'),
        ):
            self.assertEqual(preflight.resolve('example.org', socket.AF_INET), set())
            status = wizard_dns.probe('example.org', '8.8.8.8')
            self.assertTrue(status.query_failed)
            self.assertNotIn('создать A', status.message)
            self.assertNotIn('FICTIONAL_PRIVATE', status.message)

    def test_stuck_resolver_queries_are_bounded_across_repeated_scans(self):
        import threading

        release = threading.Event()
        workers = []
        lock = threading.Lock()

        def getaddrinfo(*_):
            with lock:
                workers.append(threading.current_thread())
            release.wait(3)
            return []

        try:
            with (
                patch.object(preflight, '_STRICT_RESOLVER_SLOTS', threading.BoundedSemaphore(14)),
                patch.object(socket, 'getaddrinfo', side_effect=getaddrinfo),
            ):
                for _ in range(35):
                    with self.assertRaises(RuntimeError):
                        preflight.resolve('example.org', socket.AF_INET, timeout=0.003, strict=True)
                self.assertEqual(len(workers), 14)
        finally:
            release.set()
            for worker in workers:
                worker.join(timeout=1)
            self.assertTrue(all(not worker.is_alive() for worker in workers))


class DNSWizardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'target'
        self.interface = ui.UI(
            self.root, draft=self.base / 'draft.json', view=TerminalView(plain=True)
        )
        self.interface.model = {'DOMAIN': 'example.org'}
        self.output = io.StringIO()
        output = patch('sys.stdout', self.output)
        output.start()
        self.addCleanup(output.stop)
        self.clock = 0.0

    def statuses(self, ready):
        return [
            wizard_dns.HostStatus(host, '8.8.8.8', ('8.8.8.8',) if ready else ())
            for host in schema.public_hosts(self.interface.model)
        ]

    @staticmethod
    def immediate_thread(*, target, daemon):
        return types.SimpleNamespace(start=target)

    def advance(self, timeout):
        self.clock += timeout
        return ''

    def test_already_correct_dns_does_not_ask_to_create_records_or_wait(self):
        def scan(_values, _expected, on_status):
            for status in self.statuses(True):
                on_status(status)

        with (
            patch('threading.Thread', side_effect=self.immediate_thread),
            patch.object(wizard_dns, 'scan', side_effect=scan),
            patch.object(self.interface, 'dns_wait_command') as command,
        ):
            self.interface.wait_dns('8.8.8.8')
        command.assert_not_called()
        self.assertNotIn('создать A', self.output.getvalue())
        self.assertFalse(self.root.exists())

    def test_missing_records_update_after_ten_seconds_without_overlapping_scan(self):
        scan_times = []

        def scan(_values, _expected, on_status):
            scan_times.append(self.clock)
            for status in self.statuses(len(scan_times) > 1):
                on_status(status)

        with (
            patch('threading.Thread', side_effect=self.immediate_thread),
            patch.object(wizard_dns, 'scan', side_effect=scan),
            patch.object(ui.time, 'monotonic', side_effect=lambda: self.clock),
            patch.object(self.interface, 'dns_wait_command', side_effect=self.advance),
        ):
            self.interface.wait_dns('8.8.8.8')
        self.assertEqual(scan_times, [0, 10])
        self.assertIn('создать A', self.output.getvalue())
        self.assertIn('готово: A', self.output.getvalue())
        self.assertFalse(self.root.exists())

    def test_back_save_exit_and_cancel_are_available_while_query_is_still_running(self):
        for command, exception in (
            ('назад', ui.Back),
            ('сохранить и выйти', ui.SaveExit),
            ('отмена', ui.Cancel),
        ):
            with self.subTest(command=command):
                thread = types.SimpleNamespace(start=lambda: None)
                with (
                    patch('threading.Thread', return_value=thread) as spawned,
                    patch.object(self.interface, 'dns_wait_command', return_value=command),
                    patch.object(self.interface, 'run') as run,
                ):
                    with self.assertRaises(exception):
                        self.interface.wait_dns('8.8.8.8')
                spawned.assert_called_once()
                run.assert_not_called()
                self.assertFalse(self.root.exists())

    def test_pending_query_never_starts_another_scan(self):
        def command(timeout):
            self.clock += timeout
            return 'сохранить и выйти' if self.clock >= 25 else ''

        thread = types.SimpleNamespace(start=lambda: None)
        with (
            patch('threading.Thread', return_value=thread) as spawned,
            patch.object(ui.time, 'monotonic', side_effect=lambda: self.clock),
            patch.object(self.interface, 'dns_wait_command', side_effect=command),
        ):
            with self.assertRaises(ui.SaveExit):
                self.interface.wait_dns('8.8.8.8')
        spawned.assert_called_once()

    def test_dns_wait_restores_terminal_echo_on_failure(self):
        import os
        import termios

        master, slave = os.openpty()
        stream = os.fdopen(slave, 'r')
        self.addCleanup(stream.close)
        self.addCleanup(lambda: os.close(master))
        original = termios.tcgetattr(stream.fileno())
        with patch('sys.stdin', stream):
            with self.assertRaises(ui.SaveExit):
                with self.interface.dns_input_mode():
                    self.assertFalse(termios.tcgetattr(stream.fileno())[3] & termios.ECHO)
                    raise ui.SaveExit()
        self.assertEqual(termios.tcgetattr(stream.fileno()), original)


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux PTY required')
class DNSProcessTests(unittest.TestCase):
    def test_real_terminal_wait_shows_input_and_restores_modes_on_back_and_save(self):
        import errno
        import fcntl
        import json
        import os
        import pty
        import select
        import struct
        import subprocess
        import termios
        import time

        script = """
import json, sys, termios
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import preflight, terminal_ui, terminal_views
preflight.resolve = lambda *args, **kwargs: set()
view = terminal_views.TerminalView()
interface = terminal_ui.UI(Path(sys.argv[2]) / 'target',
    draft=Path(sys.argv[2]) / 'draft.json', view=view)
interface.model = {'DOMAIN': 'example.org'}
interface.step = 2
original = termios.tcgetattr(sys.stdin.fileno())
with view.screen():
    try:
        interface.wait_dns('8.8.8.8')
    except terminal_ui.Back:
        interface.previous_step()
    except terminal_ui.SaveExit:
        interface.save()
print('TERMINAL_RESTORED', termios.tcgetattr(sys.stdin.fileno()) == original, flush=True)
"""
        for answer in ('q', 'назад\n'):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as folder:
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
                child = subprocess.Popen(
                    [sys.executable, '-B', '-c', script, str(Path(ui.__file__).parent), folder],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=dict(os.environ, TERM='xterm'),
                )
                os.close(slave)
                output = b''

                def until(marker):
                    nonlocal output
                    deadline = time.monotonic() + 6
                    while marker not in output:
                        if time.monotonic() > deadline:
                            self.fail(output.decode(errors='replace'))
                        if select.select([master], [], [], 0.1)[0]:
                            try:
                                output += os.read(master, 65536)
                            except OSError as error:
                                if error.errno == errno.EIO:
                                    break
                                raise

                try:
                    until('Команда:'.encode())
                    if answer.startswith('наз'):
                        os.write(master, 'наз'.encode())
                        until('Команда: наз'.encode())
                        os.write(master, 'ад\n'.encode())
                    else:
                        os.write(master, answer.encode())
                    until(b'TERMINAL_RESTORED True')
                    child.wait(timeout=3)
                    self.assertEqual(child.returncode, 0, output.decode(errors='replace'))
                    self.assertIn(b'TERMINAL_RESTORED True', output)
                    saved = json.loads((Path(folder) / 'draft.json').read_text())
                    self.assertEqual(saved['step'], 0 if answer.startswith('наз') else 2)
                    self.assertFalse((Path(folder) / 'target').exists())
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                    os.close(master)


if __name__ == '__main__':
    unittest.main()
