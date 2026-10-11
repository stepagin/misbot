"""Allowlisted Misbot calls; raw backend output never reaches Telegram."""

import json
from pathlib import Path
import re
import subprocess

from common import BotError

COMMANDS = {
    'status': ['status'],
    'check': ['check'],
    'start': ['start'],
    'stop': ['stop'],
    'backup': ['backup', 'create'],
    'backup_verify': ['backup', 'verify'],
    'storage': ['storage', 'status'],
    'storage_connect': ['storage', 's3', 'connect'],
    'components': ['features', 'status'],
}
WRITES = frozenset(('start', 'stop', 'backup', 'backup_verify', 'storage_connect'))


def run_control(config, operation):
    if operation not in COMMANDS or operation not in config.get('management_actions', []):
        raise BotError('Эта команда управления не подключена.')
    root = Path(config['stack_root'])
    if not root.is_absolute() or root.resolve() != root or root == Path('/'):
        raise BotError('Проверь каталог установки через SSH.')
    try:
        imported = (
            json.loads((root / 'installation.json').read_text()).get('backend')
            == 'existing-compose'
        )
    except (OSError, ValueError):
        imported = False
    if not imported:
        raise BotError('Это управление требует подключённого стека existing-compose.')
    command = [
        '/usr/bin/python3',
        str(root / 'misbot.py'),
        '--directory',
        str(root),
        *COMMANDS[operation],
    ]
    if operation == 'storage_connect':
        command += ['--config', str(root / '.misbot/s3/media.env')]
    try:
        response = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1200 if operation in WRITES else 85,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {'operation': operation, 'ok': False, 'recovery_required': True}
    result = {'operation': operation, 'ok': response.returncode == 0}
    for line in response.stdout.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(item, dict):
            continue
        if item.get('check') == 'existing_status':
            result['backup_pending'] = bool(item.get('backup_pending'))
            result['containers'] = [
                {
                    'name': c['name'],
                    'running': c.get('running') is True,
                    'healthy': c.get('health') in ('none', 'healthy'),
                }
                for c in item.get('containers', [])
                if isinstance(c, dict)
                and isinstance(c.get('name'), str)
                and re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', c['name'])
            ]
        if item.get('check') == 'existing_checks':
            result['checks'] = [
                {'name': c['name'], 'passed': c.get('passed') is True}
                for c in item.get('checks', [])
                if isinstance(c, dict)
                and isinstance(c.get('name'), str)
                and re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', c['name'])
            ]
        if item.get('check') == 'existing_storage_status':
            result['storage'] = {
                k: item.get(k) is True
                for k in (
                    'media_s3',
                    'backup_s3',
                    'media_pending',
                    'last_backup_verified',
                    'backup_pending',
                )
            }
        if item.get('check') == 'existing_features':
            result['components'] = {
                k: item.get(k) is True
                for k in ('enabled', 'pending', 'referral_bot', 'federation_bot')
            }
            for key in ('admin', 'homepage'):
                value = item.get(key)
                if isinstance(value, str) and re.fullmatch(r'[a-z0-9.-]{1,253}', value):
                    result['components'][key] = value
    if operation in ('status', 'check') and not result.get(
        'containers' if operation == 'status' else 'checks'
    ):
        result['ok'] = False
    if operation == 'storage' and 'storage' not in result:
        result['ok'] = False
    if operation == 'components' and 'components' not in result:
        result['ok'] = False
    return result
