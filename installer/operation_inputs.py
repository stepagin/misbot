"""Resolve known operation inputs; keep new choices in the existing terminal forms."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import cli_commands
import configuration
import configuration_schema as schema


class MissingBackupKey(RuntimeError):
    """A saved profile exists but its original key must be supplied or replaced deliberately."""


def saved_backup(root, *, key_override=None):
    """Return only a saved, private profile/key pair; never create a replacement key."""
    import backup_crypto
    import backup_schedule

    root = Path(root)
    metadata_path = root / 'installation.json'
    if not metadata_path.exists():
        return None
    metadata = configuration.read_private_json(metadata_path)
    if metadata.get('backend') == 'existing-compose':
        profile = root / '.misbot/s3/backup.env'
        key_value = metadata.get('existing', {}).get('backup_key')
    else:
        record_path = root / 'config/backup-schedule.json'
        record = backup_schedule.read_record(root) if record_path.exists() else None
        if record and record.get('mode') == 'external':
            profile = Path(record['settings'])
            key_value = record.get('key_file')
        else:
            profile = root / 'settings/backup.env'
            if not profile.exists():
                return None
            values = schema.validate_backup(
                configuration.parse(
                    configuration.read_private(profile), profile, schema.BACKUP_FIELDS
                ),
                str(profile),
            )
            key_value = values.get('BACKUP_KEY_FILE')
    key_value = key_override or key_value
    if not key_value or not profile.exists():
        return None
    key = Path(key_value)
    if not key.exists() and not key.is_symlink():
        raise MissingBackupKey('Сохранённый ключ копий отсутствует.')
    for path in (profile, key):
        backup_schedule.safe_path(str(path))
        backup_schedule.private_file(path)
    if key.is_relative_to(root):
        raise ValueError('Ключ резервной копии должен храниться вне установки.')
    backup_crypto.read_key(key)
    return profile, key


def saved_calendar(root):
    root = Path(root)
    metadata = configuration.read_private_json(root / 'installation.json')
    adopted = metadata.get('backend') == 'existing-compose'
    path = root / (
        '.misbot/features/backup-schedule.json' if adopted else 'config/backup-schedule.json'
    )
    if not path.exists():
        return ''
    record = configuration.read_private_json(path)
    if not adopted and record.get('root') != str(root):
        raise ValueError('Расписание принадлежит другой установке.')
    value = record.get('calendar', '')
    return schema.backup_calendar(value) if value else ''


def defaults(root, command, values):
    """No mutations and no guessed provider, recovery digest or operation target."""
    values = dict(values)
    root = Path(root)
    installed = (root / 'installation.json').exists()
    if command.path in ('backup external create', 'backup schedule external', 'backup fetch'):
        # An explicit different profile must not silently acquire the saved key.
        try:
            pair = (
                saved_backup(root, key_override=values.get('key'))
                if installed and not values.get('config')
                else None
            )
        except MissingBackupKey:
            pair = None
        if pair:
            profile, key = pair
            if not values.get('config') or Path(values['config']) == profile:
                values.setdefault('config', None)
                values['config'] = values['config'] or str(profile)
                values['key'] = values.get('key') or str(key)
    if command.path.startswith('backup schedule ') and installed and not values.get('calendar'):
        values['calendar'] = saved_calendar(root) or None
    if command.path == 'backup key create' and not values.get('key'):
        values['key'] = '/root/misbot-backup.key'
    if command.path == 'storage s3 check' and installed:
        # Only use an explicitly selected purpose; media and backups may differ.
        purpose = values.get('purpose')
        if purpose in ('media', 'backup') and not values.get('config'):
            metadata = configuration.read_private_json(root / 'installation.json')
            if metadata.get('backend') == 'existing-compose':
                candidate = root / '.misbot/s3' / (purpose + '.env')
            else:
                candidate = (
                    root / 'settings' / ('backup.env' if purpose == 'backup' else 'media-s3.env')
                )
            if candidate.exists():
                configuration.read_private(candidate)
                values['config'] = str(candidate)
    return values


def local_accounts(root):
    """Read public local account identities through the existing private MAS transport."""
    import state_store

    root = Path(root)
    with state_store.operation_guard(root, wait_seconds=5):
        metadata = configuration.read_private_json(root / 'installation.json')
        if metadata.get('backend') == 'existing-compose':
            return []
        client = configuration.read_private_json(root / 'secrets/referral-client.json')
        try:
            completed = subprocess.run(
                [
                    'docker',
                    'compose',
                    '--project-directory',
                    str(root),
                    '-f',
                    str(root / 'compose.yaml'),
                    'exec',
                    '-T',
                    'synapse',
                    'python',
                    '-c',
                    (root / 'federation_admin_api.py').read_text(),
                ],
                input=json.dumps({'client': client, 'action': 'list', 'username': ''}),
                capture_output=True,
                text=True,
                timeout=45,
                **state_store.child_process_options(),
            )
        except subprocess.SubprocessError:
            raise RuntimeError(
                'Список аккаунтов пока недоступен; повторите или введите имя.'
            ) from None

    if completed.returncode:
        raise RuntimeError('Не удалось получить список аккаунтов.')
    raw = completed.stdout
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('accounts'), list):
        raise RuntimeError('Не удалось получить список аккаунтов.')
    result = []
    for row in data['accounts']:
        name = row.get('username') if isinstance(row, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._=-]{0,254}', name):
            raise RuntimeError('Не удалось проверить список аккаунтов.')
        if type(row.get('admin')) is not bool:
            raise RuntimeError('Не удалось проверить права аккаунта.')
        result.append(
            {'username': name, 'mxid': '@' + name + ':' + metadata['domain'], 'admin': row['admin']}
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--command', choices=tuple(cli_commands.AUTOMATIC_ARGUMENTS), required=True)
    args, remaining = parser.parse_known_args(argv)
    if os.geteuid() != 0:
        return 77
    import command_wrapper
    import misbot

    command, _, values = misbot.parse(args.command.split() + remaining)
    if (args.directory / 'installation.json').exists():
        command_wrapper.trusted_installation(args.directory)
    values = defaults(args.directory, command, values)
    if all(values.get(name) is not None for name in command.arguments):
        entry = Path(__file__).resolve().parent / 'misbot.py'
        invocation = [
            '/usr/bin/python3',
            str(entry),
            '--directory',
            str(args.directory),
            *command.path.split(),
        ]
        for key, value in values.items():
            if value is True:
                invocation.append('--' + key.replace('_', '-'))
            elif isinstance(value, str):
                cli_commands.append_option(invocation, '--' + key.replace('_', '-'), value)
        os.execv(invocation[0], invocation)
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        missing = ', '.join('--' + name for name in command.arguments if values.get(name) is None)
        print(
            'Нет сохранённых данных: '
            + missing
            + '. Откройте sudo misbot ui или передайте параметры явно.',
            file=sys.stderr,
        )
        return 2
    import terminal_ui
    import ui_actions

    ui = terminal_ui.UI(directory=args.directory, view=terminal_ui.TerminalView(plain=True))
    ui.supplied_arguments = {key: value for key, value in values.items() if value is not None}
    try:
        return ui.execute(ui_actions.Action(command.path, command.purpose))
    except (terminal_ui.Back, terminal_ui.Cancel, terminal_ui.SaveExit):
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('Ввод отменён.', file=sys.stderr)
        raise SystemExit(130) from None
    except (OSError, ValueError, RuntimeError, KeyError):
        print(
            'Не удалось проверить сохранённые параметры. Откройте sudo misbot ui или укажите параметры явно.',
            file=sys.stderr,
        )
        raise SystemExit(2) from None
