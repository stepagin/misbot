"""Root service: narrowly scoped MAS and federation operations over a Unix socket.

The Telegram container receives neither MAS admin credentials nor Docker access.
MAS device authorization and current MAS privileges are checked here, not in the UI.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import sqlite3
import subprocess
import struct
import sys
import threading
import tempfile
import time

import requests
import yaml

from common import BotError, domain, private_json, read_json, receive
from control import load_backend, run_control
from mas import MAS

LOG = logging.getLogger('bridge')


def check_peer(uid, action):
    if action == 'backup_snapshot' and uid != 0:
        raise BotError('Снимок состояния доступен только локальному администратору.')
    matrix_actions = {
        'servers',
        'matrix_identity',
        'matrix_invite',
        'matrix_tokens',
        'matrix_revoke',
    }
    if uid == 0:
        return
    if uid == 10002 and action in matrix_actions:
        return
    if uid == 10001 and action not in matrix_actions - {'servers'}:
        return
    raise BotError('Это действие недоступно через данного бота.')


class Bridge:
    def __init__(self, config_path, api_factory=MAS):
        self.path = Path(config_path)
        self.config = read_json(self.path)
        servers = self.config.get('servers')
        if not isinstance(servers, dict) or len(servers) != 1:
            raise BotError('Управляющий бот должен быть привязан ровно к одному серверу.')
        self.server_name = next(iter(servers))
        if domain(self.server_name) != self.server_name:
            raise BotError('Привязка бота должна содержать канонический домен сервера.')
        self.root = self.path.parent
        self.policy_path = self.root / 'bot-policy.json'
        self.policies()
        self.db = sqlite3.connect(self.root / 'data/bot-control.sqlite3', check_same_thread=False)
        os.chmod(self.root / 'data/bot-control.sqlite3', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS invitations (
            server TEXT, uid INTEGER, request TEXT UNIQUE, code TEXT UNIQUE,
            expires INTEGER, resource TEXT, inviter_name TEXT, created_at INTEGER,
            PRIMARY KEY(server, uid, code));
        CREATE TABLE IF NOT EXISTS auth_requests (
            request TEXT PRIMARY KEY, server TEXT, uid INTEGER, device_code TEXT, expires INTEGER, interval INTEGER, next_poll INTEGER, error TEXT,
            user_code TEXT, verification_uri TEXT);
        CREATE TABLE IF NOT EXISTS authorizations (
            server TEXT, uid INTEGER, user_id TEXT, expires INTEGER, PRIMARY KEY(server,uid));
        CREATE TABLE IF NOT EXISTS telegram_accounts (
            server TEXT, user_id TEXT, uid INTEGER,
            PRIMARY KEY(server,user_id), UNIQUE(server,uid));
        CREATE TABLE IF NOT EXISTS confirmations (
            id TEXT PRIMARY KEY, server TEXT, uid INTEGER, action TEXT, domain TEXT, expires INTEGER);
        CREATE TABLE IF NOT EXISTS management_jobs (
            id TEXT PRIMARY KEY, server TEXT, uid INTEGER, operation TEXT, state TEXT, result TEXT);
        CREATE TABLE IF NOT EXISTS telegram_federation_jobs (
            id TEXT PRIMARY KEY, server TEXT, uid INTEGER);
        CREATE TABLE IF NOT EXISTS telegram_notifications (
            id TEXT, server TEXT, uid INTEGER, message TEXT, request_id TEXT,
            state TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(id, server, uid));
        """)
        # Never repeat an operation after the control service has restarted.
        self.db.execute("UPDATE management_jobs SET state='interrupted' WHERE state='running'")
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(auth_requests)')}
        for name in ('user_code', 'verification_uri'):
            if name not in columns:
                self.db.execute('ALTER TABLE auth_requests ADD COLUMN ' + name + ' TEXT')
        invitation_columns = {
            row['name'] for row in self.db.execute('PRAGMA table_info(invitations)')
        }
        for name, kind in (('inviter_name', 'TEXT'), ('created_at', 'INTEGER')):
            if name not in invitation_columns:
                self.db.execute('ALTER TABLE invitations ADD COLUMN ' + name + ' ' + kind)
        # Keep ownership after logout; the latest legacy login seeds the association.
        with self.db:
            self.db.execute(
                'INSERT OR IGNORE INTO telegram_accounts SELECT server,user_id,uid '
                'FROM authorizations WHERE server=? ORDER BY expires DESC,uid DESC',
                (self.server_name,),
            )
            for account in self.db.execute(
                'SELECT user_id,uid FROM telegram_accounts WHERE server=?', (self.server_name,)
            ).fetchall():
                previous = self.db.execute(
                    'SELECT uid FROM authorizations WHERE server=? AND user_id=? AND uid<>?',
                    (self.server_name, account['user_id'], account['uid']),
                ).fetchall()
                for row in previous:
                    self.move_telegram_account(self.server_name, row['uid'], account['uid'])
        self.db.commit()
        self.apis = {name: api_factory(cfg) for name, cfg in self.config['servers'].items()}
        self.lock = threading.RLock()
        self.auth_inflight = set()

    def policies(self):
        policies = read_json(self.policy_path).get('servers')
        if not isinstance(policies, dict) or set(policies) != {self.server_name}:
            raise BotError('Настройки бота должны относиться только к его серверу.')
        return policies

    def server(self, request):
        name = request.get('server')
        if name not in self.apis:
            raise BotError('Сервер не подключён к этому боту.')
        return name, self.apis[name], self.policies()[name]

    def external(self, operation, *args, **kwargs):
        """Run I/O outside the handle's state lock, never during a transaction."""
        if self.db.in_transaction:
            raise RuntimeError('External operation during an uncommitted transaction')
        self.lock.release()
        try:
            return operation(*args, **kwargs)
        finally:
            self.lock.acquire()

    def authorization(self, name, uid):
        return self.db.execute(
            'SELECT * FROM authorizations WHERE server=? AND uid=?', (name, uid)
        ).fetchone()

    def member(self, name, uid):
        row = self.authorization(name, uid)
        if not row or row['expires'] <= time.time():
            raise BotError('Вход в Misbot не подтверждён. Для входа отправьте /start.')
        user = self.external(self.apis[name].active_user, row['user_id'])
        current = self.authorization(name, uid)
        if not current or tuple(current) != tuple(row) or current['expires'] <= time.time():
            raise BotError('Привязка аккаунта изменилась. Для входа отправьте /start.')
        return user

    def admin(self, name, uid):
        user = self.member(name, uid)
        attr = user['attributes']
        if not attr.get('admin', attr.get('can_request_admin', False)):
            raise BotError('Для управления сервером нужны права администратора MAS.')
        return user

    def move_telegram_account(self, name, previous, current):
        """Move private history while discarding approvals and delivery recipients."""
        for table in ('invitations', 'management_jobs', 'telegram_federation_jobs'):
            self.db.execute(
                f'UPDATE {table} SET uid=? WHERE server=? AND uid=?', (current, name, previous)
            )
        for table in ('authorizations', 'auth_requests', 'confirmations', 'telegram_notifications'):
            self.db.execute(f'DELETE FROM {table} WHERE server=? AND uid=?', (name, previous))

    def bind_telegram_account(self, name, uid, user_id, expires):
        # A different MAS identity must not inherit this Telegram UID's private history.
        previous = self.db.execute(
            'SELECT user_id FROM telegram_accounts WHERE server=? AND uid=?', (name, uid)
        ).fetchone()
        unknown_history = not previous and any(
            self.db.execute(
                f'SELECT 1 FROM {table} WHERE server=? AND uid=? LIMIT 1', (name, uid)
            ).fetchone()
            for table in ('invitations', 'management_jobs', 'telegram_federation_jobs')
        )
        if (previous and previous['user_id'] != user_id) or unknown_history:
            retired = 'retired:' + secrets.token_hex(16)
            self.move_telegram_account(name, uid, retired)
            if previous:
                self.db.execute(
                    'UPDATE telegram_accounts SET uid=? WHERE server=? AND user_id=?',
                    (retired, name, previous['user_id']),
                )
        owners = {
            row['uid']
            for row in self.db.execute(
                'SELECT uid FROM telegram_accounts WHERE server=? AND user_id=? '
                'UNION SELECT uid FROM authorizations WHERE server=? AND user_id=?',
                (name, user_id, name, user_id),
            )
        }
        for previous_uid in owners - {uid}:
            self.move_telegram_account(name, previous_uid, uid)
        self.db.execute('DELETE FROM confirmations WHERE server=? AND uid=?', (name, uid))
        self.db.execute(
            'INSERT OR REPLACE INTO telegram_accounts VALUES (?,?,?)', (name, user_id, uid)
        )
        self.db.execute(
            'INSERT OR REPLACE INTO authorizations VALUES (?,?,?,?)', (name, uid, user_id, expires)
        )

    def poll_auths(self):
        now = int(time.time())
        with self.lock:
            rows = self.db.execute(
                'SELECT * FROM auth_requests WHERE server=? AND expires>? AND next_poll<=? AND error IS NULL',
                (self.server_name, now, now),
            ).fetchall()
        for row in rows:
            key = row['request']
            with self.lock:
                if (
                    key in self.auth_inflight
                    or not self.db.execute(
                        'SELECT 1 FROM auth_requests WHERE request=?', (key,)
                    ).fetchone()
                ):
                    continue
                self.auth_inflight.add(key)
            try:
                api = self.apis[row['server']]
                failure = None
                user = None
                try:
                    status, token = api.device_token(row['device_code'])
                    if status == 200:
                        user = api.device_user(token)
                except Exception as error:
                    failure = error
                with self.lock:
                    current = self.db.execute(
                        'SELECT * FROM auth_requests WHERE request=?', (key,)
                    ).fetchone()
                    # Logout or a newer grant must win over an old network response.
                    if (
                        not current
                        or tuple(current) != tuple(row)
                        or current['expires'] <= time.time()
                    ):
                        continue
                    now = int(time.time())
                    with self.db:
                        if failure is not None:
                            if isinstance(failure, BotError):
                                self.db.execute(
                                    'UPDATE auth_requests SET error=? WHERE request=?',
                                    ('denied', key),
                                )
                            else:
                                self.db.execute(
                                    'UPDATE auth_requests SET next_poll=? WHERE request=?',
                                    (now + 15, key),
                                )
                            LOG.error(
                                'device authorization poll failed type=%s', type(failure).__name__
                            )
                        elif user is not None:
                            self.bind_telegram_account(
                                row['server'], row['uid'], user['id'], now + 86400
                            )
                            self.db.execute('DELETE FROM auth_requests WHERE request=?', (key,))
                            LOG.info('account authorization completed server=%s', row['server'])
                        elif token.get('error') in ('authorization_pending', 'slow_down'):
                            interval = row['interval'] + (
                                5 if token.get('error') == 'slow_down' else 0
                            )
                            self.db.execute(
                                'UPDATE auth_requests SET interval=?,next_poll=? WHERE request=?',
                                (interval, now + interval, key),
                            )
                        else:
                            self.db.execute(
                                'UPDATE auth_requests SET error=? WHERE request=?', ('denied', key)
                            )
            finally:
                with self.lock:
                    self.auth_inflight.discard(key)

    def auth_loop(self):
        while True:
            try:
                self.poll_auths()
            except Exception as error:
                LOG.error('authorization loop failed type=%s', type(error).__name__)
            time.sleep(2)

    def management(self, request, name, uid):
        user = self.admin(name, uid)
        config = self.management_config(name)
        enabled = config.get('management_actions', [])
        if not enabled:
            raise BotError('Управление службами этого сервера не подключено.')
        controls = load_backend(config)
        action = request['action']
        if action == 'manage_jobs':
            offset = request.get('offset', 0)
            if type(offset) is not int or offset < 0:
                raise BotError('Страница не распознана. Откройте историю заново.')
            rows = self.db.execute(
                'SELECT id,operation,state FROM management_jobs WHERE server=? AND uid=? ORDER BY rowid DESC LIMIT 6 OFFSET ?',
                (name, uid, offset),
            ).fetchall()
            jobs = []
            for row in rows[:5]:
                jobs.append(self.management({'action': 'manage_job', 'job': row['id']}, name, uid))
            if self.admin(name, uid)['id'] != user['id']:
                raise BotError('Привязка аккаунта изменилась. Повторите вход.')
            return {'jobs': jobs, 'has_more': len(rows) > 5}
        if action == 'manage_job':
            row = self.db.execute(
                'SELECT * FROM management_jobs WHERE id=? AND server=? AND uid=?',
                (request.get('job'), name, uid),
            ).fetchone()
            if not row:
                raise BotError(
                    'Операция недоступна: она не найдена или связана с другим аккаунтом.'
                )
            if row['id'].startswith('SO-'):
                self.management_runtime(name)
                import server_management

                actor = '@' + user['attributes']['username'] + ':' + name
                result = server_management.operation_status(
                    Path(config['stack_root']), row['id'], actor=actor
                )
                return {'job': row['id'], 'state': result['status'], **result}
            return {
                'job': row['id'],
                'operation': row['operation'],
                'state': row['state'],
                'result': json.loads(row['result']) if row['result'] else None,
            }
        operation = request.get('operation')
        if action != 'manage_apply' and (
            operation not in controls.COMMANDS or operation not in enabled
        ):
            raise BotError('Эта команда управления не подключена. Обратитесь к администратору.')
        if action == 'manage_read':
            if operation in controls.WRITES:
                raise BotError(
                    'Для этой операции нужно подтверждение. Запросите его через меню бота.'
                )
            if operation == 'statistics':
                period = request.get('period', 'day')
                if period not in ('day', 'week', 'month', 'all'):
                    raise BotError('Период не распознан. Выберите день, неделю или месяц.')
                result = self.external(run_control, config, operation, period=period)
            else:
                result = self.external(run_control, config, operation)
            if self.admin(name, uid)['id'] != user['id']:
                raise BotError('Привязка аккаунта изменилась. Повторите вход.')
            return result
        if action == 'manage_prepare':
            if operation not in controls.WRITES:
                raise BotError('Эта операция доступна без подтверждения. Выберите её в меню бота.')
            if operation == 'storage_connect' and not controls.media_profile_ready(config):
                raise BotError(
                    'Подготовленный профиль S3 отсутствует или не прошёл проверку. '
                    'Через SSH подготовьте приватный файл .misbot/s3/media.env в каталоге установки. '
                    'Затем повторите применение профиля; ключи доступа в чат не отправляйте.'
                )
            key = secrets.token_urlsafe(16)
            preview = (
                controls.backup_preview(config['stack_root']) if operation == 'backup' else None
            )
            version = ''
            if operation == 'update':
                release = self.external(run_control, config, 'update_check').get('release', {})
                if self.admin(name, uid)['id'] != user['id']:
                    raise BotError('Привязка аккаунта изменилась. Повторите вход.')
                if not release.get('update_available') or not release.get('latest'):
                    raise BotError('Более новая поддерживаемая версия не найдена.')
                version = release['latest']
            self.db.execute('DELETE FROM confirmations WHERE expires<?', (int(time.time()),))
            self.db.execute(
                'INSERT INTO confirmations VALUES (?,?,?,?,?,?)',
                (
                    key,
                    name,
                    uid,
                    'manage:' + operation,
                    json.dumps(preview, sort_keys=True) if preview else version,
                    int(time.time()) + 300,
                ),
            )
            self.db.commit()
            return {
                'confirmation': key,
                'operation': operation,
                'version': version or None,
                **({'backup': preview} if preview is not None else {}),
            }
        if action == 'manage_apply':
            row = self.db.execute(
                'SELECT * FROM confirmations WHERE id=? AND uid=? AND server=?',
                (request.get('confirmation'), uid, name),
            ).fetchone()
            if not row or row['expires'] <= time.time() or not row['action'].startswith('manage:'):
                raise BotError(
                    'Подтверждение недействительно: срок истёк или оно уже использовано. Запросите новое подтверждение.'
                )
            operation = row['action'].removeprefix('manage:')
            if operation not in controls.WRITES or operation not in enabled:
                raise BotError('Эта команда управления не подключена. Обратитесь к администратору.')
            if operation == 'backup':
                current = json.dumps(controls.backup_preview(config['stack_root']), sort_keys=True)
                if row['domain'] != current:
                    raise BotError('Параметры копии изменились. Запросите новое подтверждение.')
            # The host worker survives Telegram, MAS and controller restarts.
            self.management_runtime(name)
            import server_management

            actor = '@' + user['attributes']['username'] + ':' + name
            try:
                queued = server_management.queue_operation(
                    Path(config['stack_root']),
                    operation,
                    actor=actor,
                    event_id='telegram-management:' + row['id'],
                    argument=row['domain'] if operation == 'update' else None,
                    **(
                        {'confirmed_backup': json.loads(row['domain'])}
                        if operation == 'backup'
                        else {}
                    ),
                )
            except (RuntimeError, OSError, ValueError):
                raise BotError(
                    'Операция не запущена или уже выполняется. Проверьте её состояние; при незавершённом изменении используйте recovery через SSH.'
                ) from None
            key = queued['operation_id']
            self.db.execute(
                'INSERT OR IGNORE INTO management_jobs VALUES (?,?,?,?,?,NULL)',
                (key, name, uid, operation, queued['status']),
            )
            self.db.execute('DELETE FROM confirmations WHERE id=?', (row['id'],))
            self.db.commit()
            LOG.info('management operation started operation=%s server=%s', operation, name)
            return {'job': key, 'state': queued['status'], **queued}
        raise BotError('Команда не распознана. Откройте меню бота.')

    def management_config(self, name):
        config = dict(self.config['servers'][name])
        actions = list(config.get('management_actions', []))
        if config.get('stack_root') and actions:
            actions = list(dict.fromkeys([*actions, 'statistics', 'update_check']))
            if 'start' in actions and 'stop' in actions:
                actions.append('restart')
            if any(action in actions for action in ('backup', 'backup_verify', 'storage')):
                actions.append('backup_schedule')
            if 'backup' in actions:
                actions.append('backup_preview')
        config['management_actions'] = actions
        return config

    def federation_control(self, request, name, uid, policy):
        user = self.admin(name, uid)
        config = self.config['servers'][name]
        if not config.get('federation_enabled', True):
            raise BotError('Управление федерацией через этого бота не подключено.')
        adopted = config.get('federation_backend') == 'misbot'
        actor = '@' + user['attributes']['username'] + ':' + name
        backend = self.federation_backend(name) if adopted else None
        current = (
            backend.operate('servers', actor=actor)['servers']
            if backend
            else policy['federation_servers']
        )
        action = request['action']
        if action == 'federation':
            return current
        if action == 'fed_read':
            operation = request.get('operation')
            if operation == 'servers':
                return {'servers': current, 'open': current is None}
            if backend is None:
                raise BotError(
                    'Общий API Misbot не подключён. Проверьте подключение компонентов через SSH.'
                )
            if operation == 'check':
                return backend.operate('check', domain(request.get('target', '')), actor=actor)
            if operation in ('requests', 'api-status'):
                return backend.operate(operation, actor=actor)
            if operation == 'operation':
                identifier = request.get('target')
                own = self.db.execute(
                    'SELECT 1 FROM telegram_federation_jobs WHERE id=? AND server=? AND uid=?',
                    (identifier, name, uid),
                ).fetchone()
                if not own:
                    raise BotError('Операция не найдена для вашего аккаунта.')
                with backend.guard():
                    job = backend.load().get('jobs', {}).get(identifier)
                if not job:
                    raise BotError('Операция не найдена. Проверьте историю через SSH.')
                return self.federation_job_view(identifier, job)
            raise BotError('Раздел федерации не распознан. Откройте меню заново.')
        operations = {
            'add',
            'remove',
            'invite',
            'approve',
            'reject',
            'cancel',
            'retry',
            'clear',
            'api-enable',
            'api-disable',
        }
        if action == 'fed_prepare':
            operation = request.get('operation')
            if operation not in operations or (
                backend is None and operation not in ('add', 'remove')
            ):
                raise BotError('Эта операция требует подключённого API Misbot.')
            target = request.get('target', request.get('domain', ''))
            summary = ''
            if operation in ('add', 'remove', 'invite'):
                target = domain(target)
                if target == name:
                    raise BotError(
                        'Выберите другой сервер. Собственный сервер остаётся разрешённым.'
                    )
                if operation == 'invite' and current is not None and target in current:
                    raise BotError('Этот сервер уже добавлен в федерацию.')
                if operation == 'invite':
                    readiness = backend.operate('check', target, actor=actor).get('invitation', {})
                    if readiness.get('ready') is not True:
                        import federation_protocol

                        raise federation_protocol.ProtocolError(
                            readiness.get('code', 'invitation_unsupported')
                        )
                summary = (
                    {
                        'invite': 'Пригласить сервер ',
                        'add': 'Добавить сервер ',
                        'remove': 'Удалить сервер ',
                    }[operation]
                    + target
                    + '?'
                )
            elif operation in ('approve', 'reject', 'cancel', 'retry'):
                if not isinstance(target, str) or not re.fullmatch(r'MB-[0-9a-f]{32}', target):
                    raise BotError('Заявка не распознана. Выберите её из списка.')
                item = backend.operate('request-status', target, actor=actor)
                item = item.get('request', item)
                expected = 'outgoing' if operation == 'cancel' else 'incoming'
                if (operation != 'retry' and item.get('direction') != expected) or item.get(
                    'state'
                ) not in ('pending', 'accepted', 'applying', 'partially_applied'):
                    raise BotError('Заявка уже обработана. Обновите список заявок.')
                summary = (
                    {
                        'approve': 'Принять заявку от ',
                        'reject': 'Отклонить заявку от ',
                        'cancel': 'Отозвать приглашение для ',
                        'retry': 'Продолжить обмен с сервером ',
                    }[operation]
                    + item['server']
                    + '?'
                )
            elif operation == 'clear':
                if target and (
                    not isinstance(target, str) or not re.fullmatch(r'MB-[0-9a-f]{32}', target)
                ):
                    raise BotError('Заявка не распознана. Обновите список.')
                target = target or ''
                summary = 'Очистить отклонённые заявки?'
            else:
                target = ''
                summary = (
                    ('Включить' if operation == 'api-enable' else 'Выключить')
                    + ' межсерверный API Misbot?\nСостояние сохранится после перезагрузки. Политика Matrix не изменится.'
                )
            key = secrets.token_urlsafe(16)
            self.db.execute('DELETE FROM confirmations WHERE expires<?', (int(time.time()),))
            self.db.execute(
                'INSERT INTO confirmations VALUES (?,?,?,?,?,?)',
                (key, name, uid, 'federation:' + operation, target, int(time.time()) + 300),
            )
            self.db.commit()
            return {
                'confirmation': key,
                'operation': operation,
                'domain': target,
                'target': target,
                'summary': summary,
                'currently_open': current is None,
            }
        row = self.db.execute(
            'SELECT * FROM confirmations WHERE id=? AND uid=? AND server=?',
            (request.get('confirmation'), uid, name),
        ).fetchone()
        operation = row['action'].removeprefix('federation:') if row else None
        if not row or row['expires'] <= time.time() or operation not in operations:
            raise BotError(
                'Подтверждение недействительно: срок истёк или оно уже использовано. Запросите новое.'
            )
        if backend is not None:
            import federation_service

            result = federation_service.queue_job(
                backend,
                {
                    'action': 'clear-rejected' if operation == 'clear' else operation,
                    'argument': row['domain'] or None,
                    'actor': actor,
                    'event_id': 'telegram-confirmation:' + row['id'],
                },
            )
            self.db.execute(
                'INSERT OR IGNORE INTO telegram_federation_jobs VALUES (?,?,?)',
                (result['operation_id'], name, uid),
            )
        else:
            if operation not in ('add', 'remove'):
                raise BotError('Эта операция требует подключённого API Misbot.')
            domains = list(current or [name])
            if operation == 'add' and row['domain'] not in domains:
                domains.append(row['domain'])
            if operation == 'remove' and row['domain'] in domains:
                domains.remove(row['domain'])
            self.apply_federation(name, sorted(domains))
            result = sorted(domains)
        self.db.execute('DELETE FROM confirmations WHERE id=?', (row['id'],))
        self.db.commit()
        return result

    @staticmethod
    def federation_job_view(identifier, job):
        result = {'operation_id': identifier, 'status': job['state'], 'action': job['action']}
        if isinstance(job.get('result'), dict):
            # Backend views contain only public request/policy fields.
            result['result'] = {
                key: value
                for key, value in job['result'].items()
                if key
                in (
                    'status',
                    'state',
                    'id',
                    'server',
                    'direction',
                    'expires_at',
                    'enabled',
                    'count',
                    'code',
                    'delivery_pending',
                    'safe_error',
                )
            }
        return result

    def enqueue_notice(self, name, uid, event, message, request_id=None):
        identifier = hashlib.sha256((name + '\0' + event).encode()).hexdigest()
        self.db.execute(
            'INSERT OR IGNORE INTO telegram_notifications(id,server,uid,message,request_id) VALUES (?,?,?,?,?)',
            (identifier, name, uid, message, request_id),
        )

    def notification_admin(self, name, uid):
        # A push subscription outlives the interactive login, but never MAS rights.
        row = self.db.execute(
            'SELECT user_id FROM authorizations WHERE server=? AND uid=?', (name, uid)
        ).fetchone()
        if not row:
            raise BotError('Получатель уведомлений не подключён.')
        user = self.external(self.apis[name].active_user, row['user_id'])
        current = self.db.execute(
            'SELECT user_id FROM authorizations WHERE server=? AND uid=?', (name, uid)
        ).fetchone()
        if not current or current['user_id'] != row['user_id']:
            raise BotError('Получатель уведомлений больше не подключён.')
        attributes = user['attributes']
        if not attributes.get('admin', attributes.get('can_request_admin', False)):
            raise BotError('Для уведомлений нужны действующие права администратора MAS.')
        return user

    def notifications(self, name):
        users = self.db.execute('SELECT uid FROM authorizations WHERE server=?', (name,)).fetchall()
        notices = []
        recipients = {}
        backend = None
        for row in users:
            uid = row['uid']
            try:
                user = self.notification_admin(name, uid)
                recipients[uid] = user['id']
                actor = '@' + user['attributes']['username'] + ':' + name
                config = self.config['servers'][name]
                if config.get('federation_backend') == 'misbot' and config.get(
                    'federation_enabled', True
                ):
                    backend = backend or self.federation_backend(name)
                    with backend.guard():
                        backend.ready(True)
                        data = backend.load()
                        requests_view = backend.requests(data)
                        data = backend.load()
                    relevant = set()
                    for item in requests_view:
                        if item.get('cleared') or item.get('hidden'):
                            continue
                        state = item.get('state')
                        if item.get('direction') == 'incoming' and state == 'pending':
                            relevant.add(item['id'])
                            self.enqueue_notice(
                                name,
                                uid,
                                'request:' + item['id'],
                                'Новая заявка на федерацию от '
                                + item['server']
                                + '.\nОткройте раздел «Федерация → Заявки».',
                                item['id'],
                            )
                        elif item.get('direction') == 'outgoing' and state in (
                            'rejected',
                            'expired',
                        ):
                            relevant.add(item['id'])
                            self.enqueue_notice(
                                name,
                                uid,
                                item['id'] + ':' + state,
                                'Заявка для '
                                + item['server']
                                + (': срок истёк.' if state == 'expired' else ': отклонена.'),
                                item['id'],
                            )
                    pending_requests = self.db.execute(
                        "SELECT id,request_id FROM telegram_notifications WHERE server=? AND uid=? AND state='pending' AND request_id IS NOT NULL",
                        (name, uid),
                    ).fetchall()
                    for notice in pending_requests:
                        if notice['request_id'] not in relevant:
                            self.db.execute(
                                "UPDATE telegram_notifications SET state='dismissed' WHERE id=? AND server=? AND uid=?",
                                (notice['id'], name, uid),
                            )
                    jobs = self.db.execute(
                        'SELECT id FROM telegram_federation_jobs WHERE server=? AND uid=?',
                        (name, uid),
                    ).fetchall()
                    for job_row in jobs:
                        job = data.get('jobs', {}).get(job_row['id'], {})
                        if job.get('state') in ('complete', 'failed', 'interrupted'):
                            view = self.federation_job_view(job_row['id'], job)
                            details = view.get('result', {})
                            state = details.get('state', details.get('status', ''))
                            message = (
                                'Операция федерации '
                                + job_row['id']
                                + ': '
                                + (
                                    'готово'
                                    if job['state'] == 'complete'
                                    else 'завершение не подтверждено'
                                )
                                + '.'
                            )
                            if state:
                                message += '\nСостояние заявки: ' + state + '.'
                            if details.get('delivery_pending'):
                                message += '\nДоставка на другой сервер ожидается; результат там ещё не подтверждён.'
                            if details.get('code') or details.get('safe_error'):
                                message += (
                                    '\nПричина: '
                                    + details.get('code', details.get('safe_error'))
                                    + '.'
                                )
                            self.enqueue_notice(name, uid, 'operation:' + job_row['id'], message)
                own_jobs = self.db.execute(
                    'SELECT * FROM management_jobs WHERE server=? AND uid=? AND id LIKE ?',
                    (name, uid, 'SO-%'),
                ).fetchall()
                if own_jobs:
                    root = self.management_runtime(name)
                    import server_management

                    for job_row in own_jobs:
                        job = server_management.operation_status(root, job_row['id'], actor=actor)
                        if job['status'] in ('complete', 'failed', 'interrupted'):
                            label = (
                                'готово'
                                if job['status'] == 'complete'
                                else 'завершение не подтверждено'
                            )
                            self.enqueue_notice(
                                name,
                                uid,
                                'management:' + job_row['id'],
                                'Операция сервера '
                                + job_row['id']
                                + ': '
                                + label
                                + '.\nОткройте сохранённую карточку операции для подробностей.',
                            )
                pending = self.db.execute(
                    "SELECT id,message,request_id FROM telegram_notifications WHERE server=? AND uid=? AND state='pending' ORDER BY rowid LIMIT 10",
                    (name, uid),
                ).fetchall()
                notices.extend(
                    {
                        'id': item['id'],
                        'uid': uid,
                        'message': item['message'],
                        'request_id': item['request_id'],
                    }
                    for item in pending
                )
            except Exception as error:
                LOG.warning('Telegram notification check failed type=%s', type(error).__name__)
            finally:
                self.db.commit()
        current = {
            row['uid']: row['user_id']
            for row in self.db.execute(
                'SELECT uid,user_id FROM authorizations WHERE server=?', (name,)
            )
        }
        return {
            'notifications': [
                item for item in notices if current.get(item['uid']) == recipients[item['uid']]
            ]
        }

    def invite(self, name, api, policy, uid, request_id, inviter_name=None):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 90:
            raise BotError('Запрос приглашения не принят. Откройте меню бота и повторите создание.')
        row = self.db.execute('SELECT * FROM invitations WHERE request=?', (request_id,)).fetchone()
        if row and (row['server'] != name or row['uid'] != uid):
            raise BotError('Это приглашение недоступно для управления из вашего аккаунта.')
        recovering = bool(row)
        if not row:
            if not isinstance(inviter_name, str) or not inviter_name or len(inviter_name) > 255:
                raise BotError('Имя пригласившего не подтверждено. Повторите вход через меню бота.')
            code = 'MIS-' + secrets.token_urlsafe(18)
            created_at = int(time.time())
            expires = created_at + policy['invitation_lifetime_seconds']
            self.db.execute(
                'INSERT INTO invitations '
                '(server,uid,request,code,expires,resource,inviter_name,created_at) '
                'VALUES (?,?,?,?,?,NULL,?,?)',
                (name, uid, request_id, code, expires, inviter_name, created_at),
            )
            self.db.commit()
            row = self.db.execute(
                'SELECT * FROM invitations WHERE request=?', (request_id,)
            ).fetchone()
        if row['resource']:
            return api.api('user-registration-tokens/' + row['resource'])['data']
        # Recover a POST whose response was lost without issuing a second token.
        existing = (
            next(
                (
                    x
                    for x in api.items('user-registration-tokens')
                    if x['attributes']['token'] == row['code']
                ),
                None,
            )
            if recovering
            else None
        )
        resource = (
            existing
            or api.api(
                'user-registration-tokens',
                method='POST',
                data={
                    'token': row['code'],
                    'usage_limit': policy['invitation_usage_limit'],
                    'expires_at': datetime.fromtimestamp(row['expires'], timezone.utc).isoformat(),
                },
            )['data']
        )
        self.db.execute(
            'UPDATE invitations SET resource=? WHERE request=?', (resource['id'], request_id)
        )
        self.db.commit()
        return resource

    def apply_federation(self, name, domains):
        """Fixed files and command only; preserve originals and restore on failure."""
        cfg = self.config['servers'][name]
        root = Path(cfg['stack_root'])
        hs, web = root / 'config/homeserver.yaml', root / 'config/web-config.json'
        saved = {p: p.read_bytes() for p in (hs, web, self.policy_path)}
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(3)
        backup = root / 'backups' / ('bot-federation-' + stamp)
        backup.mkdir(mode=0o700)
        for p, content in saved.items():
            target = backup / p.name
            target.write_bytes(content)
            target.chmod(0o600)
        command = [
            'docker',
            'compose',
            '--project-directory',
            str(root),
            '-f',
            str(root / 'compose.yaml'),
        ]
        try:
            homeserver = yaml.safe_load(saved[hs])
            homeserver['federation_domain_whitelist'] = domains
            hs.write_text(yaml.safe_dump(homeserver, sort_keys=False))
            website = json.loads(saved[web])
            website['room_directory'] = {'servers': list(dict.fromkeys([name] + domains))}
            web.write_text(json.dumps(website, indent=2))
            policies = json.loads(saved[self.policy_path])
            policies['servers'][name]['federation_servers'] = domains
            private_json(self.policy_path, policies)
            self.restart_healthy(command, cfg['synapse_url'])
        except Exception:
            for p, content in saved.items():
                p.write_bytes(content)
            try:
                self.restart_healthy(command, cfg['synapse_url'])
            except Exception:
                LOG.error('federation rollback health check failed')
            raise BotError(
                'Список разрешённых серверов не применён. Прежняя конфигурация восстановлена. Обратитесь к администратору.'
            )
        LOG.info('federation configuration applied server=%s count=%s', name, len(domains))

    def management_runtime(self, name):
        root = Path(self.config['servers'][name]['stack_root'])
        if (
            not root.is_absolute()
            or root.resolve() != root
            or root.stat().st_uid != 0
            or root.stat().st_mode & 0o022
        ):
            raise BotError('Каталог Misbot не прошёл проверку доверия. Проверьте его через SSH.')
        source = root / 'command_wrapper.py'
        if source.is_symlink() or source.stat().st_uid != 0 or source.stat().st_mode & 0o022:
            raise BotError('Runtime Misbot не прошёл проверку доверия. Проверьте его через SSH.')
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import command_wrapper

        command_wrapper.trusted_installation(root)
        if json.loads((root / 'installation.json').read_text()).get('backend', 'portable') not in (
            'portable',
            'existing-compose',
        ):
            raise BotError('Общий backend Misbot не подключён. Обратитесь к администратору.')
        return root

    def federation_backend(self, name):
        root = self.management_runtime(name)
        import federation

        return federation.backend(root)

    @staticmethod
    def restart_healthy(command, url):
        subprocess.run(
            command + ['restart', 'synapse'], check=True, capture_output=True, timeout=50
        )
        for _ in range(25):
            try:
                r = requests.get(url.rstrip('/') + '/_matrix/client/versions', timeout=3)
                if r.status_code == 200 and r.json().get('versions'):
                    return
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1)
        raise BotError('Запуск Synapse не подтверждён. Обратитесь к администратору.')

    def handle(self, request):
        action = request.get('action')
        if action == 'backup_snapshot':
            if set(request) != {'action'}:
                raise BotError('Параметры снимка не поддерживаются.')
            with self.lock:
                if self.db.in_transaction:
                    raise BotError('Состояние занято; повторите резервное копирование.')
                destination = self.root / 'data/backup.sqlite3'
                if destination.is_symlink():
                    raise BotError('Небезопасный путь снимка.')
                descriptor, temporary = tempfile.mkstemp(
                    prefix='.backup-', suffix='.sqlite3', dir=self.root / 'data'
                )
                try:
                    if os.geteuid() == 0:
                        os.fchown(descriptor, 0, 0)
                finally:
                    os.close(descriptor)
                try:
                    with closing(sqlite3.connect(temporary)) as saved:
                        self.db.backup(saved)
                        if saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise BotError('Целостность снимка не подтверждена.')
                    os.replace(temporary, destination)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                return {'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}
        if action == 'servers':
            return {
                name: {
                    'lifetime_seconds': p['invitation_lifetime_seconds'],
                    'usage_limit': p['invitation_usage_limit'],
                    'registration_required': p['registration_required'],
                    'federation_enabled': self.config['servers'][name].get(
                        'federation_enabled', True
                    ),
                    'management_actions': self.management_config(name)['management_actions'],
                }
                for name, p in self.policies().items()
            }
        with self.lock:
            if action == 'federation_notifications':
                name = request.get('server')
                if name not in self.apis:
                    raise BotError('Сервер уведомлений не подключён.')
                return self.notifications(name)
            uid = request.get('uid')
            matrix_action = action in (
                'matrix_identity',
                'matrix_invite',
                'matrix_tokens',
                'matrix_revoke',
            )
            if not matrix_action and (
                not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0
            ):
                raise BotError('Запрос не принят: пользователь Telegram не распознан.')
            name, api, policy = self.server(request)
            if action in (
                'manage_read',
                'manage_prepare',
                'manage_apply',
                'manage_job',
                'manage_jobs',
            ):
                return self.management(request, name, uid)
            if action == 'federation_notifications_ack':
                self.notification_admin(name, uid)
                identifier = request.get('id')
                if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
                    raise BotError('Уведомление не распознано.')
                delivery_status = request.get('delivery_status', 'sent')
                if delivery_status not in ('sent', 'failed'):
                    raise BotError('Состояние доставки не распознано.')
                self.db.execute(
                    'UPDATE telegram_notifications SET state=? WHERE id=? AND server=? AND uid=?',
                    (delivery_status, identifier, name, uid),
                )
                self.db.commit()
                return {'acknowledged': True}
            if action in ('federation', 'fed_read', 'fed_prepare', 'fed_apply'):
                try:
                    return self.federation_control(request, name, uid, policy)
                except Exception as error:
                    messages = {
                        'invitation_rate_limit': 'Приглашать серверы можно не чаще одного раза в минуту.',
                        'pending_request_exists': 'Для этого сервера уже есть активная заявка. Откройте список заявок.',
                        'peer_upgrade_required': 'На другом сервере требуется обновление Misbot для заявок сроком 30 дней.',
                        'peer_already_connected': 'Этот сервер уже добавлен в федерацию. Обновите список заявок.',
                        'api_disabled': 'API Misbot выключен. Включите его в разделе настроек федерации.',
                        'request_in_progress': 'Настройки этой заявки уже применяются. Проверьте операцию и продолжите её восстановление.',
                        'peer_identity_changed': 'Identity другого сервера изменилась. Проверьте её через SSH.',
                        'request_not_found': 'Заявка больше не найдена. Обновите список заявок.',
                        'misbot_not_found': 'Другой сервер не публикует Misbot API. Приглашение не отправлено.',
                        'invalid_misbot_discovery': 'Другой сервер вернул некорректные сведения Misbot API. Приглашение не отправлено.',
                        'invitation_unsupported': 'Другой сервер не поддерживает приглашения Misbot. Приглашение не отправлено.',
                        'remote_api_disabled': 'На другом сервере API Misbot выключен. Приглашение не отправлено.',
                        'unsupported_version': 'Версия API другого сервера не поддерживается. Приглашение не отправлено.',
                    }
                    if getattr(error, 'code', None):
                        raise BotError(
                            messages.get(
                                error.code,
                                'Операция федерации не подтверждена. Обновите список заявок и проверьте историю операций.',
                            )
                        ) from None
                    raise
            if matrix_action:
                uid = request.get('mxid')
                user = self.external(api.matrix_user, uid, name)
                if action == 'matrix_identity':
                    return {'authenticated': True, 'username': user['attributes']['username']}
                action = action.removeprefix('matrix_')
            if action == 'identity':
                try:
                    user = self.member(name, uid)
                except BotError:
                    return {'authenticated': False}
                attr = user['attributes']
                return {
                    'authenticated': True,
                    'username': attr['username'],
                    'admin': bool(attr.get('admin', attr.get('can_request_admin', False))),
                }
            if action in ('invite', 'tokens', 'token', 'revoke') and not matrix_action:
                user = self.member(name, uid)
            if action == 'invite':
                attributes = user['attributes']
                inviter_name = attributes.get('displayname') or attributes.get('username')
                return self.invite(name, api, policy, uid, request.get('request_id'), inviter_name)
            if action == 'tokens':
                offset = request.get('offset', 0)
                if not isinstance(offset, int) or offset < 0:
                    raise BotError('Страница не распознана. Откройте список приглашений заново.')
                limit = request.get('limit', 5) if matrix_action else 5
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
                    raise BotError('Размер страницы не принят. Откройте список приглашений заново.')
                pending = self.db.execute(
                    'SELECT request FROM invitations WHERE server=? AND uid=? AND resource IS NULL',
                    (name, uid),
                ).fetchall()
                for row in pending:
                    self.invite(name, api, policy, uid, row['request'])
                if matrix_action:
                    rows = self.db.execute(
                        'SELECT resource FROM invitations WHERE server=? AND uid=? AND resource IS NOT NULL '
                        'ORDER BY rowid DESC',
                        (name, uid),
                    ).fetchall()
                    # Filter before pagination. Used invitations remain visible even
                    # after their expiry; explicit revocation always hides them.
                    visible = []
                    for row in rows:
                        item = self.external(
                            api.api, 'user-registration-tokens/' + row['resource']
                        )['data']
                        if not self.db.execute(
                            'SELECT 1 FROM invitations WHERE server=? AND uid=? AND resource=?',
                            (name, uid, row['resource']),
                        ).fetchone():
                            raise BotError('Приглашение больше не принадлежит этому аккаунту.')
                        attr = item['attributes']
                        expired = (
                            attr.get('expires_at')
                            and datetime.fromisoformat(
                                attr['expires_at'].replace('Z', '+00:00')
                            ).timestamp()
                            <= time.time()
                        )
                        if attr.get('revoked_at') or (expired and not attr.get('times_used', 0)):
                            continue
                        visible.append(item)
                        if len(visible) > offset + limit:
                            break
                    return {
                        'tokens': visible[offset : offset + limit],
                        'has_more': len(visible) > offset + limit,
                    }
                rows = self.db.execute(
                    'SELECT resource FROM invitations WHERE server=? AND uid=? AND resource IS NOT NULL '
                    'ORDER BY rowid DESC LIMIT 6 OFFSET ?',
                    (name, uid, offset),
                ).fetchall()
                return {
                    'tokens': [
                        api.api('user-registration-tokens/' + x['resource'])['data']
                        for x in rows[:5]
                    ],
                    'has_more': len(rows) > 5,
                }
            if action == 'token':
                resource = request.get('resource')
                if (
                    not isinstance(resource, str)
                    or not self.db.execute(
                        'SELECT 1 FROM invitations WHERE server=? AND uid=? AND resource=?',
                        (name, uid, resource),
                    ).fetchone()
                ):
                    raise BotError('Приглашение недоступно. Откройте ваш список заново.')
                return api.api('user-registration-tokens/' + resource)['data']
            if action == 'revoke':
                resource = request.get('resource')
                own = self.db.execute(
                    'SELECT 1 FROM invitations WHERE server=? AND uid=? AND resource=?',
                    (name, uid, resource),
                ).fetchone()
                if not own:
                    raise BotError('Отзыв доступен только для ваших приглашений.')
                current = api.api('user-registration-tokens/' + resource)['data']
                if current['attributes'].get('revoked_at'):
                    return current
                return api.api('user-registration-tokens/' + resource + '/revoke', method='POST')[
                    'data'
                ]
            if action == 'auth_start':
                now = int(time.time())
                pending = self.db.execute(
                    'SELECT * FROM auth_requests WHERE server=? AND uid=? AND expires>? AND error IS NULL',
                    (name, uid, now),
                ).fetchone()
                if pending and pending['user_code'] and pending['verification_uri']:
                    return {
                        'url': pending['verification_uri'],
                        'user_code': pending['user_code'],
                        'expires_in': pending['expires'] - now,
                    }
                authorization = api.device_start()
                key = secrets.token_urlsafe(24)
                uri = authorization.get(
                    'verification_uri_complete', authorization['verification_uri']
                )
                self.db.execute(
                    'DELETE FROM auth_requests WHERE (uid=? AND server=?) OR expires<?',
                    (uid, name, now),
                )
                self.db.execute(
                    'INSERT INTO auth_requests (request,server,uid,device_code,expires,interval,next_poll,error,user_code,verification_uri) '
                    'VALUES (?,?,?,?,?,?,?,NULL,?,?)',
                    (
                        key,
                        name,
                        uid,
                        authorization['device_code'],
                        now + authorization['expires_in'],
                        authorization.get('interval', 5),
                        now + authorization.get('interval', 5),
                        authorization['user_code'],
                        uri,
                    ),
                )
                self.db.commit()
                return {
                    'url': uri,
                    'user_code': authorization['user_code'],
                    'expires_in': authorization['expires_in'],
                }
            if action == 'logout':
                self.db.execute('DELETE FROM authorizations WHERE server=? AND uid=?', (name, uid))
                self.db.execute('DELETE FROM auth_requests WHERE server=? AND uid=?', (name, uid))
                self.db.commit()
                return True
            raise BotError('Команда не распознана. Откройте меню бота.')


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(110)
        try:
            request = receive(self.rfile)
            peer = self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')
            )
            _, uid, _ = struct.unpack('3i', peer)
            check_peer(uid, request.get('action'))
            result = self.server.bridge.handle(request)
            answer = {'ok': True, 'result': result}
        except BotError as error:
            answer = {'ok': False, 'error': str(error)}
        except Exception as error:
            # Exception text may contain a proxy password, ID token, or an HTTP URL.
            LOG.error('operation failed type=%s', type(error).__name__)
            answer = {'ok': False, 'error': 'Не удалось выполнить запрос. Повторите позже.'}
        self.wfile.write(json.dumps(answer).encode() + b'\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--socket-directory', type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    bridge = Bridge(args.config)
    threading.Thread(target=bridge.auth_loop, daemon=True).start()
    run = args.socket_directory or bridge.root / 'bot-run'
    run.mkdir(mode=0o750, exist_ok=True)
    # UMask=0077 removes group bits during mkdir; set them explicitly afterwards.
    os.chown(run, 0, 10001)
    run.chmod(0o750)
    sock = run / 'control.sock'
    sock.unlink(missing_ok=True)
    with socketserver.ThreadingUnixStreamServer(str(sock), Handler) as server:
        server.daemon_threads = True
        server.bridge = bridge
        os.chown(sock, 0, 10001)
        sock.chmod(0o660)
        LOG.info('control service started')
        server.serve_forever()


if __name__ == '__main__':
    main()
