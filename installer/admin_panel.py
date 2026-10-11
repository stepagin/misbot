"""Private admin preferences, registration observer and durable message outbox."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import configuration
import data_migrations
import lifecycle
import releases
import state_store
from postgres_setup import literal

ACTIONS = {'panel-status', 'panel-version', 'panel-preferences', 'panel-welcome',
           'panel-broadcast', 'panel-statistics', 'panel-update', 'panel-outbox', 'panel-ack'}
DEFAULT_WELCOME = ('Добро пожаловать на сервер {domain}! Здесь можно общаться в личных '
                   'и групповых чатах, делиться файлами и звонить через Matrix. '
                   'Личные чаты поддерживают сквозное шифрование. Приглашения: @referral-bot:{domain}. '
                   'Этот бот присылает новости сервера и не отвечает на сообщения.')
PREFERENCES = {'updates': True, 'registrations': True, 'statistics': True}


def owner(root):
    root = Path(root)
    identity = root / 'config/admin-panel-identity.json'
    if identity.exists():
        return configuration.read_private_json(identity)['administrator_mxid']
    config = root / 'config/federation-bot.json'
    if config.exists():
        admins = json.loads(config.read_text()).get('administrator_mxids')
        if admins:
            return admins[0]
    return '@matrix-admin:' + configuration.read_private_json(root / 'installation.json')['domain']


def authorize(backend, actor):
    if actor != owner(backend.root):
        raise __import__('federation_protocol').ProtocolError('admin_unconfirmed')
    backend.authorize(actor)


class Panel:
    def __init__(self, root, clock=time.time):
        self.root = Path(root)
        self.clock = clock
        self.domain = configuration.read_private_json(self.root / 'installation.json')['domain']
        self.folder = configuration.directory(self.root / 'data/admin-panel')
        self.path = self.folder / 'panel.sqlite'
        if self.path.is_symlink():
            raise RuntimeError('Unsafe panel database')
        data_migrations.migrate(self.root)
        with self.database() as db:
            for key, value in {**PREFERENCES, 'welcome': DEFAULT_WELCOME, 'enabled_at': int(self.clock()),
                               'month': datetime.fromtimestamp(self.clock(), timezone.utc).strftime('%Y-%m')}.items():
                db.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', (key, json.dumps(value)))
        self.path.chmod(0o600)

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def settings(self):
        with self.database() as db:
            return {row['key']: json.loads(row['value']) for row in db.execute('SELECT * FROM settings')}

    @staticmethod
    def set(db, key, value):
        db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))

    @staticmethod
    def enqueue(db, key, recipient, via, body):
        identifier = hashlib.sha256((key + '\0' + recipient).encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO outbox(id,recipient,via,body) VALUES (?,?,?,?)',
                   (identifier, recipient, via, body))

    def notice(self, key, body):
        with self.database() as db:
            self.enqueue(db, key, owner(self.root), 'admin', body)

    def bot_ids(self):
        ids = {'@referral-bot:' + self.domain, '@server-guide:' + self.domain}
        for name in ('federation-bot.json', 'welcome-bot.json'):
            path = self.root / 'config' / name
            if path.exists():
                ids.add(json.loads(path.read_text())['user_id'])
        return ids

    def query(self, sql):
        statement="BEGIN READ ONLY; SET LOCAL statement_timeout='30s';\n"+sql+"\nCOMMIT;"
        metadata=configuration.read_private_json(self.root/'installation.json')
        if metadata.get('backend')=='existing-compose':
            import tempfile
            import yaml
            from existing_compose import Installation
            installation=Installation(self.root)
            config=yaml.safe_load((self.root/installation.model['identity']['synapse_config']).read_text())
            database=config['database']['args']['database']
            if database not in installation.databases:raise RuntimeError('Statistics database is outside accepted scope')
            with tempfile.TemporaryFile() as incoming:
                incoming.write(statement.encode());incoming.seek(0)
                _,raw=installation.pg('psql','-d',database,'-X','-qAt','-v','ON_ERROR_STOP=1','-f','-',incoming=incoming)
            return json.loads(raw)
        lifecycle.configure(self.root, 'admin-panel')
        return json.loads(lifecycle.postgres('psql', '-d', 'synapse', '-X', '-qAt',
            '-v', 'ON_ERROR_STOP=1', '-f', '-', stdin=statement).strip())

    def user_page(self, cursor):
        bots = ','.join(literal(value) for value in sorted(self.bot_ids()))
        return self.query("SELECT coalesce(json_agg(u),'[]') FROM (SELECT name AS mxid, creation_ts AS created, "
            "deactivated=0 AND is_guest=0 AS active FROM users WHERE name>" + literal(cursor) +
            " AND name NOT IN (" + bots + ") AND appservice_id IS NULL ORDER BY name LIMIT 500) u;")

    def observe_users(self):
        preferences = self.settings()
        cursor = ''
        while True:
            users = self.user_page(cursor)
            if not users:
                break
            with self.database() as db:
                for user in users:
                    mxid = user['mxid']
                    if not mxid.endswith(':' + self.domain):
                        continue
                    known = db.execute('SELECT 1 FROM users WHERE mxid=?', (mxid,)).fetchone()
                    db.execute('INSERT OR REPLACE INTO users VALUES (?,?,?)',
                               (mxid, user['created'], int(user['active'])))
                    if known or not user['active'] or (user['created'] or 0) < preferences['enabled_at']:
                        continue
                    body = preferences['welcome'].replace('{domain}', self.domain)
                    self.enqueue(db, 'welcome:' + mxid, mxid, 'welcome', body)
                    if preferences['registrations']:
                        self.enqueue(db, 'registration:' + mxid, owner(self.root), 'admin',
                                     'Новый пользователь зарегистрировался: ' + mxid)
            cursor = users[-1]['mxid']
            if len(users) < 500:
                break

    def statistics(self, period, start=None, end=None):
        durations = {'day': 86400, 'week': 7 * 86400, 'month': 30 * 86400, 'all': None}
        if period not in durations:
            raise ValueError('Unknown statistics period')
        end = self.clock() if end is None else end
        start = (0 if durations[period] is None else end - durations[period]) if start is None else start
        bots = ','.join(literal(value) for value in sorted(self.bot_ids()))
        sql = f'''SELECT json_build_object('messages', (SELECT count(*) FROM events e
            WHERE e.type IN ('m.room.message','m.room.encrypted') AND NOT e.outlier
            AND e.received_ts >= {int(start * 1000)} AND e.received_ts < {int(end * 1000)}
            AND EXISTS (SELECT 1 FROM users u WHERE u.name=e.sender AND u.appservice_id IS NULL)
            AND e.sender NOT IN ({bots})
            AND NOT EXISTS (SELECT 1 FROM rejections r WHERE r.event_id=e.event_id)
            AND NOT EXISTS (SELECT 1 FROM room_memberships m WHERE m.room_id=e.room_id
                AND (m.user_id IN ({bots}) OR EXISTS (SELECT 1 FROM users b WHERE b.name=m.user_id AND b.appservice_id IS NOT NULL))
                AND m.membership IN ('join','invite'))),
            'registrations', (SELECT count(*) FROM users WHERE creation_ts >= {int(start)}
                AND creation_ts < {int(end)} AND is_guest=0 AND appservice_id IS NULL
                AND name NOT IN ({bots})));'''
        return {'period': period, 'start_utc': datetime.fromtimestamp(start, timezone.utc).isoformat(),
                'end_utc': datetime.fromtimestamp(end, timezone.utc).isoformat(), **self.query(sql),
                'calls': None, 'average_call_seconds': None,
                'note': 'Сообщения: события обычных и зашифрованных сообщений местных пользователей; '
                        'комнаты с ботами Misbot исключены. В E2EE невозможно отличить все служебные '
                        'события от сообщений. Число и длительность звонков MatrixRTC недоступны.'}

    def check_releases(self, force=False):
        settings = self.settings()
        now = self.clock()
        if not force and now - settings.get('release_checked_at', 0) < releases.INTERVAL:
            return settings.get('release')
        # Record attempts as well: a GitHub outage must not cause an hourly request storm.
        with self.database() as db:
            self.set(db, 'release_checked_at', now)
        release = releases.latest()
        with self.database() as db:
            self.set(db, 'release', release)
            if (release and settings['updates'] and releases.minor_update(releases.installed(self.root), release['version'])
                    and releases.version(release['version']) > releases.version(settings.get('notified_version', '0.0.0'))):
                self.enqueue(db, 'release:' + release['version'], owner(self.root), 'admin',
                    'Доступна новая версия Misbot ' + release['version'] + '. Установлена ' + releases.installed(self.root)
                    + '.\n' + release['notes'] + '\n' + release['url']
                    + '\nОбновление: '+releases.comparison(self.root,release)['command']
                    + (' или команда «обновить» в этом чате.' if releases.comparison(self.root,release)['update_supported'] else '.'))
                self.set(db, 'notified_version', release['version'])
        return release

    def maintenance(self):
        self.observe_users()
        settings = self.settings()
        if settings['updates']:
            try:
                self.check_releases()
            except (OSError, ValueError, RuntimeError):
                pass
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        month = now.strftime('%Y-%m')
        if settings['month'] != month:
            if settings['statistics']:
                end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                start = (end - timedelta(days=1)).replace(day=1)
                result = self.statistics('month', start.timestamp(), end.timestamp())
                self.notice('monthly:' + start.strftime('%Y-%m'), 'Статистика за ' + start.strftime('%Y-%m')
                            + '\n' + statistics_text(result))
            with self.database() as db:
                self.set(db, 'month', month)
        from release_update import recover_job, result_notice
        recover_job(self.root)
        result_notice(self)

    def handle(self, action, argument, event):
        if action == 'panel-status':
            return {'installed': releases.installed(self.root), 'preferences': self.settings_view(),
                    'welcome': self.settings()['welcome'], 'update_command': releases.comparison(self.root,None)['command']}
        if action == 'panel-version':
            return releases.comparison(self.root, self.check_releases(True))
        if action == 'panel-preferences':
            key, enabled = argument.get('key'), argument.get('enabled')
            if key not in PREFERENCES or not isinstance(enabled, bool):
                raise ValueError('Invalid preference')
            with self.database() as db:
                self.set(db, key, enabled)
                if not enabled:
                    family = {'updates': 'Доступна новая версия Misbot %', 'registrations': 'Новый пользователь зарегистрировался:%',
                              'statistics': 'Статистика за %'}[key]
                    db.execute("UPDATE outbox SET state='cancelled' WHERE via='admin' AND state='pending' AND body LIKE ?", (family,))
            return self.settings_view()
        if action == 'panel-welcome':
            if argument is None:
                return {'text': self.settings()['welcome']}
            body = text(argument)
            with self.database() as db:
                self.set(db, 'welcome', body)
            return {'saved': True}
        if action == 'panel-broadcast':
            body = 'Сообщение от администратора сервера ' + self.domain + ',\n\n' + text(argument)
            self.observe_users()
            with self.database() as db:
                users = list(db.execute('SELECT mxid FROM users WHERE active=1 ORDER BY mxid'))
                for user in users:
                    self.enqueue(db, 'broadcast:' + event, user['mxid'], 'welcome', body)
            return {'queued': len(users), 'message': 'Рассылка поставлена в очередь; отправка ограничена по скорости.'}
        if action == 'panel-statistics':
            return self.statistics(argument)
        if action == 'panel-update':
            from release_update import queue
            return queue(self.root, argument, event)
        if action == 'panel-outbox':
            with self.database() as db:
                return {'items': [dict(row) for row in db.execute("SELECT id,recipient,via,body FROM outbox "
                    "WHERE state='pending' AND next_at<=? ORDER BY rowid LIMIT 2", (self.clock(),))]}
        if action == 'panel-ack':
            identifier, delivered = argument.get('id'), argument.get('delivered')
            if not isinstance(identifier, str) or not isinstance(delivered, bool):
                raise ValueError('Invalid delivery receipt')
            with self.database() as db:
                if delivered:
                    db.execute("UPDATE outbox SET state='sent' WHERE id=? AND state='pending'", (identifier,))
                else:
                    db.execute("UPDATE outbox SET attempts=attempts+1,next_at=? WHERE id=? AND state='pending'",
                               (self.clock() + 300, identifier))
            return {'recorded': True}
        raise ValueError('Unknown panel action')

    def settings_view(self):
        saved = self.settings()
        return {key: saved[key] for key in PREFERENCES}


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 8000:
        raise ValueError('Message must contain 1–8000 UTF-8 bytes')
    return value.strip()


def statistics_text(result):
    return ('Сообщений: ' + str(result['messages']) + '\nРегистраций: ' + str(result['registrations'])
            + '\nЗвонки: недоступно\nСредняя длительность звонка: недоступно\n' + result['note'])
