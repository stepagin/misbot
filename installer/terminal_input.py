"""Event-driven terminal line editing with resize and hidden secret input."""

import codecs
import os
import selectors
import signal
import termios
import tty


class LineEditor:
    def __init__(self):
        self.value = ''
        self.cursor = 0
        self.escape = ''
        self.pasting = False

    def insert(self, value):
        self.value = self.value[: self.cursor] + value + self.value[self.cursor :]
        self.cursor += len(value)

    def feed(self, char):
        """Consume one character; return True only for an explicit submit."""
        if self.escape:
            self.escape += char
            if self.escape in ('\x1b[', '\x1bO'):
                return False
            if self.escape.startswith('\x1b[') and (char.isdigit() or char == ';'):
                return False
            key, self.escape = self.escape, ''
            if key == '\x1b[200~':
                self.pasting = True
            elif key == '\x1b[201~':
                self.pasting = False
            elif not self.pasting:
                if key == '\x1b[D':
                    self.cursor = max(0, self.cursor - 1)
                elif key == '\x1b[C':
                    self.cursor = min(len(self.value), self.cursor + 1)
                elif key in ('\x1b[H', '\x1b[1~', '\x1bOH'):
                    self.cursor = 0
                elif key in ('\x1b[F', '\x1b[4~', '\x1bOF'):
                    self.cursor = len(self.value)
                elif key == '\x1b[3~':
                    self.value = self.value[: self.cursor] + self.value[self.cursor + 1 :]
            return False
        if char == '\x1b':
            self.escape = char
        elif self.pasting:
            if char in '\r\n\t':
                self.insert(' ')
            elif char.isprintable():
                self.insert(char)
        elif char in '\r\n':
            return True
        elif char in ('\x7f', '\b'):
            if self.cursor:
                self.value = self.value[: self.cursor - 1] + self.value[self.cursor :]
                self.cursor -= 1
        elif char == '\x01':
            self.cursor = 0
        elif char == '\x05':
            self.cursor = len(self.value)
        elif char == '\x15':
            self.value = self.value[self.cursor :]
            self.cursor = 0
        elif char == '\x0b':
            self.value = self.value[: self.cursor]
        elif char == '\x17':
            before = self.value[: self.cursor].rstrip()
            cut = max(before.rfind(' '), before.rfind('\t')) + 1
            self.value = self.value[:cut] + self.value[self.cursor :]
            self.cursor = cut
        elif char == '\x04':
            if not self.value:
                raise EOFError()
            self.value = self.value[: self.cursor] + self.value[self.cursor + 1 :]
        elif char == '\x03':
            raise KeyboardInterrupt()
        elif char.isprintable():
            self.insert(char)
        return False


def read_line(view, prompt, secret=False):
    """Block on input or signals; no timer or redraw runs while idle."""
    import sys

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    signal_read, signal_write = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    previous = signal.getsignal(signal.SIGWINCH)
    previous_wakeup = None
    editor = LineEditor()
    decoder = getattr(view, 'input_decoder', None)
    if decoder is None:
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        view.input_decoder = decoder

    def resized(signum, frame):
        # The wakeup fd schedules a redraw without writing from a Python handler.
        pass

    try:
        signal.signal(signal.SIGWINCH, resized)
        # A signal can arrive on Rich's refresh thread. Its Python handler runs
        # on main, so the C signal handler must wake the blocked selector first.
        previous_wakeup = signal.set_wakeup_fd(signal_write, warn_on_full_buffer=False)
        tty.setcbreak(fd, termios.TCSANOW)
        view.console.file.write('\x1b[?2004h')
        view.console.file.flush()
        view.draw_input(prompt, editor.value, editor.cursor, secret)
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            selector.register(signal_read, selectors.EVENT_READ)
            while True:
                pending = view.input_pending
                view.input_pending = ''
                if not pending:
                    for key, _ in selector.select():
                        if key.fd == signal_read:
                            os.read(signal_read, 4096)
                            view.draw_input(prompt, editor.value, editor.cursor, secret)
                        else:
                            data = os.read(fd, 4096)
                            if not data:
                                raise EOFError()
                            pending += decoder.decode(data)
                for index, char in enumerate(pending):
                    if editor.feed(char):
                        view.input_pending = pending[index + 1 :]
                        # A secret never becomes part of the stored page or output.
                        view.finish_input(prompt, '' if secret else editor.value)
                        return editor.value
                if pending:
                    view.draw_input(prompt, editor.value, editor.cursor, secret)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        signal.signal(signal.SIGWINCH, previous)
        if previous_wakeup is not None:
            signal.set_wakeup_fd(previous_wakeup)
        os.close(signal_read)
        os.close(signal_write)
        view.console.file.write('\x1b[?2004l')
        view.console.file.flush()
