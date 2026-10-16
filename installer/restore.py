#!/usr/bin/env python3
"""Restore a trusted full archive into a dedicated empty local-database target.

Restoration never enables public Matrix writers. Run up only after the source
has been stopped, or when recovering a failed machine with no other writer.
"""

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time

import backup_archive
import lifecycle
import referral
import state_store
import update
import vault_cache
import service_identity


def restore_path(root):
    root = Path(root).absolute()
    if (
        not re.fullmatch(r'/[a-zA-Z0-9._/-]+', str(root))
        or root == Path('/')
        or '..' in root.parts
        or root.resolve() != root
    ):
        raise RuntimeError('Use a dedicated absolute restore path without symlinks')
    return root


def validate_resume_links(root):
    for path in root.rglob('*'):
        if not path.is_symlink():
            continue
        if path != root / 'backups/latest.tar.gz':
            raise RuntimeError(
                'Unexpected symlink in interrupted restore target; inspect privately'
            )
        destination = path.readlink()
        if (
            destination.is_absolute()
            or destination.name != str(destination)
            or not re.fullmatch(r'matrix-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{6}\.tar\.gz', str(destination))
            or (path.parent / destination).is_symlink()
            or not (path.parent / destination).is_file()
        ):
            raise RuntimeError(
                'Unsafe latest backup link in interrupted restore target; inspect privately'
            )


def register_verified_archive(archive, root, digest, stamp):
    """Keep restore-check independent of the original download's location."""
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[a-f0-9]{6}', stamp):
        raise RuntimeError('Unexpected backup snapshot name; obtain a trusted installer backup')
    directory = root / 'backups'
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RuntimeError('Restore backup directory must be private and owned by root')
    link = directory / 'latest.tar.gz'
    if link.exists() and not link.is_symlink():
        raise RuntimeError('Another resource owns latest.tar.gz; no file replaced')
    target = directory / ('matrix-' + stamp + '.tar.gz')
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise RuntimeError('Another resource owns the restored archive path; no file replaced')
        with target.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != digest:
                raise RuntimeError(
                    'Existing restored archive has a different checksum; no file replaced'
                )
    else:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix='.restore-archive-', dir=directory, delete=False
            ) as output:
                temporary = Path(output.name)
                with archive.open('rb') as source:
                    shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            with temporary.open('rb') as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() != digest:
                    raise RuntimeError(
                        'Restore archive copy failed verification; retry the same restore command'
                    )
            os.replace(temporary, target)
        except OSError as error:
            if error.errno == errno.ENOSPC:
                raise RuntimeError(
                    'Not enough free disk space to retain the restored archive. Check df -h '
                    + str(root)
                    + ', expand the disk without deleting Matrix data/keys, then retry the original restore command'
                ) from None
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    # Writers remain staged and the restore lock is held. If killed between
    # these two operations, repeat simply recreates the missing link.
    link.unlink(missing_ok=True)
    link.symlink_to(target.name)


def validate_database_destination(metadata, local_postgres):
    mode = metadata.get('database_mode', 'local')
    if mode not in ('local', 'external'):
        raise RuntimeError('Unknown source database mode; obtain a trusted compatible full backup')
    if mode == 'external' and not local_postgres:
        raise RuntimeError(
            'This archive uses external PostgreSQL. To restore both dumps into a new local PostgreSQL without connecting to the source, use install.sh restore-local ARCHIVE EMPTY_DIRECTORY SHA256 PUBLIC_IP; see backup.md'
        )


def prepare_local_database(root, metadata):
    """Detach the restore-owned target from its archived external connector."""
    replacements = {
        'POSTGRES_MODE': 'local',
        'POSTGRES_HOST': '',
        'POSTGRES_PORT': '5432',
        'POSTGRES_SSL_MODE': 'require',
        'POSTGRES_SSL_ROOT_CERT': '',
        'POSTGRES_ADMIN_USER': 'postgres',
        'POSTGRES_ADMIN_PASSWORD': '',
    }
    vault_cache.mark_dirty(root)
    __import__('configuration').patch(root, replacements)
    for name in ('external-postgres-owner.json', 'postgres-admin.pgpass', 'postgres-ca.pem'):
        (root / 'config' / name).unlink(missing_ok=True)
    metadata['database_mode'] = 'local'


def restore(archive, root, digest, public_ip, local_postgres=False):
    root = restore_path(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    with state_store.operation_guard(root):
        _restore_locked(archive, root, digest, public_ip, local_postgres)


def _restore_locked(archive, root, digest, public_ip, local_postgres=False):
    archive = Path(archive).resolve(strict=True)
    with archive.open('rb') as source:
        actual = hashlib.file_digest(source, 'sha256').hexdigest()
    if actual != digest:
        raise RuntimeError('Archive SHA256 differs from the source; target unchanged')
    marker = root / 'restore-journal.json'
    if root.exists() and any(root.iterdir()):
        if not marker.is_file() or marker.is_symlink():
            raise RuntimeError(
                'Refusing to restore into a nonempty installation; use an empty target'
            )
        progress = json.loads(marker.read_text())
        if progress.get('archive_sha256') != actual:
            raise RuntimeError(
                'Another archive owns this interrupted restore; use a different empty target'
            )
        if progress.get('public_ip') != public_ip:
            raise RuntimeError(
                'Interrupted restore belongs to another target IP; repeat its original command'
            )
        if progress.get('local_postgres', False) != local_postgres:
            raise RuntimeError(
                'Interrupted restore belongs to a different database policy; repeat its original restore or restore-local command without changing the destination'
            )
        if progress.get('stage') == 'ready' and not json.loads(
            (root / 'installation.json').read_text()
        ).get('restore_pending'):
            raise RuntimeError('Target was already activated; refusing to replace its newer data')
        validate_resume_links(root)
    else:
        progress = {
            'archive_sha256': actual,
            'public_ip': public_ip,
            'stage': 'new',
            'local_postgres': local_postgres,
        }
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.matrix-restore-', dir=root.parent) as temporary:
        temporary = Path(temporary)
        outer = temporary / 'outer'
        outer.mkdir(mode=0o700)
        backup_archive.extract(archive, outer)
        snapshot = backup_archive.verify_snapshot(outer)
        files = temporary / 'files'
        files.mkdir(mode=0o700)
        backup_archive.extract(snapshot / 'files.tar.gz', files, owners=True)
        metadata = json.loads((files / 'installation.json').read_text())
        databases = backup_archive.validate_authentication_boundary(snapshot, metadata)
        configuration = __import__('configuration')
        configuration.assert_identity(files, configuration.load(files), metadata)
        service_identity.validate_installation(files, metadata)
        source_database_mode = metadata.get('database_mode', 'local')
        validate_database_destination(metadata, local_postgres)
        update.validate_manifest(files)
        saved_release = update.release(files)
        referral.verify_archive(snapshot / 'files.tar.gz')
        __import__('federation_runtime').verify_archive(snapshot / 'files.tar.gz')
        if __import__('telegram_runtime').enabled(metadata):
            __import__('telegram_runtime').verify_archive(snapshot / 'files.tar.gz')
        # Restored targets use the current compatible installer, including the
        # mandatory writer guard, even if the archive predates that guard.
        runtime = Path(__file__).resolve().parent
        entries, _ = update.validate_manifest(runtime)
        candidate = update.release(runtime)
        if (
            update.version(candidate['version']) < update.version(saved_release['version'])
            or (runtime / 'versions.json').read_bytes() != (files / 'versions.json').read_bytes()
        ):
            raise RuntimeError(
                'Restore requires a compatible installer with the same component digests and no downgrade'
            )
        update.copy_sources(entries, files)
        # Publish only staged metadata, including when killed halfway through
        # copytree. Never briefly install an archived live external connector.
        metadata.pop('databases_initialized', None)
        metadata.pop('network_committed', None)
        metadata.pop('migration_frozen', None)
        metadata['restore_pending'] = True
        if local_postgres:
            prepare_local_database(files, metadata)
        state_store.write_json(files / 'installation.json', metadata)
        root.mkdir(mode=0o700, exist_ok=True)
        root.chmod(0o700)
        with state_store.operation_guard(root):
            if marker.exists():
                progress = json.loads(marker.read_text())
                if (
                    progress.get('archive_sha256') != actual
                    or progress.get('public_ip') != public_ip
                ):
                    raise RuntimeError(
                        'Restore target changed while waiting; use its original command'
                    )
                if progress.get('local_postgres', False) != local_postgres:
                    raise RuntimeError(
                        'Restore database policy changed while waiting; repeat its original command'
                    )
                if progress.get('stage') == 'ready' and not json.loads(
                    (root / 'installation.json').read_text()
                ).get('restore_pending'):
                    raise RuntimeError('Target was activated; refusing to overwrite newer data')
            # Ownership guard runs before any existing container can be touched.
            lifecycle.configure(root, 'restore')
            lifecycle.ownership_check()
            progress.update(pid=os.getpid(), started=state_store.process_identity(os.getpid())[1])
            state_store.write_json(marker, progress)
            if progress['stage'] == 'new':
                state_store.write_json(marker, progress)
            if progress['stage'] in ('new', 'extracting'):
                progress['stage'] = 'extracting'
                state_store.write_json(marker, progress)
                # Repeat may complete an interrupted extraction of this same archive.
                shutil.copytree(files, root, dirs_exist_ok=True, copy_function=shutil.copy2)
                for source in files.rglob('*'):
                    target = root / source.relative_to(files)
                    os.chown(target, source.stat().st_uid, source.stat().st_gid)
                state_store.write_json(root / 'installation.json', metadata)
                __import__('configuration').patch(root, {'PUBLIC_IP': public_ip})
                progress['stage'] = 'files_ready'
                state_store.write_json(marker, progress)
            # A resumed target can contain an older restore worker. Replace
            # only its attested installer payload before invoking its child.
            update.copy_sources(entries, root)
            installed = json.loads((root / 'installation.json').read_text())
            installed['applied_release_version'] = candidate['version']
            state_store.write_json(root / 'installation.json', installed)
            previous = os.environ.get('MATRIX_INSTALLER_RESTORE_ARCHIVE')
            os.environ['MATRIX_INSTALLER_RESTORE_ARCHIVE'] = actual
            try:
                lifecycle.generate()
            finally:
                if previous is None:
                    os.environ.pop('MATRIX_INSTALLER_RESTORE_ARCHIVE', None)
                else:
                    os.environ['MATRIX_INSTALLER_RESTORE_ARCHIVE'] = previous
            lifecycle.dc('up', '-d', 'postgres')
            lifecycle.wait(
                lambda: lifecycle.postgres('psql', '-d', 'postgres', '-Atc', 'SELECT 1').strip()
                == '1',
                service='postgres',
            )
            if progress['stage'] != 'ready':
                # Only this restore-owned target is reset after a partial import.
                progress['stage'] = 'importing'
                state_store.write_json(marker, progress)
                for database in databases:
                    lifecycle.postgres('dropdb', '--if-exists', database)
                    lifecycle.postgres('createdb', '-T', 'template0', '-O', database, database)
                    with (snapshot / (database + '.dump')).open('rb') as source:
                        result = subprocess.run(
                            lifecycle.postgres_command(
                                'pg_restore', '--exit-on-error', '-d', database
                            ),
                            stdin=source,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            **state_store.child_process_options(),
                        )
                    if result.returncode:
                        raise RuntimeError(
                            'Database import failed; target writers remain disabled. Retry the same restore command'
                        )
                    # Verify the restored schema exists, rather than merely accepting pg_restore exit.
                    count = lifecycle.postgres(
                        'psql',
                        '-d',
                        database,
                        '-Atc',
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
                    ).strip()
                    if not count.isdigit() or int(count) == 0:
                        raise RuntimeError('Restored database contains no application tables')
                    metadata_path = snapshot / 'backup-format.json'
                    expected = (
                        json.loads(metadata_path.read_text())
                        .get('database_inventory', {})
                        .get(database)
                        if metadata_path.exists()
                        else None
                    )
                    if expected is not None and lifecycle.database_inventory(database) != expected:
                        raise RuntimeError(
                            'Restored application row counts differ from the backup inventory'
                        )
                    if database == 'synapse':
                        lifecycle.clear_restored_one_time_keys(database)
                    lifecycle.emit(
                        'restored_database',
                        database=database,
                        tables=int(count),
                        passed=True,
                        inventory_compared=expected is not None,
                    )
                metadata = lifecycle.state()
                metadata['databases_initialized'] = True
                metadata['network_committed'] = True
                state_store.write_json(root / 'installation.json', metadata)
                register_verified_archive(archive, root, actual, snapshot.name)
                progress['stage'] = 'ready'
                progress['completed_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                state_store.write_json(marker, progress)
            else:
                register_verified_archive(archive, root, actual, snapshot.name)
            lifecycle.emit(
                'restore_staged',
                passed=True,
                server_name=metadata['domain'],
                writers_disabled=True,
                database_mode='local',
                source_database_mode=source_database_mode,
                **(
                    {
                        'authentication': 'external-MAS-not-restored',
                        'next_action': 'Stop the source; retain or restore the matching external MAS state, then activate with --external-mas-confirm preserved-state.',
                    }
                    if metadata.get('mas_mode') == 'external'
                    else {
                        'next_action': 'Stop the source writer, then use install.sh restore-activate'
                    }
                ),
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--public-ip', required=True)
    parser.add_argument(
        '--local-postgres',
        action='store_true',
        help='Explicitly restore either source database mode into new local PostgreSQL',
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run restore as root')
    import ipaddress

    address = ipaddress.ip_address(args.public_ip)
    if address.version != 4 or not address.is_global:
        raise RuntimeError('Use the target public IPv4 address')
    os.umask(0o077)
    restore(args.archive, args.directory, args.sha256, args.public_ip, args.local_postgres)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    'check': 'restore',
                    'passed': False,
                    'error_type': type(error).__name__,
                    'message': str(error)
                    if isinstance(error, RuntimeError)
                    else 'Restore failed; keep the target and retry the same command after correcting the cause',
                }
            )
        )
        raise SystemExit(1)
