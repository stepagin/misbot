"""Read-only native storage summary without printing provider credentials."""

import argparse
import json
import os
import re
from pathlib import Path

import configuration
import state_store


def summary(root):
    root = Path(root)
    with state_store.operation_guard(root, wait_seconds=5):
        metadata = configuration.read_private_json(root / 'installation.json')
        profile = root / 'settings/backup.env'
        record_path = root / 'config/backup-schedule.json'
        record = configuration.read_private_json(record_path) if record_path.exists() else {}
        verified = None
        catalogs = sorted(
            (
                path
                for path in (root / 'stages/external-backups').glob('*/catalog.json')
                if re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]+', path.parent.name)
            ),
            reverse=True,
        )
        if catalogs:
            stamp = catalogs[0].parent.name.split('-', 1)[0]
            latest = [path for path in catalogs if path.parent.name.startswith(stamp + '-')]
            if len(latest) == 1:
                catalog = configuration.read_private_json(latest[0])
                verified = (
                    catalog.get('upload_verified')
                    if type(catalog.get('upload_verified')) is bool
                    else None
                )
        try:
            from backup_schedule import verified_status

            restored = verified_status(root).get('last_verified')
        except (OSError, RuntimeError, ValueError):
            restored = None
        return {
            'check': 'storage_status',
            'media_s3': metadata.get('media_mode') == 's3',
            'database_mode': metadata.get('database_mode', 'local'),
            'backup_s3': profile.exists() or record.get('mode') == 'external',
            'manual_backup_destination': 'local',
            'local_backup_directory': str(root / 'backups'),
            'media_pending': bool(metadata.get('media_transition_pending')),
            'database_pending': (root / 'database-transition.json').exists(),
            'last_backup_verified': verified,  # Legacy: upload integrity, not database restore.
            'upload_verified': verified,
            'restore_verified': True if restored else None,
            'restore_scope': 'scheduled-history' if restored else None,
            'last_restore_verified_at': restored.get('completed_utc') if restored else None,
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        return 77
    try:
        print(json.dumps(summary(args.directory)))
        return 0
    except (OSError, RuntimeError, ValueError):
        print(
            json.dumps(
                {
                    'check': 'storage_status',
                    'passed': False,
                    'message': 'Сохранённое состояние хранилища не удалось проверить.',
                }
            )
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
