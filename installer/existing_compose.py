"""Manage an explicitly inventoried Compose installation without regenerating it."""

import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid

import backup_crypto
import cli_commands
import command_wrapper
import telegram_backup
import existing_s3

_locks = threading.local()


class Error(RuntimeError):
    pass


def emit(check, **values):
    print(json.dumps({'check': check, **values}, ensure_ascii=False), flush=True)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def private_directory(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path or path == Path('/'):
        raise Error('Нужен отдельный абсолютный каталог без symlink.')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise Error('Служебный каталог должен принадлежать root и иметь права 0700.')
    return path


def save(path, value):
    if path.exists() and (path.is_symlink() or path.stat().st_uid != 0):
        raise Error('Небезопасный служебный файл; изменение отклонено.')
    command_wrapper.atomic(path, (json.dumps(value, indent=2) + '\n').encode(), 0o600)


class Frames(io.RawIOBase):
    def __init__(self, source, key):
        self.source = source if hasattr(source, 'read') else None
        self.blocks = iter(
            backup_crypto.decrypted_stream(source, key)
            if hasattr(source, 'read')
            else backup_crypto.decrypted_blocks(source, key)
        )
        self.pending = b''

    def readable(self):
        return True

    def readinto(self, target):
        if not self.pending:
            self.pending = next(self.blocks, b'')
        size = min(len(target), len(self.pending))
        target[:size] = self.pending[:size]
        self.pending = self.pending[size:]
        return size

    def close(self):
        self.blocks.close()
        if self.source:
            self.source.close()
        super().close()


class Installation:
    def __init__(self, root):
        self.root = command_wrapper.trusted_installation(root)
        self.state = json.loads((self.root / 'installation.json').read_text())
        self.model = self.state.get('existing', {})
        if self.state.get('backend') != 'existing-compose' or self.model.get('format') != 1:
            raise Error('Каталог не зарегистрирован как существующий Compose-стек.')
        self.control = private_directory(self.root / '.misbot')
        self.journal = self.control / 'backup-writers.json'
        self.projects = self.model.get('projects', [])
        self.services = {}
        if not self.projects:
            raise Error('Не задан перечень Compose-проектов.')
        for project in self.projects:
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', project['name']):
                raise Error('Некорректное имя Compose-проекта.')
            directory = self.root / project['directory']
            if directory.resolve() != directory or not directory.is_relative_to(self.root):
                raise Error('Compose-каталог должен находиться внутри установки без symlink.')
            if not project.get('services'):
                raise Error('Не задан перечень принадлежащих установке служб.')
            for service, name in project['services'].items():
                if (
                    not all(
                        re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value)
                        for value in (service, name)
                    )
                    or name in self.services
                ):
                    raise Error('Некорректный или повторный контейнер.')
                self.services[name] = (project['name'], service)
        self.writers = self.model.get('writers', [])
        if not self.writers or not set(self.writers).issubset(self.services):
            raise Error('Не задан полный перечень служб записи.')
        if self.model.get('postgres_container') not in self.services:
            raise Error('PostgreSQL не принадлежит заданной установке.')
        self.databases = self.model.get('databases', [])
        if (
            not self.databases
            or len(set(self.databases)) != len(self.databases)
            or not all(
                re.fullmatch(r'[a-z][a-z0-9_]{0,40}', name)
                and name not in ('postgres', 'template0', 'template1')
                for name in self.databases
            )
        ):
            raise Error('Некорректный перечень рабочих баз данных.')
        if Path(self.model['backup_directory']).resolve().is_relative_to(self.root):
            raise Error('Каталог копий должен находиться вне установки.')

    def run(self, arguments, *, incoming=None, outgoing=None, timeout=120, allowed=(0,)):
        options = self.child_options()
        result = subprocess.run(
            arguments,
            stdin=incoming,
            stdout=outgoing or subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            **options,
        )
        if result.returncode not in allowed:
            raise Error(
                'Команда не завершена. Проверьте состояние служб; настройки и данные сохранены.'
            )
        return result.returncode, (result.stdout or b'')

    def child_options(self):
        descriptor = getattr(_locks, 'held', {}).get(str(self.root))
        if descriptor is None:
            return {}
        environment = dict(
            os.environ,
            MISBOT_EXISTING_LOCK_FD=str(descriptor),
            MISBOT_EXISTING_LOCK_PARENT=str(os.getpid()),
        )
        return {'pass_fds': (descriptor,), 'env': environment}

    def compose(self, project, *arguments):
        directory = self.root / project['directory']
        paths = [
            directory / 'docker-compose.yml',
            self.control / 'pins' / (project['name'] + '.json'),
        ]
        for path in paths:
            info = command_wrapper.regular(path)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise Error(
                    'Compose-файлы должны принадлежать root и быть защищены от чужой записи.'
                )
        return [
            'docker',
            'compose',
            '--project-directory',
            str(directory),
            '-p',
            project['name'],
            '-f',
            str(paths[0]),
            '-f',
            str(paths[1]),
            *arguments,
        ]

    def container(self, name):
        template = (
            '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} '
            '{{.Image}} {{index .Config.Labels "com.docker.compose.project"}} '
            '{{index .Config.Labels "com.docker.compose.service"}}'
        )
        code, raw = self.run(['docker', 'inspect', '--format', template, name], allowed=(0, 1))
        if code:
            return {'name': name, 'exists': False, 'running': False}
        parts = raw.decode().strip().split()
        if len(parts) != 5 or tuple(parts[3:]) != self.services[name]:
            raise Error('Обнаружен чужой контейнер с совпадающим именем; действие отклонено.')
        return {
            'name': name,
            'exists': True,
            'running': parts[0] == 'running',
            'status': parts[0],
            'health': parts[1],
            'image_id': parts[2],
        }

    def accepted_runtime_mount(self, name, volume):
        from existing_proxy import accepted_mount as proxy_mount
        from telegram_container import accepted_mount as worker_mount

        if proxy_mount(self, name, volume) or worker_mount(self, name, volume):
            return True
        source = Path(volume.get('source', ''))
        accepted = {'container': name, 'source': str(source), 'target': volume.get('target')}
        if (
            name != 'misbot-referral-bot'
            or volume.get('target') != '/run/control'
            or volume.get('read_only') is not True
            or source.parent != Path('/run')
            or accepted not in self.model.get('external_runtime_mounts', [])
        ):
            return False
        info = source.lstat()
        return (
            source.resolve() == source
            and stat.S_ISDIR(info.st_mode)
            and info.st_uid == 0
            and info.st_gid == 10001
            and stat.S_IMODE(info.st_mode) == 0o750
        )

    def validate(self):
        import yaml
        from existing_proxy import Proxy, validate_service as validate_proxy
        from telegram_container import paths as worker_paths, validate_service as validate_worker

        Proxy(self)
        if self.model.get('telegram_control', {}).get('worker_container'):
            name, _, _, _ = worker_paths(self.root, self.model)
            if name not in self.services:
                raise Error('Telegram worker отсутствует в принятом Compose-проекте.')
        for relative in self.model.get('critical_paths', []):
            path = self.root / relative
            if not path.is_relative_to(self.root) or path.resolve() != path or not path.exists():
                raise Error('Отсутствует обязательный файл данных или identity; запуск отклонён.')
        identity = self.model['identity']
        for field in ('synapse_config', 'mas_config', 'signing_key'):
            path = self.root / identity[field]
            if path.resolve() != path or not path.is_relative_to(self.root):
                raise Error('Identity-файлы должны находиться внутри установки без symlink.')
        synapse = yaml.safe_load((self.root / identity['synapse_config']).read_text())
        mas = yaml.safe_load((self.root / identity['mas_config']).read_text())
        if (
            synapse.get('server_name') != self.state['domain']
            or mas.get('http', {}).get('issuer') != identity['issuer']
        ):
            raise Error('Изменились server_name или issuer; запуск отклонён.')
        if digest(self.root / identity['signing_key']) != identity['signing_sha256']:
            raise Error('Изменился signing key; запуск отклонён.')
        for project in self.projects:
            _, raw = self.run(self.compose(project, 'config', '--format', 'json'))
            model = json.loads(raw)
            if set(model['services']) != set(project['services']):
                raise Error('Состав Compose-проекта отличается от принятой инвентаризации.')
            for service, name in project['services'].items():
                entry = model['services'][service]
                validate_proxy(self, name, entry)
                validate_worker(self, name, entry)
                if entry.get('container_name') != name or entry.get('pull_policy') != 'never':
                    raise Error('Имя контейнера или политика загрузки образов изменились.')
                if entry.get('image') != self.model['images'][name]['digest']:
                    raise Error(
                        'Образ отличается от принятой версии; автоматическое обновление отклонено.'
                    )
                for volume in entry.get('volumes', []):
                    source = Path(volume.get('source', ''))
                    if (
                        volume.get('type') != 'bind'
                        or not source.exists()
                        or (
                            not source.resolve().is_relative_to(self.root)
                            and not self.accepted_runtime_mount(name, volume)
                        )
                    ):
                        raise Error('Хранилище отсутствует или находится вне принятого каталога.')
                current = self.container(name)
                if current['exists'] and current['image_id'] != self.model['images'][name]['id']:
                    raise Error('Работающий контейнер использует другую версию образа.')
        emit('existing_configuration', passed=True, server_name=self.state['domain'])

    def status(self):
        containers = [self.container(name) for name in self.services]
        emit(
            'existing_status',
            directory=str(self.root),
            server_name=self.state['domain'],
            containers=containers,
            backup_pending=self.journal.exists(),
            recovery='sudo misbot backup recover' if self.journal.exists() else None,
            media_s3=(self.control / 's3/media-active.json').exists(),
            backup_s3=(self.control / 's3/backup-active.json').exists(),
            storage_pending=(self.control / 's3/media-journal.json').exists(),
            source_update_pending=(self.control / 'source-update.json').exists(),
            supported_commands=[
                c.path for c in cli_commands.COMMANDS if c.backend in cli_commands.EXISTING_BACKENDS
            ],
        )

    def require_no_pending(self):
        if (self.control / 'runtime-migration.json').exists():
            raise Error(
                'Незавершённый перенос runtime. Выполните recovery сохранённого плана переноса; журнал не удаляйте.'
            )
        if self.journal.exists():
            raise Error(
                'Есть незавершённая копия. Выполните sudo misbot backup recover; журнал не удаляйте.'
            )
        if (self.control / 's3/media-journal.json').exists():
            raise Error(
                'Есть незавершённое подключение S3. Выполните sudo misbot storage recover; журнал не удаляйте.'
            )
        if (self.control / 'source-update.json').exists():
            raise Error(
                'Есть незавершённое обновление исходников. Выполните misbot update recover --source PATH.'
            )
        if (self.control / 'features/homepage.json').exists():
            raise Error('Есть незавершённая главная страница. Выполните misbot homepage recover.')

    def start(self):
        self.require_no_pending()
        self.validate()
        for project in self.projects:
            if project['name'] == 'misbot-addons' and self.state.get('federation_provisioned'):
                import federation_runtime

                federation_runtime.controller(self.root, lambda cmd: self.run(cmd)[1].decode())
            self.run(
                self.compose(
                    project,
                    'up',
                    '-d',
                    '--pull',
                    'never',
                    '--no-build',
                    '--wait',
                    '--wait-timeout',
                    '180',
                ),
                timeout=210,
            )
        self.status()

    def stop(self):
        self.require_no_pending()
        self.validate()
        if self.state.get('federation_provisioned'):
            import federation_runtime

            federation_runtime.controller(
                self.root, lambda cmd: self.run(cmd)[1].decode(), start=False
            )
        for project in reversed(self.projects):
            self.run(self.compose(project, 'stop', '--timeout', '30'))
        self.status()

    def check(self):
        from urllib.parse import urlsplit
        from urllib.request import urlopen

        self.validate()
        results = []
        for name in self.services:
            current = self.container(name)
            results.append(
                {
                    'name': name,
                    'passed': current['running'] and current.get('health') in ('none', 'healthy'),
                }
            )
        for item in self.model.get('http_checks', []):
            url = urlsplit(item['url'])
            if (
                url.username
                or url.password
                or url.query
                or url.fragment
                or (
                    url.scheme != 'https'
                    and not (
                        url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')
                    )
                )
            ):
                raise Error('Проверки допускают HTTPS или локальный HTTP без credentials.')
            try:
                with urlopen(item['url'], timeout=15) as response:
                    passed = response.status == item.get('status', 200)
                    if item.get('issuer'):
                        passed = (
                            passed
                            and json.loads(response.read(32768)).get('issuer') == item['issuer']
                        )
            except Exception:
                passed = False
            results.append({'name': item['name'], 'passed': passed})
        passed = all(item['passed'] for item in results)
        emit('existing_checks', passed=passed, checks=results, client_acceptance_required=True)
        return 0 if passed else 1

    def pg(self, tool, *arguments, incoming=None, outgoing=None):
        command = [
            'docker',
            'exec',
            *(['-i'] if incoming is not None else []),
            self.model['postgres_container'],
            'sh',
            '-c',
            'exec "$@" -U "$POSTGRES_USER"',
            'sh',
            tool,
            *arguments,
        ]
        return self.run(command, incoming=incoming, outgoing=outgoing, timeout=300)

    def recover(self):
        if not self.journal.exists():
            emit('existing_backup_recovery', pending=False)
            return
        value = json.loads(self.journal.read_text())
        names = value['running_writers']
        if value.get('format') != 1 or not set(names).issubset(self.writers):
            raise Error('Неверный журнал резервного копирования; ничего не изменено.')
        for name in names:
            current = self.container(name)
            if not current['exists']:
                raise Error('Контейнер из журнала отсутствует; журнал сохранён.')
        # Reverse stop order: Synapse is available before MAS reconnects.
        if names:
            self.run(['docker', 'start', *reversed(names)], timeout=30)
        deadline = time.monotonic() + 120
        while names:
            current = [self.container(name) for name in names]
            if all(
                item['running'] and item.get('health') in ('none', 'healthy') for item in current
            ):
                break
            if time.monotonic() >= deadline:
                raise Error('Возобновление не подтверждено; журнал сохранён.')
            time.sleep(2)
        self.journal.unlink()
        emit('existing_backup_recovery', pending=False, resumed=len(names))

    def backup(self, profile=None, key_file=None):
        self.require_no_pending()
        self.validate()
        key = backup_crypto.read_key(self.model['backup_key'])
        if key_file and backup_crypto.read_key(key_file) != key:
            raise Error('Для этой установки используйте её существующий recovery key.')
        active = self.control / 's3/backup-active.json'
        if profile is None and active.exists():
            profile = self.control / 's3/backup.env'
        values = existing_s3.profile(profile, 'backup') if profile else None
        if values:
            if active.exists() and json.loads(active.read_text())[
                'storage'
            ] != existing_s3.media_transfer.identity(values):
                raise Error(
                    'Назначение S3-копий изменилось. Верните действующий профиль; прежние копии сохранены.'
                )
            media_profile = self.control / 's3/media.env'
            if media_profile.exists():
                existing_s3.separate(existing_s3.profile(media_profile, 'media'), values)
            existing_s3.media_transfer.client(values).head_bucket(Bucket=values['S3_BUCKET'])
        base = private_directory(self.model['backup_directory'])
        required = 1024**3
        if not values:
            _, raw = self.run(['du', '-sx', '-B1', str(self.root)])
            required += int(raw.split()[0])
        else:
            for name in self.databases:
                _, raw = self.pg(
                    'psql', '-d', name, '-Atc', 'SELECT pg_database_size(current_database())'
                )
                required += int(raw.strip())
        if shutil.disk_usage(base).free < required:
            raise Error('Для копии недостаточно свободного места с резервом 1 ГБ.')
        destination = base / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        destination.mkdir(mode=0o700)
        database = destination / 'database'
        database.mkdir(mode=0o700)
        running = [name for name in self.writers if self.container(name)['running']]
        save(
            self.journal, {'format': 1, 'running_writers': running, 'destination': str(destination)}
        )
        started = time.monotonic()
        process, remote = None, None
        catalog = destination / 's3-catalog.json'
        try:
            # Reserve startup/readiness time within a 15-minute writer pause.
            signal.alarm(720)
            if running:
                self.run(['docker', 'stop', '--time', '30', *running])
                if any(self.container(name)['running'] for name in running):
                    raise Error('Остановка писателей не подтверждена; копия отклонена.')
            emit('existing_backup', phase='dumping', writers_paused=len(running))
            for name in self.databases:
                with (database / (name + '.dump')).open('xb') as output:
                    args = ['-Fc', '--create', name]
                    if name == 'synapse':
                        args.insert(0, '--exclude-table-data=public.e2e_one_time_keys_json')
                    self.pg('pg_dump', *args, outgoing=output)
            with (database / 'roles.sql').open('xb') as output:
                self.pg('pg_dumpall', '--roles-only', outgoing=output)
            telegram = telegram_backup.snapshot(self.root, destination, self.model)
            control_snapshot = destination / 'misbot-control'
            control_snapshot.mkdir(mode=0o700)
            # Operation journals/locks and changing SQLite are not copied as config.
            for path in self.backup_control_sources():
                info = command_wrapper.regular(path)
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise Error('Служебные настройки должны принадлежать root без чужой записи.')
                target = control_snapshot / path.relative_to(self.control)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                target.chmod(0o600)
            manifest = {
                'format': 'misbot-existing-compose-backup-v1',
                'server_name': self.state['domain'],
                'consistency': 'writers-paused',
                'source_directory': str(self.root),
                'database_sha256': {path.name: digest(path) for path in database.iterdir()},
                'images': self.model['images'],
                'one_time_keys_excluded': True,
            }
            if telegram:
                manifest['telegram'] = telegram
            manifest['control_sha256'] = {
                p.relative_to(destination).as_posix(): digest(p)
                for p in control_snapshot.rglob('*')
                if p.is_file()
            }
            save(destination / 'manifest.json', manifest)
            relative = str(self.root).lstrip('/')
            arguments = [
                'nice',
                '-n',
                '15',
                'ionice',
                '-c',
                '3',
                'tar',
                '-czf',
                '-',
                '--exclude=' + relative + '/.misbot',
                '--exclude=' + relative + '/backups',
                '--exclude=' + relative + '/backup-*.sql',
                '--exclude=*.log',
                '--exclude=*.log.*',
            ]
            for item in self.model['raw_database_paths']:
                arguments.append('--exclude=' + relative + '/' + item)
            worker_data = telegram_backup.raw_data_exclusion(self.root, self.model)
            if worker_data:
                arguments.append('--exclude=' + worker_data)
            arguments += ['-C', '/', relative]
            for path in self.model.get('external_backup_paths', []):
                if not Path(path).is_absolute() or Path(path).resolve() != Path(path):
                    raise Error('Внешний путь копии должен быть абсолютным и не содержать symlink.')
                arguments.append(path.lstrip('/'))
            arguments += ['-C', str(destination), 'database', 'manifest.json', 'misbot-control']
            if telegram:
                arguments.append('telegram')
            process = subprocess.Popen(
                arguments, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **self.child_options()
            )
            archive = destination / 'backup.tar.gz.aesgcm'
            if values:
                remote = existing_s3.ObjectStream(values, catalog, save)
                backup_crypto.encrypt_stream(process.stdout, remote, key)
            else:
                backup_crypto.encrypt(
                    Path('/proc/self/fd') / str(process.stdout.fileno()), archive, key
                )
            process.stdout.close()
            if process.wait(timeout=30) != 0:
                raise Error(
                    'Архив изменился во время копирования или не завершён; копия не подтверждена.'
                )
            if remote:
                record = remote.complete()
                manifest.update(
                    encrypted_archive_sha256=record['sha256'], encrypted_bytes=record['bytes']
                )
            else:
                manifest.update(
                    encrypted_archive_sha256=digest(archive), encrypted_bytes=archive.stat().st_size
                )
            save(destination / 'manifest.json', manifest)
            selected = {
                'directory': str(destination),
                'sha256': manifest['encrypted_archive_sha256'],
            }
            if remote:
                selected['remote'] = record
            else:
                save(self.control / 'last-backup.json', selected)
        finally:
            signal.alarm(0)
            previous = {
                signum: signal.signal(signum, signal.SIG_IGN)
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
            }
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if remote and remote.record['phase'] not in ('uploaded', 'verified'):
                remote.abort()
            try:
                self.recover()
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
        paused = round(time.monotonic() - started)
        if remote:
            # Verify full ciphertext, every GCM frame and temporary DB restores after resume.
            self.verify(selected, values)
            existing_s3.s3_diagnostics.private_backup_probe(
                remote.service, values, {'key': record['key']}
            )
            remote.record.update(verified=True, phase='verified')
            save(catalog, remote.record)
            selected['remote'] = remote.record
            save(self.control / 'last-backup.json', selected)
            s3_folder = private_directory(self.control / 's3')
            profile_target = s3_folder / 'backup.env'
            if Path(profile) != profile_target:
                command_wrapper.atomic(profile_target, Path(profile).read_bytes(), 0o600)
            save(active, {'storage': existing_s3.media_transfer.identity(values)})
            for name in ('database', 'telegram', 'misbot-control'):
                scratch = destination / name
                if scratch.exists():
                    shutil.rmtree(scratch)
        emit(
            'existing_backup',
            passed=True,
            archive='s3' if remote else str(archive),
            sha256=manifest['encrypted_archive_sha256'],
            encrypted_bytes=manifest['encrypted_bytes'],
            paused_seconds=paused,
            verified=bool(remote),
        )

    def backup_control_sources(self):
        sources = [
            *(self.control / 'pins').glob('*.json'),
            *(self.control / 's3').glob('*.env'),
            *(self.control / 's3').glob('*-active.json'),
            *[
                self.control / 'features' / name
                for name in (
                    'site.json',
                    'policy.json',
                    'backup-schedule.json',
                    'backup-schedule-verified.json',
                )
                if (self.control / 'features' / name).exists()
            ],
        ]
        theme = self.control / 'features/site-template'
        if theme.exists() or theme.is_symlink():
            info = theme.lstat()
            if (
                theme.resolve() != theme
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_mode & 0o077
            ):
                raise Error('Небезопасный локальный шаблон; копия не подтверждена.')
            for name, limit in (('index.html', 2 * 1024**2), ('site.css', 256 * 1024)):
                path = theme / name
                info = command_wrapper.regular(path)
                if (
                    path.resolve() != path
                    or info.st_uid != 0
                    or info.st_mode & 0o077
                    or info.st_size > limit
                ):
                    raise Error('Небезопасный файл локального шаблона; копия не подтверждена.')
                sources.append(path)
        return sources

    def verify(self, selected=None, values=None):
        self.require_no_pending()
        self.validate()
        selected = selected or json.loads((self.control / 'last-backup.json').read_text())
        directory = Path(selected['directory'])
        if (
            not directory.is_relative_to(Path(self.model['backup_directory']))
            or directory.resolve() != directory
        ):
            raise Error('Копия находится вне собственного каталога.')
        remote = selected.get('remote')
        archive = (
            existing_s3.open_backup(
                values or existing_s3.profile(self.control / 's3/backup.env', 'backup'), remote
            )
            if remote
            else directory / 'backup.tar.gz.aesgcm'
        )
        if not remote and digest(archive) != selected['sha256']:
            raise Error('Контрольная сумма копии не совпадает; восстановление отклонено.')
        expected = {'database/' + name + '.dump' for name in self.databases}
        with tempfile.TemporaryDirectory(prefix='verify-', dir=self.control) as folder:
            temporary = Path(folder)
            found, manifest, control_hashes = set(), None, {}
            raw = Frames(archive, backup_crypto.read_key(self.model['backup_key']))
            with io.BufferedReader(raw) as reader:
                with tarfile.open(fileobj=reader, mode='r|gz') as payload:
                    for item in payload:
                        if item.name.startswith('misbot-control/') and item.isfile():
                            if (
                                not re.fullmatch(
                                    r'misbot-control/(pins/[a-z0-9_-]+\.json|s3/(media|backup)(\.env|-active\.json)|features/(site|policy|backup-schedule|backup-schedule-verified)\.json|features/site-template/(index\.html|site\.css))',
                                    item.name,
                                )
                                or item.name in control_hashes
                                or item.size > 2 * 1024**2
                            ):
                                raise Error('Некорректный payload служебных настроек.')
                            control_hashes[item.name] = existing_s3.media_transfer.stream_digest(
                                payload.extractfile(item)
                            )
                            continue
                        if item.name not in expected | {'manifest.json'} | telegram_backup.MEMBERS:
                            continue
                        if not item.isfile() or item.name in found or item.size > 512 * 1024**2:
                            raise Error('Некорректный или повторный payload базы данных.')
                        found.add(item.name)
                        source = payload.extractfile(item)
                        if item.name == 'manifest.json':
                            if item.size > 1024**2:
                                raise Error('Некорректный manifest копии.')
                            manifest = json.load(source)
                        else:
                            target = (
                                temporary / item.name
                                if item.name in telegram_backup.MEMBERS
                                else temporary / Path(item.name).name
                            )
                            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                            with target.open('xb') as output:
                                shutil.copyfileobj(source, output)
                # Authenticate the empty final frame and reject appended bytes.
                while reader.read(1024 * 1024):
                    pass
            if remote:
                archive.close()
                if (
                    archive.hash.hexdigest() != selected['sha256']
                    or archive.size != remote['bytes']
                ):
                    raise Error(
                        'Контрольная сумма S3-копии не совпадает; восстановление отклонено.'
                    )
            if (
                found - telegram_backup.MEMBERS != expected | {'manifest.json'}
                or not manifest
                or manifest.get('format') != 'misbot-existing-compose-backup-v1'
                or manifest.get('server_name') != self.state['domain']
            ):
                raise Error('Копия не принадлежит этой установке или неполна.')
            telegram_backup.verify(temporary, manifest, found)
            if control_hashes != manifest.get('control_sha256', {}):
                raise Error('Служебные настройки в копии неполны или изменились.')
            for index, database in enumerate(self.databases):
                source = temporary / (database + '.dump')
                if digest(source) != manifest['database_sha256'][source.name]:
                    raise Error('Контрольная сумма дампа не совпадает.')
                name = 'misbot_verify_' + uuid.uuid4().hex + '_' + str(index)
                created = False
                try:
                    self.pg('createdb', '--template=template0', '--encoding=UTF8', name)
                    created = True
                    with source.open('rb') as incoming:
                        self.pg(
                            'pg_restore',
                            '--exit-on-error',
                            '--no-owner',
                            '--no-acl',
                            '-d',
                            name,
                            incoming=incoming,
                        )
                    if database == 'synapse':
                        _, count = self.pg(
                            'psql',
                            '-d',
                            name,
                            '-Atc',
                            'SELECT count(*) FROM e2e_one_time_keys_json',
                        )
                        if count.strip() != b'0':
                            raise Error(
                                'В копии остались одноразовые ключи; восстановление отклонено.'
                            )
                    emit('existing_temporary_database_restore', database=database, passed=True)
                finally:
                    if created:
                        self.pg('dropdb', name)
        emit('existing_backup_verified', passed=True, plaintext_archive_written=False)

    def s3_check(self, config, purpose, read_only=False, catalog_directory=None, **ignored):
        self.require_no_pending()
        result = existing_s3.s3_diagnostics.diagnose(
            config,
            purpose,
            read_only=read_only,
            catalog_directory=catalog_directory or private_directory(self.control / 's3/probes'),
        )
        emit(
            'existing_s3_check',
            purpose=purpose,
            passed=result['passed'],
            cleanup_pending=result.get('cleanup_pending', False),
            failure_codes=[s['code'] for s in result['stages'] if s['status'] == 'failed'],
        )
        return 0 if result['passed'] else 1

    def s3_cleanup(self, config, catalog):
        record = existing_s3.s3_diagnostics.private_catalog(Path(catalog))
        result = existing_s3.s3_diagnostics.diagnose(
            config, record['purpose'], cleanup_catalog=catalog
        )
        emit(
            'existing_s3_cleanup',
            passed=result['passed'],
            cleanup_pending=result['cleanup_pending'],
        )
        return 0 if result['passed'] else 1

    def storage_status(self):
        folder = self.control / 's3'
        media, backup = folder / 'media-active.json', folder / 'backup-active.json'
        last = self.control / 'last-backup.json'
        selected = json.loads(last.read_text()) if last.exists() else {}
        copied = json.loads(media.read_text()) if media.exists() else {}
        emit(
            'existing_storage_status',
            media_s3=media.exists(),
            backup_s3=backup.exists(),
            media_files_verified=copied.get('files_verified', 0),
            media_pending=(folder / 'media-journal.json').exists(),
            local_media_retained=True,
            last_backup_verified=selected.get('remote', {}).get('verified') is True,
            backup_pending=self.journal.exists(),
        )

    def media_service(self):
        import yaml

        config = yaml.safe_load((self.root / self.model['identity']['synapse_config']).read_text())
        container_path = Path(config.get('media_store_path', '/data/media_store'))
        if not container_path.is_absolute():
            raise Error('Не удалось определить абсолютный путь медиа Synapse.')
        matches = []
        for project in self.projects:
            _, raw = self.run(self.compose(project, 'config', '--format', 'json'))
            data = json.loads(raw)
            for service, name in project['services'].items():
                if 'synapse' not in service.lower():
                    continue
                for volume in data['services'][service].get('volumes', []):
                    target = Path(volume['target'])
                    if volume['type'] == 'bind' and container_path.is_relative_to(target):
                        path = Path(volume['source']) / container_path.relative_to(target)
                        if (
                            path.resolve() == path
                            and path.is_relative_to(self.root)
                            and path.is_dir()
                        ):
                            matches.append((project, service, name, path))
        if len(matches) != 1:
            raise Error('Нужно однозначное bind-хранилище медиа внутри установки.')
        return matches[0]

    def restart_media(self, project, service):
        self.run(
            self.compose(
                project,
                'up',
                '-d',
                '--no-deps',
                '--force-recreate',
                '--pull',
                'never',
                '--no-build',
                '--wait',
                '--wait-timeout',
                '120',
                service,
            ),
            timeout=150,
        )

    def connect_media(self, config):
        import yaml

        self.require_no_pending()
        self.validate()
        values = existing_s3.profile(config, 'media')
        folder = private_directory(self.control / 's3')
        active = folder / 'media-active.json'
        if active.exists():
            if json.loads(active.read_text())['storage'] != existing_s3.media_transfer.identity(
                values
            ):
                raise Error('Назначение работающего S3 изменять нельзя; нужен отдельный перенос.')
            if existing_s3.profile(folder / 'media.env', 'media') != values:
                raise Error('Смена credentials требует отдельного плана; текущий профиль сохранён.')
            emit('existing_media_s3', passed=True, already_connected=True)
            return
        backup = folder / 'backup.env'
        if backup.exists():
            existing_s3.separate(values, existing_s3.profile(backup, 'backup'))
        selected = json.loads((self.control / 'last-backup.json').read_text())
        if not selected.get('remote', {}).get('verified'):
            raise Error(
                'Перед S3 нужна проверенная полная внешняя копия: misbot backup external create.'
            )
        project, service, name, media = self.media_service()
        if not self.container(name)['running']:
            raise Error('Перед подключением Synapse должен работать.')
        extension = existing_s3.verify_extension(
            self.root, name, self.model['images'][name]['digest']
        )
        target = self.root / self.model['identity']['synapse_config']
        current = yaml.safe_load(target.read_text())
        if current.get('media_storage_providers'):
            raise Error('Уже есть storage provider; нужен отдельный план переноса.')
        probe = existing_s3.s3_diagnostics.diagnose(
            config, 'media', catalog_directory=folder / 'probes'
        )
        if not probe['passed']:
            raise Error('S3 не прошёл проверку; рабочие настройки не менялись.')
        checkpoint = private_directory(folder / 'transitions' / uuid.uuid4().hex)
        pins = self.control / 'pins' / (project['name'] + '.json')
        for path, label in ((target, 'synapse.before'), (pins, 'pins.before')):
            shutil.copyfile(path, checkpoint / label)
            (checkpoint / label).chmod(0o600)
        profile_target = folder / 'media.env'
        if Path(config) != profile_target:
            command_wrapper.atomic(profile_target, Path(config).read_bytes(), 0o600)
        save(
            folder / 'media-journal.json',
            {
                'format': 1,
                'phase': 'prepared',
                'checkpoint': str(checkpoint),
                'project': project['name'],
                'service': service,
                'container': name,
                'media': str(media),
                'storage': existing_s3.media_transfer.identity(values),
            },
        )
        current['media_storage_providers'] = [
            {
                'module': 'misbot_s3_provider.S3StorageProviderBackend',
                'store_local': True,
                'store_remote': True,
                'store_synchronous': True,
                'config': {
                    'bucket': values['S3_BUCKET'],
                    'region_name': values['S3_REGION'],
                    'endpoint_url': values['S3_ENDPOINT_URL'] or None,
                    'access_key_id': values['S3_ACCESS_KEY_ID'],
                    'secret_access_key': values['S3_SECRET_ACCESS_KEY'],
                    'prefix': values['S3_PREFIX'],
                    'threadpool_size': 2,
                },
            }
        ]
        overlay = json.loads(pins.read_text())
        entry = overlay['services'][service]
        entry.setdefault('volumes', []).append(
            {
                'type': 'bind',
                'source': str(extension / 'dependencies'),
                'target': '/opt/misbot-s3',
                'read_only': True,
            }
        )
        adapter = self.root / 'misbot_s3_provider.py'
        info = command_wrapper.regular(adapter)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise Error('S3 adapter должен принадлежать root без чужой записи.')
        entry['volumes'].append(
            {
                'type': 'bind',
                'source': str(adapter),
                'target': '/opt/misbot-s3-adapter/misbot_s3_provider.py',
                'read_only': True,
            }
        )
        entry.setdefault('environment', {})['PYTHONPATH'] = '/opt/misbot-s3:/opt/misbot-s3-adapter'
        entry['environment']['PYTHONDONTWRITEBYTECODE'] = '1'
        # Single-file bind mounts must keep their inode until the container is recreated.
        with target.open('w') as output:
            output.write(yaml.safe_dump(current, sort_keys=False))
            output.flush()
            os.fsync(output.fileno())
        save(pins, overlay)
        started = time.monotonic()
        try:
            self.validate()
            self.restart_media(project, service)
        except BaseException:
            self.rollback_media()
            raise
        journal = json.loads((folder / 'media-journal.json').read_text())
        journal.update(phase='copying', restart_seconds=round(time.monotonic() - started, 2))
        save(folder / 'media-journal.json', journal)
        emit('existing_media_s3', phase='copying', restart_seconds=journal['restart_seconds'])
        self.recover_media()

    def rollback_media(self):
        folder = self.control / 's3'
        journal = json.loads((folder / 'media-journal.json').read_text())
        checkpoint = Path(journal['checkpoint'])
        if checkpoint.parent != folder / 'transitions' or checkpoint.resolve() != checkpoint:
            raise Error('Некорректный checkpoint S3; настройки сохранены.')
        project = next(p for p in self.projects if p['name'] == journal['project'])
        if project['services'].get(journal['service']) != journal['container']:
            raise Error('Checkpoint S3 не принадлежит принятой службе.')
        target = self.root / self.model['identity']['synapse_config']
        with target.open('wb') as output:
            output.write((checkpoint / 'synapse.before').read_bytes())
            output.flush()
            os.fsync(output.fileno())
        save(
            self.control / 'pins' / (project['name'] + '.json'),
            json.loads((checkpoint / 'pins.before').read_text()),
        )
        self.restart_media(project, journal['service'])
        (folder / 'media-journal.json').unlink()
        emit(
            'existing_media_s3_rollback',
            passed=True,
            local_files_retained=True,
            remote_objects_retained=True,
        )

    def recover_media(self):
        folder = self.control / 's3'
        path = folder / 'media-journal.json'
        if not path.exists():
            emit('existing_media_recovery', pending=False)
            return
        journal = json.loads(path.read_text())
        if journal.get('phase') == 'prepared':
            self.rollback_media()
            return
        if journal.get('phase') != 'copying':
            raise Error('Неизвестный этап S3; сохраните checkpoint.')
        self.validate()
        project, service, name, media = self.media_service()
        if (
            name != journal['container']
            or str(media) != journal['media']
            or not self.container(name)['running']
        ):
            raise Error('Проверьте службу Synapse; checkpoint S3 сохранён.')
        values = existing_s3.profile(folder / 'media.env', 'media')
        if journal['storage'] != existing_s3.media_transfer.identity(values):
            raise Error('Верните исходный профиль переноса S3; данные сохранены.')
        result = existing_s3.copy_media(
            media,
            values,
            folder / 'media-copy.json',
            save,
            lambda **v: emit('existing_media_copy', **v),
        )
        save(
            folder / 'media-active.json',
            {'storage': existing_s3.media_transfer.identity(values), **result},
        )
        path.unlink()
        emit('existing_media_s3', passed=True, **result)

    def ui(self, plain=False):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise Error('SSH-меню требует TTY: sudo misbot ui.')
        import terminal_ui

        previous = {
            number: signal.getsignal(number)
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }

        def interrupted_ui(signum, frame):
            if signum == signal.SIGINT:
                raise KeyboardInterrupt()
            raise terminal_ui.state_store.OperationInterrupted('SSH/UI interrupted')

        try:
            for number in previous:
                signal.signal(number, interrupted_ui)
            interface = terminal_ui.UI(
                directory=self.root, source=self.root, view=terminal_ui.TerminalView(plain=plain)
            )
            with interface.view.screen():
                code = interface.menu()
            if interface.view.screen_used:
                print('Мастер закрыт. ' + interface.continuation())
            return code
        except (EOFError, terminal_ui.state_store.OperationInterrupted):
            print('Ввод прерван. Повторите sudo misbot ui; проверьте misbot status.')
            return 130
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)

    @contextlib.contextmanager
    def lock(self):
        path = self.control / 'operation.lock'
        held = getattr(_locks, 'held', {})
        if str(self.root) in held:
            yield
            return
        inherited = os.environ.get('MISBOT_EXISTING_LOCK_FD')
        parent = os.environ.get('MISBOT_EXISTING_LOCK_PARENT')
        if inherited is not None or parent is not None:
            try:
                # Only a directly spawned child may adopt a validated descriptor.
                if int(parent) != os.getppid() or int(parent) == os.getpid():
                    raise ValueError()
                descriptor = os.dup(int(inherited))
                actual, expected = os.fstat(descriptor), path.stat()
                if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                    os.close(descriptor)
                    raise ValueError()
            except (OSError, TypeError, ValueError):
                raise Error('Неверная унаследованная блокировка; действие отклонено.') from None
        else:
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != 0
                or info.st_mode & 0o077
            ):
                raise Error('Небезопасная блокировка операции.')
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Error(
                    'Другой процесс управляет установкой; дождитесь его завершения.'
                ) from None
            _locks.held = {**held, str(self.root): descriptor}
            try:
                yield
            finally:
                _locks.held = held
        finally:
            os.close(descriptor)


def interrupted(signum, frame):
    raise Error('Операция прервана; проверьте misbot status и backup recover.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument(
        'action',
        choices=sorted(cli_commands.EXISTING_BACKENDS - {'wrapper-install', 'wrapper-remove'}),
    )
    parser.add_argument('--config', type=Path)
    parser.add_argument('--key', type=Path)
    parser.add_argument('--purpose', choices=('media', 'backup'))
    parser.add_argument('--catalog', type=Path)
    parser.add_argument('--catalog-directory', type=Path)
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--plain', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        emit('existing_operation', passed=False, reason='root_required')
        return 77
    os.umask(0o077)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        installation = Installation(args.directory)
        actions = {
            'status': installation.status,
            'check': installation.check,
            'up': installation.start,
            'stop': installation.stop,
            'configuration-validate': installation.validate,
            'backup': installation.backup,
            'backup-recover': installation.recover,
            'restore-check': installation.verify,
            'ui': lambda: installation.ui(plain=args.plain),
            'storage-status': installation.storage_status,
            'media-recover': installation.recover_media,
            'media-s3': lambda: installation.connect_media(args.config),
            'backup-external': lambda: installation.backup(args.config, args.key),
            's3-check': lambda: installation.s3_check(
                args.config, args.purpose, args.read_only, args.catalog_directory
            ),
            's3-cleanup': lambda: installation.s3_cleanup(args.config, args.catalog),
        }
        if (
            args.action in ('media-s3', 'backup-external', 's3-check', 's3-cleanup')
            and not args.config
        ):
            raise Error('Нужен --config с путём к приватному S3 env.')
        if args.action == 'backup-external' and not args.key:
            raise Error('Нужен --key с путём к существующему recovery key.')
        if args.action == 's3-check' and not args.purpose:
            raise Error('Нужно --purpose media или backup.')
        if args.action == 's3-cleanup' and not args.catalog:
            raise Error('Нужен --catalog собственного probe.')
        if args.action == 'ui':
            return installation.ui() or 0
        if args.action in ('status', 'storage-status', 'check'):
            return actions[args.action]() or 0
        with installation.lock():
            return actions[args.action]() or 0
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, Error)
            else 'Операция не подтверждена. Проверьте sudo misbot status; секреты и данные сохранены.'
        )
        emit('existing_operation', passed=False, explanation=message)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
