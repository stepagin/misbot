#!/usr/bin/env python3
"""Thin Misbot command router. Backend processes retain their locks and signals."""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import sys

sys.dont_write_bytecode = True

import cli_commands as vocabulary
import command_wrapper


class InputError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse messages may contain secret values. Only registered option
        # names and static explanations may reach the user.
        if message.startswith('the following arguments are required:'):
            names = [
                action.option_strings[0]
                for action in self._actions
                if action.required
                and action.option_strings
                and action.option_strings[0] in message.split(':', 1)[1].strip().split(', ')
            ]
            explanation = 'Не указаны обязательные аргументы: ' + ', '.join(names) + '.'
        else:
            action = next(
                (
                    action
                    for action in self._actions
                    if any(
                        message.startswith('argument ' + option + ': expected one argument')
                        for option in action.option_strings
                    )
                ),
                None,
            )
            if action:
                explanation = 'Для аргумента ' + action.option_strings[0] + ' требуется значение.'
            elif message.startswith('unrecognized arguments:'):
                explanation = 'Указан неизвестный аргумент или лишнее значение.'
            else:
                explanation = 'Неверная команда или аргументы.'
        raise InputError(explanation + ' Справка: ' + self.prog + ' --help')

    def _check_value(self, action, value):
        if action.choices is not None and value not in action.choices:
            raise InputError(
                'Недопустимое значение '
                + action.option_strings[0]
                + '. Допустимо: '
                + ', '.join(action.choices)
                + '. Справка: '
                + self.prog
                + ' --help'
            )


def operation_help(command):
    syntax = (
        'misbot '
        + command.path
        + ' '
        + ' '.join('--' + name + ' VALUE' for name in command.arguments)
    )
    print(command.purpose + '\n\nКоманда: ' + syntax.rstrip())
    print('Обязательные аргументы: ' + (', '.join('--' + n for n in command.arguments) or 'нет'))
    print(
        'Необязательные: --directory PATH; для update --source PATH; для S3 check --read-only, --json, --catalog-directory PATH; для S3 cleanup --json.'
        if command.backend.startswith('s3-')
        else 'Необязательные: --directory PATH'
        + (
            '; --source PATH (доверенный распакованный комплект)'
            if command.backend in ('update', 'update-recover')
            else ''
        )
        + '.'
    )
    print(
        'Defaults: сохранённый системный каталог; из распакованного комплекта укажите --directory. init/restore: /opt/matrix-stack.'
    )
    if command.backend == 's3-check':
        print(
            'S3 defaults: --purpose обязателен; режим с записью; catalog /root/.local/state/misbot/s3-probes. --read-only не проверяет GET/PUT/DELETE. --json: schema=1 и безопасные stages; exit 0/1/130.'
        )
    elif command.backend == 's3-cleanup':
        print(
            'S3 cleanup: --catalog обязателен; удаляется только доказанный собственный объект/VersionId. --json: schema=1; exit 0/1/130.'
        )
    elif command.backend in ('homepage-check', 'homepage-apply'):
        print(
            'Аргументы необязательны: --mode landing|element|external (default landing), --url HTTPS_URL (обязателен для external), --name TEXT, --description TEXT, --support-url HTTPS_URL. Неуказанные поля сохраняются. Пустой --support-url удаляет блок поддержки.'
        )
        print(
            'Пример: sudo misbot homepage check --mode external --url https://community.example.net/'
        )
        print(
            'Затем: sudo misbot homepage apply --mode external --url https://community.example.net/ . URL чужого сайта проверяется синтаксически; доступность и содержимое не подтверждаются.'
        )
    print('Root: да; используйте sudo. В non-TTY: sudo -n (без запроса пароля).')
    print('Остановка служб: ' + command.stops + '\nВлияние на данные: ' + command.data)
    sample = {
        'domain': 'example.org',
        'config': '/root/misbot-s3.env',
        'purpose': 'media',
        'input': '/root/misbot-user.json',
        'key': '/root/misbot-backup.key',
        'catalog': '/root/.local/state/misbot/s3-probes/PROBE.json',
        'calendar': '*-*-* 03:00:00 UTC',
        'archive': '/root/misbot-backup.tar.gz',
        'sha256': 'ARCHIVE_SHA256',
        'public-ip': '203.0.113.20',
        'confirm': 'source-stopped' if command.backend == 'restore-activate' else 'target-stopped',
        'object': 'backup/example/object.enc',
        'output': '/root/verified-backup.tar.gz',
        'tag': 'admin-panel',
        'server': 'example.org',
        'request': 'MB-0123456789abcdef0123456789abcdef',
    }
    if command.backend.startswith('vault-'):
        sample['config'] = '/root/misbot-vault.env'
    if command.backend == 'database-external':
        sample['config'] = '/root/misbot-postgres.env'
    if command.backend.startswith('backup-'):
        sample['config'] = '/root/misbot-backup-s3.env'
    if command.backend == 'backup-cleanup':
        sample['catalog'] = '/opt/matrix-stack/stages/external-backups/OWNED/catalog.json'
    example = 'sudo ' + syntax.split(' --', 1)[0]
    for name in command.arguments:
        example += ' --' + name + ' ' + shlex.quote(sample[name])
    if command.backend in ('update', 'update-recover'):
        example += ' --source /root/matrix-installer'
    print('Пример: ' + example)
    print(
        'Результат: код 0 и подтверждение операции backend; S3 --json: schema=1, stages и conclusion.'
    )
    print(
        'После ошибки: сохраните данные/ключи; исправьте указанную причину, выполните misbot status и точную recovery-команду из сообщения. Для S3 исправьте приватный файл и повторите эту проверку; cleanup повторяйте только по своему catalog.'
    )


def help_text(topic=''):
    if command_wrapper.default_backend() == 'existing-compose':
        supported = [c for c in vocabulary.COMMANDS if c.backend in vocabulary.EXISTING_BACKENDS]
        if topic in vocabulary.BY_PATH:
            command = vocabulary.BY_PATH[topic]
            if command.backend not in vocabulary.EXISTING_BACKENDS:
                raise InputError(
                    'Команда не поддерживается для подключённого существующего Compose-стека. Выполните misbot help.'
                )
            if command.backend == 'restore-check':
                print(
                    'Проверить AES-GCM архив и восстановить дампы в новые временные БД с их последующим удалением.'
                )
                return
            operation_help(command)
            return
        print(
            'Misbot — управление существующим Compose-стеком. Конфигурация, identity и версии сохраняются.'
        )
        found = [c for c in supported if not topic or c.path.startswith(topic + ' ')]
        if topic and not found:
            raise InputError(
                'Область не поддерживается для существующего стека. Выполните misbot help.'
            )
        for command in found:
            print('  misbot ' + command.path + ' — ' + command.purpose)
        print(
            '  misbot version\nКопии этого режима зашифрованы; recovery key хранится отдельно. Подробно: existing-compose.md.'
        )
        return
    if topic in vocabulary.BY_PATH:
        operation_help(vocabulary.BY_PATH[topic])
        return
    if not topic:
        print(
            'Misbot — команды управления Matrix\n\nСостояние и службы: status, check, start, stop, remove\nВерсия: version\n'
        )
        for name, title in vocabulary.TOPICS.items():
            print('  ' + name + ' — ' + title)
        print(
            '\nПодробно: misbot storage help; misbot storage s3 check --help\nВыбор установки: --directory PATH. До установки: sudo python3 misbot.py ui.\nВ TTY без аргументов открывается мастер/меню; без TTY — эта справка. Явно: sudo misbot ui.'
        )
        return
    found = [c for c in vocabulary.COMMANDS if c.path.startswith(topic + ' ')]
    if not found:
        raise InputError('Неизвестная область. Выполните misbot help.')
    print(vocabulary.TOPICS.get(topic, topic))
    for c in found:
        print('  misbot ' + c.path + ' — ' + c.purpose)
    print('\nАргументы и условия: misbot COMMAND --help')


def parse(argv):
    # Extract global options wherever they appear; argv values are never executed.
    common = Parser(prog='misbot', add_help=False, allow_abbrev=False)
    common.add_argument('--directory')
    common.add_argument('--source')
    defaults, remaining = common.parse_known_args(argv)
    if not remaining or remaining == ['--help'] or remaining == ['help']:
        return None, defaults, ''
    if remaining[0] == 'help':
        return None, defaults, ' '.join(remaining[1:])
    if remaining[-1:] in (['help'], ['--help']):
        words = []
        for value in remaining[:-1]:
            if value.startswith('-'):
                break
            words.append(value)
        return None, defaults, ' '.join(words)
    if remaining == ['version']:
        return 'version', defaults, None
    words = []
    for value in remaining:
        if value.startswith('-'):
            break
        words.append(value)
    command = vocabulary.BY_PATH.get(' '.join(words))
    if command is None:
        raise InputError('Неизвестная команда. Выполните misbot help.')
    parser = Parser(prog='misbot ' + command.path, add_help=False, allow_abbrev=False)
    for name in command.arguments:
        parser.add_argument(
            '--' + name,
            dest=name,
            required=True,
            choices=('media', 'backup') if name == 'purpose' else None,
        )
    for name in command.optional_arguments:
        parser.add_argument('--' + name.replace('_', '-'))
    for name in command.switches:
        parser.add_argument('--' + name.replace('_', '-'), action='store_true')
    args = parser.parse_args(remaining[len(words) :])
    if defaults.source and command.backend not in ('update', 'update-recover'):
        raise InputError('--source применяется только к update apply/recover.')
    return command, defaults, vars(args)


def plan(command, options, values, source=None):
    source = Path(source or Path(__file__).resolve().parent)
    if options.directory:
        root = Path(options.directory)
    else:
        root = command_wrapper.default_directory()
        if root is None and (source / 'installation.json').is_file():
            root = source
        if root is None and command.backend in ('init', 'restore', 'restore-local'):
            root = Path('/opt/matrix-stack')
    if root is not None and (root / 'installation.json').is_file():
        command_wrapper.trusted_installation(root)
        if (
            json.loads((root / 'installation.json').read_text()).get('backend')
            == 'existing-compose'
        ):
            if command.backend not in vocabulary.EXISTING_BACKENDS:
                raise InputError(
                    'Команда не поддерживается для существующего Compose-стека. Настройки и данные сохранены; выполните misbot help.'
                )
            if command.backend in ('update', 'update-recover'):
                if not options.source:
                    raise InputError(
                        'Для update укажите --source PATH к доверенному распакованному комплекту.'
                    )
                candidate = Path(options.source)
                if not candidate.is_absolute() or candidate.resolve() != candidate:
                    raise InputError('--source требует абсолютный каталог без symlink.')
                info = command_wrapper.regular(candidate / 'existing_update.py')
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise InputError('Source updater должен принадлежать root без чужой записи.')
                backend = candidate
            else:
                backend = root
            return vocabulary.execution_argv(command, values, root, backend, existing=True)
    if command.backend in ('s3-check', 's3-cleanup'):
        if root is not None:
            if not root.is_absolute() or root == Path('/') or root.resolve() != root:
                raise InputError('Нужен абсолютный каталог без symlinks и переходов ..')
            if root.exists() and any(root.iterdir()):
                command_wrapper.trusted_installation(root)
        args = ['/usr/bin/python3', str(source / 's3_diagnostics.py'), command.backend[3:]]
        for name in command.arguments:
            args += ['--' + name, values[name]]
        if values.get('json'):
            args += ['--json']
        if values.get('read_only'):
            args += ['--read-only']
        if values.get('catalog_directory'):
            args += ['--catalog-directory', values['catalog_directory']]
        if root is not None:
            args += ['--directory', str(root)]
        # Installed help runtime is public; diagnostics runs from selected backend.
        if (source / 'command.json').is_file():
            if root is None:
                raise InputError('Укажите --directory PATH или используйте распакованный комплект.')
            command_wrapper.trusted_installation(root)
            args[1] = str(root / 's3_diagnostics.py')
        return args
    if command.backend == 'ui':
        if root is not None and (
            not root.is_absolute() or root == Path('/') or root.resolve() != root
        ):
            raise InputError('UI требует отдельный абсолютный каталог без symlink и ..')
        backend = source
        if (source / 'command.json').is_file():
            if root is None:
                raise InputError('До init: sudo python3 misbot.py ui из распакованного комплекта.')
            command_wrapper.trusted_runtime(root)
            backend = root
        return [
            '/usr/bin/python3',
            str(backend / 'terminal_ui.py'),
            *(['--directory', str(root)] if root else []),
            *(['--plain'] if values.get('plain') else []),
        ]
    if command.backend.startswith('features-'):
        raise InputError(
            'Этот раздел предназначен для подключённого существующего Compose-стека; обычная установка уже содержит компоненты.'
        )
    if command.backend == 'storage-status':
        if root is None:
            raise InputError('Укажите --directory PATH.')
        command_wrapper.trusted_installation(root)
        return ['/bin/bash', str(root / 'install.sh'), 'status', str(root)]
    if root is None and not command.installed:
        root = Path('/opt/matrix-stack')
    if root is None:
        raise InputError(
            'Установка не выбрана. Укажите --directory PATH; сначала выполните config init из распакованного комплекта.'
        )
    if not root.is_absolute() or root == Path('/') or root.resolve() != root:
        raise InputError('Нужен абсолютный каталог установки без symlinks и переходов ..')
    if command.installed:
        if command.backend == 'remove':
            command_wrapper.trusted_runtime(root)
        else:
            command_wrapper.trusted_installation(root)
    backend = Path(options.source) if options.source else (root if command.installed else source)
    if (source / 'command.json').is_file() and not command.installed:
        backend = command_wrapper.default_directory()
        if backend is None:
            raise InputError(
                'До настройки используйте python3 misbot.py из распакованного комплекта.'
            )
        command_wrapper.trusted_runtime(backend)
    if command.backend in ('update', 'update-recover'):
        if options.source is None:
            if not (source / 'command.json').is_file() and source != root:
                backend = source
            else:
                raise InputError(
                    'Для update укажите --source PATH к доверенному распакованному комплекту.'
                )
        # The updater verifies the complete manifest and compatibility itself.
        # Do not import it from the intentionally small public help runtime.
        if not backend.is_absolute() or backend.resolve() != backend:
            raise InputError('--source требует абсолютный каталог без symlinks.')
        info = command_wrapper.regular(backend / 'install.sh')
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise InputError(
                'Распакованный комплект должен принадлежать root и быть защищён от записи другими пользователями.'
            )
        if (
            json.loads((backend / 'release.json').read_text()).get('family')
            != command_wrapper.FAMILY
        ):
            raise InputError('Чужой комплект обновления.')
    return vocabulary.execution_argv(command, values, root, backend)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        argv = ['ui']
    try:
        command, options, values = parse(argv)
        if command is None:
            help_text(values)
            return 0
        if command == 'version':
            print(
                'Misbot '
                + json.loads((Path(__file__).resolve().parent / 'release.json').read_text())[
                    'version'
                ]
            )
            return 0
        if os.geteuid() != 0:
            prefix = 'sudo -n' if not sys.stdin.isatty() else 'sudo'
            follow = prefix + ' ' + shlex.join(['misbot', *argv])
            message = 'Нужны права root. ' + (
                'sudo отсутствует; обратитесь к администратору или войдите как root.'
                if not shutil.which('sudo')
                else 'Выполните: ' + follow
            )
            print(message + ' Пароль автоматически не запрашивается.', file=sys.stderr)
            return 77
        args = plan(command, options, values)
        os.execv(args[0], args)
    except (InputError, OSError, ValueError, RuntimeError, KeyError):
        # Input/backend selection errors never expose credentials or raw driver text.
        message = (
            str(sys.exc_info()[1])
            if isinstance(sys.exc_info()[1], InputError)
            else 'Каталог или комплект не принадлежит совместимой установке Misbot. Сохраните данные и проверьте --directory/--source.'
        )
        print(message, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
