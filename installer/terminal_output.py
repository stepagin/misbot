"""Render backend records without changing execution, locks or signal handling."""

from contextlib import contextmanager, redirect_stdout
import sys

from terminal_process import MAX_LINE, present_output
from terminal_views import literal


class HumanOutput:
    def __init__(self, path, stream):
        self.path = path
        self.stream = stream
        self.pending = ''
        self.dropping = False
        self.previous = None

    def write(self, text):
        self.pending += text
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            if not self.dropping and len(line) <= MAX_LINE:
                self.show(line)
            self.dropping = False
        if len(self.pending) > MAX_LINE:
            self.pending = ''
            self.dropping = True
        return len(text)

    def show(self, line):
        for message in present_output(self.path, line):
            if message and message != self.previous:
                self.stream.write(literal(message) + '\n')
                self.previous = message
        self.stream.flush()

    def flush(self):
        self.stream.flush()

    def finish(self):
        if self.pending and not self.dropping:
            self.show(self.pending)
        self.pending = ''


@contextmanager
def human_output(path, enabled=True):
    if not enabled:
        yield
        return
    output = HumanOutput(path, sys.stdout)
    with redirect_stdout(output):
        try:
            yield
        finally:
            output.finish()
