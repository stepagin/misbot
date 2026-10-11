"""Protected configuration and consistent SQLite snapshots for a host Telegram bot."""
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat

CONFIG_FILES = ('worker.json', 'bot-config.json', 'bot-policy.json', 'mas-client.json',
                'misbot-telegram-control.service', 'misbot-telegram-worker.service', 'telegram-source.tar.gz')
DATABASE_FILES = ('control.sqlite3', 'worker.sqlite3')
MEMBERS = frozenset(['telegram/config/' + name for name in CONFIG_FILES] +
                    ['telegram/' + name for name in DATABASE_FILES])


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def regular(path, owner):
    path = Path(path)
    info = path.lstat()
    if path.resolve() != path or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner or info.st_mode & 0o077:
        raise RuntimeError('Unsafe Telegram backup source')
    return path


def snapshot(root, destination, model):
    root, destination = Path(root), Path(destination)
    config = model.get('telegram_control')
    if not config:
        return None
    control = root / config['directory']
    worker = Path(config['worker_directory'])
    if not control.is_relative_to(root) or control.resolve() != control or not worker.is_absolute() or worker.resolve() != worker:
        raise RuntimeError('Invalid Telegram backup directories')
    target = destination / 'telegram'
    target.mkdir(mode=0o700)
    (target / 'config').mkdir(mode=0o700)
    for name in CONFIG_FILES:
        source = regular(control / name, 0)
        shutil.copyfile(source, target / 'config' / name)
        (target / 'config' / name).chmod(0o600)
    sources = ((control / 'data/bot-control.sqlite3', 0, 'control.sqlite3'),
               (worker / 'worker.sqlite3', 10001, 'worker.sqlite3'))
    for source, owner, name in sources:
        source = regular(source, owner)
        output = target / name
        with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=10) as live:
            with sqlite3.connect(output) as saved:
                live.backup(saved)
                if saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Telegram SQLite snapshot not verified')
        output.chmod(0o600)
    hashes = {str(path.relative_to(destination)): digest(path) for path in target.rglob('*') if path.is_file()}
    return {'format': 1, 'sqlite_snapshot': True, 'sha256': hashes}


def verify(temporary, manifest, found):
    metadata = manifest.get('telegram')
    if not metadata:
        if found & MEMBERS:
            raise RuntimeError('Unregistered Telegram backup payload')
        return
    if metadata.get('format') != 1 or set(metadata.get('sha256', {})) != MEMBERS or not MEMBERS.issubset(found):
        raise RuntimeError('Incomplete Telegram backup')
    for member, expected in metadata['sha256'].items():
        path = Path(temporary) / member
        if digest(path) != expected:
            raise RuntimeError('Telegram backup checksum mismatch')
    for name in DATABASE_FILES:
        with sqlite3.connect((Path(temporary) / 'telegram' / name).as_uri() + '?mode=ro', uri=True) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Telegram backup database not verified')
