"""Fullscreen restores the terminal and keeps results readable before navigation."""

import errno
import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import terminal_ui as ui
import terminal_views as views
import ui_actions

ENTER_SCREEN = '\x1b[?1049h'
LEAVE_SCREEN = '\x1b[?1049l'
BUNDLE = Path(__file__).resolve().parents[1]


@unittest.skipIf(views.Console is None, 'Rich is an optional rendering dependency')
class FullscreenTests(unittest.TestCase):
    def test_restores_screen_after_success_error_and_interruption(self):
        for error in (None, RuntimeError('fictional failure'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                console = views.Console(file=output, force_terminal=True, width=120)
                view = views.TerminalView(console=console)
                with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True):
                    try:
                        with view.screen():
                            self.assertTrue(view.fullscreen)
                            self.assertTrue(view.screen_used)
                            view.heading('Misbot')
                            if error is not None:
                                raise error
                    except (RuntimeError, KeyboardInterrupt) as caught:
                        self.assertIs(caught, error)
                self.assertFalse(view.fullscreen)
                self.assertIn(ENTER_SCREEN, output.getvalue())
                self.assertIn(LEAVE_SCREEN, output.getvalue())

    def test_plain_dumb_no_color_and_redirected_output_skip_alternate_screen(self):
        for plain, terminal, environment in (
            (True, True, {'TERM': 'xterm'}),
            (False, True, {'TERM': 'dumb'}),
            (False, True, {}),
            (False, True, {'TERM': 'xterm', 'NO_COLOR': ''}),
            (False, False, {'TERM': 'xterm'}),
        ):
            with self.subTest(plain=plain, terminal=terminal, environment=environment):
                output = io.StringIO()
                console = views.Console(file=output, force_terminal=terminal)
                view = views.TerminalView(console=console, plain=plain)
                with patch.dict(os.environ, environment, clear=True):
                    with view.screen():
                        self.assertFalse(view.fullscreen)
                        self.assertFalse(view.screen_used)
                        view.heading('Misbot')
                self.assertNotIn(ENTER_SCREEN, output.getvalue())

    def test_fullscreen_heading_follows_terminal_width_and_keeps_literal_text(self):
        console = views.Console(file=io.StringIO(), force_terminal=True, width=140, record=True)
        view = views.TerminalView(console=console)
        with patch.dict(os.environ, {'TERM': 'xterm'}, clear=True), view.screen():
            with patch.object(console, 'print', wraps=console.print) as printed:
                view.heading('[bold]literal[/bold]')
                self.assertEqual(printed.call_args.args[0].width, 140)
                console.width = 72
                view.heading('Resized')
                self.assertEqual(printed.call_args.args[0].width, 72)
                console.width = 24
                view.heading('Small terminal')
                self.assertTrue(view.compact)
        self.assertIn('[bold]literal[/bold]', console.export_text())

    def test_completion_waits_for_enter_only_in_fullscreen(self):
        for fullscreen in (False, True):
            with self.subTest(fullscreen=fullscreen), tempfile.TemporaryDirectory() as folder:
                output = io.StringIO()
                view = views.TerminalView(console=views.Console(file=output, force_terminal=False))
                view.fullscreen = fullscreen
                interface = ui.UI(Path(folder) / 'target', draft=Path(folder) / 'draft', view=view)
                interface.step = 9
                with (
                    patch('sys.stdout', new=output),
                    patch.object(interface, 'setup_step'),
                    patch.object(interface, 'save'),
                    patch.object(interface, 'ask', return_value='') as ask,
                ):
                    self.assertEqual(interface.wizard(), 0)
                    self.assertIn('Мастер завершён', output.getvalue())
                self.assertEqual(ask.call_count, int(fullscreen))
                if fullscreen:
                    ask.assert_called_once_with('Enter — закрыть мастер')

    def test_operation_result_waits_before_menu_clears_it(self):
        with tempfile.TemporaryDirectory() as folder:
            view = views.TerminalView(plain=True)
            view.fullscreen = True
            interface = ui.UI(Path(folder), draft=Path(folder) / 'draft', view=view)
            events = []
            action = ui_actions.MENUS['server'][1][0]
            with (
                patch.object(interface, 'available_actions', return_value=[action]),
                patch.object(interface, 'choose', side_effect=['1', '0']),
                patch.object(view, 'heading', side_effect=lambda *args: events.append('heading')),
                patch.object(
                    interface, 'execute', side_effect=lambda *args: events.append('result')
                ),
                patch.object(
                    interface, 'ask', side_effect=lambda *args: events.append('enter') or ''
                ),
            ):
                interface.task_menu('server')
            self.assertEqual(events, ['heading', 'result', 'enter', 'heading'])


@unittest.skipUnless(
    sys.platform.startswith('linux') and views.Console, 'Linux PTY and Rich required'
)
class TerminalProcessTests(unittest.TestCase):
    def interact(self, directory, *, plain=False, term='xterm', interrupt=None, resize=False):
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 120, 0, 0))
        # Only the UI privilege gate is simulated. No operation is selected;
        # filesystem permissions and backend checks retain the real host identity.
        script = """
import os, signal, sys
sys.path.insert(0, sys.argv[1])
import terminal_ui
class Identity:
    def geteuid(self): return 0
    def __getattr__(self, name): return getattr(os, name)
terminal_ui.os = Identity()
sys.argv = ['terminal_ui', '--directory', sys.argv[2], *sys.argv[3:]]
def interrupted(signum, frame):
    raise terminal_ui.state_store.OperationInterrupted('fictional interruption')
signal.signal(signal.SIGTERM, interrupted)
signal.signal(signal.SIGHUP, interrupted)
try:
    code = terminal_ui.main()
except (KeyboardInterrupt, terminal_ui.state_store.OperationInterrupted):
    print('Interrupted safely', flush=True)
    code = 130
sys.exit(code)
"""
        environment = dict(os.environ, TERM=term)
        environment.pop('NO_COLOR', None)
        process = subprocess.Popen(
            [
                sys.executable,
                '-B',
                '-c',
                script,
                str(BUNDLE),
                str(directory),
                *(['--plain'] if plain else []),
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=environment,
        )
        os.close(slave)
        output = b''
        acted = False
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        data = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not data:
                        break
                    output += data
                if not acted and 'Номер'.encode() in output:
                    acted = True
                    if resize:
                        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 40, 0, 0))
                        os.write(master, b'3\n')
                        # About keeps the resized page visible until Enter.
                        continue
                    if interrupt:
                        process.send_signal(interrupt)
                    else:
                        os.write(master, b'0\n')
                if acted and interrupt == signal.SIGINT and output.count('Номер'.encode()) >= 2:
                    # Ctrl+C cancels input in the welcome menu; wait for the
                    # next prompt before exiting normally.
                    os.write(master, b'0\n')
                    interrupt = None
                if resize and acted and 'Enter'.encode() in output:
                    os.write(master, b'\n0\n')
                    resize = False
            process.wait(timeout=3)
            self.assertTrue(acted, output.decode(errors='replace'))
            return process.returncode, output.decode(errors='replace')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)

    def test_real_terminal_restores_screen_on_exit_and_signals(self):
        for interrupt in (None, signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(interrupt=interrupt), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder) / 'target'
                code, output = self.interact(directory, interrupt=interrupt)
                self.assertEqual(code, 130 if interrupt in (signal.SIGTERM, signal.SIGHUP) else 0)
                self.assertIn(ENTER_SCREEN, output)
                self.assertIn(LEAVE_SCREEN, output)
                self.assertLess(output.index(ENTER_SCREEN), output.index(LEAVE_SCREEN))
                self.assertFalse(directory.exists())

    def test_real_terminal_plain_and_dumb_modes_do_not_use_alternate_screen(self):
        for plain, term in ((True, 'xterm'), (False, 'dumb')):
            with self.subTest(plain=plain, term=term), tempfile.TemporaryDirectory() as folder:
                code, output = self.interact(Path(folder) / 'target', plain=plain, term=term)
                self.assertEqual(code, 0)
                self.assertNotIn(ENTER_SCREEN, output)
                self.assertNotIn(LEAVE_SCREEN, output)

    def test_real_terminal_resize_keeps_about_readable_then_restores_screen(self):
        with tempfile.TemporaryDirectory() as folder:
            code, output = self.interact(Path(folder) / 'target', resize=True)
        self.assertEqual(code, 0)
        self.assertIn('Enter', output)
        self.assertIn(LEAVE_SCREEN, output)


if __name__ == '__main__':
    unittest.main()
