#!/usr/bin/env python3
"""Move existing local PostgreSQL data to an owned dedicated TLS cluster."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid

import backup_archive
import lifecycle
import media
import postgres_setup
import preflight
import state_store
import configuration
import vault_cache


class DatabaseInterrupted(state_store.OperationInterrupted):
    """Avoid InterruptedError, which syscall/selector retry loops can swallow."""

FIELDS = {'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_SSL_MODE',
          'POSTGRES_SSL_ROOT_CERT', 'POSTGRES_ADMIN_USER', 'POSTGRES_ADMIN_PASSWORD'}
WRITERS = ('referral-bot', 'referral-control', 'synapse', 'mas', 'jwt')


def settings(path):
    values = preflight.private_settings_file(path)
    if set(values) - FIELDS or any(any(ord(c) < 32 for c in v) for v in values.values()):
        raise RuntimeError('Use only the six PostgreSQL fields in a private literal env file; values must contain no control characters')
    values = {**{'POSTGRES_PORT': '5432', 'POSTGRES_SSL_MODE': 'verify-full',
                 'POSTGRES_SSL_ROOT_CERT': '', 'POSTGRES_ADMIN_USER': 'postgres'}, **values}
    if (not re.fullmatch(r'[A-Za-z0-9.-]+', values.get('POSTGRES_HOST', ''))
            or not re.fullmatch(r'[0-9]{1,5}', values['POSTGRES_PORT'])
            or not 1 <= int(values['POSTGRES_PORT']) <= 65535
            or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,62}', values['POSTGRES_ADMIN_USER'])
            or not values.get('POSTGRES_ADMIN_PASSWORD')):
        raise RuntimeError('PostgreSQL host, port, administrator or password is missing/invalid; edit the private file, then repeat database-external')
    if values['POSTGRES_SSL_MODE'] != 'verify-full':
        raise RuntimeError('Existing-data migration requires POSTGRES_SSL_MODE=verify-full; correct the CA/hostname instead of disabling TLS verification')
    if values['POSTGRES_ADMIN_USER'] in ('synapse', 'mas'):
        raise RuntimeError('Use a separate dedicated cluster administrator; synapse and mas are reserved application roles')
    return values


def target(root, values):
    identity = {k: values[k] for k in ('POSTGRES_HOST', 'POSTGRES_PORT')}
    identity['server_name'] = lifecycle.state()['domain']
    name = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    folder = root / 'stages/database-targets' / name
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (folder / 'config').mkdir(mode=0o700, exist_ok=True)
    (folder / 'secrets').mkdir(mode=0o700, exist_ok=True)
    values = dict(values)
    if values['POSTGRES_SSL_ROOT_CERT']:
        ca = Path(values['POSTGRES_SSL_ROOT_CERT'])
        if not ca.is_absolute() or ca.is_symlink() or not ca.is_file():
            raise RuntimeError('Use an absolute regular CA PEM path on this VPS; preserve the original installation and retry')
        shutil.copyfile(ca, folder / 'config/ca.pem')
        (folder / 'config/ca.pem').chmod(0o600)
        values['POSTGRES_SSL_ROOT_CERT'] = str(folder / 'config/ca.pem')
    state_store.write_text(folder / 'settings.env', ''.join(k + '=' + v + '\n' for k, v in values.items()))
    state_store.write_json(folder / 'installation.json', {'domain': identity['server_name']})
    for name in ('db_synapse', 'db_mas'):
        shutil.copyfile(root / 'secrets' / name, folder / 'secrets' / name)
        (folder / 'secrets' / name).chmod(0o600)
    return folder


class Client:
    """Private pgpass mounts and stdin; never expose PostgreSQL stderr or passwords."""
    def __init__(self, root, folder, record=None):
        self.root, self.folder, self.record = root, folder, record
        self.values = preflight.settings(folder)
        self.image = json.loads((root / 'versions.json').read_text())['digests']['postgres']
        self.passfile = folder / 'config/admin.pgpass'
        def escape(value):
            return value.replace('\\', '\\\\').replace(':', '\\:')
        prefix = ':'.join(escape(self.values[k]) for k in ('POSTGRES_HOST', 'POSTGRES_PORT')) + ':*:'
        lines = [prefix + escape(self.values['POSTGRES_ADMIN_USER']) + ':' + escape(self.values['POSTGRES_ADMIN_PASSWORD'])]
        for role in ('synapse', 'mas'):
            password_file = folder / 'secrets' / ('db_' + role)
            if password_file.exists():
                lines.append(prefix + role + ':' + escape(password_file.read_text().strip()))
        state_store.write_text(self.passfile, '\n'.join(lines) + '\n')

    def command(self, tool, arguments, user=None):
        if user is not None and user not in ('synapse', 'mas'):
            raise RuntimeError('Only the two fixed application roles can be probed')
        name = 'matrix-dbcopy-' + uuid.uuid4().hex
        if self.record is not None:
            self.record['active_client'] = name
            save(self.root, self.record)
        args = ['docker', 'run', '--rm', '--name', name, '--network', 'host', '-i',
                '--memory', '128m', '--pids-limit', '128',
                '--label', 'matrix.installer.database.root=' + str(self.root),
                '-v', str(self.passfile) + ':/config/admin.pgpass:ro']
        env = {'PGHOST': self.values['POSTGRES_HOST'], 'PGPORT': self.values['POSTGRES_PORT'],
               'PGUSER': user or self.values['POSTGRES_ADMIN_USER'], 'PGSSLMODE': 'verify-full',
               'PGPASSFILE': '/config/admin.pgpass', 'PGCONNECT_TIMEOUT': '10',
               'PGOPTIONS': '-c lock_timeout=15s'}
        # Match generate.py: an omitted custom CA uses the VPS system trust
        # bundle. libpq in the tool image otherwise looks for ~/.postgresql/root.crt
        # and fails even though the installed services connect with verify-full.
        ca = self.values['POSTGRES_SSL_ROOT_CERT'] or '/etc/ssl/certs/ca-certificates.crt'
        args += ['--mount', 'type=bind,source=' + ca + ',target=/config/ca.pem,readonly']
        env['PGSSLROOTCERT'] = '/config/ca.pem'
        for key, value in env.items():
            args += ['-e', key + '=' + value]
        return args + [self.image, tool, *arguments]

    def __call__(self, tool, *arguments, stdin=None, file=None, user=None):
        command = self.command(tool, arguments, user)
        options = {'stdin': file or subprocess.DEVNULL} if stdin is None else {'input': stdin.encode()}
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                **options, **state_store.child_process_options())
        if result.returncode:
            raise RuntimeError('Destination PostgreSQL operation failed. Keep both datasets and private checkpoints; check credentials, TLS, connectivity and capacity, then use database-recover if a transition is pending')
        return result.stdout.decode()

    def sql(self, database, sql):
        return self('psql', '-d', database, '-X', '-At', '-v', 'ON_ERROR_STOP=1', '-f', '-', stdin=sql)


def save(root, record):
    state_store.write_json(root / 'database-transition.json', record)


def claim(root, record):
    record.update(pid=os.getpid(), started=state_store.process_identity(os.getpid())[1])
    save(root, record)
    os.environ['MATRIX_INSTALLER_DATABASE_STAGE'] = record['stage']


def private_folder(root, relative, prefix):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or path.parts[:2] != ('stages', prefix):
        raise RuntimeError('Invalid database transition path; retain private checkpoints')
    folder = root / path
    if folder.is_symlink() or not folder.resolve().is_relative_to(root):
        raise RuntimeError('Unsafe database transition path; retain private checkpoints')
    return folder


def no_active_client(record):
    name = record.get('active_client')
    if not name:
        return
    result = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', name],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip() == 'true':
        raise RuntimeError('The previous owned PostgreSQL client container is still running. Wait for it to exit, then repeat database-recover; no target database reset is allowed yet')
    if result.returncode == 0 and result.stdout.strip() == 'false':return
    if (result.returncode == 1 and any(marker in result.stderr.lower()
            for marker in ('no such object','no such container'))):return
    raise RuntimeError('Previous PostgreSQL client status could not be verified. Keep both datasets and the checkpoint; check Docker availability privately, then retry database-recover. No target database reset is allowed yet')


def version_check(client):
    value = client.sql('postgres', 'SHOW server_version_num;').strip()
    if not value.isdigit() or int(value) // 10000 != 18:
        raise RuntimeError('This migration supports PostgreSQL major 18 only; use a compatible dedicated cluster')


def empty_target(client):
    for database in ('synapse', 'mas'):
        count = client.sql(database, "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%';").strip()
        if count != '0':
            raise RuntimeError('Destination contains application objects. Keep its data and use a fresh dedicated cluster; this command never adopts existing data')


def role_access(client):
    for role in ('synapse', 'mas'):
        try:
            answer = client('psql', '-d', role, '-X', '-At', '-v', 'ON_ERROR_STOP=1',
                            '-c', 'SELECT 1', user=role).strip()
            if answer != '1':
                raise RuntimeError('Application role probe failed')
        except RuntimeError:
            raise RuntimeError('Destination ' + role + ' role cannot authenticate with the preserved installation credentials. Keep both datasets and keys; ask the dedicated cluster administrator to repair role access, then repeat database-external before preparation or database-recover if pending') from None


def inventory(client, database):
    with temporary_postgres(client):
        return lifecycle.database_inventory(database)


from contextlib import contextmanager


@contextmanager
def temporary_postgres(client):
    original = lifecycle.postgres
    lifecycle.postgres = client
    try:
        yield
    finally:
        lifecycle.postgres = original


def external(root, settings_path):
    metadata = lifecycle.state()
    if (not metadata.get('databases_initialized') or not metadata.get('storage_committed')
            or metadata.get('database_mode', 'local') != 'local'):
        raise RuntimeError('database-external moves an existing committed local installation; for initial external installation use init/up, and for a completed move use check/up')
    if any(metadata.get(k) for k in ('restore_pending', 'migration_frozen', 'credential_transition_pending', 'media_transition_pending')):
        raise RuntimeError('Finish recovery or machine migration before changing database storage')
    running = {r['Service'] for r in lifecycle.service_rows() if r['State'] in ('running', 'restarting')}
    writers=WRITERS+(('federation-bot',) if metadata.get('federation_seeded') else ())
    if any(name not in running for name in writers):
        raise RuntimeError('Start and verify all Matrix writers with install.sh up/check before database-external; migration of an intentionally partially stopped installation is not supported yet')
    lifecycle.wait_writers(list(writers))
    folder = target(root, settings(settings_path))
    client = Client(root, folder)
    version_check(client)
    # Bootstrap can be repeated only with this destination-specific ownership checkpoint.
    postgres_setup.initialize(folder, client)
    empty_target(client)
    role_access(client)
    lifecycle.backup()
    lifecycle.restore_check()
    stage = root / 'stages/database-transfers' / uuid.uuid4().hex
    stage.mkdir(mode=0o700, parents=True)
    configuration.snapshot(root,stage)
    for name in ('installation.json', 'compose.yaml'):
        shutil.copyfile(root / name, stage / name)
        (stage / name).chmod(0o600)
    shutil.copytree(root / 'config', stage / 'config', copy_function=shutil.copy2)
    for path in (root / 'config').rglob('*'):
        saved = stage / 'config' / path.relative_to(root / 'config')
        os.chown(saved, path.stat().st_uid, path.stat().st_gid)
    record = {'format': 1, 'server_name': metadata['domain'], 'stage': str(stage.relative_to(root)),
              'target': str(folder.relative_to(root)), 'writers': [n for n in writers if n in running],
              'phase': 'prepared', 'activated': False, 'imported': []}
    claim(root, record)
    resume(root, record)


def snapshot(root, stage, record):
    # Keep writers off across final snapshot, imports and activation. A separate
    # journal retains their previous state even when the backup pause exits.
    if 'freeze_started_unix' not in record:
        record['freeze_started_unix'] = time.time()
        record['freeze_started_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        save(root, record)
    if record['writers']:
        lifecycle.dc('stop', '-t', '30', *record['writers'])
    if (root / 'backup-writers.json').exists():
        lifecycle.backup_recover()
    if 'archive' not in record:
        if lifecycle.state().get('database_mode', 'local') != 'local':
            raise RuntimeError('Final local snapshot is missing after a storage change; retain both datasets and inspect the private transition before proceeding')
        lifecycle.dc('up', '-d', 'postgres')
        lifecycle.wait(lambda: lifecycle.postgres('psql', '-d', 'postgres', '-X', '-Atc', 'SELECT 1').strip() == '1', service='postgres')
        archive = lifecycle.backup(leave_stopped=True)
        record['archive'] = str(archive.relative_to(root))
        record['phase'] = 'snapshot'
        save(root, record)
    destination = stage / 'snapshot'
    if destination.exists():
        return backup_archive.verify_snapshot(destination)
    partial = stage / 'snapshot.partial'
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(mode=0o700)
    archive = root / record['archive']
    if archive.is_symlink() or not archive.resolve().is_relative_to(root / 'backups'):
        raise RuntimeError('Invalid database snapshot archive path; retain checkpoints')
    backup_archive.extract(archive, partial)
    backup_archive.verify_snapshot(partial)
    os.replace(partial, destination)
    return backup_archive.verify_snapshot(destination)


def import_databases(root, folder, client, snap, record):
    postgres_setup.initialize(folder, client)  # Refuse changed/foreign roles before any reset.
    expected = json.loads((snap / 'backup-format.json').read_text())['database_inventory']
    for database in ('synapse', 'mas'):
        if database not in record['imported']:
            sessions = client.sql('postgres', "SELECT count(*) FROM pg_stat_activity WHERE datname IN ('synapse','mas');").strip()
            if sessions != '0':
                raise RuntimeError('Destination Matrix databases have active connections; stop the unrelated client and repeat database-recover, no database reset performed')
            record['phase'] = 'importing'
            save(root, record)
            client('dropdb', '--if-exists', database)
            client('createdb', '-T', 'template0', '-O', database, '--encoding=UTF8', '--locale=C', database)
            with (snap / (database + '.dump')).open('rb') as data:
                client('pg_restore', '--single-transaction', '--exit-on-error', '-d', database, file=data)
            if inventory(client, database) != expected[database]:
                raise RuntimeError('Destination row counts differ from the consistent snapshot; writers remain stopped, retain both copies and use database-recover')
            if database == 'synapse':
                lifecycle.clear_restored_one_time_keys(database, sql=lambda db, query: client(
                    'psql', '-d', db, '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-f', '-', stdin=query))
            record['imported'].append(database)
            save(root, record)
            lifecycle.emit('database_imported', database=database, passed=True, inventory_compared=True)
        elif inventory(client, database) != expected[database]:
            raise RuntimeError('An imported destination changed before activation; keep both datasets and investigate privately instead of overwriting it')
        elif database == 'synapse':
            # Resumed legacy checkpoints may have imported an old full dump.
            if client.sql('postgres', "SELECT count(*) FROM pg_stat_activity WHERE datname='synapse';").strip() != '0':
                raise RuntimeError('Imported Synapse destination has active connections; stop the unrelated client and repeat database-recover; no keys were cleared')
            lifecycle.clear_restored_one_time_keys(database, sql=lambda db, query: client(
                'psql', '-d', db, '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-f', '-', stdin=query))


def resume(root, record):
    no_active_client(record)
    stage = private_folder(root, record['stage'], 'database-transfers')
    folder = private_folder(root, record['target'], 'database-targets')
    client = Client(root, folder, record)
    version_check(client)
    role_access(client)
    if not record['activated']:
        snap = snapshot(root, stage, record)
        import_databases(root, folder, client, snap, record)
        # Remove only the old owned PostgreSQL container; preserve its entire
        # on-disk dataset. The original Compose file is retained for preactivation abort.
        original = ['docker', 'compose', '--project-directory', str(root), '-f', str(stage / 'compose.yaml')]
        lifecycle.run(original + ['rm', '-s', '-f', 'postgres'])
        values = preflight.settings(folder)
        vault_cache.mark_dirty(root)
        configuration.restore_snapshot(root,stage)
        media.replace_settings(root, {**values, 'POSTGRES_MODE': 'external'})
        metadata = json.loads((stage / 'installation.json').read_text())
        metadata['database_mode'] = 'external'
        state_store.write_json(root / 'installation.json', metadata)
        shutil.copyfile(folder / 'config/external-postgres-owner.json', root / 'config/external-postgres-owner.json')
        (root / 'config/external-postgres-owner.json').chmod(0o600)
        record['phase'] = 'committed'
        save(root, record)
        lifecycle.generate()
        # Write BEFORE first application startup: a crash cannot later restore
        # the stale local snapshot over writes accepted by the new cluster.
        record['activated'] = True
        record['phase'] = 'activating'
        save(root, record)
    lifecycle.up()
    stopped = [name for name in WRITERS if name not in record['writers']]
    if stopped:
        lifecycle.dc('stop', '-t', '30', *stopped)
    if record['writers']:
        lifecycle.wait_writers(record['writers'])
    record['phase'] = 'complete'
    record['completed_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    record['final_freeze_window_seconds'] = (round(max(0, time.time() - record['freeze_started_unix']), 2)
                                            if 'freeze_started_unix' in record else None)
    state_store.write_json(root / 'config/database-last-transition.json', record)
    (root / 'database-transition.json').unlink()
    lifecycle.emit('database_external_connected', passed=True, databases=2, local_data_retained=True,
                   writers_restored=record['writers'], final_freeze_window_seconds=record['final_freeze_window_seconds'],
                   next_action='Run install.sh check, then verify existing accounts, encrypted rooms and historical/new media in your clients; keep the cold local data and backups. If Vault reports an unverified snapshot, retain the complete local cache and run vault-sync when its service is available')


def recover(root):
    record = state_store.database_transition_record(root)
    if record is None:
        lifecycle.emit('database_recovery', pending=False, next_action='Run install.sh check; no database transition pending')
        return
    no_active_client(record)
    if record['phase'] == 'aborting':
        abort(root)
        return
    claim(root, record)
    resume(root, record)


def abort(root):
    record = state_store.database_transition_record(root)
    if record is None:
        raise RuntimeError('No database transition is pending; run install.sh check')
    no_active_client(record)
    if record['activated']:
        raise RuntimeError('Destination may already contain new writes. Automatic return to the cold local database would lose them and is refused. Use database-recover; reverse migration requires a new consistent destination backup and a separately verified restore')
    claim(root, record)
    stage = private_folder(root, record['stage'], 'database-transfers')
    record['phase'] = 'aborting'
    save(root, record)
    configuration.restore_snapshot(root,stage)
    for name in ('installation.json', 'compose.yaml'):
        state_store.write_text(root / name, (stage / name).read_text())
    shutil.copytree(stage / 'config', root / 'config', dirs_exist_ok=True, copy_function=shutil.copy2)
    for path in (stage / 'config').rglob('*'):
        restored = root / 'config' / path.relative_to(stage / 'config')
        os.chown(restored, path.stat().st_uid, path.stat().st_gid)
    for name in ('external-postgres-owner.json', 'postgres-admin.pgpass', 'postgres-ca.pem'):
        if not (stage / 'config' / name).exists():
            (root / 'config' / name).unlink(missing_ok=True)
    if (root / 'backup-writers.json').exists():
        lifecycle.backup_recover()
    lifecycle.dc('up', '-d', 'postgres')
    if record['writers']:
        lifecycle.dc('up', '-d', *record['writers'])
        lifecycle.wait_writers(record['writers'])
    record['phase'] = 'aborted'
    state_store.write_json(stage / 'result.json', record)
    (root / 'database-transition.json').unlink()
    lifecycle.emit('database_local_restored', passed=True, destination_data_retained=True,
                   next_action='Run install.sh check. Retain destination/checkpoints; another migration requires a fresh empty dedicated target')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('external', 'recover', 'abort'))
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--settings', type=Path)
    args = parser.parse_args()
    state_store.require_root()
    root = args.directory.resolve(strict=True)
    lifecycle.configure(root, 'database-' + args.action)
    def interrupted(signum, frame):
        raise DatabaseInterrupted('Database transition interrupted')
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    with state_store.operation_guard(root):
        lifecycle.ownership_check()
        if args.action == 'external':
            state_store.assert_update_allowed(root)
            if not args.settings:
                raise RuntimeError('Use database-external PRIVATE_POSTGRES_ENV')
            external(root, args.settings)
        else:
            # Other pending operations must be recovered by their own commands;
            # database recovery alone admits its own journal.
            if (root / 'update-journal.json').exists():
                raise RuntimeError('Recover the pending source update first with update-recover')
            {'recover': recover, 'abort': abort}[args.action](root)


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, (KeyboardInterrupt, InterruptedError, DatabaseInterrupted)):
            message = 'Database transition interrupted; keep both datasets. Inspect status, wait for any owned client to exit, then use database-recover. database-abort is allowed only before destination activation'
        elif type(error) is RuntimeError:
            message = str(error)
        else:
            message = 'Database operation failed; keep data and private checkpoints. Use database-recover if pending, or correct the private PostgreSQL file and retry database-external'
        print(json.dumps({'check': 'database_storage', 'passed': False, 'message': message}), flush=True)
        raise SystemExit(1)
