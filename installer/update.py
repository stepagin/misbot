#!/usr/bin/env python3
"""Apply a verified source release to an existing portable installation.

Source updates and an explicitly admitted component pair keep persistent data.
Component rollback checks the actual schema before starting the older runtime.
"""

import argparse
import fcntl
import state_store
import hashlib
import json
import os
from pathlib import Path
import re
import releases
import signal
import shutil
import subprocess
import sys
import time
import uuid

active_child = None
interruption_requested = False


def application_databases(root):
    import configuration_schema

    path = Path(root) / 'installation.json'
    metadata = json.loads(path.read_text()) if path.exists() else {}
    return configuration_schema.owned_databases(metadata)


def install_signals():
    def interrupted(signum, frame):
        global interruption_requested
        interruption_requested = True
        if active_child is not None:
            try:
                active_child.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)


def raise_if_interrupted():
    global interruption_requested
    if interruption_requested:
        interruption_requested = False
        raise InterruptedError(
            'Update interrupted; retained checkpoints and data. Inspect status and use update-recover if recovery is pending'
        )


def run_child(command, output):
    """Forward signals and let backup/up perform their own cleanup before rollback."""
    global active_child
    raise_if_interrupted()
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=subprocess.STDOUT,
        **state_store.child_process_options(),
    )
    active_child = process
    try:
        if interruption_requested:
            process.send_signal(signal.SIGTERM)
        result = subprocess.CompletedProcess(command, process.wait())
    finally:
        active_child = None
    raise_if_interrupted()
    return result


FAMILY = 'misbot-matrix-portable'
ESSENTIAL = {
    'install.sh',
    'generate.py',
    'lifecycle.py',
    'verify.py',
    'referral.py',
    'versions.json',
    'release.json',
    'update.py',
}
SNAPSHOT_FILES = (
    'compose.yaml',
    'images.lock.json',
    'referral-images.lock.json',
    'federation-images.lock.json',
    'synapse-s3-image.lock.json',
    'installation.json',
    'mas-proxy.env',
    'settings.env',
    'data/referral-control/bot-config.json',
    'data/referral-control/bot-policy.json',
)
SNAPSHOT_DIRS = ('config', 'public', 'settings')
CONFIG_FORMAT_VERSIONS = (1, 2)
COMPONENT_SETS = {
    'synapse161': '44639c63a9c13f0c06c70b003731ab49c86f4ac2e3dd40176513be73f699a0b3',
    'synapse162': '145c2ac2c5d58ca922c00bad8ec32c33b8d68d769065034381d2b362a3916099',
}


def component_fingerprint(folder):
    path = folder / 'versions.json'
    if not path.is_file() or path.is_symlink():
        fail('Missing regular component inventory; retain the update checkpoint')
    return hashlib.sha256(
        json.dumps(json.loads(path.read_text()), sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()


def component_plan(before, after):
    previous, candidate = component_fingerprint(before), component_fingerprint(after)
    if previous == candidate:
        return None
    if (previous, candidate) != (COMPONENT_SETS['synapse161'], COMPONENT_SETS['synapse162']):
        fail(
            'Image digest changes are not an approved component path. Use the trusted '
            'Synapse 1.161.0 to 1.162.0 bundle without changing other images; '
            'other paths require a separate full backup and migration procedure'
        )
    return {
        'previous': previous,
        'candidate': candidate,
        'component': 'synapse',
        'from': '1.161.0',
        'to': '1.162.0',
        'schema': 94,
        'compat': 84,
    }


def component_schema(root, wait_background=False):
    import lifecycle

    lifecycle.configure(root, 'component-schema')

    def checked():
        try:
            result = json.loads(
                lifecycle.postgres(
                    'psql',
                    '-d',
                    'synapse',
                    '-X',
                    '-At',
                    '-v',
                    'ON_ERROR_STOP=1',
                    '-c',
                    "SELECT json_build_object('version',"
                    "(SELECT version FROM schema_version),'compat',(SELECT compat_version FROM schema_compat_version),"
                    "'pending',(SELECT count(*) FROM background_updates));",
                )
            )
            if (result.get('version'), result.get('compat')) != (94, 84):
                fail('Actual Synapse schema is outside the approved component pair')
            return result.get('pending') == 0
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
            fail(
                'Cannot verify the approved Synapse schema94/compat84. Keep the data and '
                'update checkpoint; inspect PostgreSQL connectivity/schema privately. '
                'Do not start an older Synapse or replace database files manually'
            )

    if wait_background:
        lifecycle.emit(
            'waiting_for_background_updates',
            attempts=60,
            retry_seconds=2,
            next_action='Wait for this update to finish; if it fails keep the checkpoint and use trusted update-recover',
        )
        for attempt in range(60):
            raise_if_interrupted()
            if checked():
                return
            time.sleep(2)
        fail(
            'Synapse background updates did not finish after 60 checks. Keep the checkpoint and '
            'current database; inspect Synapse background updates privately, then repeat '
            'update-recover from the trusted bundle. Do not delete the journal or start an older runtime manually'
        )
    elif not checked():
        fail(
            'Synapse background updates are still pending; wait for them to finish and '
            'retry update/update-recover; keep all checkpoints and backups'
        )


def component_rollback_guard(root, folder, plan):
    if not plan:
        if component_fingerprint(root) != component_fingerprint(folder):
            fail(
                'Unrecorded component change; no older runtime started. Keep the journal '
                'and restore a verified full archive into an empty target; see backup.md'
            )
        return
    _validate_component_recovery(folder, plan)
    if component_fingerprint(root) not in (plan['previous'], plan['candidate']):
        fail(
            'Component recovery inventory does not match the approved pair; retain the journal and data'
        )
    import lifecycle

    lifecycle.configure(root, 'component-rollback')
    writers = (
        'referral-bot',
        'referral-control',
        'synapse',
        *(['mas'] if 'mas' in application_databases(root) else []),
        'jwt',
    )
    if any(row['Service'] == 'federation-bot' for row in lifecycle.service_rows()):
        writers = ('federation-bot', *writers)
    lifecycle.dc('stop', '-t', '30', *writers)
    if any(
        row['Service'] in (*writers, 'federation-bot')
        and row['State'] in ('running', 'restarting', 'paused')
        for row in lifecycle.service_rows()
    ):
        fail(
            'Component rollback writers did not stop; keep the journal and retry trusted update-recover'
        )
    component_schema(root)


def _validate_component_recovery(folder, plan):
    # A killed atomic source copy can leave either inventory. Validate the
    # recorded pair using constants, without trusting a candidate path in JSON.
    if plan != {
        'previous': COMPONENT_SETS['synapse161'],
        'candidate': COMPONENT_SETS['synapse162'],
        'component': 'synapse',
        'from': '1.161.0',
        'to': '1.162.0',
        'schema': 94,
        'compat': 84,
    }:
        fail('Unsupported component recovery plan; retain the journal')
    if component_fingerprint(folder) != plan['previous']:
        fail('Previous component checkpoint differs from the recovery plan')


def component_runtime(root, expected):
    import lifecycle

    lifecycle.configure(root, 'component-runtime')
    if (
        lifecycle.dc(
            'exec',
            '-T',
            'synapse',
            'python',
            '-c',
            'from synapse import __version__; print(__version__)',
        ).strip()
        != expected
    ):
        fail(
            'Running Synapse version differs from the approved component; retain backups and use trusted update-recover if pending'
        )


def fail(message):
    raise RuntimeError(message)


def version(value):
    try:
        return releases.product_version(value)
    except ValueError as error:
        fail(str(error))


def validate_manifest(source):
    path = source / 'SHA256SUMS'
    if not path.is_file() or path.is_symlink():
        fail('Source SHA256SUMS is missing')
    entries = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([^\r\n]+)', line)
        if not match:
            fail('Invalid source checksum manifest')
        digest, name = match.groups()
        relative = Path(name)
        if (
            relative.is_absolute()
            or '..' in relative.parts
            or name in entries
            or name == 'SHA256SUMS'
        ):
            fail('Unsafe or duplicate source path')
        file = source / relative
        if not file.is_file() or file.is_symlink():
            fail('Source file is missing or is a symlink: ' + name)
        if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            fail('Source checksum mismatch: ' + name)
        entries[name] = file
    if not ESSENTIAL.issubset(entries):
        fail('Source release lacks essential runtime files')
    return entries, hashlib.sha256(path.read_bytes()).hexdigest()


def release(source):
    info = json.loads((source / 'release.json').read_text())
    if info.get('family') != FAMILY or info.get('update_scope') not in (
        'source-and-configuration',
        'source-configuration-and-approved-components',
    ):
        fail('Unexpected installer release family or update scope')
    version(info.get('version'))
    schema = info.get('data_schema_version', 0)
    if type(schema) is not int or schema < 0:
        fail('Invalid release data schema version')
    return info


def admit_release(root, installed, candidate):
    """Shared, read-only version/schema admission before either updater mutates state."""
    import sqlite3

    import data_migrations

    if installed.get('config_format_version', 1) not in CONFIG_FORMAT_VERSIONS:
        fail(
            'Unsupported configuration format; obtain a compatible installer before changing files'
        )
    previous = release(root) if (root / 'release.json').exists() else {}
    for old_version in (previous.get('version'), installed.get('applied_release_version')):
        if old_version is not None and version(candidate['version']) < version(old_version):
            fail('Downgrade refused; use a dedicated restore procedure')
    schema = previous.get('data_schema_version', 0)
    path = data_migrations.database_path(root)
    if path.exists():
        # Read only: admission must not create a database or run candidate migrations.
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)
        try:
            schema = max(schema, db.execute('PRAGMA user_version').fetchone()[0])
        finally:
            db.close()
    if candidate.get('data_schema_version', 0) < schema:
        fail('Data schema downgrade refused; use a dedicated restore procedure')


def run_data_migrations(source, root, candidate, action, checkpoint=None):
    if not candidate.get('data_schema_version'):
        return
    entries, _ = validate_manifest(source)
    if 'data_migrations.py' not in entries:
        fail('Release lacks its declared data migration implementation')
    command = [
        sys.executable,
        str(source / 'data_migrations.py'),
        action,
        '--directory',
        str(root),
        '--expected-schema',
        str(candidate['data_schema_version']),
    ]
    if checkpoint is not None:
        command.extend(['--checkpoint', str(checkpoint)])
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **state_store.child_process_options(),
    )
    if result.returncode:
        fail(
            'Misbot data ' + action + ' failed; retain data and update checkpoints. '
            'Do not start an older runtime on an unknown schema'
        )


def state(root, require_sources=True):
    path = root / 'installation.json'
    if not path.is_file() or path.is_symlink():
        fail('Target is not an initialized portable installation')
    result = json.loads(path.read_text())
    if not result.get('domain') or (
        require_sources
        and not all(
            (root / name).is_file() for name in ('install.sh', 'generate.py', 'lifecycle.py')
        )
    ):
        fail('Target does not contain portable installer state')
    return result


def ensure_regular(path):
    if path.is_symlink():
        fail('Symlink is not allowed in managed path: ' + str(path))


def copy_sources(entries, root):
    changed = []
    for name, source in entries.items():
        target = root / name
        ensure_regular(target)
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            changed.append(name)
    for name in changed:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.new-' + uuid.uuid4().hex)
        shutil.copyfile(entries[name], temporary)
        temporary.chmod(0o700 if name == 'install.sh' else 0o600)
        os.replace(temporary, target)
    manifest = root / 'SHA256SUMS'
    source_manifest = entries['release.json'].parent / 'SHA256SUMS'
    if manifest.resolve() != source_manifest.resolve():
        ensure_regular(manifest)
        temporary = manifest.with_name(manifest.name + '.new-' + uuid.uuid4().hex)
        shutil.copyfile(source_manifest, temporary)
        temporary.chmod(0o600)
        os.replace(temporary, manifest)
    return changed


def copy_tree(source, target):
    if not source.exists():
        return
    for item in source.rglob('*'):
        ensure_regular(item)
    shutil.copytree(source, target, copy_function=shutil.copy2)


def snapshot(root, entries, stamp):
    folder = root / 'stages' / 'updates' / stamp
    folder.mkdir(parents=True, mode=0o700)
    # Recovery must include files present only in the previous manifest, such
    # as former developer tests/reports. Otherwise its checksum check fails.
    previous = validate_manifest(root)[0] if (root / 'SHA256SUMS').is_file() else {}
    names = set(entries) | set(previous) | set(SNAPSHOT_FILES)
    for name in sorted(names):
        path = root / name
        ensure_regular(path)
        if path.is_file():
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for name in SNAPSHOT_DIRS:
        path = root / name
        ensure_regular(path)
        if path.is_dir():
            copy_tree(path, folder / name)
    manifest = root / 'SHA256SUMS'
    if manifest.is_file():
        shutil.copy2(manifest, folder / 'SHA256SUMS')
    return folder


def restore(root, folder, entries):
    names = set(entries) | set(SNAPSHOT_FILES) | {'SHA256SUMS'}
    for name in sorted(names):
        saved = folder / name
        target = root / name
        if saved.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
        elif target.is_file():
            target.unlink()
    for name in SNAPSHOT_DIRS:
        saved = folder / name
        if saved.is_dir():
            shutil.copytree(saved, root / name, dirs_exist_ok=True, copy_function=shutil.copy2)


def run_up(root, log_path):
    with log_path.open('w') as output:
        result = run_child(['bash', str(root / 'install.sh'), 'up', str(root)], output)
    log_path.chmod(0o600)
    return result.returncode


def verified_backup(source, root, folder):
    """Use the candidate backup implementation before replacing installed files."""
    log_path = folder / 'backup.log'
    with log_path.open('w') as output:
        for action in ('backup', 'restore-check'):
            result = run_child(
                ['python3', str(source / 'lifecycle.py'), '--directory', str(root), action], output
            )
            if result.returncode:
                fail(
                    'Pre-update backup or restoration check failed; installed source unchanged. '
                    'Correct the cause and retry update; inspect private log ' + str(log_path)
                )
    log_path.chmod(0o600)


def update_resource_check(root, entries):
    """Refuse before a snapshot/backup can turn a recoverable low-disk state into an outage."""
    import preflight

    preflight.system_check(root)
    # Conservative duplicated payload/config + reserve for a custom DB dump,
    # filesystem metadata and regenerated configuration. Media is compressed
    # below but not assumed to compress for this admission check.
    payload = sum(path.stat().st_size for path in entries.values())
    data = sum(
        path.stat().st_size
        for name in SNAPSHOT_DIRS
        + (
            'data/synapse',
            'data/referral-bot',
            'data/referral-control',
            'data/federation-bot',
            'data/federation',
            'data/admin-panel',
        )
        for path in (root / name).rglob('*')
        if path.is_file() and not path.is_symlink()
    )
    import lifecycle

    lifecycle.configure(root, 'update-resources')
    try:
        database_bytes = int(
            lifecycle.postgres(
                'psql',
                '-d',
                'postgres',
                '-X',
                '-At',
                '-v',
                'ON_ERROR_STOP=1',
                '-c',
                'SELECT '
                + ' + '.join(
                    "pg_database_size('" + name + "')" for name in application_databases(root)
                )
                + ';',
            ).strip()
        )
        if database_bytes <= 0:
            raise ValueError()
    except (OSError, RuntimeError, ValueError):
        fail(
            'Cannot estimate the pre-update database backup size. Installed files unchanged; '
            'check PostgreSQL access privately, then retry update from the trusted bundle'
        )
    required = 3 * 1024**3 + 2 * (payload + data + database_bytes) + 128 * 1024**2
    if shutil.disk_usage(root).free < required:
        fail(
            'Not enough free space for the pre-update snapshot/archive and restart reserve. '
            'Installed files were not changed; check df -h, expand the disk or remove unrelated '
            'files without deleting Matrix data/keys/backups, then retry update from the trusted bundle'
        )


def refresh_command(root):
    """Refresh only after source/health verification; legacy rollback removes
    an owned new wrapper rather than advertising unavailable runtime commands.
    """
    helper = root / 'command_wrapper.py'
    action = 'install'
    if not helper.is_file():
        record = Path('/usr/local/lib/misbot/command.json')
        try:
            owned = json.loads(record.read_text())
        except (OSError, ValueError):
            return
        if owned.get('family') != FAMILY or owned.get('directory') != str(root):
            return
        helper = Path(__file__).resolve().parent / 'command_wrapper.py'
        action = 'remove'
        if not helper.is_file():
            fail('Restore the owned command using the trusted current bundle')
    result = subprocess.run(
        ['/usr/bin/python3', str(helper), action, '--directory', str(root), '--keep-default'],
        capture_output=True,
        **state_store.child_process_options(),
    )
    if result.returncode:
        fail(
            'Command refresh failed; check owned wrapper permissions/conflict without deleting data'
        )


def apply_changes(
    root, folder, entries, candidate, revision, component_transition=None, migration_source=None
):
    try:
        run_data_migrations(
            migration_source or Path(__file__).resolve().parent, root, candidate, 'apply', folder
        )
        changed = copy_sources(entries, root)
        raise_if_interrupted()
        if run_up(root, folder / 'apply.log'):
            fail('Candidate startup failed')
        if component_transition:
            component_schema(root, wait_background=True)
            component_runtime(root, component_transition['to'])
        current = state(root)
        if (
            current.get('applied_release_version') != candidate['version']
            or current.get('applied_release_revision') != revision
        ):
            fail('Candidate release verification failed')
        refresh_command(root)
        return changed
    except BaseException:
        run_data_migrations(
            migration_source or Path(__file__).resolve().parent, root, candidate, 'rollback-check'
        )
        if component_transition:
            component_rollback_guard(root, folder, component_transition)
        previous = validate_manifest(folder)[0] if (folder / 'SHA256SUMS').is_file() else {}
        restore(root, folder, dict.fromkeys(set(entries) | set(previous)))
        recovered = run_up(root, folder / 'rollback.log') == 0
        if recovered:
            refresh_command(root)
        if recovered:
            (root / 'update-journal.json').unlink(missing_ok=True)
        fail(
            'Update failed; previous files restored; rollback healthy='
            + str(recovered)
            + '; inspect private logs in '
            + str(folder)
        )


def require_root():
    if os.geteuid() != 0:
        fail('Run update as root')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('seed', 'apply', 'recover'))
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    require_root()
    install_signals()
    source = args.source.resolve(strict=True)
    root = args.directory.resolve(strict=True)
    if root == Path('/') or root.is_symlink() or source == Path('/'):
        fail('Unsafe source or installation directory')
    with state_store.operation_guard(root):
        if args.action == 'recover':
            validate_manifest(source)
            recover_update(root, source)
        else:
            perform_update(source, root, args.action)


def record_owner(root, record):
    record.update(pid=os.getpid(), started=state_store.process_identity(os.getpid())[1])
    state_store.write_json(root / 'update-journal.json', record)
    os.environ['MATRIX_INSTALLER_UPDATE_STAGE'] = record['stage']


def recover_update(root, trusted_source=None):
    journal = root / 'update-journal.json'
    if not journal.is_file() or journal.is_symlink():
        fail('No private interrupted-update journal exists; use update or up instead')
    info = journal.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        fail('Update journal must be private and owned by the current user')
    record = json.loads(journal.read_text())
    relative = Path(record.get('stage', ''))
    folder = root / relative
    if (
        relative.is_absolute()
        or '..' in relative.parts
        or relative.parts[:2] != ('stages', 'updates')
        or len(relative.parts) != 3
        or folder.resolve() != folder.absolute()
    ):
        fail('Unsafe update recovery checkpoint; retain files and inspect privately')
    entries, _ = validate_manifest(folder)
    release(folder)
    before = state(folder)
    if before.get('config_format_version', 1) not in CONFIG_FORMAT_VERSIONS:
        fail('Unsupported update recovery configuration format')
    if before.get('domain') != state(root, require_sources=False).get('domain'):
        fail('Update recovery checkpoint belongs to another Matrix identity')
    names = record.get('candidate_files')
    if not isinstance(names, list) or any(
        not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
        for name in names
    ):
        fail('Unsafe update recovery source inventory')
    record_owner(root, record)
    if trusted_source is not None:
        run_data_migrations(trusted_source, root, release(trusted_source), 'rollback-check')
    else:
        import data_migrations

        data_migrations.check(root)
    component_rollback_guard(root, folder, record.get('component_transition'))
    restore(root, folder, dict.fromkeys(set(names) | set(entries)))
    if run_up(root, folder / 'recovery.log'):
        fail(
            'Previous files restored but service recovery failed. Keep the checkpoint, correct the cause and repeat update-recover from the trusted installer'
        )
    refresh_command(root)
    validate_manifest(root)
    journal.unlink()
    print('update_recovered', before.get('applied_release_version'), 'healthy', True)


def operation_pending(root):
    if (root / 'update-journal.json').exists():
        fail(
            'Recover the interrupted update first: sudo bash install.sh update-recover '
            + str(root)
            + '; run it from a trusted extracted installer'
        )


def perform_update(source, root, action):
    state_store.assert_backup_allowed(root)
    operation_pending(root)
    state_store.assert_update_allowed(root)
    entries, revision = validate_manifest(source)
    candidate = release(source)
    installed = state(root, require_sources=action != 'seed')
    admit_release(root, installed, candidate)
    import service_identity

    service_identity.validate_installation(root, installed)
    if action == 'seed':
        if installed.get('applied_release_version'):
            fail('Seed is only for first initialization')
        changed = copy_sources(entries, root)
        print('source_release_seeded', candidate['version'], 'files', len(changed))
        return
    if not installed.get('databases_initialized'):
        fail('Run install.sh up once before applying release updates')
    transition = component_plan(root, source)
    if transition:
        component_schema(root)
        component_runtime(root, transition['from'])
    if source == root:
        fail('Run update from a newly extracted release, not from the installation directory')
    configuration = __import__('configuration')
    configuration.assert_identity(root, configuration.load(root), installed)
    run_data_migrations(source, root, candidate, 'check')
    update_resource_check(root, entries)
    lock_dir = root / 'stages'
    lock_dir.mkdir(exist_ok=True)
    with (lock_dir / 'update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('Another update is in progress')
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:6]
        saved = snapshot(root, entries, stamp)
        raise_if_interrupted()
        verified_backup(source, root, saved)
        record_owner(
            root,
            {
                'format': 1,
                'stage': str(saved.relative_to(root)),
                'candidate_files': sorted(entries),
                'component_transition': transition,
            },
        )
        changed = apply_changes(root, saved, entries, candidate, revision, transition, source)
        (root / 'update-journal.json').unlink()
        log = saved / 'apply.log'
        print('release_applied', candidate['version'])
        print('revision', revision[:16])
        print('source_files_changed', len(changed))
        if transition:
            print(
                'component_applied', transition['component'], transition['from'], transition['to']
            )
        print('private_update_log', log)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print('update_error', str(exc), file=sys.stderr)
        sys.exit(1)
