"""Rotate both fixed PostgreSQL application passwords with forward recovery."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import uuid

import credentials
import lifecycle
import postgres_setup
import preflight
import state_store
import vault_cache

JOURNAL = 'database-password-transition.json'
WRITERS = ('referral-bot', 'referral-control', 'synapse', 'mas', 'jwt')


def private_json(path, limit=16384):
    """Bounded, descriptor-checked input; never follow links or block on a FIFO."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > limit
        ):
            raise RuntimeError(
                'Database password checkpoint must be an owned private regular file, without hard links, at most 16 KiB; keep recovery files'
            )
        payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise RuntimeError('Database password checkpoint is too large; keep recovery files')
    return json.loads(payload)


def destination(root):
    metadata = lifecycle.state()
    result = {
        'server_name': metadata['domain'],
        'database_mode': metadata.get('database_mode', 'local'),
    }
    if result['database_mode'] == 'external':
        values = preflight.settings(root)
        result.update(host=values['POSTGRES_HOST'], port=values['POSTGRES_PORT'])
    return result


def record(root):
    saved = private_json(root / JOURNAL)
    if (
        not isinstance(saved, dict)
        or saved.get('format') != 1
        or not isinstance(saved.get('stage'), str)
        or not re.fullmatch(r'stages/database-passwords/[0-9a-f]{32}', saved['stage'])
        or saved.get('destination') != destination(root)
        or type(saved.get('pid')) is not int
        or saved['pid'] <= 0
        or not isinstance(saved.get('started'), str)
    ):
        raise RuntimeError(
            'Database password checkpoint does not match this installation/destination; keep all recovery files and the original settings'
        )
    folder = root / saved['stage']
    if folder.resolve() != folder or not folder.is_dir():
        raise RuntimeError('Unsafe database password checkpoint directory; keep recovery files')
    return saved


def claim(root, saved):
    saved.update(pid=os.getpid(), started=state_store.process_identity(os.getpid())[1])
    state_store.write_json(root / JOURNAL, saved)
    os.environ['MATRIX_INSTALLER_DATABASE_PASSWORD_STAGE'] = saved['stage']


def replacement(root, saved):
    values = private_json(root / saved['stage'] / 'replacement.json')
    if (
        not isinstance(values, dict)
        or set(values) != {'synapse', 'mas'}
        or any(
            not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value)
            for value in values.values()
        )
    ):
        raise RuntimeError(
            'Invalid generated database password checkpoint; preserve it and the verified backup'
        )
    return values


def apply(root, values):
    """One transaction: validate ownership, then alter both roles; stdin only."""
    marker = None
    if lifecycle.external_database():
        owned = private_json(root / 'config/external-postgres-owner.json')
        identity = {
            'POSTGRES_HOST': preflight.settings(root)['POSTGRES_HOST'],
            'POSTGRES_PORT': preflight.settings(root)['POSTGRES_PORT'],
            'server_name': lifecycle.state()['domain'],
        }
        if (
            owned.get('format') != 1
            or owned.get('identity') != identity
            or not isinstance(owned.get('owner'), str)
            or not re.fullmatch(r'[0-9a-f]{32}', owned['owner'])
        ):
            raise RuntimeError(
                'External PostgreSQL ownership checkpoint is invalid; no password statement sent'
            )
        marker = 'matrix-installer:' + owned['owner']
    delimiter = '$matrix_' + uuid.uuid4().hex + '$'
    ownership = (
        (
            " OR shobj_description(r.oid,'pg_authid') IS DISTINCT FROM "
            + postgres_setup.literal(marker)
        )
        if marker
        else ''
    )
    sql = (
        postgres_setup.PRIVATE_ROLE_LOGGING_SQL
        + f"""SET standard_conforming_strings=on;
BEGIN;
SET LOCAL lock_timeout='10s';
SET LOCAL statement_timeout='60s';
SELECT pg_advisory_xact_lock(738412609);
DO {delimiter} BEGIN
 IF (SELECT count(*) FROM pg_roles WHERE rolname IN ('synapse','mas')) <> 2
    OR (SELECT count(*) FROM pg_database WHERE datname IN ('synapse','mas')) <> 2
    OR EXISTS(SELECT FROM pg_database d JOIN pg_roles r ON r.rolname=d.datname
       WHERE d.datname IN ('synapse','mas') AND (d.datdba<>r.oid{ownership})) THEN
  RAISE EXCEPTION 'Matrix database ownership mismatch';
 END IF;
END {delimiter};
ALTER ROLE synapse PASSWORD {postgres_setup.literal(values['synapse'])};
ALTER ROLE mas PASSWORD {postgres_setup.literal(values['mas'])};
COMMIT;
"""
    )
    lifecycle.postgres(
        'psql', '-d', 'postgres', '-X', '-v', 'ON_ERROR_STOP=1', '-f', '-', stdin=sql
    )


def stop_writers():
    writers = WRITERS + (('federation-bot',) if lifecycle.state().get('federation_seeded') else ())
    lifecycle.dc('stop', '-t', '30', *writers)
    if any(
        row['Service'] in writers and row['State'] in ('running', 'restarting')
        for row in lifecycle.service_rows()
    ):
        raise RuntimeError('Database writers did not stop; keep the password recovery checkpoint')


def finish(root, saved):
    values = replacement(root, saved)
    stop_writers()
    vault_cache.mark_dirty(root)
    apply(root, values)
    for name, value in values.items():
        state_store.write_text(root / 'secrets' / ('db_' + name), value + '\n')
    lifecycle.generate()
    lifecycle.dc(
        'up',
        '-d',
        *WRITERS,
        *(('federation-bot',) if lifecycle.state().get('federation_seeded') else ()),
    )
    credentials.wait_services()
    lifecycle.check()
    (root / JOURNAL).unlink()
    vault_cache.sync_optional(root, lifecycle.emit)
    lifecycle.emit(
        'database_passwords_changed',
        passed=True,
        roles=2,
        passwords_omitted=True,
        next_action='Run install.sh check; retain the verified pre-change backup and private recovery directory',
    )


def rotate(root):
    if (root / JOURNAL).exists() or (root / JOURNAL).is_symlink():
        raise RuntimeError(
            'Database password change is pending; run install.sh database-password-recover'
        )
    credentials.wait_services()
    # Fail connection/privacy checks before writing a checkpoint or stopping services.
    lifecycle.postgres(
        'psql',
        '-d',
        'postgres',
        '-X',
        '-v',
        'ON_ERROR_STOP=1',
        '-f',
        '-',
        stdin=postgres_setup.PRIVATE_ROLE_LOGGING_SQL,
    )
    lifecycle.backup()
    lifecycle.restore_check()
    parent = root / 'stages/database-passwords'
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.resolve() != parent:
        raise RuntimeError('Unsafe database password directory; no password changed')
    parent.chmod(0o700)
    folder = parent / uuid.uuid4().hex
    folder.mkdir(mode=0o700)
    state_store.write_json(
        folder / 'replacement.json', {name: secrets.token_hex(32) for name in ('synapse', 'mas')}
    )
    saved = {'format': 1, 'stage': str(folder.relative_to(root)), 'destination': destination(root)}
    claim(root, saved)  # Durable intent BEFORE the first password statement.
    finish(root, saved)


def recover(root):
    if not (root / JOURNAL).exists() and not (root / JOURNAL).is_symlink():
        lifecycle.emit('database_password_recovery', pending=False, changed=False)
        return
    saved = record(root)
    claim(root, saved)
    # The unreleased 1.2.46 test candidate generated URL-safe tokens rejected by
    # generate.secret(). Persist a canonical replacement BEFORE its SQL apply.
    # This is forward recovery, never an uncoordinated old-password rollback.
    path = root / saved['stage'] / 'replacement.json'
    values = private_json(path)
    if (
        isinstance(values, dict)
        and set(values) == {'synapse', 'mas'}
        and all(
            isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]{48}', v) for v in values.values()
        )
    ):
        state_store.write_json(path, {name: secrets.token_hex(32) for name in ('synapse', 'mas')})
        lifecycle.emit(
            'database_password_checkpoint_upgraded',
            passed=True,
            passwords_omitted=True,
            next_action='Complete this forward recovery; both application passwords will use the persisted canonical format',
        )
    finish(root, saved)  # Reapplying the same two passwords also handles a lost COMMIT response.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('action', choices=('rotate', 'recover'))
    args = parser.parse_args()
    state_store.require_root()
    root = args.directory.resolve(strict=True)
    lifecycle.configure(root, 'database-password-' + args.action)

    def interrupted(signum, frame):
        raise state_store.OperationInterrupted('Database password change interrupted')

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    with state_store.operation_guard(root):
        if args.action == 'recover' and (root / JOURNAL).exists():
            claim(root, record(root))
        state_store.assert_update_allowed(root)
        lifecycle.ownership_check()
        metadata = lifecycle.state()
        if (
            not metadata.get('databases_initialized')
            or any(
                metadata.get(key)
                for key in (
                    'restore_pending',
                    'migration_frozen',
                    'media_transition_pending',
                    'credential_transition_pending',
                )
            )
            or (root / 'secrets/user-password-pending.json').exists()
        ):
            raise RuntimeError(
                'Finish installation or existing recovery/migration before changing database passwords'
            )
        (rotate if args.action == 'rotate' else recover)(root)


def run_cli():
    try:
        main()
    except Exception as error:
        # The database driver, malformed input, or signal exception can contain secrets.
        print(
            json.dumps(
                {
                    'check': 'database_passwords',
                    'passed': False,
                    'message': str(error)
                    if isinstance(error, state_store.S3CredentialRecoveryRequired)
                    else 'Database password operation did not complete. Keep data, original settings, verified backup and private checkpoints. Check install.sh status; correct PostgreSQL credentials, TLS, connectivity or capacity privately. If database_password_recovery_pending is true, run install.sh database-password-recover [DIRECTORY]; otherwise correct the cause and repeat the original command. Do not start writers or delete checkpoints manually.',
                }
            )
        )
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(run_cli())
