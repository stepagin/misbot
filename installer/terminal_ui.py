"""Task-oriented SSH interface using the existing CLI and private wizard drafts."""

import argparse
from contextlib import contextmanager
from contextlib import nullcontext
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time

import configuration_schema as schema
import cli_commands
import state_store
from terminal_views import TerminalView, literal
from terminal_diagnostics import CHECKS, SERVICES, check_summary, parse_checks
import ui_actions
import terminal_navigation


class Back(Exception):
    pass


class Cancel(Exception):
    pass


class SaveExit(Exception):
    pass


def text(message):
    print(message, flush=True)


def question(label, default='', secret=False, reader=None):
    shown = 'настроен' if secret and default else default
    prompt = literal(label) + (' [' + literal(shown) + ']' if shown else '') + ': '
    if reader:
        value = reader(prompt, secret)
    elif secret:
        # getpass fallback would echo on a non-terminal: explicitly forbid it.
        if not sys.stdin.isatty():
            raise Cancel()
        value = getpass.getpass(prompt)
    else:
        value = input(prompt)
    commands = {
        'назад': Back,
        'b': Back,
        'отмена': Cancel,
        'сохранить и выйти': SaveExit,
        'q': SaveExit,
    }
    if value.casefold().strip() in commands:
        raise commands[value.casefold().strip()]()
    return value if value else default


class UI:
    def __init__(
        self, directory=None, source=None, reader=None, runner=None, draft=None, view=None
    ):
        self.source = Path(source or Path(__file__).resolve().parent)
        self.root = Path(directory or '/opt/matrix-stack')
        self.reader = reader
        self.runner = runner
        self.view = view or TerminalView()
        self.check_results = []
        self.result_lines = []
        self.result_total = 0
        self.result_messages = []
        self.result_notices = []
        self.restart_requested = False
        self.supplied_arguments = {}
        self.explicit_draft = draft is not None
        self.draft = Path(draft) if self.explicit_draft else self.draft_path(self.root)
        self.model = {}
        self.backup = {}
        self.admin_tag = 'admin-panel'
        self.dns_verified = None
        self.confirmed_domains = set()
        self.step = 0
        if self.draft.exists():
            from configuration import read_private_json

            saved = read_private_json(self.draft)
            self.model = saved['values']
            self.backup = saved.get('backup', {})
            self.dns_verified = saved.get('dns_verified')
            self.root = Path(saved['directory'])
            self.admin_tag = saved.get('admin_tag', self.saved_admin_tag())
            self.confirmed_domains = set(saved.get('confirmed_domains', []))
            self.step = saved.get('step', 0)
            self.root = Path(saved['directory'])
        elif (
            (self.root / 'installation.json').exists()
            and not self.adopted()
            and not (self.root / 'configuration-transition.json').exists()
        ):
            from configuration import load

            self.model = load(self.root, complete=False)
            self.admin_tag = self.saved_admin_tag()

    def saved_admin_tag(self):
        from configuration import read_private_json

        identity = self.root / 'config/admin-panel-identity.json'
        if identity.exists():
            return read_private_json(identity)['user_id'].split(':', 1)[0].removeprefix('@')
        return 'admin-panel'

    @staticmethod
    def draft_path(root):
        import wizard_state

        return wizard_state.draft_path(root)

    def source_entrypoint(self):
        outer = self.source.parent / 'misbot.py'
        return (
            outer
            if self.source.name == '.misbot' and outer.is_file()
            else self.source / 'misbot.py'
        )

    def ask(self, label, default='', secret=False):
        reader = self.reader
        if (
            reader is None
            and self.view.fullscreen
            and sys.stdin.isatty()
            and os.isatty(sys.stdin.fileno())
        ):
            reader = self.view.read_input
        return question(label, default, secret, reader)

    def pause(self, label='Enter — продолжить'):
        if self.view.fullscreen:
            self.ask(label)

    def installation(self):
        path = self.root / 'installation.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def adopted(self):
        return self.installation().get('backend') == 'existing-compose'

    def supported(self, path):
        command = cli_commands.BY_PATH.get(path)
        if command is None:
            return False
        if command.installed and not self.installation():
            return False
        if self.installation().get('decommissioned') and path not in ('status', 'remove'):
            return False
        if path == 'admin bot set' and (
            self.installation().get('federation_provisioned')
            or any(
                (self.root / 'data/referral-control' / name).exists()
                for name in ('federation-session.json', 'managed-federation-user.json')
            )
        ):
            return False
        if self.adopted():
            return command.backend in cli_commands.EXISTING_BACKENDS
        return not command.backend.startswith('features-')

    def choose(self, title, choices, exit_label='Назад', aliases=None, keep_hints=False):
        choices = list(choices) + [('0', exit_label, '')]
        valid = {item[0] for item in choices}
        snapshot = self.view.snapshot()
        page = 0
        warning = ''
        while True:
            self.view.redraw(snapshot)
            if warning:
                self.view.message(warning, 'yellow')
            pages, show_hints = self.view.choice_pages(title, choices, exit_label, keep_hints)
            page = min(page, len(pages) - 1)
            self.view.options(title, pages[page], show_hints)
            if len(pages) > 1:
                self.view.message(
                    'Страница ' + str(page + 1) + '/' + str(len(pages)) + ' · > далее · < ранее',
                    'dim',
                )
            self.view.footer(exit_label)
            value = self.ask('Номер', '0').strip()
            value = (aliases or {}).get(value, value)
            if value in valid:
                return value
            if value in ('>', '<') and len(pages) > 1:
                page = max(0, min(page + (1 if value == '>' else -1), len(pages) - 1))
                warning = ''
                continue
            warning = 'Выберите номер из списка. Enter — ' + exit_label.lower() + '.'

    def confirm(self, label='Выполнить операцию?'):
        return self.answer(label + ' да/нет', ('да', 'нет'), 'нет') == 'да'

    def answer(self, label, choices, default):
        while True:
            answer = self.ask(label, default).strip().casefold()
            if answer in choices:
                return answer
            self.view.message(
                'Допустимые ответы: ' + ', '.join(choices) + '. Enter — ' + default + '.',
                'yellow',
            )

    def input_value(self, label, default, validate, secret=False):
        while True:
            value = self.ask(label, default, secret)
            try:
                return validate(value)
            except schema.InvalidConfiguration as error:
                self.view.message(error.field + ': ' + error.explanation, 'yellow')
            except (ValueError, RuntimeError) as error:
                self.view.message(str(error), 'yellow')
            except OSError:
                self.view.message('Не удалось проверить путь. Проверьте каталог и права.', 'yellow')

    def confirm_domain(self, value):
        if not any(part.startswith('xn--') for part in value.split('.')):
            return True
        if value in self.confirmed_domains:
            return True
        try:
            readable = value.encode('ascii').decode('idna')
        except UnicodeError:
            self.view.message('Не удалось прочитать международное имя. Проверьте домен.', 'yellow')
            return False
        self.view.details([('Имя домена', readable), ('Адрес для DNS', value)])
        self.view.message(
            'В домене есть нелатинские символы. Проверьте раскладку и лишние буквы. '
            'Если имя неверно, выберите «нет» и введите его заново.',
            'yellow',
        )
        if not self.confirm('Использовать именно этот домен?'):
            return False
        self.confirmed_domains.add(value)
        return True

    def check_domain_inputs(self):
        for key in ('DOMAIN', 'MAS_DOMAIN'):
            try:
                value = schema.field_value(key, self.model.get(key, ''))
            except schema.InvalidConfiguration:
                self.field(key)
                continue
            if not self.confirm_domain(value):
                self.field(key)
            else:
                self.model[key] = value
        self.save()

    def save(self):
        from configuration import directory

        previous = self.draft
        destination = previous if self.explicit_draft else self.draft_path(self.root)
        if destination != previous and destination.exists():
            raise RuntimeError(
                'Для этого каталога уже есть черновик. Сохраните текущий и откройте '
                'ui --directory для нужного каталога, чтобы продолжить его.'
            )
        directory(self.draft.parent)
        state_store.write_json(
            destination,
            {
                'format': 1,
                'directory': str(self.root),
                'step': self.step,
                'values': self.model,
                'backup': self.backup,
                'admin_tag': self.admin_tag,
                'dns_verified': self.dns_verified,
                'confirmed_domains': sorted(self.confirmed_domains),
            },
        )
        self.draft = destination
        if previous != destination:
            previous.unlink(missing_ok=True)

    def draft_hints(self):
        import shutil

        entry = (
            'sudo misbot'
            if shutil.which('misbot')
            else ('sudo python3 ' + shlex.quote(str(self.source_entrypoint())))
        )
        command = entry + ' --directory ' + shlex.quote(str(self.root))
        return (
            'Черновик сохранён приватно.\n'
            'Продолжить: ' + command + ' ui\n'
            'Показать состояние черновика: ' + command + ' config draft show\n'
            'Удалить черновик: ' + command + ' config draft reset --confirm discard-draft'
        )

    def initialized(self):
        from configuration import read_private_json

        path = self.root / 'installation.json'
        return path.exists() and bool(read_private_json(path).get('databases_initialized'))

    def use_active_configuration(self):
        from configuration import load, backup_profile

        self.model = load(self.root, complete=False)
        self.backup = backup_profile(self.root)
        self.admin_tag = self.saved_admin_tag()
        self.view.message(
            'Базы уже созданы. Сохранённые настройки и ключи используются для продолжения запуска.'
        )

    def field_label(self, key):
        label = schema.FIELDS[key].label
        return label

    def field(self, key):
        if key not in schema.FIELDS:
            self.view.message('Неизвестное поле; выберите имя из показанного списка.')
            return
        item = schema.FIELDS[key]
        while True:
            label = self.field_label(key)
            self.view.message(key + ' — ' + label + '. ' + item.required + '; ' + item.impact)
            current = self.model.get(key, item.default)
            displayed = current
            if key == 'MAS_DOMAIN' and self.model.get('MAS_MODE', 'local') == 'local':
                displayed = current or 'mas.' + self.model['DOMAIN']
            choices = {
                'media': (('local', 'Хранить медиа на этом сервере'), ('s3', 'Хранить медиа в S3')),
                'mas': (
                    ('local', 'Установить MAS на этом VPS'),
                    ('external', 'Подключить уже работающий внешний MAS'),
                ),
                'homepage': (
                    ('landing', 'Страница с описанием и кнопками входа'),
                    ('element', 'Сразу открыть клиент чата'),
                    ('external', 'Перейти на внешний сайт'),
                ),
                'bool': (('true', 'Включено'), ('false', 'Выключено')),
            }.get(item.kind)
            if choices and self.view.console:
                self.view.options(
                    'Выберите вариант',
                    [
                        (str(i), label, 'Текущее значение' if value == current else '')
                        for i, (value, label) in enumerate(choices, 1)
                    ],
                )
                default = next(
                    (str(i) for i, (value, label) in enumerate(choices, 1) if value == current), ''
                )
                value = self.ask('Вариант', default)
                value = dict((str(i), value) for i, (value, label) in enumerate(choices, 1)).get(
                    value, value
                )
            else:
                if item.secret or item.kind in (
                    'text',
                    'https',
                    'short_text',
                    'description',
                    'email',
                    'domain',
                    'ip',
                ):
                    self.view.message(
                        'Enter — сохранить значение. /очистить — убрать значение, если поле необязательное.',
                        'dim',
                    )
                value = self.ask(label if self.view.console else key, displayed, item.secret)
                if value == '/очистить':
                    value = ''
                elif key == 'MAS_DOMAIN' and not current and value == displayed:
                    value = ''
            try:
                normalized = schema.field_value(key, value)
                if key == 'MAS_DOMAIN':
                    schema.validate({'DOMAIN': self.model['DOMAIN'], 'MAS_DOMAIN': normalized})
                    if self.model.get(
                        'MAS_MODE'
                    ) == 'external' and normalized in schema.public_hosts(self.model):
                        raise schema.InvalidConfiguration(
                            'форма',
                            key,
                            'mas_host_conflict',
                            'Укажите отдельный домен внешнего MAS, не адрес службы этого VPS.',
                        )
                if (
                    self.model.get('MAS_MODE') == 'external'
                    and key
                    in (
                        'MAS_DOMAIN',
                        'MAS_SHARED_SECRET',
                        'MAS_CLIENT_ID',
                        'MAS_CLIENT_SECRET',
                    )
                    and not normalized
                ):
                    raise schema.InvalidConfiguration(
                        'форма',
                        key,
                        'mas_required',
                        'Это поле нужно для подключения внешнего MAS.',
                    )
            except schema.InvalidConfiguration as error:
                self.view.message(error.field + ': ' + error.explanation)
                continue
            if item.kind == 'domain' and not self.confirm_domain(normalized):
                continue
            self.model[key] = normalized
            if not normalized and key in (
                'GOOGLE_CLIENT_ID',
                'GOOGLE_CLIENT_SECRET',
                'TELEGRAM_CLIENT_ID',
                'TELEGRAM_CLIENT_SECRET',
            ):
                provider = key.split('_', 1)[0]
                self.disable_provider(provider)
            self.save()
            return

    def disable_provider(self, provider):
        self.model[provider + '_CLIENT_ID'] = ''
        self.model[provider + '_CLIENT_SECRET'] = ''
        if provider == 'TELEGRAM':
            self.model['SOCKS5_CONFIG'] = ''
        self.view.message('Вход через ' + provider.title() + ' отключён.')

    def setup_provider(self, provider):
        previous = self.model.copy()
        try:
            enabled = self.answer(
                'Подключить вход через ' + provider.title() + '? да/нет',
                ('да', 'нет'),
                'да' if self.model.get(provider + '_CLIENT_ID') else 'нет',
            )
            if enabled == 'нет':
                self.disable_provider(provider)
            else:
                self.field(provider + '_CLIENT_ID')
                if not self.model.get(provider + '_CLIENT_ID'):
                    return
                self.field(provider + '_CLIENT_SECRET')
                if self.model.get(provider + '_CLIENT_SECRET') and provider == 'TELEGRAM':
                    self.field('SOCKS5_CONFIG')
                    self.check_telegram_login()
            schema.validate(
                {
                    'DOMAIN': self.model['DOMAIN'],
                    provider + '_CLIENT_ID': self.model.get(provider + '_CLIENT_ID', ''),
                    provider + '_CLIENT_SECRET': self.model.get(provider + '_CLIENT_SECRET', ''),
                }
            )
            self.save()
        except (Back, Cancel, SaveExit, KeyboardInterrupt):
            self.model = previous
            self.save()
            raise

    def check_telegram_login(self):
        from telegram_login_check import check, LoginCheckError

        while True:
            try:
                with self.view.busy('Проверяем связь с Telegram, до 36 секунд'):
                    check(self.model)
            except LoginCheckError as error:
                self.view.message(str(error), 'yellow')
                choice = self.answer(
                    '1 — исправить прокси, 2 — повторить, 3 — отключить Telegram; назад',
                    ('1', '2', '3'),
                    '1',
                )
                if choice == '3':
                    self.disable_provider('TELEGRAM')
                    return
                if choice == '1':
                    self.field('SOCKS5_CONFIG')
                continue
            self.view.message(
                'Telegram ответил на три HTTPS-запроса через выбранное подключение. '
                'Обрывов во время проверки не обнаружено.'
            )
            self.view.message(
                'Client ID и Client Secret относятся к OIDC, а не к токену бота. '
                'Их правильность и Redirect URI подтверждаются входом через Telegram '
                'после запуска сервера; эта проверка их не подтверждает.',
                'dim',
            )
            return

    def setup_mas(self):
        previous = self.model.copy()
        try:
            self.field('MAS_MODE')
            if self.model['MAS_MODE'] == 'external':
                self.view.message(
                    'Внешний MAS должен быть заранее настроен для этого сервера Matrix: '
                    + self.model['DOMAIN']
                    + '. Его адрес и IPv4 относятся к другому серверу.'
                )
                self.view.message(
                    'В настройках MAS нужны адрес Matrix https://matrix.'
                    + self.model['DOMAIN']
                    + ', совпадающий общий ключ и управляющее приложение с правами администратора. '
                    'Misbot не устанавливает, не удаляет и не копирует данные внешнего MAS.',
                    'dim',
                )
                self.view.message(
                    'На внешнем MAS регистрация должна требовать приглашение: '
                    'account.password_registration_token_required=true; '
                    'для внешних способов входа — registration_token_required=true.',
                    'dim',
                )
                for key in (
                    'MAS_DOMAIN',
                    'MAS_PUBLIC_IP',
                    'MAS_SHARED_SECRET',
                    'MAS_CLIENT_ID',
                    'MAS_CLIENT_SECRET',
                ):
                    self.field(key)
                for key in (
                    'GOOGLE_CLIENT_ID',
                    'GOOGLE_CLIENT_SECRET',
                    'TELEGRAM_CLIENT_ID',
                    'TELEGRAM_CLIENT_SECRET',
                    'SOCKS5_CONFIG',
                ):
                    self.model[key] = ''
            else:
                if previous.get('MAS_MODE', 'local') == 'external':
                    self.model['MAS_DOMAIN'] = ''
                for key in (
                    'MAS_PUBLIC_IP',
                    'MAS_SHARED_SECRET',
                    'MAS_CLIENT_ID',
                    'MAS_CLIENT_SECRET',
                ):
                    self.model[key] = ''
                self.view.message('Адрес входа: https://mas.' + self.model['DOMAIN'])
            schema.validate(
                {
                    key: self.model.get(key, '')
                    for key in (
                        'DOMAIN',
                        'MAS_MODE',
                        'MAS_DOMAIN',
                        'MAS_PUBLIC_IP',
                        'MAS_SHARED_SECRET',
                        'MAS_CLIENT_ID',
                        'MAS_CLIENT_SECRET',
                    )
                }
            )
            self.save()
        except (Back, Cancel, SaveExit, KeyboardInterrupt):
            self.model = previous
            self.save()
            raise

    def edit_setup_field(self, key):
        if key == 'MAS_MODE':
            return self.setup_mas()
        previous = self.model.copy()
        try:
            self.field(key)
            if key == 'MEDIA_MODE' and self.model[key] == 's3':
                # Keep the valid previous mode until all dependent fields are entered.
                self.model['MEDIA_MODE'] = previous.get('MEDIA_MODE', 'local')
                while True:
                    try:
                        self.s3_form('media')
                        break
                    except (Back, Cancel):
                        self.model = previous.copy()
                        self.save()
                        self.view.heading('Хранилище медиа', 'Ввод S3 отменён. Выберите хранилище.')
                        self.field('MEDIA_MODE')
                        if self.model[key] != 's3':
                            break
                        self.model['MEDIA_MODE'] = previous.get('MEDIA_MODE', 'local')
            elif key == 'POSTGRES_MODE' and self.model[key] == 'external':
                for name in (
                    'POSTGRES_HOST',
                    'POSTGRES_PORT',
                    'POSTGRES_SSL_MODE',
                    'POSTGRES_SSL_ROOT_CERT',
                    'POSTGRES_ADMIN_USER',
                    'POSTGRES_ADMIN_PASSWORD',
                ):
                    self.field(name)
            self.save()
        except (Back, Cancel, SaveExit, KeyboardInterrupt):
            self.model = previous
            self.save()
            raise

    def run(self, path, **values):
        self.result_messages = []
        self.result_notices = []
        self.operation_records = []
        title = next(
            (
                item.title
                for _, actions in ui_actions.MENUS.values()
                for item in actions
                if item.path == path
            ),
            cli_commands.BY_PATH[path].purpose,
        )
        label = 'Выполняется: ' + title
        self.view.message('Установка: ' + str(self.root), 'dim')
        args = [
            sys.executable,
            str(self.source / 'misbot.py'),
            '--directory',
            str(self.root),
            *path.split(),
        ]
        for key, value in values.items():
            args += ['--' + key.replace('_', '-')]
            if value is not True:
                args.append(str(value))
        if self.runner:
            self.operation_records = []
            with self.view.busy(label):
                return self.runner(args)
        from terminal_process import (
            read_output,
            read_interrupted_output,
            present_output,
            process_group_running,
        )

        if path == 'users invitation list':
            # Use the shared read backend directly: one CLI JSON line can exceed
            # the terminal process line limit on a server with many invitations.
            import registration

            with self.view.busy(label):
                result = registration.manage(self.root, 'list')
            self.result_lines = present_output(path, json.dumps(result))
            self.result_total = len(self.result_lines)
            return 0

        interactive = path in (
            'users invitation show',
            'credentials admin show',
            'update latest',
            'remove',
        )
        records = {}
        self.operation_records = []
        self.result_lines = []
        self.result_total = 0
        if path in ('check', 'status'):
            self.check_results = []

        def collect_check(line):
            # Progress can produce hundreds of events while HTTPS starts. Keep
            # the latest result per known check/service so a one-off DNS result
            # or pending-operation status cannot be evicted by that history.
            if len(line) > 8192:
                return
            try:
                record = json.loads(line)
            except ValueError:
                return
            if not isinstance(record, dict) or not isinstance(record.get('check'), str):
                return
            name = record['check']
            if name == 'service':
                service = record.get('name')
                if not isinstance(service, str) or service not in SERVICES:
                    return
                key = 'service:' + service
            elif name in CHECKS:
                if type(record.get('passed')) is not bool:
                    return
                key = name
            elif name in (
                'installation_status',
                'existing_status',
                'server_status',
                'existing_storage_status',
                'storage_status',
                'backup_schedule_status',
                'failed',
            ):
                key = name
            else:
                return
            if key in records or len(records) < 256:
                records[key] = line

        presentation_result = None

        def remember_output(record, messages):
            if not isinstance(record, dict):
                return
            nonlocal presentation_result
            from terminal_results import presentation_outcome

            outcome = presentation_outcome(path, record)
            if outcome is not None:
                presentation_result = outcome
            if (
                (path == 'storage s3 check' and record.get('schema') == 1)
                or (path.startswith('users invitation ') and 'state' in record)
                or path in ('federation requests', 'federation servers', 'admin access list')
            ):
                self.operation_records.append(record)
                del self.operation_records[:-100]
            if path in (
                'backup create',
                'backup move export',
                'backup external create',
                'backup fetch',
            ) and record.get('check') in (
                'consistent_backup',
                'encrypted_external_backup',
                'external_backup_download',
            ):
                self.operation_records.append(
                    {
                        key: record[key]
                        for key in (
                            'check',
                            'passed',
                            'archive',
                            'downloaded_archive',
                            'archive_sha256',
                            'sha256',
                        )
                        if key in record
                    }
                )
                del self.operation_records[:-100]
            if path != 'users invitation list':
                self.result_messages = messages[:100]
            from terminal_results import format_record

            if format_record(path, record) is not None:
                self.result_notices.extend(messages[:100])
            failed = (
                record.get('passed') is False
                or record.get('status') in ('failed', 'error')
                or record.get('check') == 'failed'
            )
            if failed:
                self.result_notices.extend(messages[:100])
            else:
                self.result_notices.extend(
                    message
                    for message in messages
                    if message.startswith(
                        (
                            'Внимание:',
                            'Следующий шаг:',
                            'Архив копии:',
                            'SHA256:',
                            'Операция требует продолжения.',
                            'Сохраните или восстановите',
                        )
                    )
                )
            self.result_notices = list(dict.fromkeys(self.result_notices))[-100:]

        def remember_plain_output(line):
            from terminal_results import format_plain_result

            known = format_plain_result(path, line)
            if known:
                self.result_notices = list(dict.fromkeys([*self.result_notices, *known]))[-100:]
                return
            # These helpers deliberately print human guidance on failure. Other
            # unstructured child output may be a provider dump or traceback.
            if not path.startswith(('admin bot ', 'admin access ')):
                return
            text = line.strip()
            if (
                not text
                or len(text) > 2048
                or text.startswith(('{', '[', 'Traceback', 'File ', 'botocore.', 'urllib.'))
                or any(ord(char) < 32 or ord(char) == 127 for char in text)
                or re.search(
                    r'(?i)(authorization|access[_ -]?key|secret[_ -]?key|password|bearer|token)\s*[:=]',
                    text,
                )
            ):
                return
            self.result_messages = [*self.result_messages, text][-20:]

        # Only the UI writes the terminal while animation is active. Secret display
        # and interactive backend confirmations keep their foreground terminal.
        input_mode = self.view.external_terminal() if interactive else nullcontext()
        input_mode.__enter__()
        try:
            child = subprocess.Popen(
                args,
                stdin=None if interactive else subprocess.DEVNULL,
                stdout=None if interactive else subprocess.PIPE,
                stderr=None if interactive else subprocess.STDOUT,
                env={**os.environ, 'PYTHONUNBUFFERED': '1', 'MISBOT_TERMINAL_UI': '1'},
                bufsize=0,
                start_new_session=not interactive,
            )
        except BaseException:
            input_mode.__exit__(*sys.exc_info())
            raise
        try:
            if interactive:
                self.view.message(label)
                code = child.wait()
                if (
                    path in ('credentials admin show', 'users invitation show')
                    and self.view.fullscreen
                ):
                    # A UI line editor redraw would erase the foreground secret.
                    # Read acknowledgement directly without capturing its output.
                    if self.reader:
                        self.reader('Enter — вернуться в Misbot: ', True)
                    else:
                        getpass.getpass('Enter — вернуться в Misbot: ', stream=sys.stdout)
                    self._result_waited = True
                return code
            with self.view.busy(label) as progress:

                def emit(line):
                    if path in ('check', 'status'):
                        # Verification produces short structured records, not secrets.
                        collect_check(line)
                    if path == 'status':
                        return
                    if path != 'check':
                        messages = present_output(path, line)
                        try:
                            record = json.loads(line)
                            remember_output(record, messages)
                        except ValueError:
                            remember_plain_output(line)
                        try:
                            invitation_record = path == 'users invitation list' and isinstance(
                                json.loads(line).get('invitations'), list
                            )
                        except (ValueError, AttributeError):
                            invitation_record = False
                        if invitation_record:
                            self.result_total += len(messages)
                            self.result_lines.extend(messages)
                            del self.result_lines[:-1000]
                            progress.log(
                                'Получен список приглашений: ' + str(len(messages)) + ' строк.'
                            )
                        else:
                            for message in messages:
                                progress.log(message)

                code = read_output(child, emit)
            if path in ('check', 'status'):
                self.check_results = parse_checks('\n'.join(records.values()))
            if path == 'status':
                self.result_lines = [
                    message
                    for line in records.values()
                    for message in present_output(path, line)
                    if not any(
                        row.label + ': ' == message[: len(row.label) + 2]
                        for row in self.check_results
                    )
                ]
            return 1 if code == 0 and presentation_result is False else code
        except (KeyboardInterrupt, state_store.OperationInterrupted):
            if child.poll() is None or (not interactive and process_group_running(child)):
                if not interactive and isinstance(getattr(child, 'pid', None), int):
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    child.send_signal(signal.SIGTERM)
            self.view.message(
                'Операция прервана. Ожидается завершение дочернего процесса; checkpoints сохраняются.'
            )

            def collect_recovery(line):
                if path in ('check', 'status'):
                    collect_check(line)
                try:
                    record = json.loads(line)
                    remember_output(record, present_output(path, line))
                except ValueError:
                    remember_plain_output(line)

            with nullcontext() if interactive else self.view.busy('Завершаем прерванную операцию'):
                while child.poll() is None or (not interactive and process_group_running(child)):
                    try:
                        if interactive or child.stdout is None:
                            child.wait(timeout=1)
                        else:
                            if path == 'start':
                                read_interrupted_output(child, collect_recovery, timeout=10)
                            else:
                                read_interrupted_output(child, collect_recovery)
                            if process_group_running(child):
                                time.sleep(0.2)
                    except subprocess.TimeoutExpired:
                        pass
                    except (KeyboardInterrupt, state_store.OperationInterrupted):
                        pass
            if path == 'check':
                self.check_results = parse_checks('\n'.join(records.values()))
            self.view.message(self.continuation())
            return 130
        finally:
            try:
                if child.stdout is not None:
                    child.stdout.close()
            finally:
                input_mode.__exit__(None, None, None)

    def s3_profile(self, values, *, reuse=True):
        """Collect a complete profile without changing a wizard or active settings."""
        import s3_config

        scratch = {key: values.get(key, '') for key in s3_config.FIELDS}
        try:
            saved = s3_config.validate(scratch)
        except s3_config.ConfigError:
            saved = None
        if saved and reuse:
            choice = self.choose(
                'Использовать сохранённое подключение S3?',
                [
                    ('1', 'Использовать сохранённые настройки', saved['S3_BUCKET']),
                    ('2', 'Изменить настройки', 'Ключи доступа вводятся скрыто'),
                ],
            )
            if choice == '0':
                raise Back()
            if choice == '1':
                return saved
        for key in (
            'S3_BUCKET',
            'S3_ENDPOINT_URL',
            'S3_REGION',
            'S3_PREFIX',
            'S3_ACCESS_KEY_ID',
            'S3_SECRET_ACCESS_KEY',
        ):
            self.view.heading('Параметры S3')
            self.view.message('0 / назад / отмена — вернуться. Ключи вводятся скрыто.', 'dim')
            self.s3_field(key, scratch, save=False)
        return s3_config.validate(scratch)

    def s3_form(self, purpose):
        import s3_diagnostics as probe
        import s3_config
        from configuration import directory, serialize

        previous = self.model.copy() if purpose == 'media' else self.backup.copy()
        values = self.s3_profile(previous)
        path = self.draft.parent / (
            self.draft.stem + '-s3-probe-recovery-' + secrets.token_hex(8) + '.env'
        )
        keep_profile = False
        try:
            while True:
                if keep_profile:
                    # Preserve credentials tied to an uncleared object before trying a new profile.
                    path = self.draft.parent / (
                        self.draft.stem + '-s3-probe-recovery-' + secrets.token_hex(8) + '.env'
                    )
                    keep_profile = False
                directory(path.parent)
                state_store.write_text(path, serialize(values))
                with self.view.busy('Проверяем подключение к S3') as progress:
                    result = probe.diagnose(
                        path,
                        purpose,
                        directory=self.root if (self.root / 'installation.json').exists() else None,
                        progress=lambda record: progress.log(record['explanation']),
                    )
                keep_profile = bool(result.get('cleanup_pending'))
                self.view.message('Итог: ' + result['conclusion'])
                if result['passed']:
                    if purpose == 'media':
                        self.model.update(values, MEDIA_MODE='s3')
                    else:
                        self.backup.update(values)
                    self.save()
                    return
                if keep_profile:
                    for action in result['next_actions']:
                        self.view.message(action)
                choice = self.answer(
                    '1 — исправить реквизиты, 2 — повторить проверку, 3 — отложить S3; '
                    'назад — отменить выбор; сохранить и выйти',
                    ('1', '2', '3'),
                    '1',
                )
                if choice == '3':
                    if purpose == 'media':
                        self.model['MEDIA_MODE'] = 'local'
                        for key in s3_config.FIELDS:
                            self.model.pop(key, None)
                        self.view.message(
                            'Медиа останутся на этом VPS. S3 можно подключить в меню «Медиа и базы данных».'
                        )
                    else:
                        self.backup = {}
                        self.view.message(
                            'Внешние копии можно настроить позднее в меню «Резервные копии».'
                        )
                    self.save()
                    return
                if choice == '1':
                    values = self.s3_profile(values, reuse=False)
        except (Back, Cancel, SaveExit, KeyboardInterrupt):
            if purpose == 'media':
                self.model = previous
            else:
                self.backup = previous
            self.save()
            raise
        finally:
            # A failed owned-object cleanup needs the exact private profile for recovery.
            if not keep_profile:
                path.unlink(missing_ok=True)

    def s3_field(self, key, values, save=True):
        import s3_config

        item = schema.FIELDS[key]

        def validate(value):
            if value.strip() == '0':
                raise Back()
            if key == 'S3_ENDPOINT_URL' and value == '/очистить':
                value = ''
            return s3_config.validate_field(key, schema.field_value(key, value))

        if key == 'S3_ENDPOINT_URL':
            self.view.message('Для AWS нажмите Enter. /очистить — убрать сохранённый адрес.', 'dim')
        values[key] = self.input_value(
            'Введите ' + item.label + ' (' + key + ')',
            values.get(key, item.default),
            validate,
            item.secret,
        )
        if save:
            self.save()

    def backup_key(self, value):
        import stat

        value = schema.field_value('BACKUP_KEY_FILE', value, schema=schema.BACKUP_FIELDS)
        if not value or Path(value).is_relative_to(self.root):
            raise ValueError('Укажите путь ключа вне каталога установки.')
        path = Path(value)
        if path.exists():
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size != 32
            ):
                raise ValueError('Нужен существующий ключ Misbot: приватный файл 0600, 32 байта.')
        elif not path.parent.is_dir():
            raise ValueError('Каталог для нового ключа должен существовать.')
        return value

    def calendar_value(self, value):
        if value == '/очистить':
            return ''
        if not value:
            return value
        value = schema.backup_calendar(value)
        try:
            result = subprocess.run(
                ['systemd-analyze', 'calendar', value],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError(
                'Не удалось проверить расписание через systemd-analyze. Повторите ввод.'
            ) from None
        if result.returncode:
            raise ValueError('Неверное расписание systemd. Пример: *-*-* 03:00:00 UTC')
        return value

    def previous_step(self):
        # The system check has no editable inputs; DNS failures return to the domain.
        self.step = 0 if self.step == 2 else max(0, self.step - 1)
        self.save()

    def operation_argument(self, key, value, backend):
        value = value.strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Это поле обязательно. Введите одну строку без управляющих символов.')
        if key == 'public-ip':
            return schema.field_value('PUBLIC_IP', value)
        if key == 'server':
            value = schema.field_value('DOMAIN', value)
            if not self.confirm_domain(value):
                raise ValueError('Введите домен заново.')
        if key == 'sha256':
            if not re.fullmatch(r'[0-9a-fA-F]{64}', value):
                raise ValueError('SHA256 должен содержать ровно 64 шестнадцатеричных символа.')
            return value.lower()
        if key == 'purpose' and value not in ('media', 'backup'):
            raise ValueError('Назначение S3: media или backup.')
        if key == 'confirm':
            expected = 'source-stopped' if backend == 'restore-activate' else 'target-stopped'
            if value != expected:
                raise ValueError('Подтверждение остановки другого сервера: ' + expected)
        if key == 'external_mas_confirm' and value != 'preserved-state':
            raise ValueError('Нужно подтвердить сохранность внешнего MAS: preserved-state.')
        if key == 'request' and not re.fullmatch(r'MB-[0-9a-f]{32}', value):
            raise ValueError('Выберите идентификатор заявки MB-… из показанного списка.')
        if key == 'tag':
            mxid = (
                value
                if ':' in value
                else '@' + value.removeprefix('@') + ':' + self.installation()['domain']
            )
            if not re.fullmatch(
                r'@[a-z0-9][a-z0-9._=-]{0,63}:' + re.escape(self.installation()['domain']), mxid
            ):
                raise ValueError('Укажите локальное имя бота: строчные латинские буквы и цифры.')
        if key == 'mxid':
            domain = self.installation()['domain']
            if ':' not in value:
                value = '@' + value.removeprefix('@') + ':' + domain
            if not re.fullmatch(r'@[a-z0-9][a-z0-9._=-]{0,63}:' + re.escape(domain), value):
                raise ValueError(
                    'Укажите зарегистрированный аккаунт на этом сервере: @имя:' + domain
                )
        if key == 'calendar':
            return self.calendar_value(value)
        if key == 'key':
            return self.backup_key(value)
        if key in ('config', 'input', 'archive', 'output', 'catalog', 'source'):
            value = schema.field_value('BACKUP_KEY_FILE', value, schema=schema.BACKUP_FIELDS)
            if Path(value) == Path('/'):
                raise ValueError('Укажите отдельный абсолютный путь без symlink и ..')
        if key == 'object' and (value.startswith('/') or '..' in value.split('/')):
            raise ValueError('Укажите ключ объекта в выделенном префиксе копий, без ..')
        return value

    def setup_admin_tag(self):
        import service_identity

        def validate(value):
            localpart = value.removeprefix('@')
            if not re.fullmatch(r'[a-z0-9][a-z0-9._=-]{0,63}', localpart):
                raise ValueError('Имя бота: строчные латинские буквы, цифры, точка, _ , = или -.')
            if localpart in (
                'referral-bot',
                'server-guide',
                service_identity.administrator(self.root).username,
            ):
                raise ValueError('Это имя уже используется другим ботом или администратором.')
            return localpart

        self.admin_tag = self.input_value(
            'Имя административного бота до его создания', self.admin_tag, validate
        )
        self.save()

    def dns_wait_command(self, timeout):
        if self.reader:
            command = self.ask('Команда ожидания DNS').strip().casefold()
            if not command:
                time.sleep(timeout)
            return command
        import select

        if not select.select([sys.stdin], [], [], timeout)[0]:
            return ''
        if hasattr(self, '_dns_decoder'):
            chunk = os.read(sys.stdin.fileno(), 1)
            if not chunk:
                raise SaveExit()
            value = self._dns_decoder.decode(chunk)
            if value in ('\n', '\r'):
                command = self.dns_command_buffer
                self.dns_command_buffer = ''
                return command.strip().casefold()
            if value in ('\x7f', '\b'):
                self.dns_command_buffer = self.dns_command_buffer[:-1]
            elif value in ('b', 'q') and not self.dns_command_buffer:
                return 'назад' if value == 'b' else 'сохранить и выйти'
            elif value and value.isprintable() and len(self.dns_command_buffer) < 128:
                self.dns_command_buffer += value
            return ''
        command = sys.stdin.readline(129)
        if not command:
            raise SaveExit()
        return command.strip().casefold()

    @contextmanager
    def dns_input_mode(self):
        if self.reader or not sys.stdin.isatty():
            yield
            return
        import termios
        import codecs

        descriptor = sys.stdin.fileno()
        original = termios.tcgetattr(descriptor)
        quiet = list(original)
        quiet[3] &= ~(termios.ECHO | termios.ICANON)
        quiet[6] = list(original[6])
        quiet[6][termios.VMIN] = 1
        quiet[6][termios.VTIME] = 0
        self._dns_decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.dns_command_buffer = ''
        termios.tcsetattr(descriptor, termios.TCSANOW, quiet)
        try:
            yield
        finally:
            # Immediate b/q navigation must not leave a trailing Enter for the next form.
            termios.tcflush(descriptor, termios.TCIFLUSH)
            termios.tcsetattr(descriptor, termios.TCSANOW, original)
            del self._dns_decoder
            self.dns_command_buffer = ''

    def wait_dns(self, expected):
        import queue
        import threading
        import wizard_dns

        hosts = schema.public_hosts(self.model)
        if self.model.get('MAS_MODE') == 'external':
            hosts.append(schema.mas_host(self.model))
        statuses = {}
        events = queue.Queue()
        worker = None
        next_check = time.monotonic()
        complete = False
        dirty = True
        hint = 'Назад: b или «назад». Выход: q или «сохранить и выйти»; отмена.'
        with (
            self.dns_input_mode(),
            self.view.busy('Проверяем DNS; повторная проверка каждые 10 секунд') as progress,
        ):
            while True:
                if worker is None and time.monotonic() >= next_check:
                    complete = False
                    statuses = {}
                    dirty = True

                    def scan():
                        try:
                            wizard_dns.scan(self.model.copy(), expected, events.put)
                        finally:
                            events.put(None)

                    worker = threading.Thread(target=scan, daemon=True)
                    worker.start()
                while not events.empty():
                    status = events.get_nowait()
                    if status is None:
                        complete = True
                        worker = None
                        next_check = time.monotonic() + 10
                    else:
                        statuses[status.host] = status
                    dirty = True
                if dirty:
                    if progress.animated:
                        progress.logs.clear()
                    progress.log('В панели DNS вашего домена создайте или исправьте записи типа A:')
                    progress.log('Для доменов этого VPS значение A — публичный IPv4: ' + expected)
                    if self.model.get('MAS_MODE') == 'external':
                        progress.log(
                            'Внешний MAS использует адрес своего сервера; его DNS менять на адрес этого VPS не нужно.'
                        )
                    for host in hosts:
                        status = statuses.get(host)
                        progress.log(host + ': ' + (status.message if status else 'проверяется'))
                    progress.log('Для доменов этого VPS отключите CDN proxy: режим DNS only.')
                    progress.log(hint)
                    progress.log('Команда: ' + getattr(self, 'dns_command_buffer', ''))
                    dirty = False
                if (
                    complete
                    and len(statuses) == len(hosts)
                    and all(row.passed for row in statuses.values())
                ):
                    import select

                    pending = bool(
                        not self.reader
                        and sys.stdin.isatty()
                        and select.select([sys.stdin], [], [], 0)[0]
                    )
                    if not getattr(self, 'dns_command_buffer', '') and not pending:
                        return
                timeout = (
                    0.1 if worker is not None else min(0.1, max(0, next_check - time.monotonic()))
                )
                previous = getattr(self, 'dns_command_buffer', '')
                command = self.dns_wait_command(timeout)
                if previous != getattr(self, 'dns_command_buffer', ''):
                    dirty = True
                if command == 'назад':
                    raise Back()
                if command == 'сохранить и выйти':
                    raise SaveExit()
                if command == 'отмена':
                    raise Cancel()
                if command:
                    hint = 'Допустимые команды: назад, отмена, сохранить и выйти.'
                    dirty = True

    def dns_values(self):
        return {
            key: self.model.get(key, 'local' if key == 'MAS_MODE' else '')
            for key in (
                'DOMAIN',
                'MAS_MODE',
                'MAS_DOMAIN',
                'PUBLIC_IP',
                'MAS_PUBLIC_IP',
            )
        }

    def check_dns_settings(self):
        import preflight

        domain = self.model['DOMAIN']
        ip = self.model.get('PUBLIC_IP')
        if not ip:
            with self.view.busy('Определяем IPv4 по основному домену'):
                addresses = preflight.resolve(domain, __import__('socket').AF_INET)
            if len(addresses) == 1:
                ip = next(iter(addresses))
            while not ip:
                self.view.message(
                    'По записи A пока нельзя определить один IPv4 VPS. '
                    'Укажите публичный IPv4 из панели VPS, чтобы проверить все имена '
                    'и дождаться их DNS-записей. Этот адрес нужен только пока его нельзя определить по DNS.',
                    'yellow',
                )
                self.field('PUBLIC_IP')
                ip = self.model.get('PUBLIC_IP')
        self.wait_dns(ip)
        with self.view.busy('Проверяем DNS, сертификаты и сеть'):
            preflight.dns_check(
                domain,
                ip,
                self.model.get('MAS_DOMAIN', ''),
                self.model.get('MAS_MODE', 'local'),
                self.model.get('MAS_PUBLIC_IP', ''),
            )
            preflight.caa_check(
                domain,
                self.model.get('MAS_DOMAIN', ''),
                self.model.get('MAS_MODE', 'local'),
            )
            preflight.network_check(self.root, self.model.get('DOCKER_SUBNET', '172.30.50.0/24'))
        self.dns_verified = self.dns_values()
        self.save()

    def setup_summary(self):
        page = 0
        warning = ''
        while True:
            self.model = {
                **{key: item.default for key, item in schema.FIELDS.items()},
                **schema.validate(self.model, complete=False),
            }
            self.view.heading('Итог установки')
            self.view.message(
                'Итог. Внутренние пароли и ключи создаются автоматически '
                'и сохраняются при повторном запуске.'
            )
            fields = [
                key
                for key, value in self.model.items()
                if key
                in (
                    'DOMAIN',
                    'MAS_MODE',
                    'MAS_DOMAIN',
                    'PUBLIC_IP',
                    'ACME_EMAIL',
                    'MEDIA_MODE',
                    'POSTGRES_MODE',
                )
                or (value and value != schema.FIELDS[key].default)
            ]
            fields = [
                key
                for key in fields
                if key
                not in (
                    'GOOGLE_CLIENT_ID',
                    'GOOGLE_CLIENT_SECRET',
                    'TELEGRAM_CLIENT_ID',
                    'TELEGRAM_CLIENT_SECRET',
                    'SOCKS5_CONFIG',
                )
                and not (key.startswith('S3_') and self.model['MEDIA_MODE'] != 's3')
                and not (
                    key
                    in ('MAS_PUBLIC_IP', 'MAS_SHARED_SECRET', 'MAS_CLIENT_ID', 'MAS_CLIENT_SECRET')
                    and self.model['MAS_MODE'] != 'external'
                )
            ]
            rows = []
            labels = {
                'DOMAIN': 'Основной домен',
                'MAS_MODE': 'Сервер авторизации',
                'MAS_DOMAIN': 'Домен авторизации MAS',
                'PUBLIC_IP': 'IPv4 VPS',
                'ACME_EMAIL': 'Email для сертификатов',
                'MEDIA_MODE': 'Хранилище медиа',
                'POSTGRES_MODE': 'База данных',
            }
            for index, key in enumerate(fields, 1):
                value = self.model[key]
                if key == 'MAS_DOMAIN':
                    value = schema.mas_host(self.model)
                elif key == 'MAS_MODE':
                    value = 'На этом VPS' if value == 'local' else 'Внешний MAS'
                shown = 'настроен' if schema.FIELDS[key].secret and value else value or 'не задано'
                shown = shown if len(shown) <= 100 else shown[:97] + '…'
                rows.append((str(index) + '. ' + labels.get(key, self.field_label(key)), shown))
            admin_number = str(len(fields) + 1)
            rows.append((admin_number + '. Имя административного бота', self.admin_tag))
            backup_number = str(len(fields) + 2)
            rows.append(
                (backup_number + '. Внешние копии', 'настроены' if self.backup else 'отложены')
            )
            provider_numbers = {}
            for provider in ('GOOGLE', 'TELEGRAM') if self.model['MAS_MODE'] == 'local' else ():
                number = str(len(rows) + 1)
                provider_numbers[number] = provider
                enabled = bool(self.model.get(provider + '_CLIENT_ID'))
                rows.append(
                    (
                        number + '. Вход через ' + provider.title(),
                        'включён' if enabled else 'отключён',
                    )
                )
            pages = [rows]
            if self.view.fullscreen and self.view.console:
                budget = max(1, self.view.console.height - self.view.used_lines - 13)
                pages, part = [], []
                for row in rows:
                    if part and self.view.details([*part, row], measure=True) > budget:
                        pages.append(part)
                        part = []
                    part.append(row)
                pages.append(part)
            page = min(page, len(pages) - 1)
            self.view.details(pages[page])
            if len(pages) > 1:
                self.view.message('Страница ' + str(page + 1) + '/' + str(len(pages)))
            self.view.message(
                'Команды:\n'
                '  номер поля — изменить значение\n'
                '  применить — начать установку\n'
                '  назад — предыдущий шаг\n'
                '  отмена — отменить ввод и выйти\n'
                '  сохранить и выйти — сохранить черновик'
                + ('\n  > / < — страницы полей' if len(pages) > 1 else '')
            )
            if warning:
                self.view.message(warning, 'yellow')
            change = self.ask('Команда или номер поля', 'сохранить и выйти').strip()
            warning = ''
            if change.casefold() == 'сохранить и выйти':
                raise SaveExit()
            if change.casefold() == 'применить':
                try:
                    self.model = schema.validate(self.model)
                except schema.InvalidConfiguration as error:
                    warning = error.field + ': ' + error.explanation
                    continue
                try:
                    if self.dns_verified != self.dns_values():
                        self.check_dns_settings()
                except Back:
                    continue
                return
            if change in ('>', '<'):
                page = max(0, min(page + (1 if change == '>' else -1), len(pages) - 1))
                continue
            previous_backup = self.backup.copy()
            try:
                if change in provider_numbers:
                    self.setup_provider(provider_numbers[change])
                elif change == admin_number:
                    self.setup_admin_tag()
                elif change == backup_number:
                    self.setup_step(5)
                elif change.isdecimal() and 1 <= int(change) <= len(fields):
                    self.edit_setup_field(fields[int(change) - 1])
                elif change in fields:
                    self.edit_setup_field(change)
                else:
                    warning = 'Введите номер поля или команду из списка.'
            except Back:
                if change == backup_number:
                    self.backup = previous_backup
                    self.save()

    def reusable_removed_directory(self, path):
        if not (path / '.misbot-empty.json').is_file():
            return False
        import command_wrapper
        import update

        try:
            command_wrapper.trusted_runtime(path)
            entries, _ = update.validate_manifest(path)
            allowed = {Path(name).parts[0] for name in entries} | {
                'SHA256SUMS',
                '.misbot-empty.json',
                '__pycache__',
            }
            return all(item.name in allowed for item in path.iterdir())
        except (OSError, ValueError, RuntimeError):
            return False

    def setup_step(self, index):
        import preflight

        if index == 0:
            self.field('DOMAIN')
            while True:
                path = self.input_value(
                    'Пустой каталог установки, абсолютный путь; допускаются пробелы',
                    str(self.root),
                    schema.installation_directory,
                )
                if (
                    path.exists()
                    and any(path.iterdir())
                    and not (path / 'installation.json').exists()
                    and not self.reusable_removed_directory(path)
                ):
                    self.view.message('Каталог чужой и непустой. Выберите другой.')
                    continue
                if (
                    not self.explicit_draft
                    and self.draft_path(path) != self.draft
                    and self.draft_path(path).exists()
                ):
                    self.view.message(
                        'Для этого каталога уже есть черновик. Чтобы продолжить его, '
                        'откройте ui --directory ' + shlex.quote(str(path)) + '.',
                        'yellow',
                    )
                    continue
                self.root = path
                self.save()
                break
        elif index == 1:
            import terminal_dependencies

            terminal_dependencies.ensure(self)
            self.view.message(
                'Проверка Linux amd64, systemd, памяти, диска и портов. Ошибку исправьте и повторите; шаг не пропускается.'
            )
            with self.view.busy('Проверяем систему и свободные ресурсы'):
                preflight.system_check(self.root)
        elif index == 2:
            self.check_domain_inputs()
            self.setup_mas()
            self.view.message(
                'IPv4 будет определён по A-записи основного домена. '
                'Если запись ещё не создана, мастер попросит адрес из панели VPS.',
                'dim',
            )
            self.field('ACME_EMAIL')
            self.check_dns_settings()
        elif index == 3:
            self.edit_setup_field('MEDIA_MODE')
        elif index == 4:
            if self.model.get('MAS_MODE') == 'external':
                self.view.message(
                    'Способы входа Google, Telegram и пароль настраиваются на внешнем MAS. '
                    'Misbot использует его существующие настройки.'
                )
                return
            self.view.message(
                'Парольный вход доступен без Google/Telegram. OAuth можно пропустить и настроить до запуска позднее.'
            )
            for provider in ('GOOGLE', 'TELEGRAM'):
                self.setup_provider(provider)
            self.view.message(
                'Срок приглашений можно изменить после установки в настройках сервера.', 'dim'
            )
        elif index == 5:
            self.view.message(
                'Внешние копии требуют отдельного private bucket/prefix и recovery key вне установки. Можно явно отложить; локальная копия не защищает от потери VPS.'
            )
            if (
                self.answer(
                    'Настроить внешние копии сейчас? да/отложить',
                    ('да', 'отложить'),
                    'да' if self.backup else 'отложить',
                )
                == 'да'
            ):
                self.s3_form('backup')
                if self.backup:
                    self.backup['BACKUP_KEY_FILE'] = self.input_value(
                        'Путь recovery key вне установки; отсутствующий ключ будет создан',
                        self.backup.get('BACKUP_KEY_FILE', '/root/misbot-backup.key'),
                        self.backup_key,
                    )
                    self.backup['BACKUP_CALENDAR'] = self.input_value(
                        'Systemd calendar (пусто — ручные копии; /очистить — убрать расписание)',
                        self.backup.get('BACKUP_CALENDAR', ''),
                        self.calendar_value,
                    )
                    schema.validate_backup(self.backup)
            else:
                self.backup = {}
            self.save()
        elif index == 6:
            self.check_domain_inputs()
            self.setup_summary()
        elif index == 7:
            from configuration import write_model

            if not (self.root / 'installation.json').exists():
                code = self.run('config init', domain=self.model['DOMAIN'])
                if code:
                    raise RuntimeError(
                        'Init не завершён. Настройки draft сохранены; повторите мастер, не удаляйте secrets.'
                    )
            metadata = json.loads((self.root / 'installation.json').read_text())
            if metadata.get('databases_initialized'):
                self.use_active_configuration()
            else:
                write_model(self.root, self.model, self.backup)
                if self.admin_tag != self.saved_admin_tag() and not metadata.get(
                    'federation_provisioned'
                ):
                    if self.run('admin bot set', tag=self.admin_tag):
                        raise RuntimeError(
                            'Имя административного бота не сохранено. Проверьте выбранное имя; '
                            'настройки и ключи сохранены.'
                        )
            code = self.run('start')
            if code:
                raise RuntimeError(
                    'Запуск не завершён. «Повторить» продолжит запуск с сохранёнными данными '
                    'и ключами. «Назад» откроет меню управления сервером.'
                )
        elif index == 8:
            if self.run('check'):
                raise RuntimeError(
                    'Готовность сервера не подтверждена. Выполните sudo misbot status; если запуск не завершён, sudo misbot start, затем sudo misbot check. Повторный init не нужен.'
                )
            if self.backup:
                key = self.backup['BACKUP_KEY_FILE']
                if not Path(key).exists() and self.run('backup key create', key=key):
                    raise RuntimeError(
                        'Не удалось создать recovery key; сохраните настройки и повторите backup key create.'
                    )
                from configuration import backup_profile

                profile = self.root / 'settings/backup.env'
                command = (
                    'backup schedule external'
                    if self.backup.get('BACKUP_CALENDAR')
                    else 'backup external create'
                )
                args = {'config': profile, 'key': key}
                if self.backup.get('BACKUP_CALENDAR'):
                    args['calendar'] = self.backup['BACKUP_CALENDAR']
                if self.run(command, **args):
                    raise RuntimeError(
                        'Сервер работает; внешняя копия не подтверждена. Настройки сохранены; повторите backup external create.'
                    )
        elif index == 9:
            self.finish()

    def finish(self):
        from registration import manage
        import service_identity

        domain = self.installation()['domain']
        self.view.heading(
            'Настройка завершена', 'Откройте клиент и войдите в аккаунт администратора.'
        )
        self.view.details(
            [
                ('Клиент', 'https://web.' + domain),
                ('Администратор', service_identity.administrator(self.root, domain).matrix_id),
            ]
        )
        self.view.message('Пароль можно открыть кнопкой «Показать пароль администратора».')
        try:
            result = manage(self.root, 'ensure')
            self.view.details(
                [
                    (
                        'Первое приглашение',
                        {
                            'valid': 'готово к выдаче',
                            'used': 'использовано',
                            'expired': 'срок истёк',
                            'revoked': 'отозвано',
                            'pending': 'ожидает подтверждения',
                        }.get(result['state'], 'не подтверждено'),
                    )
                ]
            )
            self.view.message('Код: «Пользователи и приглашения» → «Пригласить пользователя».')
            self.view.message(
                'Пользователь вводит код в форме регистрации клиента. Не добавляйте код в URL.'
            )
        except Exception:
            self.view.message(
                'Сервер запущен. Код пока не подготовлен; выберите «Пользователи и приглашения» → «Пригласить пользователя», чтобы продолжить.'
            )

        if self.reader or sys.stdin.isatty():
            while True:
                choice = self.choose(
                    'Что сделать дальше?',
                    [
                        ('1', 'Показать пароль администратора', 'Пароль появится в этом терминале'),
                        ('2', 'Пользователи и приглашения', 'Получить код для регистрации'),
                        ('3', 'Администраторы и доступ', 'Выдать права личному аккаунту'),
                    ],
                    exit_label='Завершить настройку',
                )
                if choice == '0':
                    break
                if choice == '1':
                    self.execute(
                        ui_actions.Action(
                            'credentials admin show', 'Показать пароль администратора'
                        )
                    )
                else:
                    self.task_menu('users' if choice == '2' else 'admin')

    def wizard(self):
        names = [
            'Домен и каталог',
            'Система',
            'DNS и порты',
            'Медиа',
            'Авторизация',
            'Внешние копии',
            'Итог',
            'Применение и запуск',
            'Проверка',
            'Завершение',
        ]
        if 8 <= self.step <= 9 and self.initialized():
            self.use_active_configuration()
        while self.step < len(names):
            if self.step < 7 and self.initialized():
                self.use_active_configuration()
                self.step = 7
                self.save()
            self.view.wizard_step(self.step, names)
            self.view.message('Установка: ' + str(self.root), 'dim')
            self.view.message(
                'Назад: «назад» · Выход: «сохранить и выйти» или «отмена». '
                'Enter сохраняет текущее значение.',
                'dim',
            )
            try:
                self.setup_step(self.step)
            except Back:
                if self.initialized():
                    self.view.message(
                        'Первоначальное применение уже выполнено. Возвращаемся в меню управления существующей установкой.'
                    )
                    return self.menu()
                self.previous_step()
                continue
            except (Cancel, KeyboardInterrupt, EOFError, state_store.OperationInterrupted):
                self.view.message(
                    'Ввод отменён; действующая конфигурация не изменена. Draft сохранён.'
                )
                self.save()
                return 130
            except SaveExit:
                self.save()
                self.view.message(self.draft_hints())
                return 0
            except Exception as error:
                self.view.message(
                    'Этап не завершён. Исправьте ввод или повторите проверку.'
                    if not self.installation()
                    else 'Этап не завершён. Данные, настройки и ключи сохранены.'
                )
                self.failure_reason(error)
                try:
                    self.answer(
                        'Повторить шаг / назад / сохранить и выйти', ('повторить',), 'повторить'
                    )
                except Back:
                    if self.initialized():
                        return self.menu()
                    self.previous_step()
                except (EOFError, state_store.OperationInterrupted):
                    self.save()
                    return 130
                except (SaveExit, Cancel, KeyboardInterrupt):
                    self.save()
                    self.view.message(self.draft_hints())
                    return 0
                continue
            self.step += 1
            self.save()
        self.view.message('Мастер завершён. Управление: sudo misbot ui')
        try:
            self.pause('Enter — закрыть мастер')
        except (EOFError, state_store.OperationInterrupted):
            self.save()
            return 130
        except (Back, Cancel, SaveExit):
            pass
        return 0

    def failure_reason(self, error):
        # Exception messages can contain credentials or remote response bodies.
        # Preserve useful local validation, and identify unexpected failures by
        # code location only, without serializing values or traceback locals.
        if type(error) is RuntimeError or isinstance(error, schema.InvalidConfiguration):
            self.view.message(str(error))
            return
        location = error.__traceback__
        while location and location.tb_next:
            location = location.tb_next
        code = type(error).__name__
        if location:
            code += ' · ' + Path(location.tb_frame.f_code.co_filename).name
            code += ':' + str(location.tb_lineno)
        self.view.message('Шаг не выполнен. Код для диагностики: ' + code, 'yellow')
        self.view.message(
            'Сохранённые настройки оставлены. Можно повторить шаг или вернуться назад.'
        )

    def continuation(self):
        if (self.root / 'homepage-transition.json').exists():
            return 'Меню: «Продолжить прерванную операцию» → «Сайт после прерывания».'
        return 'Меню: «Состояние и работа сервера» → «Показать состояние».'

    def report(self, title, rows, messages=(), result_code=None, continuation=''):
        """Show every result, with simple page navigation for short terminals."""
        page = 0
        while True:
            self.view.heading(title)
            if result_code is not None:
                self.view.result(title, result_code, continuation)
            for message in messages:
                self.view.message(message)
            pages = [rows]
            if self.view.fullscreen and self.view.console:
                budget = max(1, self.view.console.height - self.view.used_lines - 5)
                pages, part = [], []
                for row in rows:
                    if part and self.view.details([*part, row], measure=True) > budget:
                        pages.append(part)
                        part = []
                    part.append(row)
                pages.append(part)
            page = min(page, len(pages) - 1)
            self.view.details(pages[page])
            if not self.view.fullscreen:
                return
            if len(pages) > 1:
                self.view.message('Страница ' + str(page + 1) + '/' + str(len(pages)))
            answer = self.ask(
                '> далее · < ранее · Enter — вернуться к меню'
                if len(pages) > 1
                else 'Enter — вернуться к меню'
            ).strip()
            self._result_waited = True
            if answer not in ('>', '<'):
                return
            page = max(0, min(page + (1 if answer == '>' else -1), len(pages) - 1))

    def check_result(self, code):
        if code == 0:
            outcome = 'Доступность подтверждена.'
        elif code == 130:
            outcome = 'Проверка прервана; некоторые результаты могут отсутствовать.'
        else:
            outcome = 'Доступность не подтверждена.'
        messages = [outcome, check_summary(self.check_results, code)]
        if code:
            if (
                not self.check_results
                or not any(not row.passed for row in self.check_results)
                or any('не уточнена' in row.message for row in self.check_results)
            ):
                messages.append('Меню: «Состояние и работа сервера» → «Показать состояние».')
            messages.append(
                'Для остановленного сервера выберите «Запустить сервер» и повторите проверку.'
            )
        rows = [
            (('✓ ' if row.passed else '× ') + row.label, row.message) for row in self.check_results
        ]
        self.report('Доступность сервера', rows, messages)

    def status_result(self, code):
        services = [row for row in self.check_results if row.kind == 'service']
        running = sum(row.passed for row in services)
        stopped = sum(row.message.startswith('Остановлена') for row in services)
        if services and stopped == len(services):
            outcome = 'Сервер остановлен. Для общения выберите «Запустить сервер».'
        elif services and running == len(services):
            outcome = 'Все созданные службы работают. Доступность проверяется отдельно.'
        elif services:
            outcome = 'Сервер работает частично; проверьте отмеченные службы.'
        else:
            outcome = 'Сведения о службах не получены.'
        if code:
            outcome += ' Просмотр состояния не завершён.'
        rows = [
            (
                ('✓ ' if row.passed else '○ ' if row.message.startswith('Остановлена') else '× ')
                + row.label,
                row.message,
            )
            for row in services
        ]
        self.report('Состояние сервера', rows, [outcome, *self.result_lines])

    def unavailable_reason(self, path):
        if not self.installation():
            return 'Сервер не создан или уже удалён. Для этого действия нужна установка.'
        if self.installation().get('decommissioned'):
            return 'Сервер удалён частично. Сначала завершите удаление сохранённых данных.'
        return 'Действие недоступно для выбранного типа установки.'

    def available_actions(self, group):
        return [
            item
            for item in ui_actions.MENUS[group][1]
            if (
                bool(self.available_actions(item.path[1:]))
                if item.path.startswith('@')
                else self.supported(item.path)
            )
        ]

    def welcome(self):
        while True:
            try:
                self.view.heading('Misbot', 'Создайте сервер или восстановите его из копии.')
                choice = self.choose(
                    'С чего начать?',
                    [
                        (
                            '1',
                            'Установить новый сервер',
                            'Мастер проверит настройки перед запуском',
                        ),
                        (
                            '2',
                            'Восстановить сервер из копии',
                            'Сначала восстановление, затем отдельная активация',
                        ),
                        ('3', 'О программе и лицензиях', 'Misbot MIT · Rich MIT'),
                        ('4', 'Войти в меню', 'Создание сервера не начнётся'),
                    ],
                    exit_label='Выйти из Misbot',
                )
                if choice == '0':
                    return 0
                if choice == '1':
                    return self.wizard()
                if choice == '3':
                    self.about()
                    continue
                if choice == '4':
                    return self.menu()
                self.task_menu('restore')
                if self.installation():
                    return self.menu()
            except (Back, Cancel, KeyboardInterrupt):
                continue
            except SaveExit:
                return 0

    def restart_without_installation(self):
        self.view.heading(
            'Установка отсутствует', 'Сохранён черновик, но installation.json больше нет.'
        )
        self.view.message(
            'Проверку прежней установки продолжить нельзя. Настройку нужно пройти с начала.'
        )
        if self.root.exists() and any(self.root.iterdir()):
            self.view.message(
                'В каталоге остались файлы. Для новой установки мастер предложит выбрать пустой каталог; оставшиеся файлы сохраняются.',
                'yellow',
            )
        try:
            choice = self.choose(
                'Как продолжить?',
                [
                    (
                        '1',
                        'Проверить сохранённые настройки с начала',
                        'Параметры черновика сохраняются',
                    ),
                    ('2', 'Начать новую настройку', 'Прежний черновик сохраняется отдельно'),
                    ('3', 'Войти в меню', 'Создание сервера не начнётся'),
                ],
                exit_label='Выйти из Misbot',
            )
        except (Back, Cancel, SaveExit, KeyboardInterrupt):
            return 0
        if choice == '0':
            return 0
        if choice == '3':
            return self.menu()
        if choice == '2':
            from configuration import directory, read_private_json

            previous = directory(self.draft.parent / 'previous')
            state_store.write_json(
                previous / (secrets.token_hex(16) + '.json'), read_private_json(self.draft)
            )
            self.model = {}
            self.backup = {}
            self.confirmed_domains = set()
            self.admin_tag = 'admin-panel'
            self.dns_verified = None
        self.step = 0
        self.save()
        return self.wizard()

    def execute(self, item):
        if not self.supported(item.path):
            self.view.message(self.unavailable_reason(item.path), 'yellow')
            return 2
        import terminal_forms

        if item.path in terminal_forms.HANDLED_PATHS:
            return terminal_forms.execute(self, item)
        if item.path in ('storage s3 check', 'storage s3 connect'):
            return self.execute_s3(item)
        if item.path in ('users password set', 'credentials admin set'):
            return self.execute_password(item)
        if item.path == 'users invitation ensure':
            return self.execute_invitation(item)
        command = cli_commands.BY_PATH[item.path]
        self.view.heading(item.title, item.hint)
        args = {
            key: value
            for key, value in self.supplied_arguments.items()
            if value is not None and value is not False
        }
        execution_root = self.root
        if command.path == 'backup external cleanup':
            args = terminal_navigation.cleanup_arguments(self, args)
        if command.backend in ('restore', 'restore-local'):
            while True:
                target = self.input_value(
                    'Пустой каталог для восстановления, абсолютный путь',
                    str(self.root),
                    schema.installation_directory,
                )
                if not target.exists() or not any(target.iterdir()):
                    execution_root = target
                    break
                self.view.message(
                    'Выберите пустой абсолютный каталог без symlink и переходов ..', 'yellow'
                )
        if command.backend in ('restore', 'restore-local'):
            args.update(terminal_forms.collect_restore(self, command.backend))
        for key in command.arguments:
            if key in args:
                continue
            if key == 'calendar':
                args[key] = self.schedule_calendar()
                continue
            selected = terminal_navigation.operation_selection(self, command, key)
            if selected is not None:
                args[key] = selected
                continue
            label, default = ui_actions.ARGUMENTS[key]
            if key == 'config':
                if command.path.startswith('backup '):
                    label = 'Приватный профиль S3 для копий, абсолютный путь'
                elif command.backend.startswith('vault-'):
                    label = 'Приватный профиль Vault, абсолютный путь'
                elif command.backend == 'database-external':
                    label = 'Приватный профиль PostgreSQL, абсолютный путь'
                elif command.backend == 'features-enable':
                    label = 'Проверенный приватный план компонентов, абсолютный путь'
                if self.adopted() and command.backend in (
                    's3-check',
                    'media-s3',
                    'backup-external',
                    'backup-schedule',
                ):
                    purpose = 'backup' if command.path.startswith('backup ') else 'media'
                    default = str(self.root / '.misbot/s3' / (purpose + '.env'))
            if key == 'key' and self.adopted():
                default = self.installation().get('existing', {}).get('backup_key', default)
            if key == 'confirm':
                acknowledged = self.confirm(
                    'Исходный сервер остановлен и DNS переключён на этот сервер?'
                    if command.backend == 'restore-activate'
                    else 'Сервер назначения остановлен и DNS возвращён на исходный сервер?'
                )
                if not acknowledged:
                    self.view.message('Действие отменено. Сначала завершите переключение серверов.')
                    return 0
                args[key] = (
                    'source-stopped' if command.backend == 'restore-activate' else 'target-stopped'
                )
                continue
            args[key] = self.input_value(
                label, default, lambda value: self.operation_argument(key, value, command.backend)
            )
        if command.backend in ('update', 'update-recover') and 'source' not in args:
            with self.view.busy('Проверяем текущий установочный комплект'):
                source = terminal_navigation.trusted_bundle(self.source, self.root)
            args['source'] = source or self.input_value(
                'Доверенный распакованный комплект, абсолютный путь',
                '',
                lambda value: self.operation_argument('source', value, command.backend),
            )
        if (
            command.backend == 'restore-activate'
            and self.installation().get('mas_mode') == 'external'
            and not args.get('external_mas_confirm')
        ):
            self.view.heading('Подтвердить сохранность внешнего MAS')
            self.view.message(
                'Копия Misbot не содержит пользователей, устройств, ключей и данных внешнего MAS. '
                'Перед запуском Matrix сохраните или восстановите соответствующее состояние '
                'внешнего MAS из отдельной копии. Его учётные записи должны соответствовать '
                'восстановленной базе Matrix.',
                'yellow',
            )
            if not self.confirm('Соответствующие данные внешнего MAS сохранены или восстановлены?'):
                self.view.message('Активация отменена. Сначала проверьте данные внешнего MAS.')
                return 0
            args['external_mas_confirm'] = 'preserved-state'
        if not item.read_only and command.path != 'remove':
            rows = [
                ('Операция', item.title),
                ('Установка', str(execution_root)),
                ('Перерыв в работе', command.stops),
                ('Данные', command.data),
            ]
            rows.extend(
                (
                    ui_actions.ARGUMENTS.get(key, (key.replace('_', ' '), ''))[0],
                    'Подтверждено' if key == 'confirm' else value,
                )
                for key, value in args.items()
                if key not in ('source', 'external_mas_confirm')
            )
            if 'source' in args:
                rows.append(('Комплект', args['source']))
            if 'external_mas_confirm' in args:
                rows.append(('Данные внешнего MAS', 'Сохранены или восстановлены отдельно'))
            self.view.details(rows)
            if not self.confirm():
                self.view.message('Действие отменено.')
                return 0
        previous = self.root
        try:
            self.root = execution_root
            code = self.run(command.path, **args)
        finally:
            self.root = previous
        if code == 0 and command.path in ('backup create', 'backup move export'):
            terminal_forms.remember_backup_results(self, command.path)
        if command.path == 'check':
            self.check_result(code)
            return code
        if command.path == 'status':
            self.status_result(code)
            return code
        completion = []
        if command.backend in ('restore', 'restore-local') and code == 0:
            from configuration import read_private_json

            restored = read_private_json(execution_root / 'installation.json')
            self.root = execution_root
            self.draft = self.draft_path(self.root)
            self.model, self.backup = {}, {}
            continuation = (
                'Продолжение: «Восстановление и перенос» → «Активировать восстановленный сервер».'
            )
            if restored.get('mas_mode') == 'external':
                continuation += ' Потребуется подтверждение сохранности внешнего MAS.'
            completion.extend(
                [
                    'Копия восстановлена. Перед активацией остановите источник и переключите DNS.',
                    continuation,
                ]
            )
        if command.backend in ('update', 'update-recover', 'release-update') and code == 0:
            completion.append(
                'Операция завершена. После закрытия результата откроется интерфейс установленной версии.'
            )
        self.result_notices.extend(completion)
        self.show_operation_result(item, code)
        if not self.view.fullscreen:
            for message in completion:
                self.view.message(message)
        if command.path == 'remove' and code == 0 and not self.installation():
            self.draft.unlink(missing_ok=True)
            self.model, self.backup = {}, {}
            self.step = 0
        if command.path == 'users invitation list' and code == 0:
            self.show_result_lines(item.title)
        if command.backend in ('update', 'update-recover', 'release-update') and code == 0:
            if not getattr(self, '_result_waited', False):
                try:
                    self.pause('Enter — закрыть мастер')
                except (Back, Cancel, SaveExit):
                    pass
            self.restart_requested = True
            raise SaveExit()
        return code

    def schedule_calendar(self, default=''):
        return terminal_navigation.schedule_calendar(self, default)

    def reopen_updated(self, plain=False):
        import command_wrapper

        command_wrapper.trusted_installation(self.root)
        arguments = [
            '/usr/bin/python3',
            str(self.root / 'misbot.py'),
            '--directory',
            str(self.root),
            'ui',
        ]
        if plain:
            arguments.append('--plain')
        os.execv(arguments[0], arguments)

    def configuration_recovery(self):
        """Reach recovery without loading partially published editable settings."""
        while (self.root / 'configuration-transition.json').exists():
            self.view.heading('Прервано сохранение настроек')
            self.view.message(
                'Настройки и сохранённая копия остаются на сервере. Выберите продолжение.'
            )
            try:
                choice = self.choose(
                    'Как продолжить?',
                    [
                        ('1', 'Продолжить сохранение настроек', ''),
                        ('2', 'Вернуть прежние настройки', ''),
                    ],
                    exit_label='Выйти из Misbot',
                )
            except (Back, Cancel, SaveExit):
                return 0
            if choice == '0':
                return 1
            path = 'config recover' if choice == '1' else 'config rollback'
            try:
                self.execute(ui_actions.Action(path, 'Восстановить настройки'))
            except (Back, Cancel, SaveExit):
                return 0
        self.use_active_configuration()
        return self.menu()

    def show_operation_result(self, item, code, messages=None):
        if messages is None:
            messages = list(dict.fromkeys([*self.result_notices, *self.result_messages]))
        if self.view.fullscreen and messages:
            self.report(
                item.title,
                [('', message) for message in messages],
                result_code=code,
                continuation=self.operation_continuation(item.path),
            )
        else:
            # Plain terminals already printed the complete sequential output.
            self.view.heading(item.title)
            self.view.result(item.title, code, self.operation_continuation(item.path))

    def execute_password(self, item):
        import tempfile
        from configuration import directory
        import service_identity

        administrator = service_identity.administrator(self.root)
        if item.path == 'credentials admin set' and not administrator.password_managed:
            self.view.message(
                'Пароль этого администратора задаётся через MAS; Misbot его не хранит.'
            )
            return 2
        self.view.heading(item.title)
        self.view.message('Введите новый пароль дважды. Ввод скрыт; файл создавать не нужно.')
        values = {}
        if item.path == 'users password set':
            domain = self.installation()['domain']

            def username(value):
                name = value.strip()
                if name.startswith('@'):
                    suffix = ':' + domain
                    if not name.endswith(suffix):
                        raise ValueError('Укажите аккаунт на этом сервере: @имя:' + domain)
                    name = name[1 : -len(suffix)]
                if name == administrator.username:
                    raise ValueError(
                        'Для первоначального администратора используйте управление аккаунтом MAS.'
                    )
                if not re.fullmatch(r'[a-z0-9][a-z0-9._=-]{0,254}', name):
                    raise ValueError('Введите имя существующего пользователя или @имя:' + domain)
                return name

            selected = terminal_navigation.account_selection(self)
            values['username'] = selected or self.input_value('Аккаунт пользователя', '', username)
        else:
            values['username'] = administrator.username
        self.view.message('Пароль: от 16 до 1024 символов. Существующие сеансы сохранятся.')

        def password(value):
            if not 16 <= len(value) <= 1024 or any(ord(char) < 32 for char in value):
                raise ValueError('Нужно от 16 до 1024 символов без управляющих символов.')
            return value

        while True:
            value = self.input_value('Новый пароль', '', password, secret=True)
            repeated = self.ask('Повторите новый пароль', secret=True)
            if value == repeated:
                break
            self.view.message('Пароли не совпадают. Повторите ввод.', 'yellow')
        command = cli_commands.BY_PATH[item.path]
        self.view.details(
            [
                ('Аккаунт', '@' + values['username'] + ':' + self.installation()['domain']),
                ('Перерыв в работе', command.stops),
                ('Данные', command.data),
            ]
        )
        if not self.confirm():
            self.view.message('Действие отменено.')
            return 0
        values['password'] = value
        if item.path == 'credentials admin set':
            del values['username']
        parent = directory(self.draft.parent)
        with tempfile.TemporaryDirectory(prefix='.password-operation-', dir=parent) as folder:
            path = Path(folder) / 'input.json'
            state_store.write_json(path, values)
            code = self.run(item.path, input=path)
        if code == 0:
            self.view.message('Пароль изменён. Войдите в клиент с новым паролем.')
        else:
            self.view.message(
                'Смена пароля не подтверждена. Если операция началась, её можно продолжить '
                'в разделе «Продолжить прерванную операцию» → «Пароли и ключи после прерывания».'
            )
        self.show_operation_result(item, code)
        return code

    def execute_invitation(self, item):
        self.view.heading(item.title)
        self.view.message(
            'Код для одной регистрации появится в этом терминале. '
            'Действующий код будет сохранён; использованный или истёкший будет заменён.'
        )
        if not self.confirm():
            return 0
        code = self.run('users invitation ensure')
        records = getattr(self, 'operation_records', [])
        state = records[-1].get('state') if records else None
        if code == 0 and state in ('used', 'expired', 'revoked'):
            code = self.run('users invitation new')
            records = getattr(self, 'operation_records', [])
            state = records[-1].get('state') if records else None
        if code == 0 and state == 'valid':
            self.view.message('Передайте код человеку. Его нужно ввести в форме регистрации.')
            return self.run('users invitation show')
        if code == 0:
            code = 1
            self.view.message('Код пока не подтверждён. Повторите «Пригласить пользователя».')
        self.show_operation_result(item, code)
        return code

    def execute_s3(self, item):
        import tempfile
        import s3_config
        from configuration import directory, serialize

        self.view.heading(item.title)
        self.view.message('Введите параметры S3 здесь. Ключи доступа скрыты при вводе.')
        self.view.message('«Назад» отменяет ввод; Ctrl+C прерывает выполняемую операцию.', 'dim')
        explicit = self.supplied_arguments
        try:
            purpose = explicit.get('purpose', 'media')
            if item.path == 'storage s3 check' and not explicit.get('purpose'):
                choice = self.choose(
                    'Что проверить?',
                    [('1', 'Хранилище медиа', ''), ('2', 'Хранилище резервных копий', '')],
                    aliases={'media': '1', 'backup': '2'},
                )
                if choice == '0':
                    raise Back()
                purpose = 'media' if choice == '1' else 'backup'
            initial = self.model if purpose == 'media' else self.backup
            values = (
                s3_config.read_profile(Path(explicit['config']), purpose)
                if explicit.get('config')
                else self.s3_profile(initial)
            )
        except (Back, Cancel):
            self._result_waited = True
            self.view.message('Ввод S3 отменён. Возвращаемся в меню.')
            return 0
        self.view.heading(item.title, 'Проверьте настройки перед выполнением.')
        self.view.details(
            [
                (
                    schema.FIELDS[key].label,
                    'настроен'
                    if schema.FIELDS[key].secret
                    else values[key] or 'AWS для выбранного региона',
                )
                for key in (
                    'S3_BUCKET',
                    'S3_ENDPOINT_URL',
                    'S3_REGION',
                    'S3_PREFIX',
                    'S3_ACCESS_KEY_ID',
                    'S3_SECRET_ACCESS_KEY',
                )
            ]
        )
        if item.path == 'storage s3 connect':
            self.view.message(
                'Для переноса будет создана копия и остановлен Matrix. Локальные файлы сохраняются.'
            )
        elif explicit.get('read_only'):
            self.view.message('Проверка только чтения. Проверочный объект не создаётся.')
        else:
            self.view.message(
                'Будет создан и удалён собственный проверочный объект. Текущие файлы сохраняются.'
            )
        if not self.confirm():
            self.view.message('Действие отменено.')
            return 0
        parent = directory(self.draft.parent)
        recovery_lines = []
        with tempfile.TemporaryDirectory(prefix='.s3-operation-', dir=parent) as folder:
            profile = (
                Path(explicit['config']) if explicit.get('config') else Path(folder) / 'profile.env'
            )
            if not explicit.get('config'):
                state_store.write_text(
                    profile, serialize({key: values[key] for key in sorted(s3_config.FIELDS)})
                )
            args = {'config': profile}
            if item.path == 'storage s3 check':
                args.update(purpose=purpose, json=True)
                for key in ('read_only', 'catalog_directory'):
                    if explicit.get(key):
                        args[key] = explicit[key]
            code = self.run(item.path, **args)
            records = getattr(self, 'operation_records', [])
            pending = next(
                (record for record in records if record.get('cleanup_pending') is True), None
            )
            if pending and not explicit.get('config'):
                # Keep only required recovery material when the provider refuses
                # cleanup; all successful/cancelled temporary forms are removed.
                recovery = parent / (
                    self.draft.stem + '-s3-probe-recovery-' + secrets.token_hex(8) + '.env'
                )
                state_store.write_text(recovery, serialize(values))
                recovery_lines.append(
                    'Провайдер не подтвердил удаление проверочного объекта. Приватный профиль сохранён: '
                    + str(recovery)
                )
                for action in pending.get('next_actions', []):
                    if isinstance(action, str) and ' storage s3 cleanup ' in action:
                        recovery_lines.append(
                            action.replace(shlex.quote(str(profile)), shlex.quote(str(recovery)))
                        )
                pending['next_actions'] = [
                    action.replace(shlex.quote(str(profile)), shlex.quote(str(recovery)))
                    for action in pending.get('next_actions', [])
                    if isinstance(action, str)
                ]
        messages = list(recovery_lines)
        for record in records:
            if item.path == 'storage s3 check':
                from terminal_process import present_output

                shown = present_output(item.path, json.dumps(record))
                # Temporary input profiles have been removed. Retry through the
                # form, retaining only a real cleanup continuation when needed.
                messages.extend(
                    message
                    for message in shown
                    if not message.startswith('Следующий шаг:') or ' storage s3 cleanup ' in message
                )
        if item.path != 'storage s3 check':
            messages.extend([*self.result_notices, *self.result_messages])
        self.show_operation_result(item, code, list(dict.fromkeys(messages)))
        if not self.view.fullscreen:
            for message in recovery_lines:
                self.view.message(message, 'yellow')
        return code

    def show_result_lines(self, title):
        if not self.result_lines:
            return
        if not self.view.fullscreen or self.result_lines == ['Приглашений пока нет.']:
            for line in self.result_lines:
                self.view.message(line)
            return
        self.report(
            title,
            [(str(index), line) for index, line in enumerate(self.result_lines, 1)],
            messages=['Коды приглашений не отображаются.'],
        )

    def operation_continuation(self, path):
        if path == 'remove':
            return 'Повторите удаление с тем же выбором данных. Сохранённый журнал отмечает завершённые шаги.'
        if path.startswith('federation '):
            if (self.root / 'federation-transition.json').exists():
                return 'Меню: «Продолжить прерванную операцию» → «Федерация после прерывания».'
            return (
                'Меню: «Связь с другими серверами». Проверьте выбранный домен или состояние заявки.'
            )
        return self.continuation()

    def task_menu(self, group):
        title = ui_actions.MENUS[group][0]
        while True:
            pending = terminal_navigation.pending_actions(self) if group == 'recovery' else []
            actions = (
                self.available_actions(group)
                if self.adopted()
                or group == 'admin'
                or (
                    group == 'restore'
                    and not self.installation()
                    and not (self.root / '.misbot-empty.json').exists()
                )
                else list(ui_actions.MENUS[group][1])
            )
            if not actions:
                self.view.message('Этот раздел недоступен для выбранной установки.')
                return
            if pending:
                actions = pending + list(actions)
            if self.adopted():
                actions = [
                    ui_actions.Action(
                        item.path, 'Создать копию', 'Используется сохранённое место хранения'
                    )
                    if item.path == 'backup create'
                    else item
                    for item in actions
                ]
            try:
                self.view.heading(title)
                if group == 'recovery':
                    self.view.message(
                        'В начале списка — действия для найденных незавершённых операций. Сохранённые этапы не удаляются.'
                    )
                choices = []
                for i, item in enumerate(actions, 1):
                    available = (
                        bool(self.available_actions(item.path[1:]))
                        if item.path.startswith('@')
                        else self.supported(item.path)
                    )
                    hint = (
                        item.hint
                        if available
                        else 'Недоступно: ' + self.unavailable_reason(item.path)
                    )
                    choices.append((str(i), item.title, hint))
                choice = self.choose('Что сделать?', choices, keep_hints=True)
                if choice == '0':
                    return
                item = actions[int(choice) - 1]
                if item.path.startswith('@'):
                    self.task_menu(item.path[1:])
                else:
                    self._result_waited = False
                    self.execute(item)
                    if not self._result_waited:
                        self.pause('Enter — вернуться к меню')
            except (Back, Cancel, KeyboardInterrupt):
                self.view.message('Ввод отменён.')
                return

    def menu(self):
        state = self.installation()
        version = json.loads((self.source / 'release.json').read_text())['version']
        self.view.heading('Открываем управление сервером')
        if state:
            self.run('status')
        while True:
            try:
                state = self.installation()
                self.view.heading(
                    'Misbot ' + version,
                    'Управление сервером ' + state['domain']
                    if state
                    else 'Сервер не создан или удалён',
                )
                self.view.details(
                    [
                        ('Установка', str(self.root)),
                        (
                            'Режим',
                            'Подключённая Compose-установка'
                            if self.adopted()
                            else 'Установка Misbot',
                        ),
                    ]
                )
                choices = [
                    item
                    for item in ui_actions.MAIN
                    if not (self.adopted() and item[0] in ('2', '7'))
                ]
                if self.adopted():
                    choices.append(
                        ('12', 'Дополнительные компоненты', 'Подключить подготовленный план')
                    )
                if not state:
                    choices = [
                        (
                            number,
                            title,
                            hint if number in ('4', '11') else 'Недоступно: нужен созданный сервер',
                        )
                        for number, title, hint in choices
                    ]
                    choices.append(('n', 'Создать новый сервер', 'Открыть мастер настройки'))
                choice = self.choose(
                    'Главное меню', choices, exit_label='Выйти из Misbot', keep_hints=True
                )
                if choice == '0':
                    return 0
                if choice == 'n':
                    return self.wizard()
                if not state and choice in ('5', '7'):
                    self.view.message(self.unavailable_reason('homepage show'), 'yellow')
                    self.pause('Enter — вернуться к меню')
                    continue
                if choice == '5':
                    self.homepage_menu()
                elif choice == '7':
                    self.settings_menu()
                elif choice == '11':
                    self.about()
                elif choice == '12' and self.adopted():
                    self.task_menu('features')
                else:
                    self.task_menu(ui_actions.MAIN_TARGETS[choice])
            except (EOFError, state_store.OperationInterrupted):
                return 130
            except (Back, Cancel, KeyboardInterrupt):
                self.view.message('Ввод отменён; возвращаемся в главное меню.')
            except SaveExit:
                self.view.message('Выход из Misbot.')
                return 0
            except Exception as error:
                self.view.message('Операция не завершена.', 'yellow')
                self.failure_reason(error)
                self.view.message(self.continuation())

    def about(self):
        self.view.heading('О программе и лицензиях')
        self.view.details(
            [
                ('Misbot', 'MIT · Copyright (c) 2026 Stepan Ponamarev'),
                ('Оформление', 'Rich · MIT · Copyright (c) 2020 Will McGugan'),
                ('Лицензия Misbot', str(self.source / 'LICENSE')),
                ('Сторонние компоненты', str(self.source / 'third-party-licenses.md')),
            ]
        )
        if self.view.console is None:
            self.view.message(
                'Сейчас используется простой вывод. Rich: sudo apt-get install python3-rich. Для оформления нужен терминал с TERM, отличным от dumb.'
            )
        self.view.message('Системные пакеты и их зависимости сохраняют собственные лицензии.')
        self.pause()

    def edit_fields(self, title, keys, section='', subtitle='', providers=False):
        before = dict(self.model)
        tracked = list(keys)
        if providers:
            tracked.extend(
                (
                    'GOOGLE_CLIENT_ID',
                    'GOOGLE_CLIENT_SECRET',
                    'TELEGRAM_CLIENT_ID',
                    'TELEGRAM_CLIENT_SECRET',
                    'SOCKS5_CONFIG',
                )
            )
        aliases = {key: str(i) for i, key in enumerate(keys, 1)}
        aliases['применить'] = 'a'
        while True:
            self.view.heading(section or title, subtitle)
            choices = [
                (
                    str(i),
                    schema.FIELDS[key].label,
                    'настроен'
                    if schema.FIELDS[key].secret and self.model[key]
                    else self.model[key] or 'не задано',
                )
                for i, key in enumerate(keys, 1)
            ]
            choices.append(
                ('a', 'Применить изменения', 'Сначала покажем итог и запросим подтверждение')
            )
            if providers:
                choices.extend(
                    (
                        letter,
                        'Вход через ' + provider.title(),
                        'Включён' if self.model.get(provider + '_CLIENT_ID') else 'Выключен',
                    )
                    for letter, provider in (('g', 'GOOGLE'), ('t', 'TELEGRAM'))
                )
            choice = self.choose(title, choices, aliases=aliases, keep_hints=True)
            if choice == '0':
                return None
            if choice == 'a':
                return {
                    key: self.model.get(key, '')
                    for key in tracked
                    if self.model.get(key, '') != before.get(key, '')
                }
            if providers and choice in ('g', 't'):
                self.setup_provider('GOOGLE' if choice == 'g' else 'TELEGRAM')
                continue
            key = keys[int(choice) - 1]
            self.view.heading(schema.FIELDS[key].label, section)
            self.field(key)

    def homepage_menu(self):
        import homepage

        if self.adopted():
            import existing_features

            current = existing_features.Homepage(self.root).current()
            self.model = {key: current[key] for key in homepage.KEYS}
        else:
            from configuration import load

            self.model = load(self.root, True)
        before = dict(self.model)
        changes = self.edit_fields(
            'Что изменить?',
            homepage.KEYS,
            'Сайт сервера',
            'Выберите поле, проверьте итог и примените изменения.',
        )
        if not changes:
            self.view.message('Изменения не применялись.')
            return
        for key, value in changes.items():
            schema.field_value(key, value)
        self.view.heading('Применить настройки сайта', 'Проверьте выбранные изменения.')
        self.view.details(
            [
                (schema.FIELDS[key].label, before[key] + ' → ' + value)
                for key, value in changes.items()
            ]
        )
        self.view.message('Для применения обновится Caddy; возможен короткий перерыв HTTPS.')
        if not self.confirm('Проверить и применить эти изменения?'):
            return
        names = {
            'HOMEPAGE_MODE': 'mode',
            'HOMEPAGE_URL': 'url',
            'SITE_NAME': 'name',
            'SITE_DESCRIPTION': 'description',
            'SITE_SUPPORT_URL': 'support_url',
        }
        self._result_waited = False
        code = self.run('homepage apply', **{names[key]: value for key, value in changes.items()})
        self.show_operation_result(ui_actions.Action('homepage apply', 'Сайт сервера'), code)
        if not self._result_waited:
            self.pause('Enter — вернуться к меню')
        return code

    def settings_menu(self):
        from configuration import load, apply_settings

        if self.adopted():
            self.view.message('Настройки подключённого стека меняются его собственными средствами.')
            return
        self.model = load(self.root, complete=True)
        keys = (
            'PUBLIC_IP',
            'ACME_EMAIL',
            'FEDERATION_SERVERS',
            'INVITATION_LIFETIME_SECONDS',
            'INVITATION_USAGE_LIMIT',
            'OPEN_REGISTRATION',
            'REGISTRATION_TOKEN_REQUIRED',
            'MAX_UPLOAD_SIZE_MIB',
        )
        before = dict(self.model)
        changes = self.edit_fields(
            'Что изменить?',
            keys,
            'Настройки сервера',
            'Домен сервера закреплён. Хранилище и ключи доступа — в отдельных разделах.',
            providers=self.model.get('MAS_MODE', 'local') == 'local',
        )
        if not changes:
            self.view.message('Изменения не применялись.')
            return
        schema.validate(self.model)
        self.view.heading('Применить настройки сервера', 'Проверьте выбранные изменения.')
        self.view.details(
            [
                (
                    schema.FIELDS[key].label,
                    ('настроено' if value else 'отключено')
                    if schema.FIELDS[key].secret
                    else before.get(key, '') + ' → ' + value,
                )
                for key, value in changes.items()
            ]
        )
        self.view.message('Будет создана согласованная копия; возможен перезапуск служб.')
        if self.confirm('Применить изменения?'):
            with self.view.busy('Проверяем копию и применяем настройки сервера'):
                apply_settings(self.root, changes)
            self.view.message('Настройки применены.', 'green')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--plain', action='store_true')
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            'UI требует SSH TTY: ssh -t USER@HOST; затем sudo misbot ui. Для скрипта: misbot help',
            file=sys.stderr,
        )
        return 2
    if os.geteuid() != 0:
        print('Выполните sudo misbot ui; автоматически пароль не запрашивается.', file=sys.stderr)
        return 77
    view = TerminalView(plain=args.plain)
    with view.screen():
        with view.busy('Загружаем настройки установки'):
            ui = UI(args.directory, view=view)
        code = interaction(ui)
    if ui.restart_requested:
        ui.reopen_updated(plain=args.plain)
    if ui.view.screen_used:
        text(
            'Мастер закрыт. Продолжение: sudo python3 '
            + shlex.quote(str(ui.source_entrypoint()))
            + ' ui --directory '
            + shlex.quote(str(ui.root))
        )
    return code


def interaction(ui):
    if (ui.root / 'installation.json').exists():
        import command_wrapper

        command_wrapper.trusted_installation(ui.root)
        state = json.loads((ui.root / 'installation.json').read_text())
        if state.get('decommissioned'):
            return ui.menu()
        if state.get('backend') == 'existing-compose':
            return ui.menu()
        if (ui.root / 'configuration-transition.json').exists():
            return ui.configuration_recovery()
        if state.get('restore_pending'):
            return ui.menu()
        if state.get('databases_initialized'):
            if ui.draft.exists() and 7 <= ui.step <= 9:
                return ui.wizard()
            return ui.menu()
    if (ui.root / '.misbot-empty.json').exists():
        return ui.welcome()
    if ui.draft.exists() and ui.step >= 8 and not ui.installation():
        return ui.restart_without_installation()
    return ui.wizard() if ui.draft.exists() or ui.installation() else ui.welcome()


if __name__ == '__main__':
    try:

        def interrupted(signum, frame):
            raise state_store.OperationInterrupted('SSH/UI interrupted')

        for number in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(number, interrupted)
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError, state_store.OperationInterrupted):
        text(
            'Ввод прерван; сохранённый draft и checkpoints оставлены. Повторите sudo misbot ui; проверьте misbot status.'
        )
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError, KeyError):
        text(
            'Не удалось прочитать настройки мастера. Сохранённые файлы оставлены. '
            'Повторите ту же команду ui из распакованного комплекта с тем же --directory.'
        )
        raise SystemExit(1)
