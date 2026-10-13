"""Allowlisted server reads and durable operations shared by administrator interfaces."""

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

import command_wrapper
import configuration
import state_store

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
    'backup_schedule': ['backup', 'schedule', 'status'],
    'backup_preview': [],
    'statistics': [],
    'update_check': [],
    'restart': [],
    'update': [],
}
WRITES = frozenset(
    ('start', 'stop', 'restart', 'backup', 'backup_verify', 'storage_connect', 'update')
)
PERIODS = frozenset(('day', 'week', 'month', 'all'))
ACTIVE = frozenset(('queued', 'running'))
FINISHED = frozenset(('complete', 'failed', 'interrupted'))
MAX_JOBS = 1000
MAX_QUEUE_BYTES = 4 * 1024 * 1024
RESOURCE_FIELDS = frozenset(
    (
        'cpu_used_percent',
        'cpu_count',
        'cpu_load',
        'memory_total_bytes',
        'memory_used_bytes',
        'memory_available_bytes',
        'swap_total_bytes',
        'swap_used_bytes',
        'disk_total_bytes',
        'disk_used_bytes',
        'disk_free_bytes',
        'uptime_seconds',
    )
)
RECOVERY = 'Завершение операции не подтверждено. Проверьте sudo misbot status через SSH; при pending используйте указанную команду восстановления.'


def root_path(value):
    root = Path(value)
    if (
        not root.is_absolute()
        or root.resolve() != root
        or root == Path('/')
        or any(ord(c) < 32 for c in str(root))
    ):
        raise RuntimeError('Каталог установки не подтверждён. Проверьте его через SSH.')
    if (root / 'installation.json').is_symlink():
        raise RuntimeError('Каталог установки не подтверждён. Проверьте его через SSH.')
    return root


def installation_kind(root):
    try:
        kind = json.loads((root / 'installation.json').read_text()).get('backend', 'portable')
    except (OSError, ValueError, TypeError):
        raise RuntimeError('Установка не подтверждена. Проверьте её через SSH.') from None
    if kind not in ('existing-compose', 'portable'):
        raise RuntimeError('Этот режим установки не поддерживает управление службами.')
    return kind


def records(raw):
    for line in raw.splitlines():
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict):
            yield value


def safe_name(value):
    return isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_.-]{1,80}', value)


def check_detail(item):
    from terminal_diagnostics import FAILURES

    code = item.get('failure_code')
    detail = (
        FAILURES.get(code, FAILURES['check_failed'])
        if isinstance(code, str)
        else FAILURES['check_failed']
    )
    if code == 'http_status':
        status, expected = item.get('status'), item.get('expected')
        if type(status) is int and 100 <= status <= 599:
            detail = 'HTTP ' + str(status) + '.'
            if type(expected) is int and 100 <= expected <= 599:
                detail += ' Ожидается HTTP ' + str(expected) + '.'
                if status == expected:
                    detail += ' Содержимое ответа не соответствует настройкам.'
    return detail


def media_profile_ready(config):
    """Validate the prepared private profile locally, without revealing its contents."""
    import s3_config

    root = root_path(config['stack_root'])
    try:
        s3_config.read_profile(root / '.misbot/s3/media.env')
    except (s3_config.ConfigError, OSError, ValueError):
        return False
    return True


def utc_timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value
    ):
        return False
    try:
        datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
        return True
    except ValueError:
        return False


def safe_timer_date(value):
    return isinstance(value, str) and bool(
        re.fullmatch(
            r'(?:[A-Za-z]{3} )?\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?: [A-Za-z0-9_+:/-]{1,30})?',
            value,
        )
    )


def backup_preview(root):
    metadata = configuration.read_private_json(Path(root) / 'installation.json')
    adopted = metadata.get('backend') == 'existing-compose'
    return {
        'destination': 'external'
        if adopted and (Path(root) / '.misbot/s3/backup-active.json').is_file()
        else 'local',
        'authentication': 'external-MAS-not-included'
        if metadata.get('mas_mode') == 'external'
        else 'included',
    }


def sanitized(operation, response):
    result = {
        'operation': operation,
        'ok': response.returncode == 0,
        'checked_at': int(time.time()),
    }
    checks = []
    backup_complete = False
    restore_complete = False
    for item in records(response.stdout or b''):
        kind = item.get('check')
        if kind in ('existing_status', 'server_status'):
            resources = item.get('resources')
            if isinstance(resources, dict):
                result['resources'] = {
                    key: value
                    for key, value in resources.items()
                    if key in RESOURCE_FIELDS
                    and type(value) in (int, float)
                    and math.isfinite(value)
                    and value >= 0
                }
            result['backup_pending'] = item.get('backup_pending') is True
            result['containers'] = [
                {
                    'name': c['name'],
                    'running': c.get('running') is True,
                    'healthy': c.get('health') in ('none', '', 'healthy'),
                }
                for c in item.get('containers', [])
                if isinstance(c, dict) and safe_name(c.get('name'))
            ]
        if kind == 'existing_checks':
            checks.extend(
                {
                    'name': c['name'],
                    'passed': c.get('passed') is True,
                    **({'detail': check_detail(c)} if c.get('passed') is not True else {}),
                }
                for c in item.get('checks', [])
                if isinstance(c, dict) and safe_name(c.get('name'))
            )
        elif operation == 'check' and safe_name(kind) and type(item.get('passed')) is bool:
            checks.append(
                {
                    'name': kind,
                    'passed': item['passed'],
                    **({'detail': check_detail(item)} if not item['passed'] else {}),
                }
            )
        elif operation == 'check' and kind == 'service' and safe_name(item.get('name')):
            checks.append(
                {
                    'name': item['name'],
                    'passed': item.get('state') == 'running'
                    and item.get('health', '') in ('', 'healthy'),
                }
            )
        if kind in ('existing_storage_status', 'storage_status'):
            result['storage'] = {
                key: item.get(key) if type(item.get(key)) is bool else None
                for key in (
                    'media_s3',
                    'backup_s3',
                    'media_pending',
                    'last_backup_verified',
                    'backup_pending',
                    'upload_verified',
                    'restore_verified',
                )
            }
            if item.get('manual_backup_destination') in ('local', 'external'):
                result['storage']['manual_backup_destination'] = item['manual_backup_destination']
            if item.get('restore_scope') in ('scheduled-history', 'selected-backup'):
                result['storage']['restore_scope'] = item['restore_scope']
            if utc_timestamp(item.get('last_restore_verified_at')):
                result['storage']['last_restore_verified_at'] = item['last_restore_verified_at']
        if kind in ('existing_features', 'server_components'):
            result['components'] = {
                key: item.get(key) if type(item.get(key)) is bool else None
                for key in ('enabled', 'pending', 'referral_bot', 'federation_bot')
            }
            for key in ('admin', 'homepage'):
                value = item.get(key)
                if isinstance(value, str) and re.fullmatch(r'[a-z0-9.-]{1,253}', value):
                    result['components'][key] = value
        if kind in ('consistent_backup', 'existing_backup') and operation == 'backup':
            if (
                isinstance(item.get('archive'), str)
                and item['archive']
                and (kind == 'consistent_backup' or item.get('passed') is True)
            ):
                backup_complete = True
            if kind == 'consistent_backup' or type(item.get('passed')) is bool:
                backup = result.setdefault('backup', {})
                backup['destination'] = 'external' if item.get('archive') == 's3' else 'local'
                archive = item.get('archive')
                if isinstance(archive, str) and archive != 's3':
                    name = Path(archive).name
                    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,149}', name):
                        backup['artifact'] = name
                if type(item.get('encrypted_bytes')) is int and item['encrypted_bytes'] >= 0:
                    backup['bytes'] = item['encrypted_bytes']
                if item.get('authentication') == 'external-MAS-not-included':
                    backup['authentication'] = 'external-MAS-not-included'
                if kind == 'existing_backup' and item.get('verified') is True:
                    backup.update(upload_verified=True, restore_verified=True)
                if item.get('passed') is False:
                    result['ok'] = False
        if operation == 'backup_verify' and kind == 'temporary_database_restore':
            if item.get('database') in ('synapse', 'mas') and item.get('passed') is True:
                backup = result.setdefault('backup', {})
                backup.setdefault('verified_databases', []).append(item['database'])
                restore_complete = True
            if item.get('passed') is False:
                result['ok'] = False
        if operation == 'backup_verify' and kind == 'existing_backup_verified':
            restore_complete = item.get('passed') is True
            result.setdefault('backup', {})['restore_verified'] = item.get('passed') is True
            if item.get('passed') is False:
                result['ok'] = False
        if operation == 'backup_schedule' and kind in (
            'backup_schedule_status',
            'existing_backup_schedule',
        ):
            schedule = result.setdefault('schedule', {})
            for key in (
                'configured',
                'enabled',
                'needs_reconfigure',
                'verification_on_this_target_required',
            ):
                if type(item.get(key)) is bool:
                    schedule[key] = item[key]
            verified = item.get('last_verified')
            if isinstance(verified, dict) and utc_timestamp(verified.get('completed_utc')):
                schedule['last_verified_at'] = verified['completed_utc']
                schedule['restore_verified'] = verified.get('two_database_restores') is True
            if item.get('resource') == 'timer':
                value = item.get('NextElapseUSecRealtime')
                if safe_timer_date(value):
                    schedule['next_run'] = value
            if (
                item.get('resource') == 'service'
                and item.get('execution_metadata_available') is True
                and item.get('ActiveState') in ('inactive', 'failed')
                and item.get('SubState') not in ('running', 'start', 'start-pre', 'start-post')
                and item.get('Result')
                in (
                    'success',
                    'exit-code',
                    'timeout',
                    'signal',
                )
            ):
                schedule['last_run_status'] = item['Result']
            if item.get('passed') is False:
                result['ok'] = False
        if kind in (
            'consistent_backup',
            'existing_backup',
            'existing_backup_verified',
            'storage_status',
            'existing_storage_status',
            'existing_features',
            'server_components',
            'backup_schedule_status',
            'existing_backup_schedule',
        ) and (
            item.get('passed') is False
            or item.get('success') is False
            or item.get('partial') is True
            or item.get('status')
            in ('partial', 'partially_applied', 'failed', 'error', 'interrupted')
            or item.get('state')
            in ('partial', 'partially_applied', 'failed', 'error', 'interrupted')
        ):
            result['ok'] = False
    if operation == 'backup_schedule':
        # The adopted backend prints fixed systemctl timer properties after its JSON record.
        raw = response.stdout or b''
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='replace')
        for line in raw.splitlines():
            if line.startswith('NextElapseUSecRealtime=') and safe_timer_date(
                line.partition('=')[2]
            ):
                result.setdefault('schedule', {})['next_run'] = line.partition('=')[2]
    if operation == 'backup' and not backup_complete:
        result['ok'] = False
    if operation == 'backup_verify' and not restore_complete:
        result['ok'] = False
    if checks:
        result['checks'] = checks
        if any(not item['passed'] for item in checks):
            result['ok'] = False
    if (
        operation == 'backup_verify'
        and result.get('ok')
        and result.get('backup', {}).get('verified_databases')
    ):
        result['backup']['restore_verified'] = True

    expected = {
        'status': 'containers',
        'check': 'checks',
        'storage': 'storage',
        'components': 'components',
        'backup_schedule': 'schedule',
    }.get(operation)
    if expected and not result.get(expected):
        result['ok'] = False
    if result['ok'] is False:
        result['code'] = 'backend_unconfirmed'
    return result


def cpu_sample():
    # Guest time is already counted in user/nice; do not count it twice.
    fields = Path('/proc/stat').read_text().splitlines()[0].split()
    if fields[0] != 'cpu' or len(fields) < 5:
        raise ValueError('CPU counters unavailable')
    counters = [int(value) for value in fields[1:9]]
    if any(value < 0 for value in counters):
        raise ValueError('CPU counters unavailable')
    idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
    return sum(counters), idle


def host_statistics(root):
    result = {}
    try:
        disk = shutil.disk_usage(root)
        result.update(
            disk_free_bytes=disk.free, disk_total_bytes=disk.total, disk_used_bytes=disk.used
        )
    except OSError:
        pass
    try:
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            name, value = line.split(':', 1)
            memory[name] = int(value.strip().split()[0]) * 1024
        if 0 <= memory['MemAvailable'] <= memory['MemTotal']:
            result['memory_total_bytes'] = memory['MemTotal']
            result['memory_available_bytes'] = memory['MemAvailable']
            result['memory_used_bytes'] = memory['MemTotal'] - memory['MemAvailable']
        if 0 <= memory.get('SwapFree', -1) <= memory.get('SwapTotal', -1):
            result['swap_total_bytes'] = memory['SwapTotal']
            result['swap_used_bytes'] = memory['SwapTotal'] - memory['SwapFree']
    except (OSError, ValueError, KeyError, IndexError):
        pass
    try:
        result['cpu_load'] = round(os.getloadavg()[0], 2)
    except OSError:
        pass
    count = os.cpu_count()
    if count is not None:
        result['cpu_count'] = count
    try:
        before_total, before_idle = cpu_sample()
        time.sleep(0.1)
        after_total, after_idle = cpu_sample()
        elapsed, idle = after_total - before_total, after_idle - before_idle
        if elapsed > 0 and 0 <= idle <= elapsed:
            result['cpu_used_percent'] = round(100 * (elapsed - idle) / elapsed, 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        result['uptime_seconds'] = int(float(Path('/proc/uptime').read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    return result


def statistics(root, period):
    from admin_panel import Panel

    data = host_statistics(root)
    ok = True
    try:
        activity = Panel(root).statistics(period)
        for key in ('messages', 'registrations', 'period', 'start_utc', 'end_utc', 'note'):
            if key in activity:
                data[key] = activity[key]
    except Exception:
        # Database/SDK failures may contain credentials. Missing counters stay unknown.
        ok = False
        data['note'] = (
            'Статистика сообщений и регистраций недоступна. Показаны только полученные данные ресурсов.'
        )
    return {'operation': 'statistics', 'ok': ok, 'statistics': data, 'checked_at': int(time.time())}


def update_check(root):
    import releases

    try:
        release = releases.comparison(root, releases.latest())
        return {'operation': 'update_check', 'ok': True, 'release': release}
    except Exception:
        try:
            current = releases.installed(root)
        except Exception:
            current = None
        return {
            'operation': 'update_check',
            'ok': False,
            'release': {'installed': current, 'latest': None, 'update_available': False},
        }


def run_control(config, operation, *, period='day', runner=subprocess.run):
    if operation not in COMMANDS or operation not in config.get('management_actions', []):
        raise RuntimeError('Эта команда управления не подключена. Обратитесь к администратору.')
    if period not in PERIODS or (operation != 'statistics' and period != 'day'):
        raise RuntimeError('Выберите допустимый период статистики.')
    root = root_path(config['stack_root'])
    kind = installation_kind(root)
    if kind == 'portable':
        command_wrapper.trusted_installation(root)
    if operation == 'backup_preview':
        return {'operation': operation, 'ok': True, 'backup': backup_preview(root)}
    if operation == 'components' and kind == 'portable':
        metadata = configuration.read_private_json(root / 'installation.json')
        return {
            'operation': operation,
            'ok': True,
            'components': {
                'enabled': True,
                'pending': False,
                'referral_bot': metadata.get('referral_provisioned')
                if type(metadata.get('referral_provisioned')) is bool
                else None,
                'federation_bot': metadata.get('federation_provisioned')
                if type(metadata.get('federation_provisioned')) is bool
                else None,
            },
        }
    if operation == 'statistics':
        return statistics(root, period)
    if operation == 'update_check':
        return update_check(root)
    if operation in ('restart', 'update'):
        raise RuntimeError('Для этой операции требуется отдельное подтверждение и фоновое задание.')
    command = [
        '/usr/bin/python3',
        str(root / 'misbot.py'),
        '--directory',
        str(root),
        *COMMANDS[operation],
    ]
    if kind == 'portable' and operation == 'status':
        command = [
            '/usr/bin/python3',
            str(root / 'server_management.py'),
            '--directory',
            str(root),
            '--read',
            'status',
        ]
    if operation == 'storage_connect':
        command += ['--config', str(root / '.misbot/s3/media.env')]
    try:
        response = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=1200 if operation in WRITES else 85,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        result = {
            'operation': operation,
            'ok': False,
            'recovery_required': True,
            'code': 'operation_timeout'
            if isinstance(error, subprocess.TimeoutExpired)
            else 'runner_unavailable',
        }
    else:
        result = sanitized(operation, response)
    if operation == 'status' and 'resources' not in result:
        result['resources'] = host_statistics(root)
    if operation == 'backup_verify' and kind == 'portable':
        preview = backup_preview(root)
        expected = (
            {'synapse'}
            if preview['authentication'] == 'external-MAS-not-included'
            else {'synapse', 'mas'}
        )
        if preview['authentication'] == 'external-MAS-not-included':
            result.setdefault('backup', {})['authentication'] = preview['authentication']
        if set(result.get('backup', {}).get('verified_databases', [])) != expected:
            result['ok'] = False
            result.setdefault('backup', {})['restore_verified'] = False
    if operation == 'backup':
        preview = backup_preview(root)
        backup = result.setdefault('backup', {})
        if preview['authentication'] == 'external-MAS-not-included':
            backup['authentication'] = preview['authentication']
    return result


def native_status(root):
    import lifecycle

    lifecycle.configure(root, 'server-status')
    lifecycle.ownership_check()
    rows = lifecycle.service_rows()
    return {
        'check': 'server_status',
        'resources': host_statistics(root),
        'backup_pending': (root / 'backup-writers.json').exists(),
        'containers': [
            {
                'name': row['Service'],
                'running': row.get('State') == 'running',
                'health': row.get('Health', ''),
            }
            for row in rows
            if safe_name(row.get('Service'))
        ],
    }


def wait_ready(check, *, seconds=120, sleeper=time.sleep):
    for _ in range(seconds // 2):
        if check():
            return
        sleeper(2)
    if not check():
        raise RuntimeError('Службы не подтвердили готовность после перезапуска.')


def restart(root):
    """Restart only the running services within the previously accepted ownership scope."""
    if installation_kind(root) == 'existing-compose':
        from existing_compose import Installation

        installation = Installation(root)
        with installation.lock():
            installation.require_no_pending()
            installation.validate()
            running = {
                name for name in installation.services if installation.container(name)['running']
            }
            for project in installation.projects:
                selected = [
                    service for service, name in project['services'].items() if name in running
                ]
                if selected:
                    installation.run(
                        installation.compose(
                            project, 'restart', '--no-deps', '--timeout', '30', *selected
                        ),
                        timeout=210,
                    )
            wait_ready(
                lambda: all(
                    installation.container(name)['running']
                    and installation.container(name).get('health') in ('none', 'healthy')
                    for name in running
                )
            )
            ok = installation.check() == 0
    else:
        import lifecycle

        with state_store.operation_guard(root):
            state_store.assert_update_allowed(root)
            lifecycle.configure(root, 'server-restart')
            lifecycle.up_admission_check()
            lifecycle.ownership_check()
            running = [
                row['Service']
                for row in lifecycle.service_rows()
                if row.get('State') == 'running' and safe_name(row.get('Service'))
            ]
            if running:
                lifecycle.dc('restart', '--no-deps', '--timeout', '30', *running)
            wait_ready(
                lambda: all(
                    row.get('State') == 'running' and row.get('Health', '') in ('', 'healthy')
                    for row in lifecycle.service_rows()
                    if row.get('Service') in running
                )
                and set(running).issubset({row.get('Service') for row in lifecycle.service_rows()})
            )
            lifecycle.check()
            ok = True
    return {'operation': 'restart', 'ok': ok}


def unit(root, identifier):
    return (
        'misbot-server-'
        + hashlib.sha256(str(root).encode()).hexdigest()[:12]
        + '-'
        + identifier[3:19]
    )


def read_queue(path):
    """Job history has a separate bound from small configuration documents."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > MAX_QUEUE_BYTES
        ):
            raise RuntimeError('Небезопасный или слишком большой журнал управления. Файл сохранён.')
        raw = source.read(MAX_QUEUE_BYTES + 1)
    if len(raw) > MAX_QUEUE_BYTES:
        raise RuntimeError('Журнал управления превышает допустимый размер. Файл сохранён.')
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise RuntimeError('Некорректный журнал управления. Файл сохранён.') from None


@contextmanager
def database(root):
    """The queue lock never substitutes for the installation's mutation lock."""
    folder = configuration.directory(root / 'data/admin-panel')
    path = folder / 'server-jobs.json'
    descriptor = os.open(folder / 'server-jobs.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise RuntimeError('Небезопасный журнал управления сервером.')
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        data = read_queue(path) if path.exists() or path.is_symlink() else {'format': 1, 'jobs': {}}
        if data.get('format') != 1 or not isinstance(data.get('jobs'), dict):
            raise RuntimeError('Неизвестный формат журнала управления. Файл сохранён.')
        for identifier, job in data['jobs'].items():
            if (
                not re.fullmatch(r'SO-[a-f0-9]{32}', identifier)
                or not isinstance(job, dict)
                or job.get('operation') not in WRITES
                or job.get('status') not in ACTIVE | FINISHED
            ):
                raise RuntimeError('Некорректный журнал управления. Файл сохранён.')
        yield data
        state_store.write_json(path, data)
    finally:
        os.close(descriptor)


def view(job):
    return {
        key: value
        for key, value in job.items()
        if key in ('operation_id', 'operation', 'status', 'result', 'created_at', 'finished_at')
    }


def active_unit(name, runner):
    try:
        response = runner(
            ['systemctl', 'is-active', '--quiet', name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if response.returncode in (0, 3, 4):
            return response.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def refresh(root, job, runner):
    if job['status'] not in ACTIVE:
        return
    if job.get('update_id'):
        import release_update

        path = release_update.job_path(root)
        try:
            update = configuration.read_private_json(path)
        except (OSError, RuntimeError, ValueError):
            if (
                not path.exists()
                and time.time() - job['created_at'] >= 120
                and active_unit(unit(root, job['operation_id']), runner) is False
            ):
                job.update(
                    status='interrupted',
                    result={'ok': False, 'message': RECOVERY},
                    finished_at=time.time(),
                )
            return
        if update.get('id') != job['update_id']:
            job.update(
                status='interrupted',
                result={'ok': False, 'message': RECOVERY},
                finished_at=time.time(),
            )
        elif update.get('state') in FINISHED:
            result = update.get('result') or {}
            job.update(
                status='complete'
                if update['state'] == 'complete' and result.get('success') is True
                else 'failed'
                if update['state'] == 'complete'
                else update['state'],
                finished_at=time.time(),
                result={
                    'ok': update['state'] == 'complete' and result.get('success') is True,
                    'version': update.get('version'),
                    'installed': result.get('installed'),
                    'message': 'Выпуск Misbot обновлён.'
                    if update['state'] == 'complete' and result.get('success') is True
                    else RECOVERY,
                },
            )
        elif (
            time.time() - job['created_at'] >= 120
            and active_unit(update.get('unit', release_update.unit(root, job['update_id'])), runner)
            is False
        ):
            job.update(
                status='interrupted',
                result={'ok': False, 'message': RECOVERY},
                finished_at=time.time(),
            )
        return
    if time.time() - job['created_at'] < 120:
        return
    if active_unit(unit(root, job['operation_id']), runner) is False:
        job.update(
            status='interrupted', result={'ok': False, 'message': RECOVERY}, finished_at=time.time()
        )


def queue_operation(
    root, operation, actor, event_id, argument=None, *, confirmed_backup=None, runner=subprocess.run
):
    root = root_path(root)
    command_wrapper.trusted_installation(root)
    if (
        operation not in WRITES
        or not isinstance(actor, str)
        or not re.fullmatch(r'@[^\s:]{1,255}:[a-zA-Z0-9.-]{1,253}', actor)
        or not isinstance(event_id, str)
        or not 1 <= len(event_id) <= 255
    ):
        raise RuntimeError('Некорректная операция управления сервером.')
    if operation == 'update':
        import releases

        if installation_kind(root) != 'portable':
            raise RuntimeError(
                'Обновление existing-compose выполняется проверенным source plan через SSH.'
            )
        releases.version(argument)
    elif argument is not None:
        raise RuntimeError('Параметры этой операции не поддерживаются.')
    preview = None
    if operation == 'backup':
        preview = backup_preview(root) if confirmed_backup is None else confirmed_backup
        if (
            not isinstance(preview, dict)
            or set(preview) != {'destination', 'authentication'}
            or preview['destination'] not in ('local', 'external')
            or preview['authentication'] not in ('included', 'external-MAS-not-included')
        ):
            raise RuntimeError('Параметры копии не подтверждены.')
    elif confirmed_backup is not None:
        raise RuntimeError('Параметры копии не относятся к этой операции.')
    identifier = 'SO-' + hashlib.sha256((actor + '\0' + event_id).encode()).hexdigest()[:32]
    with database(root) as data:
        if identifier in data['jobs']:
            job = data['jobs'][identifier]
            if (
                job['operation'] != operation
                or job.get('argument') != argument
                or job.get('actor') != actor
                or (operation == 'backup' and job.get('backup_preview') != preview)
            ):
                raise RuntimeError(
                    'Параметры подтверждённой операции изменились. Подготовьте новое подтверждение.'
                )
            refresh(root, job, runner)
            return view(job)
        if len(data['jobs']) >= MAX_JOBS:
            raise RuntimeError(
                'Достигнут предел истории операций. Новая задача не создана; сохраните и проверьте журнал через SSH.'
            )
        for job in data['jobs'].values():
            refresh(root, job, runner)
            if job['status'] in ACTIVE:
                raise RuntimeError(
                    'Операция управления сервером уже выполняется. Проверьте её результат.'
                )
        job = {
            'operation_id': identifier,
            'operation': operation,
            'status': 'queued',
            'actor': actor,
            'event_id': event_id,
            'argument': argument,
            'created_at': time.time(),
        }
        if preview is not None:
            job['backup_preview'] = dict(preview)
        data['jobs'][identifier] = job
        # Publish before launching: the detached worker may start immediately.
        state_store.write_json(root / 'data/admin-panel/server-jobs.json', data)
        try:
            runner(
                [
                    'systemd-run',
                    '--quiet',
                    '--collect',
                    '--unit=' + unit(root, identifier),
                    '--property=Type=exec',
                    '--property=UMask=0077',
                    '--property=TimeoutStopSec=30',
                    '/usr/bin/python3',
                    str(root / 'server_management.py'),
                    '--directory',
                    str(root),
                    '--job',
                    identifier,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            job.update(
                status='failed',
                finished_at=time.time(),
                result={
                    'ok': False,
                    'message': 'Не удалось запустить фоновую операцию. Проверьте systemd и sudo misbot status через SSH.',
                },
            )
        return view(job)


def operation_status(root, identifier, actor, *, runner=subprocess.run):
    root = root_path(root)
    command_wrapper.trusted_installation(root)
    with database(root) as data:
        job = data['jobs'].get(identifier)
        if not job or job.get('actor') != actor:
            raise RuntimeError('Операция не найдена или принадлежит другому администратору.')
        refresh(root, job, runner)
        return view(job)


def authorize_current(root, actor):
    """Check live MAS rights independently of federation initialization and cached bot sessions."""
    metadata = configuration.read_private_json(root / 'installation.json')
    suffix = ':' + metadata['domain']
    if not isinstance(actor, str) or not actor.startswith('@') or not actor.endswith(suffix):
        raise RuntimeError('Права администратора не подтверждены.')
    username = actor[1 : -len(suffix)]
    if not username or ':' in username or any(ord(c) < 32 for c in username):
        raise RuntimeError('Права администратора не подтверждены.')
    if metadata.get('backend') == 'existing-compose':
        from existing_compose import Installation
        from existing_features import mas_api

        api = mas_api(Installation(root))
        user = api.matrix_user(actor, metadata['domain'])
        api.administrator_user(user['id'])
        return
    import lifecycle

    lifecycle.configure(root, 'server-authorize')
    lifecycle.ownership_check()
    payload = {
        'client': configuration.read_private_json(root / 'secrets/referral-client.json'),
        'username': username,
    }
    raw = lifecycle.dc(
        'exec',
        '-T',
        'synapse',
        'python',
        '-c',
        (root / 'federation_admin_api.py').read_text(),
        stdin=json.dumps(payload),
    )
    if json.loads(raw) != {'authorized': True}:
        raise RuntimeError('Права администратора не подтверждены.')


def run_job(root, identifier):
    root = root_path(root)
    command_wrapper.trusted_installation(root)
    with database(root) as data:
        job = data['jobs'].get(identifier)
        if not job or job['status'] != 'queued':
            raise RuntimeError('Задание уже выполнялось; повторный запуск запрещён.')
        job.update(status='running')
        if job['operation'] == 'update':
            # Link before launching the second unit so a killed wrapper remains recoverable.
            job['update_id'] = hashlib.sha256(job['event_id'].encode()).hexdigest()
        saved = dict(job)
    stage = 'authorization'
    try:
        # Recheck current MAS administrator role immediately before mutation.
        authorize_current(root, saved['actor'])
        if saved['operation'] == 'backup':
            stage = 'confirmation'
            if saved.get('backup_preview') != backup_preview(root):
                raise RuntimeError('Backup configuration changed')
        stage = 'execution'
        if saved['operation'] == 'update':
            import release_update

            release = release_update.queue(root, saved['argument'], saved['event_id'])
            result = {'ok': release['status'] not in ('failed', 'interrupted')}
            # Even an already completed update is confirmed from its durable result by refresh.
            status = (
                'running'
                if release['status'] in ACTIVE or release['status'] == 'complete'
                else release['status']
            )
            update_id = hashlib.sha256(saved['event_id'].encode()).hexdigest()
        else:
            result = (
                restart(root)
                if saved['operation'] == 'restart'
                else run_control(
                    {'stack_root': str(root), 'management_actions': sorted(WRITES)},
                    saved['operation'],
                )
            )
            status = 'complete' if result.get('ok') else 'failed'
            update_id = None
    except Exception:
        result, status, update_id = (
            {
                'ok': False,
                'message': RECOVERY,
                'code': 'authorization_unavailable'
                if stage == 'authorization'
                else 'configuration_changed'
                if stage == 'confirmation'
                else 'operation_unconfirmed',
            },
            'failed',
            None,
        )
    with database(root) as data:
        job = data['jobs'][identifier]
        if job['status'] != 'running':
            raise RuntimeError('Состояние операции изменилось; результат сохраните через SSH.')
        job.update(status=status, result=result)
        if status in FINISHED:
            job['finished_at'] = time.time()
        if update_id:
            job['update_id'] = update_id


def operation_notice(job):
    names = {
        'start': 'Запуск служб',
        'stop': 'Остановка служб',
        'restart': 'Перезапуск служб',
        'backup': 'Резервная копия',
        'backup_verify': 'Проверка копии',
        'update': 'Обновление Misbot',
        'storage_connect': 'Подключение S3',
    }
    result = job.get('result') or {}
    state = job.get('status')
    lines = [
        names.get(job.get('operation'), 'Обслуживание сервера')
        + ': '
        + (
            'завершено.'
            if state == 'complete' and result.get('ok') is True
            else 'прервано; результат не подтверждён.'
            if state == 'interrupted'
            else 'завершение не подтверждено.'
        )
    ]
    backup = result.get('backup', {})
    if backup.get('destination') in ('local', 'external'):
        lines.append(
            'Назначение копии: '
            + ('этот сервер' if backup['destination'] == 'local' else 'внешнее хранилище')
            + '.'
        )
    if backup.get('authentication') == 'external-MAS-not-included':
        lines.append('Внешний MAS в копию не входит. Сохраните его отдельно.')
    if backup.get('restore_verified') is True:
        lines.append('Восстановление временных баз проверено.')
    version = result.get('installed') or result.get('version')
    if isinstance(version, str) and re.fullmatch(
        r'[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?', version
    ):
        lines.append('Версия Misbot: ' + version)
    if state != 'complete' or result.get('ok') is not True:
        lines.append(RECOVERY)
    return '\n'.join(lines)


def operation_notices(panel, *, runner=subprocess.run):
    root = root_path(panel.root)
    if not (root / 'data/admin-panel/server-jobs.json').exists():
        return
    with database(root) as data:
        notices = []
        for job in data['jobs'].values():
            refresh(root, job, runner)
            if job['status'] in FINISHED:
                text = operation_notice(job)
                notices.append(('server-operation:' + job['operation_id'], text))
    for key, body in notices:
        panel.notice(key, body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--job')
    parser.add_argument('--read', choices=('status',))
    args = parser.parse_args()
    state_store.require_root()
    command_wrapper.trusted_installation(args.directory)
    if args.job:
        run_job(args.directory, args.job)
    elif args.read:
        print(json.dumps(native_status(args.directory), ensure_ascii=False))
    else:
        parser.error('Specify --job or --read')


if __name__ == '__main__':
    main()
