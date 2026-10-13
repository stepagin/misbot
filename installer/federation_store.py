"""Bounded, atomic format-1 federation snapshots, including replay checkpoints.

The configuration reader has deliberately smaller limits. Federation history is
retained until capacity; never evict event IDs or retired requests to admit work.
"""

import json
import os
from pathlib import Path
import stat

import federation_protocol as protocol
import state_store

MAX_BYTES = 32 * 1024 * 1024
CAPACITIES = {'events': 100000, 'requests': 1000, 'jobs': 1000}


def validate(value):
    if not isinstance(value, dict) or value.get('format') != 1:
        raise protocol.ProtocolError('invalid_federation_state')
    for name in ('requests', 'peers', 'nonces', 'events', 'jobs', 'incoming_invitation_at'):
        entries = value.get(name, {})
        if not isinstance(entries, dict):
            raise protocol.ProtocolError('invalid_federation_state')
        if name in CAPACITIES and len(entries) > CAPACITIES[name]:
            raise protocol.ProtocolError('federation_state_capacity')
    return value


def decode(raw):
    if len(raw) > MAX_BYTES:
        raise protocol.ProtocolError('federation_state_capacity')
    try:
        return validate(json.loads(raw.decode('utf-8-sig')))
    except (UnicodeError, ValueError):
        raise protocol.ProtocolError('unreadable_federation_state') from None


def read(path):
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise protocol.ProtocolError('unsafe_federation_state')
            if info.st_size > MAX_BYTES:
                raise protocol.ProtocolError('federation_state_capacity')
            raw = stream.read(MAX_BYTES + 1)
        return decode(raw)
    except OSError:
        raise protocol.ProtocolError('unreadable_federation_state') from None


def write(path, value):
    validate(value)
    text = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    if len(text.encode('utf-8')) > MAX_BYTES:
        raise protocol.ProtocolError('federation_state_capacity')
    # Refuse unknown ownership/permissions as well as symlinks on replacement.
    if Path(path).exists() or Path(path).is_symlink():
        read(path)
    state_store.write_text(path, text)
