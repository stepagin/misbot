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
from terminal_diagnostics import check_summary, parse_checks
import ui_actions


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
    commands = {'назад': Back, 'отмена': Cancel, 'сохранить и выйти': SaveExit}
    if value.lower().strip() in commands:
        raise commands[value.lower().strip()]()
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
        return question(label, default, secret, self.reader)

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
        if key == 'MAS_DOMAIN' and self.model.get('DOMAIN'):
            label = label.replace('mas.<DOMAIN>', 'mas.' + self.model['DOMAIN'])
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
            choices = {
                'media': (('local', 'Хранить медиа на этом сервере'), ('s3', 'Хранить медиа в S3')),
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
                if not item.secret and item.kind in (
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
                value = self.ask(label if self.view.console else key, current, item.secret)
                if value == '/очистить' and not item.secret:
                    value = ''
            try:
                normalized = schema.field_value(key, value)
                if key == 'MAS_DOMAIN':
                    schema.validate({'DOMAIN': self.model['DOMAIN'], 'MAS_DOMAIN': normalized})
            except schema.InvalidConfiguration as error:
                self.view.message(error.field + ': ' + error.explanation)
                continue
            if item.kind == 'domain' and not self.confirm_domain(normalized):
                continue
            self.model[key] = normalized
            self.save()
            return

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

        interactive = path in (
            'users invitation show',
            'credentials admin show',
            'update latest',
            'remove',
        )
        records = []
        self.operation_records = []
        self.result_lines = []
        self.result_total = 0
        if path in ('check', 'status'):
            self.check_results = []

        def collect_check(line):
            records.append(line[:8192])
            del records[:-256]

        def remember_output(record, messages):
            if not isinstance(record, dict):
                return
            if path == 'storage s3 check' and record.get('schema') == 1:
                self.operation_records.append(record)
                del self.operation_records[:-100]
            if path != 'users invitation list':
                self.result_messages = messages[:100]
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
                self.check_results = parse_checks('\n'.join(records))
            if path == 'status':
                self.result_lines = [
                    message
                    for line in records
                    for message in present_output(path, line)
                    if not any(
                        row.label + ': ' == message[: len(row.label) + 2]
                        for row in self.check_results
                    )
                ]
            return code
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
                            read_interrupted_output(child, collect_recovery)
                            if process_group_running(child):
                                time.sleep(0.2)
                    except subprocess.TimeoutExpired:
                        pass
                    except (KeyboardInterrupt, state_store.OperationInterrupted):
                        pass
            if path == 'check':
                self.check_results = parse_checks('\n'.join(records))
            self.view.message(self.continuation())
            return 130
        finally:
            try:
                if child.stdout is not None:
                    child.stdout.close()
            finally:
                input_mode.__exit__(None, None, None)

    def s3_form(self, purpose):
        import s3_diagnostics as probe
        import s3_config

        values = self.model if purpose == 'media' else self.backup
        keys = (
            'S3_BUCKET',
            'S3_ENDPOINT_URL',
            'S3_REGION',
            'S3_PREFIX',
            'S3_ACCESS_KEY_ID',
            'S3_SECRET_ACCESS_KEY',
        )
        for key in keys:
            self.s3_field(key, values)
        path = self.draft.parent / (self.draft.stem + '-' + purpose + '.env')
        while True:
            from configuration import serialize

            state_store.write_text(
                path, serialize({key: values.get(key, '') for key in sorted(s3_config.FIELDS)})
            )
            result = probe.diagnose(
                path,
                purpose,
                directory=self.root if (self.root / 'installation.json').exists() else None,
                progress=lambda item: self.view.message(
                    item['stage'] + ': ' + item['status'] + ' — ' + item['explanation']
                ),
            )
            self.view.message('Итог: ' + result['conclusion'])
            for action in result['next_actions']:
                self.view.message(action)
            if result['passed']:
                return
            choice = self.answer(
                '1 исправить поле, 2 повторить, 3 локальные медиа/отложить, сохранить и выйти',
                ('1', '2', '3'),
                '1',
            )
            if choice == '3':
                if purpose == 'media':
                    self.model['MEDIA_MODE'] = 'local'
                else:
                    self.backup = {}
                self.view.message(
                    'Позднее: sudo misbot storage s3 connect --config /root/misbot-s3.env; существующие файлы переносятся и сохраняются.'
                    if purpose == 'media'
                    else 'Позднее: misbot backup external create --help; настройки можно сохранить в settings/backup.env.'
                )
                self.save()
                return
            if choice == '1':
                selection = self.choose(
                    'Какое поле исправить?',
                    [(str(i), schema.FIELDS[key].label, '') for i, key in enumerate(keys, 1)],
                    aliases={key: str(i) for i, key in enumerate(keys, 1)},
                )
                if selection == '0':
                    raise Back()
                key = keys[int(selection) - 1]
                self.s3_field(key, values)

    def s3_field(self, key, values):
        import s3_config

        item = schema.FIELDS[key]

        def validate(value):
            if key == 'S3_ENDPOINT_URL' and value == '/очистить':
                value = ''
            return s3_config.validate_field(key, schema.field_value(key, value))

        if key == 'S3_ENDPOINT_URL':
            self.view.message(
                'Enter сохраняет endpoint; /очистить выбирает AWS endpoint для региона.', 'dim'
            )
        values[key] = self.input_value(
            item.label + ' (' + key + ')',
            values.get(key, item.default),
            validate,
            item.secret,
        )
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
        def validate(value):
            localpart = value.removeprefix('@')
            if not re.fullmatch(r'[a-z0-9][a-z0-9._=-]{0,63}', localpart):
                raise ValueError('Имя бота: строчные латинские буквы, цифры, точка, _ , = или -.')
            if localpart in ('referral-bot', 'server-guide', 'matrix-admin'):
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
            termios.tcsetattr(descriptor, termios.TCSANOW, original)
            del self._dns_decoder
            self.dns_command_buffer = ''

    def wait_dns(self, expected):
        import queue
        import threading
        import wizard_dns

        hosts = schema.public_hosts(self.model)
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
                    progress.log('Ожидаемый IPv4 VPS: ' + expected)
                    for host in hosts:
                        status = statuses.get(host)
                        progress.log(host + ': ' + (status.message if status else 'проверяется'))
                    progress.log('CDN proxy должен быть отключён (DNS only).')
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
        return {key: self.model.get(key, '') for key in ('DOMAIN', 'MAS_DOMAIN', 'PUBLIC_IP')}

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
                    'и дождаться их DNS-записей. «Назад» вернёт к вводу домена.',
                    'yellow',
                )
                self.field('PUBLIC_IP')
                ip = self.model.get('PUBLIC_IP')
        self.wait_dns(ip)
        with self.view.busy('Проверяем DNS, сертификаты и сеть'):
            preflight.dns_check(domain, ip, self.model.get('MAS_DOMAIN', ''))
            preflight.caa_check(domain, self.model.get('MAS_DOMAIN', ''))
            preflight.network_check(self.root, self.model.get('DOCKER_SUBNET', '172.30.50.0/24'))
        self.dns_verified = self.dns_values()
        self.save()

    def setup_summary(self):
        page = 0
        warning = ''
        while True:
            self.model = schema.validate(self.model)
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
                    'MAS_DOMAIN',
                    'PUBLIC_IP',
                    'ACME_EMAIL',
                    'MEDIA_MODE',
                    'POSTGRES_MODE',
                )
                or (value and value != schema.FIELDS[key].default)
            ]
            rows = []
            labels = {
                'DOMAIN': 'Основной домен',
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
                shown = 'настроен' if schema.FIELDS[key].secret and value else value or 'не задано'
                shown = shown if len(shown) <= 100 else shown[:97] + '…'
                rows.append((str(index) + '. ' + labels.get(key, self.field_label(key)), shown))
            admin_number = str(len(fields) + 1)
            rows.append((admin_number + '. Имя административного бота', self.admin_tag))
            backup_number = str(len(fields) + 2)
            rows.append(
                (backup_number + '. Внешние копии', 'настроены' if self.backup else 'отложены')
            )
            pages = [rows]
            if self.view.fullscreen and self.view.console:
                budget = max(1, self.view.console.height - self.view.used_lines - 11)
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
                if self.dns_verified != self.dns_values():
                    self.check_dns_settings()
                return
            if change in ('>', '<'):
                page = max(0, min(page + (1 if change == '>' else -1), len(pages) - 1))
                continue
            previous_backup = self.backup.copy()
            try:
                if change == admin_number:
                    self.setup_admin_tag()
                elif change == backup_number:
                    self.setup_step(5)
                elif change.isdecimal() and 1 <= int(change) <= len(fields):
                    self.field(fields[int(change) - 1])
                elif change in fields:
                    self.field(change)
                else:
                    warning = 'Введите номер поля или команду из списка.'
            except Back:
                if change == backup_number:
                    self.backup = previous_backup
                    self.save()

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
            import shutil

            missing = [tool for tool in ('dig', 'ip') if not shutil.which(tool)]
            if missing:
                raise RuntimeError(
                    'Отсутствуют локальные зависимости: '
                    + ', '.join(missing)
                    + '. Выполните sudo apt-get update; sudo apt-get install -y dnsutils iproute2 python3-yaml; затем повторите ui. Это не ошибка DNS или S3.'
                )
            self.view.message(
                'Проверка Linux amd64, systemd, памяти, диска и портов. Ошибку исправьте и повторите; шаг не пропускается.'
            )
            with self.view.busy('Проверяем систему и свободные ресурсы'):
                preflight.system_check(self.root)
        elif index == 2:
            self.check_domain_inputs()
            self.field('MAS_DOMAIN')
            self.field('PUBLIC_IP')
            self.field('ACME_EMAIL')
            self.check_dns_settings()
        elif index == 3:
            self.field('MEDIA_MODE')
            if self.model['MEDIA_MODE'] == 's3':
                self.s3_form('media')
        elif index == 4:
            self.view.message(
                'Парольный вход доступен без Google/Telegram. OAuth можно пропустить и настроить до запуска позднее.'
            )
            for provider in ('GOOGLE', 'TELEGRAM'):
                default = (
                    'да'
                    if self.model.get(provider + '_CLIENT_ID')
                    or self.model.get(provider + '_CLIENT_SECRET')
                    else 'нет'
                )
                enabled = self.answer('Подключить ' + provider + '? да/нет', ('да', 'нет'), default)
                if enabled == 'да':
                    self.field(provider + '_CLIENT_ID')
                    self.field(provider + '_CLIENT_SECRET')
                    if provider == 'TELEGRAM':
                        self.field('SOCKS5_CONFIG')
                else:
                    self.model[provider + '_CLIENT_ID'] = ''
                    self.model[provider + '_CLIENT_SECRET'] = ''
                    self.save()
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

        domain = self.installation()['domain']
        self.view.heading(
            'Настройка завершена', 'Откройте клиент и войдите в аккаунт администратора.'
        )
        self.view.details(
            [('Клиент', 'https://web.' + domain), ('Администратор', '@matrix-admin:' + domain)]
        )
        self.view.message(
            'Пароль: sudo misbot credentials admin show — только по отдельному запросу.'
        )
        try:
            result = manage(self.root, 'ensure')
            self.view.details(
                [('Первое приглашение', result['state']), ('Срок до', result['expires_at'])]
            )
            self.view.message(
                'Пригласить пользователя: sudo misbot ui → Пользователи и приглашения → Показать код приглашения.'
            )
            self.view.message(
                'Пользователь вводит код в форме регистрации клиента. Не добавляйте код в URL.'
            )
        except Exception:
            self.view.message(
                'Сервер запущен; выдача токена не подтверждена. Сохранённый intent будет продолжен: sudo misbot users invitation ensure. Переустановка не требуется.'
            )

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
            except (Cancel, KeyboardInterrupt):
                self.view.message(
                    'Ввод отменён; действующая конфигурация не изменена. Draft сохранён.'
                )
                self.save()
                return 130
            except SaveExit:
                self.save()
                self.view.message(
                    'Сохранено приватно. Продолжение: sudo python3 '
                    + shlex.quote(str(self.source_entrypoint()))
                    + ' ui --directory '
                    + shlex.quote(str(self.root))
                )
                return 0
            except (RuntimeError, OSError):
                self.view.message(
                    'Этап не завершён. Черновик сохранён; исправьте ввод или повторите проверку.'
                    if not self.installation()
                    else 'Этап не завершён. Данные, настройки и ключи сохранены.'
                )
                # Show only schema/preflight static safe messages, never SDK bodies.
                error = sys.exc_info()[1]
                if type(error) is RuntimeError or isinstance(error, schema.InvalidConfiguration):
                    self.view.message(str(error))
                try:
                    self.answer(
                        'Повторить шаг / назад / сохранить и выйти', ('повторить',), 'повторить'
                    )
                except Back:
                    if self.initialized():
                        return self.menu()
                    self.previous_step()
                except (SaveExit, Cancel, KeyboardInterrupt):
                    self.save()
                    self.view.message('Draft сохранён; повторите ui для продолжения.')
                    return 0
                continue
            self.step += 1
            self.save()
        self.view.message('Мастер завершён. Управление: sudo misbot ui')
        try:
            self.pause('Enter — закрыть мастер')
        except (Back, Cancel, SaveExit):
            pass
        return 0

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
        if item.path in ('storage s3 check', 'storage s3 connect'):
            return self.execute_s3(item)
        command = cli_commands.BY_PATH[item.path]
        self.view.heading(item.title, item.hint)
        args = {}
        execution_root = self.root
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
        if 'request' in command.arguments:
            self.run('federation requests')
        for key in command.arguments:
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
                default = ''
                self.view.message(
                    'Введите source-stopped после остановки источника и переключения DNS.'
                    if command.backend == 'restore-activate'
                    else 'Введите target-stopped после остановки назначения и возврата DNS.'
                )
            args[key] = self.input_value(
                label, default, lambda value: self.operation_argument(key, value, command.backend)
            )
        if command.backend in ('update', 'update-recover'):
            args['source'] = self.input_value(
                'Доверенный распакованный комплект, абсолютный путь',
                '',
                lambda value: self.operation_argument('source', value, command.backend),
            )
        if (
            command.backend == 'restore-activate'
            and self.installation().get('mas_mode') == 'external'
        ):
            self.view.heading('Подтвердить сохранность внешнего MAS')
            self.view.message(
                'Копия Misbot не содержит пользователей, устройств, ключей и данных внешнего MAS. '
                'Перед запуском Matrix сохраните или восстановите соответствующее состояние '
                'внешнего MAS из отдельной копии. Его учётные записи должны соответствовать '
                'восстановленной базе Matrix.',
                'yellow',
            )
            args['external_mas_confirm'] = self.input_value(
                'Введите preserved-state после проверки внешнего MAS',
                '',
                lambda value: self.operation_argument(
                    'external_mas_confirm', value, command.backend
                ),
            )
        if not item.read_only and command.path != 'remove':
            rows = [
                ('Операция', item.title),
                ('Установка', str(execution_root)),
                ('Перерыв в работе', command.stops),
                ('Данные', command.data),
            ]
            rows.extend(
                (ui_actions.ARGUMENTS[key][0], value)
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
            continuation = (
                'Продолжение: sudo misbot --directory '
                + shlex.quote(str(execution_root))
                + ' backup activate --confirm source-stopped'
            )
            if restored.get('mas_mode') == 'external':
                continuation += ' --external-mas-confirm preserved-state'
            completion.extend(
                [
                    'Копия восстановлена. Перед активацией остановите источник и переключите DNS.',
                    continuation,
                ]
            )
        if command.backend in ('update', 'release-update') and code == 0:
            completion.append(
                'Обновление применено. Откройте sudo misbot ui заново, чтобы использовать новый интерфейс.'
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
        if command.backend in ('update', 'release-update') and code == 0:
            if not getattr(self, '_result_waited', False):
                try:
                    self.pause('Enter — закрыть мастер')
                except (Back, Cancel, SaveExit):
                    pass
            raise SaveExit()
        return code

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

    def execute_s3(self, item):
        import tempfile
        import s3_config
        from configuration import directory, serialize

        self.view.heading(item.title)
        self.view.message('Введите параметры S3 здесь. Ключи доступа скрыты при вводе.')
        self.view.message('«Назад» отменяет ввод; Ctrl+C прерывает выполняемую операцию.', 'dim')
        purpose = 'media'
        if item.path == 'storage s3 check':
            choice = self.choose(
                'Что проверить?',
                [('1', 'Хранилище медиа', ''), ('2', 'Хранилище резервных копий', '')],
                aliases={'media': '1', 'backup': '2'},
            )
            if choice == '0':
                return 0
            purpose = 'media' if choice == '1' else 'backup'
        initial = self.model if purpose == 'media' else self.backup
        values = self.s3_profile(initial)
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
            profile = Path(folder) / 'profile.env'
            state_store.write_text(
                profile, serialize({key: values[key] for key in sorted(s3_config.FIELDS)})
            )
            args = {'config': profile}
            if item.path == 'storage s3 check':
                args.update(purpose=purpose, json=True)
            code = self.run(item.path, **args)
            records = getattr(self, 'operation_records', [])
            pending = next(
                (record for record in records if record.get('cleanup_pending') is True), None
            )
            if pending:
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
        if self.result_total > len(self.result_lines):
            self.view.message(
                'Список ограничен последними 1000 приглашениями. Для полного списка используйте CLI.',
                'yellow',
            )
        if not self.view.fullscreen or self.result_lines == ['Приглашений пока нет.']:
            for line in self.result_lines:
                self.view.message(line)
            return
        while True:
            self.view.heading(
                title,
                'Коды приглашений не отображаются.'
                + (
                    ' Показаны последние 1000 из ' + str(self.result_total) + '.'
                    if self.result_total > len(self.result_lines)
                    else ''
                ),
            )
            choice = self.choose(
                'Приглашения и статусы',
                [(str(index), line, '') for index, line in enumerate(self.result_lines, 1)],
            )
            if choice == '0':
                return
            self.view.heading('Приглашение')
            self.view.message(self.result_lines[int(choice) - 1])
            self.pause('Enter — вернуться к приглашениям')

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
            actions = (
                self.available_actions(group)
                if self.adopted()
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
            try:
                self.view.heading(title)
                if group == 'recovery':
                    self.view.message(
                        'Сначала проверьте состояние. Выберите продолжение, указанное сервером; не удаляйте сохранённые этапы операции.'
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
            except (Back, Cancel, KeyboardInterrupt):
                self.view.message('Ввод отменён; возвращаемся в главное меню.')
            except SaveExit:
                self.view.message('Выход из Misbot.')
                return 0
            except (RuntimeError, OSError, ValueError):
                self.view.message('Операция не завершена.', 'yellow')
                error = sys.exc_info()[1]
                if type(error) is RuntimeError or isinstance(error, schema.InvalidConfiguration):
                    self.view.message(str(error))
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

    def edit_fields(self, title, keys, section='', subtitle=''):
        before = {key: self.model[key] for key in keys}
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
            choice = self.choose(title, choices, aliases=aliases, keep_hints=True)
            if choice == '0':
                return None
            if choice == 'a':
                return {key: self.model[key] for key in keys if self.model[key] != before[key]}
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
        if self.adopted():
            names = {
                'HOMEPAGE_MODE': 'mode',
                'HOMEPAGE_URL': 'url',
                'SITE_NAME': 'name',
                'SITE_DESCRIPTION': 'description',
                'SITE_SUPPORT_URL': 'support-url',
            }
            code = self.run(
                'homepage apply', **{names[key]: value for key, value in changes.items()}
            )
            self.view.result('Сайт сервера', code, self.continuation())
        else:
            with (
                self.view.busy('Проверяем и применяем настройки сайта'),
                state_store.operation_guard(self.root),
            ):
                result = homepage.Backend(self.root).apply(changes)
            self.view.heading('Сайт сервера')
            self.view.message(
                'Настройки сайта применены.'
                if result['status'] == 'applied'
                else 'Настройки сайта уже действуют.'
                if result['status'] == 'unchanged'
                else 'Восстановлены прежние настройки сайта.',
                'green',
            )
            self.view.details([(schema.FIELDS[key].label, value) for key, value in changes.items()])
        self.pause('Enter — вернуться к меню')

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
        )
        if not changes:
            self.view.message('Изменения не применялись.')
            return
        schema.validate(self.model)
        self.view.heading('Применить настройки сервера', 'Проверьте выбранные изменения.')
        self.view.details(
            [
                (schema.FIELDS[key].label, before[key] + ' → ' + value)
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
            text(
                'Незавершённая публикация: sudo misbot --directory '
                + shlex.quote(str(ui.root))
                + ' config recover'
            )
            return 1
        if state.get('restore_pending'):
            return ui.menu()
        if state.get('databases_initialized'):
            if (ui.root / 'configuration-transition.json').exists():
                text(
                    'Конфигурация не опубликована полностью. Продолжение: sudo misbot --directory '
                    + shlex.quote(str(ui.root))
                    + ' config recover'
                )
                return 1
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
            'Не удалось безопасно открыть мастер. Сохраните файлы; проверьте misbot status и misbot config validate.'
        )
        raise SystemExit(1)
