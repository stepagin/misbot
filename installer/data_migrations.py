"""Ordered, transactional migrations of Misbot-owned data, preserving old readers.

Synapse/MAS schemas and Matrix crypto stores are not managed here. Published
migration IDs and SQL are immutable. Every admitted migration must remain
compatible with schema 0; incompatible changes need a separate recovery plan.
"""

import argparse
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import uuid

import configuration

SCHEMA_VERSION = 1
MIGRATIONS = (
    (
        1,
        'admin-panel-v1',
        (
            'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
            'CREATE TABLE IF NOT EXISTS users (mxid TEXT PRIMARY KEY, created INTEGER, active INTEGER)',
            'CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, recipient TEXT NOT NULL, '
            "via TEXT NOT NULL, body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', "
            'attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0)',
            'CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(state,next_at)',
        ),
    ),
)
TABLES = {
    'settings': ('key', 'value'),
    'users': ('mxid', 'created', 'active'),
    'outbox': ('id', 'recipient', 'via', 'body', 'state', 'attempts', 'next_at'),
}


def fingerprint(migration):
    return hashlib.sha256('\n'.join(migration[2]).encode()).hexdigest()


def validate_registry():
    if [item[0] for item in MIGRATIONS] != list(range(1, SCHEMA_VERSION + 1)) or len(
        {item[1] for item in MIGRATIONS}
    ) != len(MIGRATIONS):
        raise RuntimeError('Release migration history must have unique sequential versions and IDs')


def database_path(root):
    root = Path(root)
    for name in (
        'data',
        'data/admin-panel',
        'data/admin-panel/panel.sqlite',
        'data/admin-panel/panel.sqlite-journal',
        'data/admin-panel/panel.sqlite-wal',
        'data/admin-panel/panel.sqlite-shm',
    ):
        path = root / name
        if path.is_symlink():
            raise RuntimeError('Unsafe Misbot data path; retain the update checkpoint')
        if path.exists() and name.endswith(('sqlite', '-journal', '-wal', '-shm')):
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError('Misbot database must be a regular file without hard links')
    return root / 'data/admin-panel/panel.sqlite'


def inspect_database(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if not 0 <= version <= SCHEMA_VERSION:
        raise RuntimeError(
            'Unknown Misbot data schema; obtain a compatible release before updating'
        )
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise RuntimeError(
            'Misbot database integrity check failed; retain data and checked backups'
        )
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in TABLES.items():
        if table in tables:
            actual = tuple(row[1] for row in db.execute('PRAGMA table_info(' + table + ')'))
            if actual != columns:
                raise RuntimeError('Unsupported Misbot table layout; installed data unchanged')
        elif version:
            raise RuntimeError('Incomplete Misbot data schema; retain data and update checkpoint')
    if version:
        if 'misbot_migrations' not in tables:
            raise RuntimeError('Missing Misbot migration history; retain the update checkpoint')
        rows = db.execute(
            'SELECT version,id,digest FROM misbot_migrations ORDER BY version'
        ).fetchall()
        expected = [
            (number, name, fingerprint(item))
            for item in MIGRATIONS
            for number, name, _ in (item,)
            if number <= version
        ]
        if rows != expected:
            raise RuntimeError('Misbot migration history differs from the release; retain data')
    elif 'misbot_migrations' in tables:
        raise RuntimeError('Inconsistent Misbot migration history; retain data')
    return version


def check(root):
    validate_registry()
    path = database_path(root)
    if not path.exists():
        return 0
    # mode=rw cannot create a missing DB. Reads perform SQLite's own rollback
    # of a hot journal after SIGKILL; this is recovery, not a schema migration.
    db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=30)
    try:
        return inspect_database(db)
    finally:
        db.close()


def snapshot(root, checkpoint):
    """SQLite's backup API gives a consistent copy even with a live controller."""
    source = database_path(root)
    if not source.exists():
        return
    folder = configuration.directory(Path(checkpoint) / 'data-migrations')
    saved = folder / 'panel.sqlite'
    if saved.exists() or saved.is_symlink():
        raise RuntimeError('Data checkpoint already exists; retain the original copy')
    temporary = folder / ('panel-' + uuid.uuid4().hex + '.sqlite')
    incoming = sqlite3.connect(source.as_uri() + '?mode=rw', uri=True, timeout=30)
    outgoing = sqlite3.connect(temporary, timeout=30)
    try:
        temporary.chmod(0o600)
        incoming.backup(outgoing)
        inspect_database(outgoing)
    finally:
        incoming.close()
        outgoing.close()
    os.replace(temporary, saved)


def migrate(root, checkpoint=None):
    validate_registry()
    path = database_path(root)
    if checkpoint is not None:
        snapshot(root, checkpoint)
    configuration.directory(path.parent)
    previous_umask = os.umask(0o077)
    try:
        db = sqlite3.connect(path, timeout=30)
        try:
            db.execute('BEGIN IMMEDIATE')
            before = inspect_database(db)
            if before < SCHEMA_VERSION:
                db.execute(
                    'CREATE TABLE IF NOT EXISTS misbot_migrations '
                    '(version INTEGER PRIMARY KEY, id TEXT NOT NULL, digest TEXT NOT NULL)'
                )
                for migration in MIGRATIONS:
                    number, identifier, statements = migration
                    if number <= before:
                        continue
                    for statement in statements:
                        db.execute(statement)
                    db.execute(
                        'INSERT INTO misbot_migrations VALUES (?,?,?)',
                        (number, identifier, fingerprint(migration)),
                    )
                    db.execute('PRAGMA user_version=' + str(number))
            inspect_database(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        path.chmod(0o600)
        return before, SCHEMA_VERSION
    finally:
        os.umask(previous_umask)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('check', 'apply', 'rollback-check'))
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--expected-schema', type=int, default=SCHEMA_VERSION)
    parser.add_argument('--checkpoint', type=Path)
    args = parser.parse_args()
    if args.expected_schema != SCHEMA_VERSION:
        raise RuntimeError('Release data schema and migration implementation differ')
    # All admitted migrations are additive and retain schema-0 readers. Never
    # silently reverse a schema or replace live notification/crypto state.
    if args.action == 'apply':
        before, after = migrate(args.directory.resolve(), args.checkpoint)
        print('misbot_data_migrated', before, after)
    else:
        print('misbot_data_schema', check(args.directory.resolve()))


if __name__ == '__main__':
    try:
        main()
    except (OSError, sqlite3.Error, RuntimeError):
        # SQLite errors may include SQL/data. The private data are never echoed.
        raise SystemExit(
            'Misbot data migration failed; retain data and update checkpoints'
        ) from None
