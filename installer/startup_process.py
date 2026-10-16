"""Bounded lifecycle children, with safe progress for Docker downloads and builds."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import os
import errno
import fcntl
import pty
import struct
import termios
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time

import state_store

ACTIVE = ContextVar('startup_commands_active', default=False)
DEADLINE = ContextVar('lifecycle_command_deadline', default=None)
COMMAND_TIMEOUT = 120
TRANSFER_TIMEOUT = 1800
TRANSFER_IDLE_TIMEOUT = 180
TERMINATE_GRACE = 3


def bounded(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = ACTIVE.set(True)
        try:
            return function(*args, **kwargs)
        finally:
            ACTIVE.reset(token)

    return wrapped


@contextmanager
def deadline(seconds):
    limit = time.monotonic() + seconds
    previous = DEADLINE.get()
    token = DEADLINE.set(min(limit, previous) if previous is not None else limit)
    try:
        yield
    finally:
        DEADLINE.reset(token)


def process_group_running(child):
    """Wait for owned live descendants; reaped or orphaned zombies hold no locks."""
    identifier = getattr(child, 'pid', None)
    if type(identifier) is not int:
        return False
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(') ', 1)[1].split()
            if fields[0] != 'Z' and int(fields[2]) == identifier and int(fields[3]) == identifier:
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def stop(child):
    """Stop only our session, including children retaining the operation lock."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        pass
    # The leader may already be gone while its descendants still hold pipes/locks.
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    child.wait(timeout=TERMINATE_GRACE)


class DownloadProgress:
    def __init__(self, emit):
        self.emit = emit
        self.observed = {}

    def line(self, line):
        # Never replay registry responses, image names, command arguments or stderr.
        line = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', line).strip()
        status = re.fullmatch(
            r'([a-f0-9]{12,64}): (Pulling fs layer|Waiting|Download complete|Pull complete|Already exists)',
            line,
        )
        if status:
            self.emit('download_layer', layer=status[1], action=status[2])
            return
        match = re.fullmatch(
            r'([a-f0-9]{12,64}): (Downloading|Extracting)\s+(?:\[[^\]]*\]\s*)?'
            r'([0-9.]+)(B|kB|KB|MB|GB)/([0-9.]+)(B|kB|KB|MB|GB)\s*',
            line.strip(),
        )
        if not match:
            return
        layer, action, current, unit, total, total_unit = match.groups()
        scale = {'B': 1, 'kB': 1000, 'KB': 1000, 'MB': 1000000, 'GB': 1000000000}
        try:
            current = float(current) * scale[unit]
            total = float(total) * scale[total_unit]
        except ValueError:
            return
        if not 0 <= current <= total or total <= 0:
            return
        now = time.monotonic()
        previous = self.observed.get((layer, action))
        if previous and now - previous[0] < 1:
            return
        values = {'layer': layer, 'action': action, 'current': int(current), 'total': int(total)}
        if previous and current >= previous[1] and now > previous[0]:
            values['bytes_per_second'] = int((current - previous[1]) / (now - previous[0]))
        self.observed[(layer, action)] = now, current
        self.emit('download_progress', **values)


def run(command, stdin=None, emit=None, timeout=None):
    """Capture private output; emit only measured progress and elapsed time."""
    transfer = Path(command[0]).name == 'docker' and command[1:2] in (['pull'], ['build'])
    limit = timeout if timeout is not None else TRANSFER_TIMEOUT if transfer else COMMAND_TIMEOUT
    started = time.monotonic()
    expires = started + limit
    if DEADLINE.get() is not None:
        expires = min(expires, DEADLINE.get())
    label = ('docker ' + command[1]) if transfer else Path(command[0]).name
    if emit:
        emit('command_progress', command=label, elapsed_seconds=0, timeout_seconds=round(limit))
    output = {1: bytearray(), 2: bytearray()}
    progress = DownloadProgress(emit) if transfer and emit else None
    pending = {1: '', 2: ''}
    last_activity = started
    last_update = started
    exited_at = None
    with tempfile.TemporaryFile() as input_file:
        input_file.write(('' if stdin is None else stdin).encode())
        input_file.seek(0)
        # Docker only emits byte counters to a terminal. This owned PTY is never
        # attached to the user's input and its raw contents never reach the UI.
        master = slave = None
        if transfer and command[1] == 'pull':
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 160, 0, 0))
        try:
            child = subprocess.Popen(
                command,
                stdin=input_file,
                stdout=slave if slave is not None else subprocess.PIPE,
                stderr=slave if slave is not None else subprocess.PIPE,
                start_new_session=True,
                **state_store.child_process_options(),
            )
        except BaseException:
            if master is not None:
                os.close(master)
            raise
        finally:
            if slave is not None:
                os.close(slave)
        terminal = os.fdopen(master, 'rb', buffering=0) if master is not None else None
        streams = [terminal] if terminal else [child.stdout, child.stderr]
        try:
            with selectors.DefaultSelector() as selector:
                for number, stream in enumerate(streams, 1):
                    selector.register(stream, selectors.EVENT_READ, number)
                while selector.get_map() or child.poll() is None or process_group_running(child):
                    now = time.monotonic()
                    if now >= expires or (
                        transfer
                        and command[1] == 'pull'
                        and now - last_activity >= TRANSFER_IDLE_TIMEOUT
                    ):
                        raise RuntimeError(
                            'Command timed out: ' + label + '. Keep existing data and secrets; '
                            'check install.sh status, correct connectivity/service errors, '
                            'then retry install.sh up in the same directory.'
                        )
                    if child.poll() is not None:
                        exited_at = now if exited_at is None else exited_at
                        if now - exited_at >= 1:
                            # A grandchild retaining a pipe is not completion of this command.
                            raise RuntimeError(
                                'Command left unfinished child processes: '
                                + label
                                + '. Keep existing data and secrets; check install.sh status '
                                'before retrying the same operation.'
                            )
                    if emit and now - last_update >= 5:
                        emit(
                            'command_progress',
                            command=label,
                            elapsed_seconds=int(now - started),
                            timeout_seconds=round(limit),
                        )
                        last_update = now
                    for key, _ in selector.select(timeout=0.1):
                        try:
                            data = os.read(key.fd, 65536)
                        except OSError as error:
                            if terminal is None or error.errno != errno.EIO:
                                raise
                            data = b''
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        last_activity = time.monotonic()
                        output[key.data].extend(data)
                        if transfer:
                            del output[key.data][: -1024 * 1024]
                        if progress:
                            pending[key.data] += data.decode('utf-8', errors='replace').replace(
                                '\r', '\n'
                            )
                            lines = pending[key.data].split('\n')
                            pending[key.data] = lines.pop()[-4096:]
                            for line in lines:
                                progress.line(line)
                code = child.wait(timeout=TERMINATE_GRACE)
        except BaseException:
            stop(child)
            raise
        finally:
            for stream in streams:
                stream.close()
    return subprocess.CompletedProcess(
        command,
        code,
        output[1].decode('utf-8', errors='replace'),
        output[2].decode('utf-8', errors='replace'),
    )


def main():
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, required=True)
    parser.add_argument('--print-output', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('command required')

    def interrupted(signum, frame):
        raise state_store.OperationInterrupted('Operation interrupted; existing data retained')

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)

    def emit(check, **values):
        print(json.dumps({'check': check, **values}), file=sys.stderr, flush=True)

    try:
        result = run(command, emit=emit, timeout=args.timeout)
        if result.returncode:
            raise RuntimeError(
                'Command failed: '
                + Path(command[0]).name
                + '; inspect the relevant service logs privately'
            )
        if args.print_output:
            print(result.stdout, end='', flush=True)
    except (RuntimeError, state_store.OperationInterrupted) as error:
        print(
            json.dumps({'check': 'failed', 'error_type': 'RuntimeError', 'message': str(error)}),
            file=sys.stderr,
            flush=True,
        )
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
