"""Allowlisted server reads and durable operations shared by administrator interfaces."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
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


def sanitized(operation, response):
    result = {'operation': operation, 'ok': response.returncode == 0}
    checks = []
    for item in records(response.stdout or b''):
        kind = item.get('check')
        if kind in ('existing_status', 'server_status'):
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
                {'name': c['name'], 'passed': c.get('passed') is True}
                for c in item.get('checks', [])
                if isinstance(c, dict) and safe_name(c.get('name'))
            )
        elif operation == 'check' and safe_name(kind) and type(item.get('passed')) is bool:
            checks.append({'name': kind, 'passed': item['passed']})
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
                key: item.get(key) is True
                for key in (
                    'media_s3',
                    'backup_s3',
                    'media_pending',
                    'last_backup_verified',
                    'backup_pending',
                )
            }
        if kind == 'existing_features':
            result['components'] = {
                key: item.get(key) is True
                for key in ('enabled', 'pending', 'referral_bot', 'federation_bot')
            }
            for key in ('admin', 'homepage'):
                value = item.get(key)
                if isinstance(value, str) and re.fullmatch(r'[a-z0-9.-]{1,253}', value):
                    result['components'][key] = value
    if checks:
        result['checks'] = checks
    expected = {
        'status': 'containers',
        'check': 'checks',
        'storage': 'storage',
        'components': 'components',
    }.get(operation)
    if expected and not result.get(expected):
        result['ok'] = False
    return result


def host_statistics(root):
    result = {}
    try:
        disk = shutil.disk_usage(root)
        result.update(disk_free_bytes=disk.free, disk_total_bytes=disk.total)
    except OSError:
        pass
    try:
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            name, value = line.split(':', 1)
            memory[name] = int(value.strip().split()[0]) * 1024
        result['memory_total_bytes'] = memory['MemTotal']
        result['memory_used_bytes'] = memory['MemTotal'] - memory['MemAvailable']
    except (OSError, ValueError, KeyError, IndexError):
        pass
    try:
        result['cpu_load'] = round(os.getloadavg()[0], 2)
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
    except (OSError, subprocess.TimeoutExpired):
        return {'operation': operation, 'ok': False, 'recovery_required': True}
    return sanitized(operation, response)


def native_status(root):
    import lifecycle

    lifecycle.configure(root, 'server-status')
    lifecycle.ownership_check()
    rows = lifecycle.service_rows()
    return {
        'check': 'server_status',
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
        if key in ('operation_id', 'operation', 'status', 'result')
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
                job.update(status='interrupted', result={'ok': False, 'message': RECOVERY})
            return
        if update.get('id') != job['update_id']:
            job.update(status='interrupted', result={'ok': False, 'message': RECOVERY})
        elif update.get('state') in FINISHED:
            result = update.get('result') or {}
            job.update(
                status=update['state'],
                result={
                    'ok': update['state'] == 'complete' and result.get('success', True),
                    'version': update.get('version'),
                    'installed': result.get('installed'),
                    'message': 'Компоненты обновлены.'
                    if update['state'] == 'complete'
                    else RECOVERY,
                },
            )
        elif (
            time.time() - job['created_at'] >= 120
            and active_unit(update.get('unit', release_update.unit(root, job['update_id'])), runner)
            is False
        ):
            job.update(status='interrupted', result={'ok': False, 'message': RECOVERY})
        return
    if time.time() - job['created_at'] < 120:
        return
    if active_unit(unit(root, job['operation_id']), runner) is False:
        job.update(status='interrupted', result={'ok': False, 'message': RECOVERY})


def queue_operation(root, operation, actor, event_id, argument=None, *, runner=subprocess.run):
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
    identifier = 'SO-' + hashlib.sha256((actor + '\0' + event_id).encode()).hexdigest()[:32]
    with database(root) as data:
        if identifier in data['jobs']:
            job = data['jobs'][identifier]
            if (
                job['operation'] != operation
                or job.get('argument') != argument
                or job.get('actor') != actor
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
    try:
        # Recheck current MAS administrator role immediately before mutation.
        authorize_current(root, saved['actor'])
        if saved['operation'] == 'update':
            import release_update

            release = release_update.queue(root, saved['argument'], saved['event_id'])
            result = {'ok': release['status'] not in ('failed', 'interrupted')}
            status = 'running' if release['status'] in ACTIVE else release['status']
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
        result, status, update_id = {'ok': False, 'message': RECOVERY}, 'failed', None
    with database(root) as data:
        job = data['jobs'][identifier]
        if job['status'] != 'running':
            raise RuntimeError('Состояние операции изменилось; результат сохраните через SSH.')
        job.update(status=status, result=result)
        if update_id:
            job['update_id'] = update_id


def operation_notices(panel, *, runner=subprocess.run):
    root = root_path(panel.root)
    if not (root / 'data/admin-panel/server-jobs.json').exists():
        return
    with database(root) as data:
        notices = []
        for job in data['jobs'].values():
            refresh(root, job, runner)
            if job['status'] in FINISHED:
                text = 'Операция сервера: ' + job['operation'] + '\n'
                text += (
                    'Завершена.'
                    if job['status'] == 'complete' and (job.get('result') or {}).get('ok')
                    else RECOVERY
                )
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
