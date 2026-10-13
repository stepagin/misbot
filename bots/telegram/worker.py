"""Telegram long polling with an internal readiness endpoint."""

import argparse
from datetime import datetime, timedelta, timezone
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import sqlite3
import secrets
import threading
import time

import requests

from common import BotError, bridge_call, domain, read_json

LOG = logging.getLogger('worker')


class TelegramError(BotError):
    def __init__(self, code, description, retry_after=0):
        super().__init__('Telegram не подтвердил запрос. Повторите позже.')
        self.retry_after = retry_after if isinstance(retry_after, (int, float)) else 0
        self.code = code
        self.unchanged = 'message is not modified' in description.lower()
        self.permanent = code in (400, 403, 404)
        self.uneditable = code == 400 and any(
            reason in description.lower()
            for reason in (
                'message to edit not found',
                "message can't be edited",
                'message_id_invalid',
            )
        )


class Bot:
    def __init__(self, config, data_directory='/data'):
        if not isinstance(config, dict) or 'servers' in config:
            raise BotError('В worker.json нужно указать один сервер в поле server_name.')
        try:
            self._server_name = domain(config.get('server_name'))
        except BotError:
            raise BotError(
                'В worker.json нужно указать один домен сервера в поле server_name.'
            ) from None
        self.config = {**config, 'server_name': self._server_name}
        self.last_poll_success = 0
        self.db = sqlite3.connect(Path(data_directory) / 'worker.sqlite3', check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS users (uid INTEGER PRIMARY KEY, server TEXT, waiting TEXT);
        """)
        self.db_lock = threading.RLock()
        try:
            self.pin_server()
        except (BotError, sqlite3.Error):
            self.db.close()
            raise
        self.session = requests.Session()
        self.session.trust_env = False
        if config.get('proxy'):
            self.session.proxies = {'https': config['proxy']}

    def pin_server(self, confirmed=False):
        """Check saved ownership locally; create the first pin after bridge verification."""
        with self.db_lock:
            row = self.db.execute("SELECT value FROM state WHERE key='server_binding'").fetchone()
            if row:
                try:
                    saved = json.loads(row[0])
                except (ValueError, TypeError):
                    raise BotError(
                        'Сохранённая привязка Telegram-бота не распознана. Данные сохранены.'
                    ) from None
                if (
                    not isinstance(saved, dict)
                    or type(saved.get('format')) is not int
                    or saved['format'] != 1
                ):
                    raise BotError(
                        'Формат привязки Telegram-бота не поддерживается. Данные сохранены.'
                    )
                if saved.get('server_name') != self.server_name:
                    raise BotError(
                        'Сохранённый Telegram-бот привязан к другому серверу. Проверьте worker.json и каталог данных.'
                    )
                return
            legacy = {
                value[0]
                for value in self.db.execute('SELECT DISTINCT server FROM users')
                if value[0] and value[0].strip()
            }
            if legacy and self.server_name not in legacy:
                raise BotError(
                    'Сохранённые данные Telegram-бота относятся к другому серверу. Проверьте привязку; данные не изменены.'
                )
            if not confirmed:
                return
            self.db.execute(
                "INSERT INTO state VALUES ('server_binding',?)",
                (json.dumps({'format': 1, 'server_name': self.server_name}),),
            )
            self.db.commit()

    def telegram(self, method, payload=None, timeout=25):
        r = self.session.post(
            'https://api.telegram.org/bot' + self.config['token'] + '/' + method,
            json=payload or {},
            timeout=timeout,
        )
        data = r.json()
        if r.status_code != 200 or not data.get('ok'):
            raise TelegramError(
                data.get('error_code', r.status_code),
                data.get('description', ''),
                data.get('parameters', {}).get('retry_after', 0),
            )
        return data['result']

    @property
    def server_name(self):
        return self._server_name

    def servers(self, timeout=None):
        result = (
            bridge_call({'action': 'servers'}, timeout=timeout)
            if timeout is not None
            else bridge_call({'action': 'servers'})
        )
        if (
            not isinstance(result, dict)
            or set(result) != {self.server_name}
            or not isinstance(result[self.server_name], dict)
        ):
            raise BotError(
                'Привязка бота к серверу не подтверждена. Проверьте worker.json и управляющую службу.'
            )
        self.pin_server(confirmed=True)
        return result

    def selected(self, uid):
        self.servers()
        return self.server_name

    def waiting(self, uid, value):
        selected = self.selected(uid)
        with self.db_lock:
            self.db.execute(
                'INSERT INTO users VALUES (?,?,?) ON CONFLICT(uid) DO UPDATE SET server=excluded.server,waiting=excluded.waiting',
                (uid, selected, value),
            )
            self.db.commit()

    def call(self, action, uid, **extra):
        return bridge_call({'action': action, 'uid': uid, **extra, 'server': self.selected(uid)})

    def send(self, chat, text, buttons=None, parse_mode=None):
        payload = {'chat_id': chat, 'text': text, 'link_preview_options': {'is_disabled': True}}
        if parse_mode:
            payload['parse_mode'] = parse_mode
        if buttons:
            payload['reply_markup'] = {'inline_keyboard': buttons}
        response = self.deliver('sendMessage', payload)
        code_count = sum(x.get('type') == 'code' for x in response.get('entities', []))
        LOG.info('Telegram reply accepted code_entities=%s', code_count)
        return response

    def state(self, key, value=None):
        with self.db_lock:
            if value is not None:
                self.db.execute(
                    'INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value))
                )
                self.db.commit()
                return value
            row = self.db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def screen(self, chat, text, buttons=None, parse_mode=None, key=None, force_refresh=False):
        """Keep navigation separate from shareable codes and operation results."""
        key = key or 'navigation:' + str(chat)
        current = self.state(key)
        payload = {
            'chat_id': chat,
            'text': text,
            'reply_markup': {'inline_keyboard': buttons or []},
            'link_preview_options': {'is_disabled': True},
        }
        if parse_mode:
            payload['parse_mode'] = parse_mode
        delivery_key = 'delivery:screen:' + key
        self.delete_state(delivery_key)
        if current and current['payload'] == payload and not force_refresh:
            return {'message_id': current['message_id']}
        method = 'editMessageText' if current else 'sendMessage'
        if current:
            payload['message_id'] = current['message_id']
        return self.deliver(method, payload, navigation=key, identifier=delivery_key)

    def delete_state(self, key):
        with self.db_lock:
            self.db.execute('DELETE FROM state WHERE key=?', (key,))
            self.db.commit()

    def finish_delivery(self, key, item):
        if key.startswith('delivery:notification:'):
            self.state(key, item)
        else:
            if item['status'] == 'failed':
                failures = self.state('delivery_failures') or []
                failures.append(
                    {
                        'chat': item['payload'].get('chat_id'),
                        'time': time.time(),
                        'attempts': item['attempts'],
                        'error': item['error'],
                    }
                )
                self.state('delivery_failures', failures[-100:])
            self.delete_state(key)

    def deliver(self, method, payload, navigation=None, identifier=None, timeout=25):
        """Persist delivery before I/O; transport failures never replay the command."""
        key = identifier or 'delivery:' + secrets.token_hex(16)
        item = self.state(key)
        if item is None:
            item = {
                'method': method,
                'payload': payload,
                'navigation': navigation,
                'status': 'pending',
                'attempts': 0,
                'next_attempt': 0,
            }
            self.state(key, item)
        return self.deliver_pending(key, item, timeout=timeout)

    def deliver_pending(self, key, item, timeout=25):
        if item['status'] != 'pending' or item['next_attempt'] > time.time():
            return item.get('response', {})
        item['attempts'] += 1
        try:
            try:
                response = self.telegram(item['method'], item['payload'], timeout=timeout)
            except TelegramError as error:
                if error.unchanged and item['method'] == 'editMessageText':
                    response = {'message_id': item['payload']['message_id']}
                elif error.uneditable and item['method'] == 'editMessageText':
                    item['method'] = 'sendMessage'
                    item['payload'].pop('message_id', None)
                    self.state(key, item)
                    response = self.telegram(item['method'], item['payload'], timeout=timeout)
                else:
                    raise
        except (TelegramError, requests.RequestException, ValueError) as error:
            permanent = isinstance(error, TelegramError) and error.permanent
            item['status'] = 'failed' if permanent or item['attempts'] >= 8 else 'pending'
            retry_after = error.retry_after if isinstance(error, TelegramError) else 0
            item['next_attempt'] = time.time() + max(
                retry_after, min(3600, 5 * 2 ** (item['attempts'] - 1))
            )
            item['error'] = type(error).__name__
            if item['status'] == 'failed':
                self.finish_delivery(key, item)
            else:
                self.state(key, item)
            LOG.warning('Telegram delivery status=%s type=%s', item['status'], item['error'])
            return {}
        item['status'] = 'sent'
        item['response'] = {'message_id': response.get('message_id')}
        self.finish_delivery(key, item)
        if item.get('navigation') and response.get('message_id'):
            payload = {k: v for k, v in item['payload'].items() if k != 'message_id'}
            self.state(
                item['navigation'], {'message_id': response['message_id'], 'payload': payload}
            )
        return response

    def retry_deliveries(self):
        # Limit each pass so offline recipients cannot monopolize long polling.
        with self.db_lock:
            rows = self.db.execute(
                "SELECT key,value FROM state WHERE key LIKE 'delivery:%' "
                "AND key NOT LIKE 'delivery:notification:%' "
                "AND json_extract(value, '$.next_attempt')<=? ORDER BY rowid LIMIT 2",
                (time.time(),),
            ).fetchall()
        for key, value in rows:
            self.deliver_pending(key, json.loads(value), timeout=5)

    def back(self, action='menu', title='Назад'):
        return [self.button(title, action)]

    def policy(self, uid):
        return self.servers()[self.selected(uid)]

    @staticmethod
    def button(text, action):
        return {'text': text, 'callback_data': action}

    @staticmethod
    def with_code(text, code):
        escaped = html.escape(code)
        return html.escape(text).replace(escaped, '<code>' + escaped + '</code>', 1)

    def menu(self, chat, uid, force_refresh=False):
        identity = self.call('identity', uid)
        if not identity['authenticated']:
            return self.login(chat, uid, force_refresh=force_refresh)
        name = self.selected(uid)
        policy = self.policy(uid)
        if not identity.get('admin'):
            return self.invitations(chat, uid, force_refresh=force_refresh)
        actions = policy.get('management_actions', [])
        buttons = []
        for operation, title in (
            ('statistics', 'Статистика'),
            ('status', 'Статус служб'),
            ('check', 'Проверить сервер'),
        ):
            if operation in actions:
                buttons.append([self.button(title, 'manage|' + operation)])
        if policy.get('federation_enabled', True):
            buttons.append([self.button('Федерация', 'federation')])
        if any(
            operation in actions
            for operation in (
                'start',
                'stop',
                'restart',
                'update_check',
                'update',
                'backup',
                'backup_verify',
            )
        ):
            buttons.append([self.button('Обслуживание', 'maintenance')])
        buttons.append([self.button('Ещё', 'more')])
        return self.screen(
            chat,
            f'Misbot · {name}\n\nУправление сервером. Выберите статистику, статус служб или проверку доступности.',
            buttons,
            force_refresh=force_refresh,
        )

    def invitations(self, chat, uid, force_refresh=False):
        policy = self.policy(uid)
        days = policy['lifetime_seconds'] / 86400
        return self.screen(
            chat,
            f'Приглашения в {self.selected(uid)}\n\nРегистраций на код: {policy["usage_limit"]}. Срок: {days:g} дн.',
            [
                [self.button('Создать приглашение', 'invite')],
                [self.button('Мои приглашения', 'tokens|0')],
                self.back('more'),
            ],
            force_refresh=force_refresh,
        )

    def group(self, chat, uid, group):
        identity = self.call('identity', uid)
        if not identity.get('admin') and group != 'more':
            raise BotError('Управление сервером доступно администраторам.')
        actions = self.policy(uid).get('management_actions', [])
        groups = {
            'maintenance': (
                'Обслуживание',
                [
                    ('start', 'Запустить службы'),
                    ('stop', 'Остановить службы'),
                    ('restart', 'Перезапустить службы'),
                ],
            ),
            'backups': (
                'Резервные копии',
                [
                    ('storage', 'Состояние хранилищ и копий'),
                    ('backup', 'Создать резервную копию'),
                    ('backup_verify', 'Проверить последнюю копию'),
                ],
            ),
            'updates': (
                'Обновления',
                [('update_check', 'Проверить обновления'), ('components', 'Состояние компонентов')],
            ),
            'more': (
                'Ещё',
                [
                    ('storage', 'Хранилища'),
                    ('components', 'Компоненты и веб-панель'),
                    ('storage_connect', 'Подключить S3 вложений'),
                ],
            ),
        }
        title, items = groups[group]
        buttons = [
            [self.button(label, 'manage|' + operation)]
            for operation, label in items
            if identity.get('admin') and operation in actions
        ]
        if group == 'maintenance':
            if any(operation in actions for operation in ('storage', 'backup', 'backup_verify')):
                buttons.append([self.button('Резервные копии', 'backups')])
            if any(operation in actions for operation in ('components', 'update_check', 'update')):
                buttons.append([self.button('Обновления', 'updates')])
        if group == 'more':
            buttons.insert(0, [self.button('Приглашения', 'invitations')])
        buttons.append(self.back('maintenance' if group in ('backups', 'updates') else 'menu'))
        return self.screen(chat, title + '\n\nВыберите действие.', buttons)

    def login(self, chat, uid, force_refresh=False):
        auth = self.call('auth_start', uid)
        text = (
            'Войдите в свой Matrix-аккаунт. Управление сервером доступно администраторам.\n\n'
            'Код подтверждения: '
            + auth['user_code']
            + '\n\nПодтверждайте только свой запрос. После входа вернитесь в чат и нажмите «Проверить вход».'
        )
        return self.screen(
            chat,
            self.with_code(text, auth['user_code']),
            [
                [{'text': 'Войти в Misbot', 'url': auth['url']}],
                [self.button('Проверить вход', 'auth_check')],
            ],
            parse_mode='HTML',
            force_refresh=force_refresh,
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
        buttons = [
            [{'text': 'Копировать код', 'copy_text': {'text': attr['token']}}],
            [
                {
                    'text': 'Element X · Android',
                    'url': 'https://play.google.com/store/apps/details?id=io.element.android.x',
                },
                {
                    'text': 'Element X · iPhone',
                    'url': 'https://apps.apple.com/app/element-x-secure-chat-call/id1631335820',
                },
            ],
        ]
        if attr['valid']:
            buttons.append([self.button('Отозвать приглашение', 'revoke|' + item['id'])])
        self.send(chat, self.with_code(text, attr['token']), buttons, parse_mode='HTML')

    def federation(self, chat, uid):
        api = self.call('fed_read', uid, operation='api-status')
        requests = self.call('fed_read', uid, operation='requests')['requests']
        pending = sum(
            item['direction'] == 'incoming' and item['state'] not in ('rejected', 'expired')
            for item in requests
        )
        return self.screen(
            chat,
            'Федерация\n\nВходящих актуальных заявок: '
            + str(pending)
            + '\nMisbot API: '
            + ('включён' if api['enabled'] else 'выключен')
            + '\n\nПриглашение действует 30 дней. Новое приглашение можно отправить не чаще раза в минуту.',
            [
                [self.button('Пригласить сервер', 'fed_invite')],
                [self.button('Входящие заявки', 'fed_requests|incoming|0')],
                [self.button('Отправленные приглашения', 'fed_requests|outgoing|0')],
                [self.button('Отклонённые заявки', 'fed_requests|rejected|0')],
                [self.button('Серверы федерации', 'fed_servers')],
                [self.button('Настройки API', 'fed_api')],
                self.back(),
            ],
        )

    def federation_requests(self, chat, uid, view, offset=0):
        records = self.call('fed_read', uid, operation='requests')['requests']
        rejected = ('rejected', 'expired')
        records = [
            item
            for item in records
            if (
                item['state'] in rejected
                if view == 'rejected'
                else item['direction'] == view and item['state'] not in rejected
            )
        ]
        page = records[offset : offset + 5]
        title = {
            'incoming': 'Входящие заявки',
            'outgoing': 'Отправленные приглашения',
            'rejected': 'Отклонённые заявки',
        }[view]
        buttons = [[self.button(item['server'], 'fed_card|' + item['id'])] for item in page]
        paging = []
        if offset:
            paging.append(self.button('Предыдущие', f'fed_requests|{view}|{max(0, offset - 5)}'))
        if offset + 5 < len(records):
            paging.append(self.button('Следующие', f'fed_requests|{view}|{offset + 5}'))
        if paging:
            buttons.append(paging)
        if view == 'rejected' and records:
            buttons.append([self.button('Очистить отклонённые', 'fed_prepare|clear')])
        buttons.append(self.back('federation'))
        return self.screen(
            chat, title + ('\n\nВыберите заявку.' if page else '\n\nЗаявок нет.'), buttons
        )

    @staticmethod
    def date_text(value):
        if isinstance(value, (int, float)):
            date = datetime.fromtimestamp(value, timezone.utc)
        else:
            date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return date.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M МСК')

    def federation_card(self, chat, uid, request_id):
        records = self.call('fed_read', uid, operation='requests')['requests']
        item = next((record for record in records if record['id'] == request_id), None)
        if not item:
            raise BotError(
                'Заявка уже закрыта или сервер добавлен в федерацию. Откройте список заново.'
            )
        state = item['state']
        inactive = state in ('rejected', 'expired')
        text = ('Заявка от ' if item['direction'] == 'incoming' else 'Приглашение для ') + item[
            'server'
        ]
        text += '\n\nСостояние: ' + {
            'pending': 'ожидает решения',
            'received': 'ожидает решения',
            'invited': 'ожидает решения',
            'rejected': 'отклонена',
            'expired': 'срок истёк',
            'accepted': 'одобрена',
            'applying': 'правила федерации применяются',
            'partially_applied': 'правила применены частично',
            'approving': 'принятие выполняется',
            'accepting': 'принятие выполняется',
        }.get(state, 'не подтверждено')
        if item.get('expires_at'):
            text += '\nСрок: ' + self.date_text(item['expires_at'])
        if item.get('delivery_pending'):
            text += '\nДоставка на другой сервер ещё не подтверждена. Misbot повторит её при доступном API.'
        if item.get('safe_error'):
            text += '\n' + self.federation_error_text(item['safe_error'])
        buttons = []
        if inactive:
            buttons.append([self.button('Очистить запись', 'fed_prepare|clear|' + request_id)])
        elif state in ('accepted', 'applying', 'partially_applied'):
            text += '\nОдобрение сохранено. Продолжите обмен, чтобы проверить применение правил.'
            buttons.append([self.button('Продолжить обмен', 'fed_prepare|retry|' + request_id)])
        elif item['direction'] == 'incoming':
            buttons.extend(
                [
                    [self.button('Принять заявку', 'fed_prepare|approve|' + request_id)],
                    [self.button('Отклонить заявку', 'fed_prepare|reject|' + request_id)],
                ]
            )
        else:
            buttons.append(
                [
                    self.button(
                        'Повторить обмен' if item.get('safe_error') else 'Проверить другой сервер',
                        'fed_prepare|retry|' + request_id,
                    )
                ]
            )
            buttons.append(
                [self.button('Отозвать приглашение', 'fed_prepare|cancel|' + request_id)]
            )
        view = 'rejected' if inactive else item['direction']
        buttons.append(self.back('fed_requests|' + view + '|0'))
        return self.screen(chat, text, buttons)

    def federation_servers(self, chat, uid):
        result = self.call('fed_read', uid, operation='servers')
        text = 'Серверы федерации\n\n'
        text += (
            'Федерация открыта для всех серверов.'
            if result.get('open')
            else '\n'.join(result.get('servers') or []) or 'Другие серверы ещё не добавлены.'
        )
        return self.screen(chat, text, [self.back('federation')])

    def federation_api(self, chat, uid):
        result = self.call('fed_read', uid, operation='api-status')
        operation = 'api-disable' if result['enabled'] else 'api-enable'
        title = 'Выключить API' if result['enabled'] else 'Включить API'
        return self.screen(
            chat,
            'Misbot API: '
            + ('включён' if result['enabled'] else 'выключен')
            + '\n\nНастройка сохраняется после перезагрузки. При выключенном API сервер не получает приглашения и ответы других серверов. Действующая федерация сохраняется.',
            [[self.button(title, 'fed_prepare|' + operation)], self.back('federation')],
        )

    def prepare(self, chat, uid, operation, target=''):
        result = self.call('fed_prepare', uid, operation=operation, target=target)
        label = {
            'invite': 'Отправить приглашение серверу',
            'approve': 'Принять заявку',
            'reject': 'Отклонить заявку',
            'cancel': 'Отозвать приглашение',
            'retry': 'Продолжить обмен по заявке',
            'clear': 'Очистить отклонённые заявки',
            'api-enable': 'Включить Misbot API',
            'api-disable': 'Выключить Misbot API',
            'add': 'Добавить сервер',
            'remove': 'Удалить сервер',
        }[operation]
        text = (
            result.get('summary')
            or label + (' ' + result.get('target', target) if target else '') + '?'
        )
        if operation == 'invite':
            text += '\nПриглашение действует 30 дней. Другой сервер должен подтвердить его.'
        elif operation in ('approve', 'add', 'remove'):
            text += '\nИзменение правил федерации может кратко перезапустить Synapse. Результат будет проверен.'
        elif operation == 'cancel':
            text += '\nПриглашение будет удалено и на другом сервере после доставки отзыва.'
        elif operation == 'reject':
            text += '\nДругой сервер получит отказ и сможет подать заявку повторно.'
        elif operation == 'retry':
            text += '\nЗапросим состояние другого сервера и продолжим согласованное подключение. Новая заявка не создаётся.'
        elif operation == 'clear':
            text += '\nУдаляются только закрытые записи из этого списка.'
        elif operation == 'api-disable':
            text += '\nВходящие приглашения и ответы станут недоступны. Действующая федерация сохранится.'
        text += '\nПодтверждение действует 5 минут.'
        return self.screen(
            chat,
            text,
            [
                [self.button('Подтвердить', 'fed_apply|' + result['confirmation'])],
                self.back('federation', 'Отмена'),
            ],
        )

    def federation_job(self, chat, result):
        operation_id = result['operation_id']
        status = result.get('status', 'queued')
        details = result.get('result', {})
        details = details if isinstance(details, dict) else {}
        code = details.get('code') or details.get('safe_error') or result.get('safe_error')
        state = details.get('state')
        buttons = [[self.button('Проверить результат', 'fed_job|' + operation_id)]]
        if status in ('queued', 'running'):
            text = 'Операция принята.' if status == 'queued' else 'Операция выполняется.'
            text += (
                '\nПроверить результат можно этой кнопкой. Повторное подтверждение не требуется.'
            )
        elif status == 'complete' and not code and not details.get('delivery_pending'):
            if state == 'pending':
                text = (
                    'Заявка ожидает вашего решения.'
                    if details.get('direction') == 'incoming'
                    else 'Заявка ожидает решения другого сервера.'
                )
            elif state == 'connected':
                text = 'Правила федерации применены на обоих серверах.'
            elif state in ('accepted', 'applying', 'partially_applied'):
                text = (
                    'Одобрение сохранено. Подключение ещё не завершено; продолжите обмен по заявке.'
                )
            elif state in ('rejected', 'expired', 'cancelled'):
                text = {
                    'rejected': 'Заявка отклонена.',
                    'expired': 'Срок заявки истёк.',
                    'cancelled': 'Заявка закрыта.',
                }[state]
            elif isinstance(details.get('enabled'), bool):
                text = 'Misbot API ' + ('включён.' if details['enabled'] else 'выключен.')
                text += '\nНастройка сохранена.'
            elif isinstance(details.get('count'), int):
                text = 'Отклонённые записи скрыты: ' + str(details['count']) + '.'
            else:
                text = 'Операция выполнена. Откройте федерацию, чтобы увидеть актуальное состояние.'
        else:
            text = (
                'Заявка сохранена, но обмен с другим сервером ещё не подтверждён.'
                if status == 'complete' and state
                else 'Завершение операции не подтверждено.'
            )
            if code:
                text += '\n' + self.federation_error_text(code)
            if details.get('delivery_pending'):
                text += '\nДоставка результата будет повторена при доступном API.'
            if result.get('next_action'):
                text += '\n' + result['next_action']
        if details.get('server'):
            text += '\nСервер: ' + str(details['server'])
        if state in ('pending', 'accepted', 'applying', 'partially_applied'):
            view = details.get('direction', 'outgoing')
            if view in ('incoming', 'outgoing'):
                buttons.append([self.button('Открыть заявки', 'fed_requests|' + view + '|0')])
        buttons.append(self.back('federation', 'Открыть федерацию'))
        return self.screen(
            chat, text, buttons, key='federation-job:' + str(chat) + ':' + operation_id
        )

    @staticmethod
    def federation_error_text(code):
        messages = {
            'api_disabled': 'API Misbot выключен. Включите его в настройках федерации.',
            'invitation_rate_limit': 'Новое приглашение можно создать не чаще одного раза в минуту.',
            'pending_request_exists': 'Для этого сервера уже есть актуальная заявка. Откройте список заявок.',
            'peer_upgrade_required': 'На другом сервере требуется обновление Misbot для заявок сроком 30 дней.',
            'peer_already_connected': 'Этот сервер уже добавлен в федерацию. Обновите список заявок.',
            'peer_identity_changed': 'Identity другого сервера изменилась. Проверьте её через SSH.',
            'request_in_progress': 'Правила уже применяются. Продолжите обмен или проверьте восстановление через SSH.',
            'request_not_found': 'Заявка не найдена. Обновите список заявок.',
            'invalid_transition': 'Это действие уже недоступно. Обновите список заявок.',
            'admin_unconfirmed': 'Права администратора не подтверждены. Повторите вход.',
            'timeout': 'Другой сервер не ответил. Повторите обмен позже.',
            'remote_http_503': 'API другого сервера сейчас недоступен. Повторите обмен позже.',
            'dns_failed': 'Домен другого сервера пока недоступен. Повторите обмен позже.',
            'tls_failed': 'HTTPS другого сервера не подтверждён. Проверьте его сертификат.',
        }
        return messages.get(code, 'Причина не подтверждена. Проверьте состояние сервера через SSH.')

    @staticmethod
    def size_text(value):
        if value is None:
            return 'нет данных'
        return f'{value / (1024**3):.1f} ГБ'

    def manage(self, chat, uid, operation, period='day'):
        if operation == 'update_check':
            result = self.call('manage_read', uid, operation=operation)
            release = result.get('release', {})
            text = 'Обновление компонентов\n\nУстановлено: ' + str(
                release.get('installed') or 'нет данных'
            )
            text += '\nДоступный выпуск: ' + str(release.get('latest') or 'нет данных')
            if release.get('notes'):
                text += '\n\n' + str(release['notes'])[:1800]
            buttons = []
            url = release.get('url')
            if isinstance(url, str) and url.startswith('https://'):
                buttons.append([{'text': 'Описание выпуска', 'url': url}])
            if release.get('update_available') and 'update' in self.policy(uid).get(
                'management_actions', []
            ):
                buttons.append([self.button('Подготовить обновление', 'manage|update')])
            if not result.get('ok'):
                text += '\nПроверка доступного выпуска не подтверждена. Повторите позже.'
            buttons.append(self.back('updates'))
            return self.screen(chat, text, buttons)
        if operation == 'statistics':
            if period not in ('day', 'week', 'month', 'all'):
                raise BotError('Выберите день, неделю, месяц или всё время.')
            result = self.call('manage_read', uid, operation=operation, period=period)
            data = result.get('statistics', {})
            text = (
                'Статистика сервера · '
                + {'day': 'день', 'week': 'неделя', 'month': 'месяц', 'all': 'всё время'}[period]
            )
            if result.get('checked_at'):
                text += '\nПоследняя проверка: ' + self.date_text(result['checked_at'])
            text += '\n\nСвободное место: ' + self.size_text(data.get('disk_free_bytes'))
            text += '\nДиск всего: ' + self.size_text(data.get('disk_total_bytes'))
            text += '\nПамять: ' + self.size_text(data.get('memory_used_bytes'))
            text += ' / ' + self.size_text(data.get('memory_total_bytes'))
            if data.get('cpu_load') is not None:
                text += '\nНагрузка CPU: ' + str(data['cpu_load'])
            if data.get('uptime_seconds') is not None:
                text += '\nСервер работает: ' + str(data['uptime_seconds'] // 3600) + ' ч.'
            for field, label in (
                ('users', 'Аккаунты'),
                ('rooms', 'Комнаты'),
                ('messages', 'Сообщений за период'),
                ('registrations', 'Регистраций за период'),
            ):
                if data.get(field) is not None:
                    text += '\n' + label + ': ' + str(data[field])
            if data.get('samples') is not None:
                text += '\nЗамеров за период: ' + str(data['samples'])
            if data.get('note'):
                text += '\n' + str(data['note'])
            if not result.get('ok'):
                text += '\nПолная статистика недоступна. Проверьте состояние сервера.'
            return self.screen(
                chat,
                text,
                [
                    [
                        self.button(label, 'statistics|' + value)
                        for value, label in (
                            ('day', 'День'),
                            ('week', 'Неделя'),
                            ('month', 'Месяц'),
                            ('all', 'Всё время'),
                        )
                    ],
                    self.back(),
                ],
            )
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
            return self.screen(chat, text, [self.back('more')])
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
            return self.screen(chat, text, [self.back('backups')])
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
            text += (
                '\n\nПроверка служб и адресов не подтверждает вход в клиент, переписку и звонки.'
            )
            return self.screen(
                chat, text, [[self.button('Обновить', 'manage|' + operation)], self.back()]
            )
        confirmation = self.call('manage_prepare', uid, operation=operation)
        text = {
            'start': 'Запустить службы?\nИзменённые службы могут перезапуститься.',
            'stop': 'Остановить службы Matrix?\nСервер будет недоступен до запуска служб.',
            'restart': 'Перезапустить службы Matrix?\nОбщение будет кратко недоступно. После перезапуска будет проверена доступность.',
            'update': 'Обновить компоненты?\nПеред применением сервер проверит совместимость и резервную копию. Общение может быть временно недоступно.',
            'backup_verify': 'Проверить последнюю резервную копию?\nДля проверки будут восстановлены временные базы. Службы продолжат работу.',
            'storage_connect': 'Подключить S3 вложений из профиля, заполненного через SSH?\nSynapse кратко перезапустится. Локальные файлы сохранятся.',
            'backup': 'Создать согласованную резервную копию?\nSynapse и MAS временно остановятся. Если S3 подключён, полный архив отправится туда зашифрованным.',
        }.get(operation, 'Выполнить операцию управления?')
        if confirmation.get('summary'):
            text = confirmation['summary']
        if operation == 'update' and confirmation.get('version'):
            text += '\nВерсия: ' + str(confirmation['version'])
        return self.screen(
            chat,
            text + '\nПодтверждение действует 5 минут.',
            [
                [self.button('Подтвердить', 'manage_apply|' + confirmation['confirmation'])],
                self.back('maintenance', 'Отмена'),
            ],
        )

    def job_message(self, chat, job):
        if job['state'] in ('queued', 'running'):
            return self.screen(
                chat,
                ('Операция принята.' if job['state'] == 'queued' else 'Операция выполняется.')
                + ' Нажмите «Проверить результат», чтобы узнать её состояние.',
                [[self.button('Проверить результат', 'manage_job|' + job['job'])]],
                key='job:' + str(chat) + ':' + job['job'],
            )
        if job['state'] in ('complete', 'finished') and (job.get('result') or {}).get('ok'):
            return self.screen(
                chat,
                {
                    'backup': 'Резервная копия создана.',
                    'start': 'Службы запущены.',
                    'stop': 'Службы остановлены.',
                    'restart': 'Службы перезапущены.',
                    'update': 'Компоненты обновлены.',
                    'backup_verify': 'Резервная копия проверена.',
                    'storage_connect': 'S3 вложений подключён.',
                }.get(job['operation'], 'Операция завершена.'),
                key='job:' + str(chat) + ':' + job['job'],
            )
        return self.screen(
            chat,
            'Завершение операции не подтверждено. Через SSH выполните misbot status. '
            'Если резервная копия не завершена, выполните misbot backup recover.',
            key='job:' + str(chat) + ':' + job['job'],
        )

    def stale_callback(self, chat, message, command):
        # Durable cards carry read actions and token revocation, never panel confirmations.
        if command.startswith(
            ('revoke|', 'manage_job|', 'fed_job|', 'fed_requests|')
        ) or command in ('federation', 'menu'):
            return False
        current = self.state('navigation:' + str(chat))
        if not current:
            return command.startswith(('fed_apply|', 'manage_apply|', 'fed_prepare|'))
        actions = {
            item.get('callback_data')
            for row in current['payload']['reply_markup']['inline_keyboard']
            for item in row
        }
        return message.get('message_id') != current['message_id'] or command not in actions

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
            except (BotError, requests.RequestException, ValueError):
                pass
        text = callback.get('data', '') if callback else message.get('text', '').strip()
        command, _, argument = text.partition(' ')
        command = command.split('@')[0]
        try:
            if callback and self.stale_callback(chat, message, command):
                return self.screen(
                    chat,
                    'Эта кнопка устарела. Откройте актуальное меню.',
                    [self.back('menu', 'Открыть меню')],
                )
            if command in ('/start', '/help', 'menu', 'cancel', 'auth_check', '/auth', 'auth'):
                self.waiting(uid, None)
                return self.menu(chat, uid, force_refresh=not callback)
            if command == '/logout':
                self.call('logout', uid)
                self.waiting(uid, None)
                return self.screen(chat, 'Вы вышли из бота. Для входа отправьте /start.')
            if not self.call('identity', uid)['authenticated']:
                self.waiting(uid, None)
                return self.login(chat, uid)
            if command in ('maintenance', 'backups', 'updates', 'more'):
                self.waiting(uid, None)
                return self.group(chat, uid, command)
            if command == 'invitations':
                self.waiting(uid, None)
                return self.invitations(chat, uid)
            operations = {
                '/statistics': 'statistics',
                '/status': 'status',
                '/check': 'check',
                '/start_services': 'start',
                '/stop_services': 'stop',
                '/restart_services': 'restart',
                '/update_components': 'update',
                '/backup': 'backup',
                '/backup_verify': 'backup_verify',
                '/storage': 'storage',
            }
            if command in operations or command.startswith('manage|'):
                self.waiting(uid, None)
                return self.manage(chat, uid, operations.get(command) or command.split('|', 1)[1])
            if command.startswith('statistics|'):
                return self.manage(chat, uid, 'statistics', command.split('|', 1)[1])
            if command.startswith('manage_apply|'):
                result = self.call('manage_apply', uid, confirmation=command.split('|', 1)[1])
                self.screen(
                    chat,
                    'Операция принята. Состояние показано в отдельном сообщении.',
                    [self.back('maintenance')],
                )
                return self.job_message(chat, result)
            if command.startswith('manage_job|'):
                return self.job_message(
                    chat, self.call('manage_job', uid, job=command.split('|', 1)[1])
                )
            if command in ('/invite', 'invite'):
                self.waiting(uid, None)
                item = self.call('invite', uid, request_id='telegram:' + str(update['update_id']))
                return self.token_message(chat, self.selected(uid), item)
            if command == '/tokens' or command.startswith('tokens|'):
                offset = int(command.split('|')[1]) if '|' in command else 0
                if offset < 0:
                    raise ValueError('negative offset')
                page = self.call('tokens', uid, offset=offset)
                for item in page['tokens']:
                    self.token_message(chat, self.selected(uid), item)
                buttons = []
                if offset:
                    buttons.append(self.button('Предыдущие', 'tokens|' + str(max(0, offset - 5))))
                if page['has_more']:
                    buttons.append(self.button('Следующие', 'tokens|' + str(offset + 5)))
                return self.screen(
                    chat,
                    'Пока нет приглашений.'
                    if not page['tokens']
                    else 'Ваши приглашения. Страница ' + str(offset // 5 + 1),
                    ([buttons] if buttons else []) + [self.back('invitations')],
                )
            if command.startswith('revoke|'):
                self.call('revoke', uid, resource=command.split('|', 1)[1])
                return self.send(chat, 'Приглашение отозвано.')
            if command in ('/federation', 'federation'):
                self.waiting(uid, None)
                return self.federation(chat, uid)
            if command == 'fed_api':
                return self.federation_api(chat, uid)
            if command == 'fed_servers':
                return self.federation_servers(chat, uid)
            if command.startswith('fed_requests|'):
                _, view, offset = command.split('|')
                if view not in ('incoming', 'outgoing', 'rejected') or int(offset) < 0:
                    raise ValueError('invalid federation page')
                return self.federation_requests(chat, uid, view, int(offset))
            if command.startswith('fed_card|'):
                return self.federation_card(chat, uid, command.split('|', 1)[1])
            if command.startswith('fed_prepare|'):
                parts = command.split('|', 2)
                return self.prepare(chat, uid, parts[1], parts[2] if len(parts) > 2 else '')
            if command in (
                '/federation_invite',
                '/federation_add',
                '/federation_remove',
                'fed_invite',
                'fed_add',
            ):
                operation = {
                    '/federation_add': 'add',
                    '/federation_remove': 'remove',
                    'fed_add': 'add',
                }.get(command, 'invite')
                if argument:
                    self.prepare(chat, uid, operation, argument)
                    self.waiting(uid, None)
                    return
                self.call('fed_read', uid, operation='api-status')
                self.waiting(uid, operation)
                return self.screen(
                    chat,
                    'Отправьте домен Matrix-сервера, например chat.example.org.',
                    [self.back('federation', 'Отмена')],
                )
            if command.startswith('fed_apply|'):
                result = self.call('fed_apply', uid, confirmation=command.split('|', 1)[1])
                self.screen(
                    chat,
                    'Операция принята. Состояние показано в отдельном сообщении.',
                    [self.back('federation')],
                )
                if isinstance(result, dict) and isinstance(result.get('operation_id'), str):
                    return self.federation_job(chat, result)
                return self.send(
                    chat, 'Список разрешённых серверов сохранён:\n' + '\n'.join(result)
                )
            if command.startswith('fed_job|'):
                return self.federation_job(
                    chat,
                    self.call(
                        'fed_read', uid, operation='operation', target=command.split('|', 1)[1]
                    ),
                )
            with self.db_lock:
                row = self.db.execute('SELECT waiting FROM users WHERE uid=?', (uid,)).fetchone()
            if row and row[0] and not callback and not text.startswith('/'):
                self.prepare(chat, uid, row[0], text)
                self.waiting(uid, None)
                return
            return self.menu(chat, uid)
        except (BotError, ValueError) as error:
            return self.screen(
                chat,
                str(error)
                if isinstance(error, BotError)
                else 'Не удалось распознать команду. Откройте меню.',
                [self.back('menu', 'Открыть меню')],
            )

    def notifications(self):
        """Acknowledge terminal delivery outcomes; retry acknowledgements without resending."""
        self.servers()
        name = self.server_name
        feed = bridge_call({'action': 'federation_notifications', 'server': name})
        attempted = 0
        for item in feed['notifications']:
            key = 'notification:' + str(item['uid']) + ':' + item['id']
            delivery_key = 'delivery:' + key
            try:
                if not self.state(key):
                    saved = self.state(delivery_key)
                    due = saved is None or (
                        saved['status'] == 'pending' and saved['next_attempt'] <= time.time()
                    )
                    if due:
                        if attempted >= 4:
                            continue
                        attempted += 1
                    action, title = (
                        ('federation', 'Открыть федерацию')
                        if item.get('request_id')
                        else ('menu', 'Открыть меню')
                    )
                    self.deliver(
                        'sendMessage',
                        {
                            'chat_id': item['uid'],
                            'text': item['message'],
                            'reply_markup': {'inline_keyboard': [self.back(action, title)]},
                            'link_preview_options': {'is_disabled': True},
                        },
                        identifier=delivery_key,
                        timeout=5,
                    )
                    delivery = self.state(delivery_key)
                    if delivery['status'] == 'pending':
                        continue
                    self.state(key, {'status': delivery['status'], **delivery.get('response', {})})
                bridge_call(
                    {
                        'action': 'federation_notifications_ack',
                        'server': name,
                        'uid': item['uid'],
                        'id': item['id'],
                        'delivery_status': self.state(key).get('status', 'sent'),
                    }
                )
                self.delete_state(delivery_key)
                self.delete_state(key)
            except (BotError, OSError, ValueError) as error:
                LOG.warning('notification delivery failed type=%s', type(error).__name__)

    def polling(self):
        while True:
            try:
                self.servers()
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
                for update in updates:
                    self.process(update)
                    offset = update['update_id'] + 1
                    with self.db_lock:
                        self.db.execute(
                            "INSERT OR REPLACE INTO state VALUES ('offset',?)", (str(offset),)
                        )
                        self.db.commit()
                self.last_poll_success = time.monotonic()
                self.retry_deliveries()
                try:
                    self.notifications()
                except Exception as error:
                    LOG.error('notification delivery failed type=%s', type(error).__name__)
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
            self.server.bot.servers(timeout=2)
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
