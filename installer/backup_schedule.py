"""Own systemd timers for verified local or encrypted external backups."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import time

import backup_crypto
import external_backup
import lifecycle
import state_store

UNIT_DIRECTORY = Path('/etc/systemd/system')


def command(*arguments, allowed=(0,)):
    result = subprocess.run(
        list(arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        **state_store.child_process_options(),
    )
    if result.returncode not in allowed:
        raise RuntimeError(
            'Backup schedule command failed: '
            + Path(arguments[0]).name
            + '. Inspect the unit status privately, correct the cause and retry'
        )
    return result


def safe_path(value):
    path = Path(value)
    if (
        not path.is_absolute()
        or path == Path('/')
        or '..' in path.parts
        or not re.fullmatch(r'/[A-Za-z0-9 ._/-]+', str(path))
        or path.resolve() != path.absolute()
    ):
        raise RuntimeError('Use absolute backup paths without parent traversal or symlinks')
    return path


def private_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise RuntimeError(
            'A required private backup file is missing. Restore the original S3 settings/recovery key on this VPS, then retry; a new key cannot decrypt previous backups'
        ) from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise RuntimeError('Backup schedule files must be private regular files owned by root')


def unit_name(root):
    return 'matrix-backup-' + hashlib.sha256(str(root).encode()).hexdigest()[:16]


def validate_calendar(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(c) < 32 or ord(c) > 126 or c in '%\\' for c in value)
    ):
        raise RuntimeError(
            'Provide one systemd calendar expression, for example: *-*-* 03:00:00 UTC'
        )
    if command('systemd-analyze', 'calendar', value, allowed=(0, 1)).returncode:
        raise RuntimeError(
            'Invalid backup calendar. Check it with systemd-analyze calendar, then retry'
        )


def validate_record(root, record):
    if (
        record.get('format') != 1
        or record.get('root') != str(root)
        or record.get('unit') != unit_name(root)
        or record.get('mode') not in ('local', 'external')
    ):
        raise RuntimeError(
            'Backup schedule belongs to another installation or has an unsupported format'
        )
    validate_calendar(record.get('calendar'))
    if record['mode'] == 'external':
        settings = safe_path(record.get('settings', ''))
        key = safe_path(record.get('key_file', ''))
        private_file(settings)
        private_file(key)
        external_backup.settings(settings)
        backup_crypto.read_key(key)
        if key.is_relative_to(root):
            raise RuntimeError('Keep the recovery key outside the installation and its backups')
    elif lifecycle.state().get('media_mode') == 's3':
        raise RuntimeError(
            'Scheduled S3 media backups require backup-schedule with independent backup S3 settings; local-only snapshots may omit remote files'
        )


def read_record(root, allow_moved=False):
    path = root / 'config/backup-schedule.json'
    if not path.exists() and not path.is_symlink():
        return None
    private_file(path)
    record = json.loads(path.read_text())
    if not allow_moved and (
        record.get('root') != str(root) or record.get('unit') != unit_name(root)
    ):
        raise RuntimeError('Backup schedule record belongs to another installation')
    return record


def snapshot(root, record):
    validate_record(root, record)
    state_store.assert_update_allowed(root)
    metadata = lifecycle.state()
    if not metadata.get('databases_initialized'):
        raise RuntimeError('Start and check the installation before configuring regular backups')
    if metadata.get('migration_frozen') or metadata.get('restore_pending'):
        raise RuntimeError(
            'Disable the timer during migration; a frozen or staged server cannot run scheduled backups'
        )
    lifecycle.ownership_check()
    if record['mode'] == 'external':
        external_backup.export(
            root, external_backup.settings(record['settings']), record['key_file']
        )
    else:
        lifecycle.backup()
    lifecycle.restore_check()
    state_store.write_json(
        root / 'config/backup-schedule-verified.json',
        {
            'format': 1,
            'root': str(root),
            'unit': unit_name(root),
            'mode': record['mode'],
            'completed_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'two_database_restores': True,
        },
    )
    lifecycle.emit('scheduled_backup_verified', mode=record['mode'], passed=True)


def unit_texts(root, record):
    header = '# Matrix installer backup: ' + str(root) + '\n'
    service = header + (
        '[Unit]\nDescription=Verified Matrix backup\nAfter=docker.service network-online.target\n'
        'Wants=network-online.target\n\n[Service]\nType=oneshot\nUser=root\nGroup=root\nUMask=0077\n'
        'Environment=DOCKER_HOST=unix:///var/run/docker.sock\nEnvironment=DOCKER_CONTEXT=\n'
        'ExecStart=/usr/bin/python3 "'
        + str(root / 'backup_schedule.py')
        + '" run --directory "'
        + str(root)
        + '"\n'
        'TimeoutStartSec=6h\nTimeoutStopSec=10min\nKillMode=mixed\n'
        'StandardOutput=append:'
        + str(root / 'stages/backup-scheduled.log')
        + '\nStandardError=inherit\n'
    )
    timer = header + (
        '[Unit]\nDescription=Regular verified Matrix backup\n\n[Timer]\n'
        'OnCalendar=' + record['calendar'] + '\nPersistent=true\nAccuracySec=1s\n'
        'Unit=' + record['unit'] + '.service\n\n[Install]\nWantedBy=timers.target\n'
    )
    return {record['unit'] + '.service': service, record['unit'] + '.timer': timer}


def owned_unit(path, root):
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or not path.read_text().startswith('# Matrix installer backup: ' + str(root) + '\n')
    ):
        raise RuntimeError('Another resource owns the backup unit path; no unit replaced')
    return path.read_text()


def configure(root, record):
    validate_record(root, record)
    texts = unit_texts(root, record)
    previous_units = {name: owned_unit(UNIT_DIRECTORY / name, root) for name in texts}
    path = root / 'config/backup-schedule.json'
    previous = read_record(root, allow_moved=True)
    previous_bytes = path.read_text() if previous is not None else None
    log = root / 'stages/backup-scheduled.log'
    if log.exists() or log.is_symlink():
        private_file(log)
    else:
        state_store.write_text(log, '')
    with tempfile.TemporaryDirectory(prefix='matrix-backup-units-') as folder:
        staged = []
        for name, text in texts.items():
            target = Path(folder) / name
            target.write_text(text)
            staged.append(str(target))
        command('systemd-analyze', 'verify', *staged)
    # A new timer is admitted only after its actual destination and two dumps pass.
    snapshot(root, record)
    timer = record['unit'] + '.timer'
    was_enabled = command('systemctl', 'is-enabled', timer, allowed=(0, 1, 3, 4)).returncode == 0
    try:
        if any(value is not None for value in previous_units.values()):
            command('systemctl', 'disable', '--now', timer)
        state_store.write_json(path, dict(record, enabled=False))
        for name, text in texts.items():
            state_store.write_text(UNIT_DIRECTORY / name, text, mode=0o644)
        command('systemctl', 'daemon-reload')
        command('systemctl', 'enable', '--now', timer)
        state_store.write_json(path, dict(record, enabled=True))
    except BaseException as error:
        command('systemctl', 'disable', '--now', timer, allowed=(0, 1, 4, 5))
        for name, text in previous_units.items():
            target = UNIT_DIRECTORY / name
            if text is None:
                if owned_unit(target, root) is not None:
                    target.unlink()
            else:
                state_store.write_text(target, text, mode=0o644)
        if previous_bytes is None:
            path.unlink(missing_ok=True)
        else:
            state_store.write_text(path, previous_bytes)
        command('systemctl', 'daemon-reload')
        if was_enabled:
            command('systemctl', 'enable', '--now', timer)
        if isinstance(error, RuntimeError):
            raise RuntimeError(
                'Backup schedule could not be enabled. Previous units and configuration restored; '
                'existing data and the admitted backup retained. Check: sudo bash '
                + str(root / 'install.sh')
                + ' backup-schedule-status. Correct the systemd problem, then repeat your original '
                'backup-schedule or backup-schedule-local command'
            ) from None
        raise
    lifecycle.emit(
        'backup_schedule',
        enabled=True,
        unit=record['unit'],
        calendar=record['calendar'],
        log=str(log),
        next_action='Run install.sh backup-schedule-status and check free disk regularly',
    )


def disable(root):
    record = read_record(root, allow_moved=True)
    if record is None:
        lifecycle.emit('backup_schedule', enabled=False, changed=False)
        return
    timer = unit_name(root) + '.timer'
    if owned_unit(UNIT_DIRECTORY / timer, root) is not None:
        command('systemctl', 'disable', '--now', timer)
    state_store.write_json(
        root / 'config/backup-schedule.json',
        dict(record, root=str(root), unit=unit_name(root), enabled=False),
    )
    lifecycle.emit(
        'backup_schedule',
        enabled=False,
        changed=True,
        next_action='An active backup continues; use backup-schedule-status before changing or removing the installation',
    )


def verified_status(root):
    path = root / 'config/backup-schedule-verified.json'
    if not path.exists() and not path.is_symlink():
        return {'last_verified': None}
    private_file(path)
    saved = json.loads(path.read_text())
    if (
        saved.get('format') != 1
        or saved.get('mode') not in ('local', 'external')
        or saved.get('two_database_restores') is not True
        or not isinstance(saved.get('completed_utc'), str)
        or not re.fullmatch(
            r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', saved['completed_utc']
        )
    ):
        raise RuntimeError(
            'Invalid backup verification record; inspect privately and verify a new snapshot'
        )
    if saved.get('root') != str(root) or saved.get('unit') != unit_name(root):
        return {'last_verified': None, 'verification_on_this_target_required': True}
    return {
        'last_verified': {
            'mode': saved['mode'],
            'completed_utc': saved['completed_utc'],
            'two_database_restores': True,
        }
    }


def status(root):
    record = read_record(root, allow_moved=True)
    verified = verified_status(root)
    if record is None:
        lifecycle.emit('backup_schedule_status', configured=False, **verified)
        return
    if record.get('root') != str(root) or record.get('unit') != unit_name(root):
        lifecycle.emit(
            'backup_schedule_status',
            configured=True,
            needs_reconfigure=True,
            **verified,
            next_action='Transfer private backup settings/key, then configure backup-schedule here; the source timer is not modified',
        )
        return
    lifecycle.emit(
        'backup_schedule_status',
        configured=True,
        enabled=bool(record.get('enabled')),
        **verified,
        next_action='Last verified describes a completed snapshot, not the last attempt; check the service and private backup-scheduled.log for later failures',
    )
    for suffix, properties in (
        ('timer', ('ActiveState', 'NextElapseUSecRealtime', 'LastTriggerUSec')),
        ('service', ('ActiveState', 'Result', 'ExecMainStatus', 'ExecMainStartTimestampMonotonic')),
    ):
        result = command(
            'systemctl',
            'show',
            unit_name(root) + '.' + suffix,
            *['--property=' + name for name in properties],
            allowed=(0, 1, 4),
        )
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        extra = (
            {
                'execution_metadata_available': fields.get('ExecMainStartTimestampMonotonic', '')
                not in ('', '0')
            }
            if suffix == 'service'
            else {}
        )
        lifecycle.emit(
            'backup_schedule_status',
            resource=suffix,
            **extra,
            **{name: fields.get(name, '') for name in properties},
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('configure', 'run', 'disable', 'status'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--mode', choices=('local', 'external'))
    parser.add_argument('--calendar')
    parser.add_argument('--settings')
    parser.add_argument('--key-file')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run backup schedule commands as root')
    root = safe_path(args.directory)
    lifecycle.configure(root, 'backup-schedule-' + args.action)

    def interrupted(signum, frame):
        raise state_store.OperationInterrupted(
            'Scheduled backup interrupted; inspect status and backup-recover'
        )

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    if args.action == 'status':
        status(root)
        return
    with state_store.operation_guard(root):
        if args.action == 'disable':
            disable(root)
            return
        state_store.assert_update_allowed(root)
        if args.action == 'run':
            record = read_record(root)
            if record is None:
                raise RuntimeError('Configure backup-schedule or backup-schedule-local first')
            snapshot(root, record)
        else:
            record = {
                'format': 1,
                'root': str(root),
                'unit': unit_name(root),
                'mode': args.mode,
                'calendar': args.calendar,
                'settings': args.settings,
                'key_file': args.key_file,
            }
            configure(root, record)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    'check': 'backup_schedule',
                    'passed': False,
                    'message': external_backup.failure_message(error),
                }
            )
        )
        raise SystemExit(1)
