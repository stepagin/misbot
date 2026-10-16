"""Real TTY regressions for wrapped edits, resize, cancellation and secret input."""

import errno
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_input import LineEditor
import terminal_views


class EditorTests(unittest.TestCase):
    def enter(self, editor, value):
        return [editor.feed(char) for char in value]

    def test_wrapped_backspace_arrows_delete_and_word_erasure(self):
        editor = LineEditor()
        self.enter(editor, 'a' * 90 + ' tail')
        self.enter(editor, '\x17')
        self.assertEqual(editor.value, 'a' * 90 + ' ')
        self.enter(editor, '\x7f' * 72)
        self.assertEqual(editor.value, 'a' * 19)
        self.enter(editor, '\x01new \x1b[C\x1b[3~\x05!')
        self.assertEqual(editor.value, 'new ' + 'a' * 18 + '!')

    def test_bracketed_paste_cannot_submit_or_execute_a_second_answer(self):
        editor = LineEditor()
        results = self.enter(editor, '\x1b[200~example.org\nприменить\x1b[201~')
        self.assertFalse(any(results))
        self.assertEqual(editor.value, 'example.org применить')
        self.assertTrue(editor.feed('\n'))

    def test_unicode_and_control_input(self):
        editor = LineEditor()
        self.enter(editor, 'пример\x01\x1bOF\x7f')
        self.assertEqual(editor.value, 'приме')
        with self.assertRaises(KeyboardInterrupt):
            editor.feed('\x03')
        self.enter(editor, '\x15')
        with self.assertRaises(EOFError):
            editor.feed('\x04')


@unittest.skipUnless(sys.platform.startswith('linux') and terminal_views.Console, 'PTY and Rich')
class InputTerminalTests(unittest.TestCase):
    def start(self, secret=False, typeahead=False, unicode_typeahead=False, worker_signal=None):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 42, 0, 0))
        script = """
import fcntl, os, signal, sys, termios, threading, time
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
sys.path.insert(0, sys.argv[1])
from terminal_views import TerminalView
view = TerminalView()
previous = signal.getsignal(signal.SIGWINCH)
settings = termios.tcgetattr(0)
wake_read, wake_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
previous_wakeup = signal.set_wakeup_fd(wake_write)
if int(sys.argv[3]):
    def interrupt(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(int(sys.argv[3]), interrupt)
    def interrupt_worker():
        # Deliver to a worker only after the main thread blocks in the input
        # selector. Python must wake main before it can invoke the handler.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with open('/proc/self/task/' + str(os.getpid()) + '/wchan') as stream:
                wait = stream.read()
            if 'ep_poll' in wait or 'epoll' in wait:
                signal.pthread_kill(threading.get_ident(), int(sys.argv[3]))
                return
            time.sleep(0.005)
    threading.Thread(target=interrupt_worker, daemon=True).start()
try:
    with view.screen():
        view.heading('Page preserved', 'Useful instructions')
        if sys.argv[2] in ('typeahead', 'unicode-typeahead'):
            view.read_input('First: ')
            print('BETWEEN', flush=True)
            time.sleep(0.25)
            result = view.read_input('Input: ', secret=True)
            print('NO_HISTORY:' + str(all('fictional-password' not in str(row) for row in view.page)))
            with view.external_terminal():
                print('EXTERNAL_ECHO:' + str(bool(termios.tcgetattr(0)[3] & termios.ECHO) == bool(settings[3] & termios.ECHO)))
            print('UI_NO_ECHO:' + str(not termios.tcgetattr(0)[3] & termios.ECHO))
        else:
            result = view.read_input('Input: ', sys.argv[2] == 'secret')
    expected = 'пfictional-password' if sys.argv[2] == 'unicode-typeahead' else 'fictional-password' if sys.argv[2] in ('secret', 'typeahead') else 'a' * 8
    print('RESULT:' + str(result == expected))
except KeyboardInterrupt:
    print('INTERRUPTED')
restored_wakeup = signal.set_wakeup_fd(previous_wakeup)
print('RESTORED:' + str(settings == termios.tcgetattr(0) and previous == signal.getsignal(signal.SIGWINCH) and restored_wakeup == wake_write))
os.close(wake_read)
os.close(wake_write)
"""
        process = subprocess.Popen(
            [
                sys.executable,
                '-B',
                '-c',
                script,
                str(Path(__file__).resolve().parents[1]),
                'unicode-typeahead'
                if unicode_typeahead
                else 'typeahead'
                if typeahead
                else 'secret'
                if secret
                else 'normal',
                str(worker_signal or 0),
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
        self.addCleanup(os.close, master)
        self.addCleanup(self.stop, process)
        return master, process

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    def receive(self, master, expected, process, output=b''):
        deadline = time.monotonic() + 5
        while expected not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError as error:
                    if error.errno == errno.EIO:
                        break
                    raise
                output += data
            if process.poll() is not None and not select.select([master], [], [], 0)[0]:
                break
        self.assertIn(expected, output)
        return output

    def test_resize_reflows_page_and_buffer_and_erases_across_wrapped_rows(self):
        master, process = self.start()
        output = self.receive(master, b'Input:', process)
        os.write(master, b'a' * 90)
        output = self.receive(master, b'a' * 42, process, output)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 5, 12, 0, 0))
        process.send_signal(signal.SIGWINCH)
        output = self.receive(master, b'\x1b[H\x1b[2J', process)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        process.send_signal(signal.SIGWINCH)
        output = self.receive(master, b'Page preserved', process)
        self.assertIn(b'Useful instructions', output)
        self.assertIn(b'a' * 73, output)
        os.write(master, b'\x7f' * 82)
        output = self.receive(master, b'Input: aaaaaaaa', process)
        last_frame = output.rsplit(b'\x1b[H\x1b[2J', 1)[-1]
        self.assertNotIn(b'a' * 9, last_frame)
        os.write(master, b'\n')
        output = self.receive(master, b'RESTORED:True', process)
        self.assertIn(b'RESULT:True', output)

    def test_secret_never_appears_in_output_and_terminal_restores(self):
        master, process = self.start(secret=True)
        output = self.receive(master, b'Input:', process)
        os.write(master, b'fictional-password\n')
        output = self.receive(master, b'RESTORED:True', process, output)
        self.assertIn(b'RESULT:True', output)
        self.assertNotIn(b'fictional-password', output)

    def test_secret_typeahead_survives_between_prompts_without_echo_or_history(self):
        master, process = self.start(typeahead=True)
        output = self.receive(master, b'First:', process)
        os.write(master, b'first\n')
        output = self.receive(master, b'BETWEEN', process, output)
        os.write(master, b'fictional-password\n')
        output = self.receive(master, b'RESTORED:True', process, output)
        for result in (
            b'RESULT:True',
            b'NO_HISTORY:True',
            b'EXTERNAL_ECHO:True',
            b'UI_NO_ECHO:True',
        ):
            self.assertIn(result, output)
        self.assertNotIn(b'fictional-password', output)

    def test_utf8_character_split_between_two_prompts_survives(self):
        master, process = self.start(unicode_typeahead=True)
        output = self.receive(master, b'First:', process)
        os.write(master, b'first\n\xd0')
        output = self.receive(master, b'BETWEEN', process, output)
        os.write(master, b'\xbffictional-password\n')
        output = self.receive(master, b'RESTORED:True', process, output)
        self.assertIn(b'RESULT:True', output)
        self.assertNotIn(b'fictional-password', output)

    def test_idle_input_does_not_redraw_and_interrupt_restores_settings(self):
        master, process = self.start()
        self.receive(master, b'Input:', process)
        # Drain the initial draw. Idle input then waits on events without a timer.
        while select.select([master], [], [], 0.02)[0]:
            os.read(master, 65536)
        self.assertFalse(select.select([master], [], [], 0.25)[0])
        process.send_signal(signal.SIGINT)
        output = self.receive(master, b'RESTORED:True', process)
        self.assertIn(b'INTERRUPTED', output)

    def test_signal_delivered_to_worker_wakes_idle_input_and_restores_signal_state(self):
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signum=signum):
                master, process = self.start(worker_signal=signum)
                output = self.receive(master, b'RESTORED:True', process)
                self.assertIn(b'INTERRUPTED', output)
                process.wait(timeout=3)
                self.assertEqual(process.returncode, 0)


if __name__ == '__main__':
    unittest.main()
