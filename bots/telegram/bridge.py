"""Root service: narrowly scoped MAS and federation operations over a Unix socket.

The Telegram container receives neither MAS admin credentials nor Docker access.
MAS device authorization and current MAS privileges are checked here, not in the UI.
"""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import socketserver
import sqlite3
import subprocess
import struct
import threading
import time

import requests
import yaml

from common import BotError, domain, private_json, read_json, receive
from mas import MAS

LOG = logging.getLogger('bridge')


def check_peer(uid, action):
    matrix_actions = {'servers', 'matrix_identity', 'matrix_invite', 'matrix_tokens', 'matrix_revoke'}
    if uid == 0:
        return
    if uid == 10002 and action in matrix_actions:
        return
    if uid == 10001 and action not in matrix_actions - {'servers'}:
        return
    raise BotError('Это действие недоступно этому боту.')




class Bridge:
    def __init__(self, config_path, api_factory=MAS):
        self.path = Path(config_path)
        self.config = read_json(self.path)
        self.root = self.path.parent
        self.policy_path = self.root / 'bot-policy.json'
        self.db = sqlite3.connect(self.root / 'data/bot-control.sqlite3', check_same_thread=False)
        os.chmod(self.root / 'data/bot-control.sqlite3', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS invitations (
            server TEXT, uid INTEGER, request TEXT UNIQUE, code TEXT UNIQUE,
            expires INTEGER, resource TEXT, inviter_name TEXT, created_at INTEGER,
            PRIMARY KEY(server, uid, code));
        CREATE TABLE IF NOT EXISTS auth_requests (
            request TEXT PRIMARY KEY, server TEXT, uid INTEGER, device_code TEXT, expires INTEGER, interval INTEGER, next_poll INTEGER, error TEXT,
            user_code TEXT, verification_uri TEXT);
        CREATE TABLE IF NOT EXISTS authorizations (
            server TEXT, uid INTEGER, user_id TEXT, expires INTEGER, PRIMARY KEY(server,uid));
        CREATE TABLE IF NOT EXISTS confirmations (
            id TEXT PRIMARY KEY, server TEXT, uid INTEGER, action TEXT, domain TEXT, expires INTEGER);
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(auth_requests)')}
        for name in ('user_code', 'verification_uri'):
            if name not in columns:
                self.db.execute('ALTER TABLE auth_requests ADD COLUMN ' + name + ' TEXT')
        invitation_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(invitations)')}
        for name, kind in (('inviter_name', 'TEXT'), ('created_at', 'INTEGER')):
            if name not in invitation_columns:
                self.db.execute('ALTER TABLE invitations ADD COLUMN ' + name + ' ' + kind)
        self.db.commit()
        self.apis = {name: api_factory(cfg) for name, cfg in self.config['servers'].items()}
        self.lock = threading.RLock()

    def policies(self):
        return read_json(self.policy_path)['servers']

    def server(self, request):
        name = request.get('server')
        if name not in self.apis:
            raise BotError('Сервер не подключён к этому боту.')
        return name, self.apis[name], self.policies()[name]

    def member(self, name, uid):
        self.poll_auths()
        row = self.db.execute('SELECT * FROM authorizations WHERE server=? AND uid=?', (name, uid)).fetchone()
        if not row or row['expires'] <= time.time():
            raise BotError('Сначала войди в Misbot: /start.')
        return self.apis[name].active_user(row['user_id'])

    def admin(self, name, uid):
        user = self.member(name, uid)
        attr = user['attributes']
        if not attr.get('admin', attr.get('can_request_admin', False)):
            raise BotError('Управлять федерацией может только администратор сервера.')
        return user

    def poll_auths(self):
        with self.lock:
            now = int(time.time())
            rows = self.db.execute('SELECT * FROM auth_requests WHERE expires>? AND next_poll<=? AND error IS NULL', (now, now)).fetchall()
            for row in rows:
                api = self.apis[row['server']]
                try:
                    status, token = api.device_token(row['device_code'])
                    if status == 200:
                        user = api.device_user(token)
                        self.db.execute('INSERT OR REPLACE INTO authorizations VALUES (?,?,?,?)',
                            (row['server'], row['uid'], user['id'], now + 86400))
                        self.db.execute('DELETE FROM auth_requests WHERE request=?', (row['request'],))
                        LOG.info('account authorization completed server=%s', row['server'])
                    elif token.get('error') in ('authorization_pending', 'slow_down'):
                        interval = row['interval'] + (5 if token.get('error') == 'slow_down' else 0)
                        self.db.execute('UPDATE auth_requests SET interval=?,next_poll=? WHERE request=?',
                            (interval, now + interval, row['request']))
                    else:
                        self.db.execute('UPDATE auth_requests SET error=? WHERE request=?', ('denied', row['request']))
                except BotError:
                    self.db.execute('UPDATE auth_requests SET error=? WHERE request=?', ('denied', row['request']))
                except Exception as error:
                    self.db.execute('UPDATE auth_requests SET next_poll=? WHERE request=?', (now + 15, row['request']))
                    LOG.error('device authorization poll failed type=%s', type(error).__name__)
                self.db.commit()

    def auth_loop(self):
        while True:
            try:
                self.poll_auths()
            except Exception as error:
                LOG.error('authorization loop failed type=%s', type(error).__name__)
            time.sleep(2)

    def invite(self, name, api, policy, uid, request_id, inviter_name=None):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 90:
            raise BotError('Недопустимый запрос приглашения.')
        row = self.db.execute('SELECT * FROM invitations WHERE request=?', (request_id,)).fetchone()
        if row and (row['server'] != name or row['uid'] != uid):
            raise BotError('Приглашение принадлежит другому пользователю.')
        recovering = bool(row)
        if not row:
            if not isinstance(inviter_name, str) or not inviter_name or len(inviter_name) > 255:
                raise BotError('Не удалось сохранить имя пригласившего.')
            code = 'MIS-' + secrets.token_urlsafe(18)
            created_at = int(time.time())
            expires = created_at + policy['invitation_lifetime_seconds']
            self.db.execute('INSERT INTO invitations '
                '(server,uid,request,code,expires,resource,inviter_name,created_at) '
                'VALUES (?,?,?,?,?,NULL,?,?)',
                (name, uid, request_id, code, expires, inviter_name, created_at))
            self.db.commit()
            row = self.db.execute('SELECT * FROM invitations WHERE request=?', (request_id,)).fetchone()
        if row['resource']:
            return api.api('user-registration-tokens/' + row['resource'])['data']
        # Recover a POST whose response was lost without issuing a second token.
        existing = next((x for x in api.items('user-registration-tokens')
                         if x['attributes']['token'] == row['code']), None) if recovering else None
        resource = existing or api.api('user-registration-tokens', method='POST', data={
            'token': row['code'], 'usage_limit': policy['invitation_usage_limit'],
            'expires_at': datetime.fromtimestamp(row['expires'], timezone.utc).isoformat()})['data']
        self.db.execute('UPDATE invitations SET resource=? WHERE request=?', (resource['id'], request_id))
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
        command = ['docker', 'compose', '--project-directory', str(root), '-f', str(root / 'compose.yaml')]
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
            raise BotError('Не удалось применить список. Предыдущая конфигурация восстановлена; обратись к администратору.')
        LOG.info('federation configuration applied server=%s count=%s', name, len(domains))

    @staticmethod
    def restart_healthy(command, url):
        subprocess.run(command + ['restart', 'synapse'], check=True, capture_output=True, timeout=50)
        for _ in range(25):
            try:
                r = requests.get(url.rstrip('/') + '/_matrix/client/versions', timeout=3)
                if r.status_code == 200 and r.json().get('versions'):
                    return
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1)
        raise BotError('Synapse не подтвердил запуск.')

    def handle(self, request):
        with self.lock:
            action = request.get('action')
            if action == 'servers':
                return {name: {'lifetime_seconds': p['invitation_lifetime_seconds'],
                    'usage_limit': p['invitation_usage_limit'], 'registration_required': p['registration_required']}
                    for name, p in self.policies().items()}
            uid = request.get('uid')
            matrix_action = action in ('matrix_identity', 'matrix_invite', 'matrix_tokens', 'matrix_revoke')
            if not matrix_action and (not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0):
                raise BotError('Недопустимый пользователь Telegram.')
            name, api, policy = self.server(request)
            if matrix_action:
                uid = request.get('mxid')
                user = api.matrix_user(uid, name)
                if action == 'matrix_identity':
                    return {'authenticated': True, 'username': user['attributes']['username']}
                action = action.removeprefix('matrix_')
            if action == 'identity':
                try:
                    user = self.member(name, uid)
                except BotError:
                    return {'authenticated': False}
                attr = user['attributes']
                return {'authenticated': True, 'username': attr['username'],
                        'admin': bool(attr.get('admin', attr.get('can_request_admin', False)))}
            if action in ('invite', 'tokens', 'revoke') and not matrix_action:
                user = self.member(name, uid)
            if action == 'invite':
                attributes = user['attributes']
                inviter_name = attributes.get('displayname') or attributes.get('username')
                return self.invite(name, api, policy, uid, request.get('request_id'), inviter_name)
            if action == 'tokens':
                offset = request.get('offset', 0)
                if not isinstance(offset, int) or offset < 0:
                    raise BotError('Недопустимая страница.')
                limit = request.get('limit', 5) if matrix_action else 5
                if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
                    raise BotError('Недопустимый размер страницы.')
                pending = self.db.execute('SELECT request FROM invitations WHERE server=? AND uid=? AND resource IS NULL',
                    (name, uid)).fetchall()
                for row in pending:
                    self.invite(name, api, policy, uid, row['request'])
                if matrix_action:
                    rows = self.db.execute('SELECT resource FROM invitations WHERE server=? AND uid=? AND resource IS NOT NULL '
                        'ORDER BY rowid DESC', (name, uid)).fetchall()
                    # Filter before pagination. Used invitations remain visible even
                    # after their expiry; explicit revocation always hides them.
                    visible = []
                    for row in rows:
                        item = api.api('user-registration-tokens/' + row['resource'])['data']
                        attr = item['attributes']
                        expired = attr.get('expires_at') and datetime.fromisoformat(attr['expires_at'].replace('Z', '+00:00')).timestamp() <= time.time()
                        if attr.get('revoked_at') or (expired and not attr.get('times_used', 0)):
                            continue
                        visible.append(item)
                        if len(visible) > offset + limit:
                            break
                    return {'tokens': visible[offset:offset + limit], 'has_more': len(visible) > offset + limit}
                rows = self.db.execute('SELECT resource FROM invitations WHERE server=? AND uid=? AND resource IS NOT NULL '
                    'ORDER BY rowid DESC LIMIT 6 OFFSET ?', (name, uid, offset)).fetchall()
                return {'tokens': [api.api('user-registration-tokens/' + x['resource'])['data'] for x in rows[:5]],
                    'has_more': len(rows) > 5}
            if action == 'revoke':
                resource = request.get('resource')
                own = self.db.execute('SELECT 1 FROM invitations WHERE server=? AND uid=? AND resource=?',
                                      (name, uid, resource)).fetchone()
                if not own:
                    raise BotError('Можно отозвать только своё приглашение.')
                current = api.api('user-registration-tokens/' + resource)['data']
                if current['attributes'].get('revoked_at'):
                    return current
                return api.api('user-registration-tokens/' + resource + '/revoke', method='POST')['data']
            if action == 'auth_start':
                now = int(time.time())
                pending = self.db.execute('SELECT * FROM auth_requests WHERE server=? AND uid=? AND expires>? AND error IS NULL',
                    (name, uid, now)).fetchone()
                if pending and pending['user_code'] and pending['verification_uri']:
                    return {'url': pending['verification_uri'], 'user_code': pending['user_code'],
                            'expires_in': pending['expires'] - now}
                authorization = api.device_start()
                key = secrets.token_urlsafe(24)
                uri = authorization.get('verification_uri_complete', authorization['verification_uri'])
                self.db.execute('DELETE FROM auth_requests WHERE (uid=? AND server=?) OR expires<?', (uid, name, now))
                self.db.execute('INSERT INTO auth_requests (request,server,uid,device_code,expires,interval,next_poll,error,user_code,verification_uri) '
                    'VALUES (?,?,?,?,?,?,?,NULL,?,?)',
                    (key, name, uid, authorization['device_code'], now + authorization['expires_in'],
                     authorization.get('interval', 5), now + authorization.get('interval', 5), authorization['user_code'], uri))
                self.db.commit()
                return {'url': uri,
                        'user_code': authorization['user_code'], 'expires_in': authorization['expires_in']}
            if action == 'logout':
                self.db.execute('DELETE FROM authorizations WHERE server=? AND uid=?', (name, uid))
                self.db.execute('DELETE FROM auth_requests WHERE server=? AND uid=?', (name, uid))
                self.db.commit()
                return True
            if action in ('federation', 'fed_prepare', 'fed_apply'):
                self.admin(name, uid)  # Never trust a cached role or a role sent by the container.
                if action == 'federation':
                    return policy['federation_servers']
                if action == 'fed_prepare':
                    operation = request.get('operation')
                    if operation not in ('add', 'remove'):
                        raise BotError('Неизвестная операция.')
                    target = domain(request.get('domain'))
                    if operation == 'remove' and target == name:
                        raise BotError('Свой сервер остаётся в списке.')
                    key = secrets.token_urlsafe(16)
                    self.db.execute('DELETE FROM confirmations WHERE expires<?', (int(time.time()),))
                    self.db.execute('INSERT INTO confirmations VALUES (?,?,?,?,?,?)',
                        (key, name, uid, operation, target, int(time.time()) + 300))
                    self.db.commit()
                    return {'confirmation': key, 'domain': target, 'operation': operation, 'currently_open': policy['federation_servers'] is None}
                row = self.db.execute('SELECT * FROM confirmations WHERE id=? AND uid=? AND server=?',
                    (request.get('confirmation'), uid, name)).fetchone()
                if not row or row['expires'] <= time.time():
                    raise BotError('Подтверждение истекло или уже использовано.')
                domains = list(policy['federation_servers'] or [name])
                if row['action'] == 'add' and row['domain'] not in domains:
                    domains.append(row['domain'])
                if row['action'] == 'remove' and row['domain'] in domains:
                    domains.remove(row['domain'])
                self.apply_federation(name, sorted(domains))
                self.db.execute('DELETE FROM confirmations WHERE id=?', (row['id'],))
                self.db.commit()
                return sorted(domains)
            raise BotError('Неизвестная операция.')


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(110)
        try:
            request = receive(self.rfile)
            peer = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            _, uid, _ = struct.unpack('3i', peer)
            check_peer(uid, request.get('action'))
            result = self.server.bridge.handle(request)
            answer = {'ok': True, 'result': result}
        except BotError as error:
            answer = {'ok': False, 'error': str(error)}
        except Exception as error:
            # Exception text may contain a proxy password, ID token, or an HTTP URL.
            LOG.error('operation failed type=%s', type(error).__name__)
            answer = {'ok': False, 'error': 'Сервис временно недоступен. Повтори позже.'}
        self.wfile.write(json.dumps(answer).encode() + b'\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    bridge = Bridge(args.config)
    threading.Thread(target=bridge.auth_loop, daemon=True).start()
    run = bridge.root / 'bot-run'
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
