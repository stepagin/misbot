"""Telegram long polling with an internal readiness endpoint."""

import argparse
from datetime import datetime, timedelta, timezone
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

import requests

from common import BotError, bridge_call, read_json

LOG = logging.getLogger('worker')


class Bot:
    def __init__(self, config, data_directory='/data'):
        self.config = config
        self.last_poll_success = 0
        self.db = sqlite3.connect(Path(data_directory) / 'worker.sqlite3', check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS users (uid INTEGER PRIMARY KEY, server TEXT, waiting TEXT);
        """)
        self.db_lock = threading.RLock()
        self.session = requests.Session()
        self.session.trust_env = False
        if config.get('proxy'):
            self.session.proxies = {'https': config['proxy']}

    def telegram(self, method, payload=None, timeout=25):
        r = self.session.post(
            'https://api.telegram.org/bot' + self.config['token'] + '/' + method,
            json=payload or {},
            timeout=timeout,
        )
        if r.status_code != 200 or not r.json().get('ok'):
            raise BotError('Telegram не подтвердил запрос. Повторите позже.')
        return r.json()['result']

    def servers(self):
        return bridge_call({'action': 'servers'})

    def selected(self, uid):
        servers = self.servers()
        name = self.config.get('server_name', 'misbot.ru')
        if name not in servers:
            raise BotError(
                'Сервер этого бота не найден в списке подключённых. Обратитесь к администратору.'
            )
        return name

    def waiting(self, uid, value):
        selected = self.selected(uid)
        with self.db_lock:
            self.db.execute(
                'INSERT INTO users VALUES (?,?,?) ON CONFLICT(uid) DO UPDATE SET waiting=excluded.waiting',
                (uid, selected, value),
            )
            self.db.commit()

    def call(self, action, uid, **extra):
        return bridge_call({'action': action, 'uid': uid, 'server': self.selected(uid), **extra})

    def send(self, chat, text, buttons=None, parse_mode=None):
        payload = {'chat_id': chat, 'text': text, 'link_preview_options': {'is_disabled': True}}
        if parse_mode:
            payload['parse_mode'] = parse_mode
        if buttons:
            payload['reply_markup'] = {'inline_keyboard': buttons}
        response = self.telegram('sendMessage', payload)
        code_count = sum(x.get('type') == 'code' for x in response.get('entities', []))
        LOG.info('Telegram reply accepted code_entities=%s', code_count)
        return response

    @staticmethod
    def button(text, action):
        return {'text': text, 'callback_data': action}

    @staticmethod
    def with_code(text, code):
        escaped = html.escape(code)
        return html.escape(text).replace(escaped, '<code>' + escaped + '</code>', 1)

    def menu(self, chat, uid):
        identity = self.call('identity', uid)
        if not identity['authenticated']:
            return self.login(chat, uid)
        name = self.selected(uid)
        policy = self.servers()[name]
        days = policy['lifetime_seconds'] / 86400
        text = (
            f'Misbot · @{identity["username"]}:{name}\n\n'
            f'Регистраций на код: {policy["usage_limit"]}. Срок: {days:g} дн.'
        )
        buttons = [
            [self.button('Создать приглашение', 'invite')],
            [self.button('Мои приглашения', 'tokens|0')],
        ]
        if identity.get('admin'):
            if policy.get('federation_enabled', True):
                buttons.append([self.button('Федерация', 'federation')])
            actions = policy.get('management_actions', [])
            for operation, title in (
                ('status', 'Состояние служб'),
                ('check', 'Проверить сервер'),
                ('start', 'Запустить службы'),
                ('stop', 'Остановить службы'),
                ('storage', 'Хранилища и копии'),
                ('components', 'Компоненты и веб-панель'),
                ('storage_connect', 'Подключить S3 вложений'),
                ('backup', 'Создать резервную копию'),
                ('backup_verify', 'Проверить резервную копию'),
            ):
                if operation in actions:
                    buttons.append([self.button(title, 'manage|' + operation)])
        self.send(chat, text, buttons)

    def login(self, chat, uid):
        auth = self.call('auth_start', uid)
        text = (
            'Войдите в свой Matrix-аккаунт. Управление сервером доступно администраторам.\n\n'
            'Код подтверждения: '
            + auth['user_code']
            + '\n\nПодтверждайте только свой запрос. После входа вернитесь в чат и нажмите «Проверить вход».'
        )
        return self.send(
            chat,
            self.with_code(text, auth['user_code']),
            [
                [{'text': 'Войти в Misbot', 'url': auth['url']}],
                [self.button('Проверить вход', 'auth_check')],
            ],
            parse_mode='HTML',
        )

    def token_message(self, chat, name, item):
        attr = item['attributes']
        expiry = (
            datetime.fromisoformat(attr['expires_at'].replace('Z', '+00:00'))
            .astimezone(timezone(timedelta(hours=3)))
            .strftime('%d.%m.%Y %H:%M МСК')
            if attr.get('expires_at')
            else 'без срока действия'
        )
        status = (
            'действует'
            if attr['valid']
            else ('отозван' if attr.get('revoked_at') else 'использован или истёк')
        )
        text = (
            f'Приглашение в {name}\n\nКод: {attr["token"]}\n\nСрок действия: {expiry}\n'
            f'Регистраций: {attr["times_used"]}/{attr.get("usage_limit")} · {status}\n\n'
            f'Передайте другу этот код. В Element X укажите сервер {name}. При регистрации введите код приглашения.'
        )
        buttons = [[{'text': 'Копировать код', 'copy_text': {'text': attr['token']}}]]
        if attr['valid']:
            buttons.append([self.button('Отозвать приглашение', 'revoke|' + item['id'])])
        self.send(chat, self.with_code(text, attr['token']), buttons, parse_mode='HTML')

    def federation(self, chat, uid):
        domains = self.call('federation', uid)
        text = (
            'Разрешённые серверы:\n' + '\n'.join(domains)
            if domains is not None
            else 'Сейчас федерация открыта для всех серверов.'
        )
        text += (
            '\n\nЧтобы добавить сервер, нажмите «Добавить сервер» и отправьте его домен, например matrix.org.\n'
            'Удаление: /federation_remove домен\nИзменение кратко перезапускает Synapse.'
        )
        self.send(chat, text, [[self.button('Добавить сервер', 'fed_add')]])

    def prepare(self, chat, uid, operation, target):
        result = self.call('fed_prepare', uid, operation=operation, domain=target)
        text = (
            ('Добавить' if operation == 'add' else 'Удалить') + ' сервер ' + result['domain'] + '?'
        )
        if result['currently_open']:
            text += (
                '\nФедерация станет ограниченной: будут разрешены свой сервер и добавленный домен.'
            )
        text += '\nПеред применением будет проверена резервная копия. Synapse кратко перезапустится. Подтверждение действует 5 минут.'
        self.send(
            chat,
            text,
            [
                [self.button('Подтвердить', 'fed_apply|' + result['confirmation'])],
                [self.button('Отмена', 'cancel')],
            ],
        )

    def manage(self, chat, uid, operation):
        if operation == 'components':
            result = self.call('manage_read', uid, operation=operation)
            data = result.get('components', {})
            text = 'Компоненты Misbot: ' + (
                'подключены' if data.get('enabled') else 'не подтверждены'
            )
            text += '\nРеферальный Matrix-бот: ' + (
                'подключён' if data.get('referral_bot') else 'не подключён'
            )
            text += '\nПанель администратора Misbot: ' + (
                'подключён' if data.get('federation_bot') else 'не подключён'
            )
            if data.get('admin'):
                text += '\nАдминистративная панель: https://' + data['admin'] + '/'
            if data.get('homepage'):
                text += '\nСайт: https://' + data['homepage'] + '/'
            if not result.get('ok') or data.get('pending'):
                text += '\nПодключение не завершено. Проверьте misbot features status.'
            return self.send(chat, text)
        if operation == 'storage':
            result = self.call('manage_read', uid, operation=operation)
            data = result.get('storage', {})
            text = (
                'Вложения в S3: '
                + ('подключены' if data.get('media_s3') else 'не подключены')
                + '\nРезервные копии в S3: '
                + ('подключены' if data.get('backup_s3') else 'не подключены')
                + '\nПоследняя внешняя копия: '
                + ('проверена' if data.get('last_backup_verified') else 'не подтверждена')
            )
            if data.get('media_pending'):
                text += '\nПеренос вложений в S3 не завершён. Через SSH выполните misbot storage recover.'
            if not result['ok']:
                text += '\nСостояние не подтверждено. Через SSH выполните misbot storage status.'
            return self.send(chat, text)
        if operation in ('status', 'check'):
            result = self.call('manage_read', uid, operation=operation)
            text = 'Состояние служб:' if operation == 'status' else 'Проверка сервера:'
            for item in result.get('containers' if operation == 'status' else 'checks', []):
                good = (
                    item['running'] and item['healthy'] if operation == 'status' else item['passed']
                )
                text += '\n' + ('Проверено: ' if good else 'Нужна проверка: ') + item['name']
            if result.get('backup_pending'):
                text += '\nРезервная копия не завершена. Через SSH выполните misbot backup recover.'
            if not result['ok']:
                text += (
                    '\nПроверка не подтверждена. Через SSH выполните misbot status и misbot check.'
                )
            return self.send(chat, text)
        confirmation = self.call('manage_prepare', uid, operation=operation)
        text = {
            'start': 'Запустить службы?\nИзменённые службы могут перезапуститься.',
            'stop': 'Остановить службы Matrix?\nСервер будет недоступен до запуска служб.',
            'backup_verify': 'Проверить последнюю резервную копию?\nДля проверки будут восстановлены временные базы. Службы продолжат работу.',
            'storage_connect': 'Подключить S3 вложений из профиля, заполненного через SSH?\nSynapse кратко перезапустится. Локальные файлы сохранятся.',
            'backup': 'Создать согласованную резервную копию?\nSynapse и MAS временно остановятся. Если S3 подключён, полный архив отправится туда зашифрованным.',
        }.get(operation, 'Выполнить операцию управления?')
        return self.send(
            chat,
            text + '\nПодтверждение действует 5 минут.',
            [
                [self.button('Подтвердить', 'manage_apply|' + confirmation['confirmation'])],
                [self.button('Отмена', 'cancel')],
            ],
        )

    def job_message(self, chat, job):
        if job['state'] == 'running':
            return self.send(
                chat,
                'Операция выполняется. Нажмите «Проверить результат», чтобы узнать её состояние.',
                [[self.button('Проверить результат', 'manage_job|' + job['job'])]],
            )
        if job['state'] == 'finished' and job['result'].get('ok'):
            return self.send(
                chat,
                {
                    'backup': 'Резервная копия создана.',
                    'start': 'Службы запущены.',
                    'stop': 'Службы остановлены.',
                    'backup_verify': 'Резервная копия проверена.',
                    'storage_connect': 'S3 вложений подключён.',
                }.get(job['operation'], 'Операция завершена.'),
            )
        return self.send(
            chat,
            'Завершение операции не подтверждено. Через SSH выполните misbot status. '
            'Если резервная копия не завершена, выполните misbot backup recover.',
        )

    def process(self, update):
        callback = update.get('callback_query')
        message = callback.get('message') if callback else update.get('message')
        sender = callback.get('from') if callback else (message or {}).get('from')
        if (
            not message
            or not sender
            or sender.get('is_bot')
            or message.get('chat', {}).get('type') != 'private'
        ):
            return
        chat, uid = message['chat']['id'], sender['id']
        if chat != uid:
            return
        if callback:
            try:
                self.telegram('answerCallbackQuery', {'callback_query_id': callback['id']})
            except BotError:
                pass
        text = callback.get('data', '') if callback else message.get('text', '').strip()
        command, _, argument = text.partition(' ')
        command = command.split('@')[0]
        try:
            if command in ('/start', '/help', 'cancel', 'auth_check'):
                self.waiting(uid, None)
                return self.menu(chat, uid)
            if command in ('/auth', 'auth'):
                return self.menu(chat, uid)
            if command == '/logout':
                self.call('logout', uid)
                return self.send(chat, 'Вы вышли из бота. Для входа отправьте /start.')
            if not self.call('identity', uid)['authenticated']:
                return self.login(chat, uid)
            if command in (
                '/status',
                '/check',
                '/start_services',
                '/stop_services',
                '/backup',
                '/backup_verify',
                '/storage',
            ) or command.startswith('manage|'):
                operation = {
                    '/status': 'status',
                    '/check': 'check',
                    '/start_services': 'start',
                    '/stop_services': 'stop',
                    '/backup': 'backup',
                    '/backup_verify': 'backup_verify',
                    '/storage': 'storage',
                }.get(command)
                return self.manage(chat, uid, operation or command.split('|', 1)[1])
            if command.startswith('manage_apply|'):
                return self.job_message(
                    chat, self.call('manage_apply', uid, confirmation=command.split('|', 1)[1])
                )
            if command.startswith('manage_job|'):
                return self.job_message(
                    chat, self.call('manage_job', uid, job=command.split('|', 1)[1])
                )
            if command in ('/invite', 'invite'):
                item = self.call('invite', uid, request_id='telegram:' + str(update['update_id']))
                return self.token_message(chat, self.selected(uid), item)
            if command == '/tokens' or command.startswith('tokens|'):
                offset = int(command.split('|')[1]) if '|' in command else 0
                page = self.call('tokens', uid, offset=offset)
                for item in page['tokens']:
                    self.token_message(chat, self.selected(uid), item)
                buttons = []
                if offset:
                    buttons.append(self.button('Назад', 'tokens|' + str(max(0, offset - 5))))
                if page['has_more']:
                    buttons.append(self.button('Далее', 'tokens|' + str(offset + 5)))
                return self.send(
                    chat,
                    'Пока нет приглашений.'
                    if not page['tokens']
                    else 'Ваши приглашения. Страница ' + str(offset // 5 + 1),
                    [buttons] if buttons else None,
                )
            if command.startswith('revoke|'):
                self.call('revoke', uid, resource=command.split('|', 1)[1])
                return self.send(chat, 'Приглашение отозвано.')
            if command in ('/federation', 'federation'):
                return self.federation(chat, uid)
            if command in ('/federation_add', '/federation_remove', 'fed_add'):
                operation = 'remove' if command == '/federation_remove' else 'add'
                if argument:
                    return self.prepare(chat, uid, operation, argument)
                self.call('federation', uid)
                self.waiting(uid, operation)
                return self.send(
                    chat, 'Отправьте домен Matrix-сервера, например matrix.org. Отмена: /start.'
                )
            if command.startswith('fed_apply|'):
                self.send(chat, 'Подтверждение получено. Передаю изменение серверу.')
                domains = self.call('fed_apply', uid, confirmation=command.split('|', 1)[1])
                if isinstance(domains, dict) and isinstance(domains.get('operation_id'), str):
                    return self.send(
                        chat,
                        'Операция принята. Идентификатор: '
                        + domains['operation_id']
                        + '\nРезультат доступен в административном Matrix-боте и через misbot federation operations.',
                    )
                return self.send(
                    chat, 'Список разрешённых серверов сохранён:\n' + '\n'.join(domains)
                )
            with self.db_lock:
                row = self.db.execute('SELECT waiting FROM users WHERE uid=?', (uid,)).fetchone()
            if row and row[0] and not callback and not text.startswith('/'):
                self.prepare(chat, uid, row[0], text)
                self.waiting(uid, None)
                return
            return self.menu(chat, uid)
        except (BotError, ValueError) as error:
            self.send(
                chat,
                str(error)
                if isinstance(error, BotError)
                else 'Не удалось распознать команду. Откройте меню: /start.',
            )

    def polling(self):
        while True:
            try:
                me = self.telegram('getMe')
                if me['username'].lower() != self.config['username'].lower() or str(
                    me['id']
                ) != str(self.config['client_id']):
                    raise RuntimeError('Telegram bot identities do not match')
                hook = self.telegram('getWebhookInfo')
                if hook.get('url'):
                    raise RuntimeError('Existing webhook must be reviewed before enabling polling')
                with self.db_lock:
                    row = self.db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
                if row:
                    offset = int(row[0])
                else:
                    # Do not reply to conversations from before the service was installed.
                    backlog = self.telegram('getUpdates', {'offset': -1, 'timeout': 0})
                    offset = backlog[-1]['update_id'] + 1 if backlog else 0
                LOG.info('Telegram polling started username=%s', me['username'])
                break
            except Exception as error:
                LOG.error('poll initialization failed type=%s', type(error).__name__)
                time.sleep(15)
        while True:
            try:
                updates = self.telegram(
                    'getUpdates',
                    {
                        'offset': offset,
                        'timeout': 25,
                        'allowed_updates': ['message', 'callback_query'],
                    },
                    timeout=40,
                )
                self.last_poll_success = time.monotonic()
                for update in updates:
                    self.process(update)
                    offset = update['update_id'] + 1
                    with self.db_lock:
                        self.db.execute(
                            "INSERT OR REPLACE INTO state VALUES ('offset',?)", (str(offset),)
                        )
                        self.db.commit()
            except Exception as error:
                LOG.error('poll operation failed type=%s', type(error).__name__)
                time.sleep(5)


class Health(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != '/health':
            self.send_error(404)
            return
        try:
            bridge_call({'action': 'servers'}, timeout=2)
            ready = bool(
                self.server.bot.last_poll_success
                and time.monotonic() - self.server.bot.last_poll_success < 70
            )
        except Exception:
            ready = False
        body = json.dumps({'ok': ready}, separators=(',', ':')).encode()
        self.send_response(200 if ready else 503)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/config/worker.json')
    parser.add_argument('--data', default='/data')
    parser.add_argument('--health-address', default='0.0.0.0')
    parser.add_argument('--health-port', type=int, default=8080)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = read_json(args.config)
    bot = Bot(config, args.data)
    threading.Thread(target=bot.polling, daemon=True).start()
    with ThreadingHTTPServer((args.health_address, args.health_port), Health) as server:
        server.config = config
        server.bot = bot
        server.serve_forever()


if __name__ == '__main__':
    main()
