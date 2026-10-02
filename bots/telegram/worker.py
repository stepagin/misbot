"""Telegram long polling with an internal readiness endpoint."""
from datetime import datetime, timedelta, timezone
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
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
        self.db = sqlite3.connect(Path(data_directory) / 'worker.sqlite3', check_same_thread=False)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS users (uid INTEGER PRIMARY KEY, server TEXT, waiting TEXT);
        ''')
        self.db_lock = threading.RLock()
        self.session = requests.Session()
        self.session.trust_env = False
        if config.get('proxy'):
            self.session.proxies = {'https': config['proxy']}

    def telegram(self, method, payload=None, timeout=25):
        r = self.session.post('https://api.telegram.org/bot' + self.config['token'] + '/' + method,
            json=payload or {}, timeout=timeout)
        if r.status_code != 200 or not r.json().get('ok'):
            raise BotError('Telegram временно отклонил запрос.')
        return r.json()['result']

    def servers(self):
        return bridge_call({'action': 'servers'})

    def selected(self, uid):
        servers = self.servers()
        name = self.config.get('server_name', 'misbot.ru')
        if name not in servers:
            raise BotError('Сервер этого бота временно недоступен.')
        return name

    def waiting(self, uid, value):
        selected = self.selected(uid)
        with self.db_lock:
            self.db.execute('INSERT INTO users VALUES (?,?,?) ON CONFLICT(uid) DO UPDATE SET waiting=excluded.waiting',
                            (uid, selected, value))
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
        text = (f'Misbot · @{identity["username"]}:{name}\n\n'
                f'Пригласи друга: один код — {policy["usage_limit"]} человек, срок {days:g} дней.')
        buttons = [[self.button('Создать приглашение', 'invite')],
                   [self.button('Мои приглашения', 'tokens|0')]]
        if identity.get('admin'):
            buttons.append([self.button('Федерация', 'federation')])
        self.send(chat, text, buttons)

    def login(self, chat, uid):
        auth = self.call('auth_start', uid)
        text = ('Войди в свой аккаунт Misbot, чтобы приглашать друзей.\n\n'
                'Код подтверждения: ' + auth['user_code'] +
                '\n\nПодтверждай только свой запрос. После входа вернись сюда и нажми «Я вошёл».')
        return self.send(chat, self.with_code(text, auth['user_code']),
            [[{'text': 'Войти в Misbot', 'url': auth['url']}],
             [self.button('Я вошёл', 'auth_check')]], parse_mode='HTML')

    def token_message(self, chat, name, item):
        attr = item['attributes']
        expiry = datetime.fromisoformat(attr['expires_at'].replace('Z', '+00:00')).astimezone(
            timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК') if attr.get('expires_at') else 'без срока'
        status = 'действует' if attr['valid'] else ('отозван' if attr.get('revoked_at') else 'использован или истёк')
        text = (f'Приглашение на {name}\n\n{attr["token"]}\n\nДо: {expiry}\n'
                f'Использований: {attr["times_used"]}/{attr.get("usage_limit")} · {status}\n\n'
                f'Передай другу этот код. В Element X укажи сервер {name}; при регистрации введи код.')
        buttons = [[{'text': 'Копировать код', 'copy_text': {'text': attr['token']}}]]
        if attr['valid']:
            buttons.append([self.button('Отозвать приглашение', 'revoke|' + item['id'])])
        self.send(chat, self.with_code(text, attr['token']), buttons, parse_mode='HTML')

    def federation(self, chat, uid):
        domains = self.call('federation', uid)
        text = 'Разрешённые серверы:\n' + '\n'.join(domains) if domains is not None else 'Сейчас федерация открыта для всех серверов.'
        text += ('\n\nЧтобы добавить сервер, нажми кнопку и пришли его домен (например matrix.org).\n'
                 'Удаление: /federation_remove домен\nИзменение кратко перезапускает Synapse.')
        self.send(chat, text, [[self.button('Добавить сервер', 'fed_add')]])

    def prepare(self, chat, uid, operation, target):
        result = self.call('fed_prepare', uid, operation=operation, domain=target)
        text = ('Добавить' if operation == 'add' else 'Удалить') + ' сервер ' + result['domain'] + '?'
        if result['currently_open']:
            text += '\nФедерация станет ограниченной: будут разрешены свой сервер и добавленный домен.'
        text += '\nSynapse кратко перезапустится. Подтверждение действует 5 минут.'
        self.send(chat, text, [[self.button('Подтвердить', 'fed_apply|' + result['confirmation'])],
                             [self.button('Отмена', 'cancel')]])

    def process(self, update):
        callback = update.get('callback_query')
        message = callback.get('message') if callback else update.get('message')
        sender = callback.get('from') if callback else (message or {}).get('from')
        if not message or not sender or sender.get('is_bot') or message.get('chat', {}).get('type') != 'private':
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
                return self.send(chat, 'Выход из бота выполнен. Для входа: /start.')
            if not self.call('identity', uid)['authenticated']:
                return self.login(chat, uid)
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
                return self.send(chat, 'Пока нет приглашений.' if not page['tokens'] else 'Твои приглашения, страница ' + str(offset // 5 + 1),
                                 [buttons] if buttons else None)
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
                return self.send(chat, 'Пришли домен Matrix сервера, например matrix.org. Отмена: /start')
            if command.startswith('fed_apply|'):
                self.send(chat, 'Применяю список серверов. Это займёт несколько секунд.')
                domains = self.call('fed_apply', uid, confirmation=command.split('|', 1)[1])
                return self.send(chat, 'Список применён:\n' + '\n'.join(domains))
            with self.db_lock:
                row = self.db.execute('SELECT waiting FROM users WHERE uid=?', (uid,)).fetchone()
            if row and row[0] and not callback and not text.startswith('/'):
                self.prepare(chat, uid, row[0], text)
                self.waiting(uid, None)
                return
            return self.menu(chat, uid)
        except (BotError, ValueError) as error:
            self.send(chat, str(error) if isinstance(error, BotError) else 'Не удалось разобрать команду. Начни /start.')

    def polling(self):
        while True:
            try:
                me = self.telegram('getMe')
                if me['username'].lower() != self.config['username'].lower() or str(me['id']) != str(self.config['client_id']):
                    raise RuntimeError('Bot and OIDC client identities do not match')
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
                updates = self.telegram('getUpdates', {'offset': offset, 'timeout': 25,
                    'allowed_updates': ['message', 'callback_query']}, timeout=40)
                for update in updates:
                    self.process(update)
                    offset = update['update_id'] + 1
                    with self.db_lock:
                        self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset',?)", (str(offset),))
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
            ready = True
        except Exception:
            ready = False
        body = json.dumps({'ok': ready}, separators=(',', ':')).encode()
        self.send_response(200 if ready else 503)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = read_json('/config/worker.json')
    bot = Bot(config)
    threading.Thread(target=bot.polling, daemon=True).start()
    with ThreadingHTTPServer(('0.0.0.0', 8080), Health) as server:
        server.config = config
        server.serve_forever()


if __name__ == '__main__':
    main()
