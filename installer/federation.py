"""Shared federation backend. Remote exchange has no general admin dispatcher."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import time
import urllib.parse

import configuration as config
import configuration_schema as schema
import federation_protocol as protocol
import lifecycle
import state_store

TERMINAL = ('rejected', 'cancelled', 'expired')
ACTIVE = ('pending', 'accepted', 'applying', 'partially_applied')
INVITATION_INTERVAL = 60
REQUEST = re.compile(r'MB-[0-9a-f]{32}\Z')
ACTIONS = frozenset(
    (
        'servers',
        'check',
        'add',
        'remove',
        'invite',
        'requests',
        'approve',
        'reject',
        'cancel',
        'retry',
        'request-status',
        'peers',
        'operations',
        'recover',
        'rollback',
        'api-status',
        'api-enable',
        'api-disable',
        'clear-rejected',
    )
)


def failure(code, root=None, action=None):
    prefix = 'sudo misbot' + (' --directory ' + shlex.quote(str(root)) if root else '')
    messages = {
        'misbot_not_found': 'На другом сервере не обнаружен Misbot API. Установите или подключите Misbot на том сервере перед приглашением.',
        'invalid_misbot_discovery': 'Другой сервер не подтвердил поддерживаемый Misbot API. Проверьте его установку и HTTPS discovery.',
        'invitation_unsupported': 'Другой сервер не объявляет поддержку приглашений федерации Misbot. Проверьте его настройки и версию.',
        'remote_api_disabled': 'API Misbot другого сервера выключен. Его администратор должен включить API перед приглашением.',
        'peer_upgrade_required': 'Другому серверу требуется обновление Misbot для приглашений сроком 30 дней.',
        'unsupported_version': 'Версия протокола другого сервера несовместима. Проверьте версии Misbot обеих установок.',
    }
    return {
        'stage': 'federation',
        'status': 'error',
        'code': code,
        'explanation': messages.get(
            code,
            'Операция не подтверждена. Проверьте status и существующие checkpoints; неверный ввод исправьте по справке.',
        ),
        'next_action': prefix
        + ' federation '
        + (
            {
                'add': 'add --help',
                'remove': 'remove --help',
                'check': 'check --help',
                'invite': 'invite --help',
            }.get(action, 'help')
        ),
        'status_command': prefix + ' status',
    }


def directory(root):
    return config.directory(Path(root) / 'data/federation')


def setup(root, metadata, preview=False):
    folder = directory(root)
    key = folder / 'identity.pem'
    path = folder / 'state.json'
    if metadata.get('federation_seeded') and (not key.is_file() or not path.is_file()):
        raise RuntimeError(
            'Federation identity is missing; restore its private backup, do not regenerate'
        )
    if path.exists() and not key.is_file():
        raise RuntimeError('Saved federation state has no private key; restore its backup')
    if not key.exists():
        state_store.create_rsa_key(key)
    if not path.exists():
        state_store.write_json(
            path,
            {
                'format': 1,
                'instance_id': secrets.token_hex(16),
                'server_name': metadata['domain'],
                'requests': {},
                'peers': {},
                'nonces': {},
                'events': {},
                'api_enabled': True,
            },
        )
    saved = config.read_private_json(path)
    if saved.get('format') != 1 or saved.get('server_name') != metadata['domain']:
        raise RuntimeError('Foreign federation identity')
    metadata['federation_seeded'] = True
    # Socket parents are traversable only in the container mounts; state is not mounted.
    run = Path(root) / 'run/federation'
    run.mkdir(parents=True, exist_ok=True)
    run.chmod(0o755)
    return saved


class Backend:
    def __init__(self, root, transport=None, admin=None, apply=None, admission=True):
        self.root = Path(root)
        self.transport = transport or protocol.https
        self.admin = admin or self.check_admin
        self.applier = apply or self.apply_policy
        self.admission = admission

    @property
    def path(self):
        return self.root / 'data/federation/state.json'

    @property
    def key(self):
        return self.root / 'data/federation/identity.pem'

    def load(self):
        data = config.read_private_json(self.path)
        if (
            data.get('format') != 1
            or data.get('server_name')
            != config.read_private_json(self.root / 'installation.json')['domain']
        ):
            raise protocol.ProtocolError('identity_mismatch')
        # Additive format-1 settings: old backups retain identity and deadlines.
        if type(data.get('api_enabled', True)) is not bool:
            raise protocol.ProtocolError('invalid_api_setting')
        return data

    def save(self, data):
        state_store.write_json(self.path, data)

    def guard(self):
        return state_store.operation_guard(self.root)

    def auto_accept(self):
        return config.load(self.root).get('FEDERATION_AUTO_ACCEPT', 'false') == 'true'

    def api_enabled(self, data=None):
        data = self.load() if data is None else data
        return data.get('api_enabled', True)

    def require_api(self, data):
        if not self.api_enabled(data):
            raise protocol.ProtocolError('api_disabled')

    def matrix_operation(self, action, argument):
        from federation_runtime import matrix_operation

        return matrix_operation(self.root, action, argument)

    def restart_scope(self):
        return 'synapse and web'

    def doc(self, data=None):
        data = data or self.load()
        cfg = self.root / 'config/federation-bot.json'
        mxid = json.loads(cfg.read_text()).get('user_id') if cfg.exists() else None
        return protocol.document(data['server_name'], data['instance_id'], self.key, mxid)

    def ready(self, recovery=False):
        if not self.admission:
            return
        state_store.assert_update_allowed(
            self.root, allow_federation=recovery, allow_configuration=recovery
        )
        metadata = config.read_private_json(self.root / 'installation.json')
        if any(
            metadata.get(k)
            for k in (
                'restore_pending',
                'migration_frozen',
                'credential_transition_pending',
                'media_transition_pending',
            )
        ):
            raise protocol.ProtocolError('installation_pending')
        if (self.root / 'federation-transition.json').exists() and not recovery:
            raise RuntimeError(
                'Federation apply needs recovery: sudo misbot --directory '
                + shlex.quote(str(self.root))
                + ' federation recover'
            )
        lifecycle.configure(self.root, 'federation')
        lifecycle.ownership_check()

    def check_admin(self, actor):
        # CLI/root approval is local and never accepted from HTTP or bot payloads.
        if actor == 'root':
            return os.geteuid() == 0
        domain = self.load()['server_name']
        suffix = ':' + domain
        if not isinstance(actor, str) or not actor.startswith('@') or not actor.endswith(suffix):
            return False
        name = actor[1 : -len(suffix)]
        if not name or ':' in name or len(name) > 255:
            return False
        import registration

        try:
            payload = {
                'client': config.read_private_json(self.root / 'secrets/referral-client.json'),
                'username': name,
            }
            result = json.loads(
                lifecycle.dc(
                    'exec',
                    '-T',
                    'synapse',
                    'python',
                    '-c',
                    (self.root / 'federation_admin_api.py').read_text(),
                    stdin=json.dumps(payload),
                )
            )
            return result == {'authorized': True}
        except (RuntimeError, OSError, ValueError):
            return False

    def authorize(self, actor):
        if not self.admin(actor):
            raise protocol.ProtocolError('admin_unconfirmed')

    def servers(self):
        return config.load(self.root)['FEDERATION_SERVERS'].split(',')

    def policy_plan(self, server, add):
        server = schema.domain(server)
        before = self.servers()
        own = self.load()['server_name']
        if not add and server == own:
            raise protocol.ProtocolError('own_identity_preserved')
        after = (
            list(dict.fromkeys([*before, server])) if add else [x for x in before if x != server]
        )
        after = after or [own]  # never replace a restrictive policy by no whitelist
        return {
            'before': before,
            'after': after,
            'backup': 'checked consistent copy',
            'restart': self.restart_scope() if before != after else 'none',
            'data': 'identity, databases, media and keys preserved',
        }

    def change(self, server, add, actor):
        self.authorize(actor)
        plan = self.policy_plan(server, add)
        before = plan['before']
        after = plan['after']
        if before == after:
            return {'status': 'unchanged', 'servers': before, 'plan': plan}
        if self.admission:
            lifecycle.emit('federation_policy_plan', **plan)
        if self.applier == self.apply_policy:
            self.applier(after, actor=actor)
        else:
            self.applier(after)
        return {
            'status': 'applied',
            'servers': after,
            'plan': plan,
            'restart': self.restart_scope(),
            'federation_exchange': 'not_verified',
        }

    def apply_policy(self, servers, actor='root'):
        import yaml

        values = config.load(self.root, True)
        proposed = dict(values)
        proposed['FEDERATION_SERVERS'] = ','.join(servers)
        schema.validate(proposed)
        lifecycle.backup()
        lifecycle.restore_check()
        # Rights may be revoked while the checked backup restarts MAS/Synapse.
        # Recheck after that pause, before publishing any policy files.
        self.authorize(actor)
        stage = config.directory(self.root / 'stages/federation' / secrets.token_hex(16))
        config.snapshot(self.root, stage)
        for name in ('homeserver.yaml',):
            (stage / name).write_bytes((self.root / 'config' / name).read_bytes())
            (stage / name).chmod(0o600)
        state_store.write_json(
            stage / 'web-config.json',
            json.loads((self.root / 'public/web-config.json').read_text()),
        )
        journal = {
            'format': 1,
            'stage': str(stage.relative_to(self.root)),
            'servers': servers,
            'phase': 'prepared',
        }
        state_store.write_json(self.root / 'federation-transition.json', journal)
        self.recover_policy()

    def recover_policy(self, rollback=False):
        import yaml

        path = self.root / 'federation-transition.json'
        if not path.exists():
            return {'status': 'unchanged'}
        journal = config.read_private_json(path)
        if journal.get('format') != 1 or not re.fullmatch(
            'stages/federation/[0-9a-f]{32}', journal.get('stage', '')
        ):
            raise protocol.ProtocolError('invalid_checkpoint')
        stage = self.root / journal['stage']
        servers = [schema.domain(v) for v in journal['servers']]
        if not servers:
            raise protocol.ProtocolError('empty_policy')
        # Interrupted split publication is resolved before touching generated runtime.
        if (self.root / 'configuration-transition.json').exists():
            config.recover(self.root, rollback=rollback)
        if rollback:
            config.restore_snapshot(self.root, stage)
            state_store.write_text(
                self.root / 'public/web-config.json',
                json.dumps(
                    config.read_private_json(stage / 'web-config.json'),
                    ensure_ascii=False,
                    indent=2,
                )
                + '\n',
                0o644,
            )
            state_store.write_text(
                self.root / 'config/homeserver.yaml', (stage / 'homeserver.yaml').read_text()
            )
        else:
            model = config.load(self.root, True)
            model['FEDERATION_SERVERS'] = ','.join(servers)
            config.write_model(self.root, model)
            synapse = yaml.safe_load((stage / 'homeserver.yaml').read_text())
            if synapse.get('server_name') != model['DOMAIN']:
                raise protocol.ProtocolError('identity_mismatch')
            synapse['federation_domain_whitelist'] = servers
            state_store.write_text(
                self.root / 'config/homeserver.yaml', yaml.safe_dump(synapse, sort_keys=False)
            )
            web = config.read_private_json(stage / 'web-config.json')
            web.setdefault('room_directory', {})['servers'] = list(
                dict.fromkeys([model['DOMAIN'], *servers])
            )
            state_store.write_text(
                self.root / 'public/web-config.json',
                json.dumps(web, ensure_ascii=False, indent=2) + '\n',
                0o644,
            )
        # Atomic publication changes the inode of a single-file bind mount.
        # Restart alone keeps the old inode; recreate only these two services.
        lifecycle.dc('up', '-d', '--no-deps', '--force-recreate', 'synapse', 'web')
        expected = {
            'server_name': config.read_private_json(self.root / 'installation.json')['domain'],
            'servers': yaml.safe_load((self.root / 'config/homeserver.yaml').read_text()).get(
                'federation_domain_whitelist'
            ),
            'directory': json.loads((self.root / 'public/web-config.json').read_text())[
                'room_directory'
            ],
        }
        probe = "import json,sys,yaml,urllib.request;from pathlib import Path;e=json.load(sys.stdin);c=yaml.safe_load(Path('/config/homeserver.yaml').read_text());assert c['server_name']==e['server_name'];assert c.get('federation_domain_whitelist')==e['servers'];urllib.request.urlopen('http://localhost:8008/_matrix/client/versions',timeout=3).read();w=json.load(urllib.request.urlopen('http://web:8080/config.json',timeout=3));assert w['room_directory']==e['directory']"
        lifecycle.wait(
            lambda: lifecycle.dc(
                'exec', '-T', 'synapse', 'python', '-c', probe, stdin=json.dumps(expected)
            )
            is not None,
            service='synapse and web policy',
        )
        if (
            not rollback
            and yaml.safe_load((self.root / 'config/homeserver.yaml').read_text()).get(
                'federation_domain_whitelist'
            )
            != servers
        ):
            raise protocol.ProtocolError('policy_result_mismatch')
        path.unlink()
        return {'status': 'rolled_back' if rollback else 'applied'}

    def request(self, data, rid):
        if not isinstance(rid, str) or not REQUEST.fullmatch(rid):
            raise protocol.ProtocolError('invalid_request_id')
        item = data['requests'].get(rid)
        if item is None:
            raise protocol.ProtocolError('request_not_found')
        # A mutually approved saga must remain recoverable after its invitation
        # deadline. Never undo an already applied policy as an expiry side effect.
        approved = item.get('local_approval') and item.get('remote_approval')
        applied = item.get('local_applied') or item.get('remote_applied')
        if (
            item['expires_at'] <= int(time.time())
            and item['state'] in ACTIVE
            and not approved
            and not applied
        ):
            item.update(state='expired', delivery_pending=True)
            self.save(data)
        return item

    def view(self, item):
        result = {
            k: item[k]
            for k in (
                'id',
                'server',
                'direction',
                'state',
                'expires_at',
                'local_applied',
                'remote_applied',
            )
        }
        for key in ('delivery_pending', 'safe_error', 'superseded_by'):
            if key in item:
                result[key] = item[key]
        return result

    def active_for(self, data, server, except_id=None):
        for rid in data['requests']:
            if rid != except_id:
                item = self.request(data, rid)
                if item['server'] == server and item['state'] in ACTIVE:
                    return item
        return None

    def dismiss_connected(self, data, item):
        """Hide stale unapproved requests without disrupting an applying saga."""
        if item['state'] not in ACTIVE or (
            item.get('local_approval') and item.get('remote_approval')
        ):
            return False
        if item.get('local_applied') or item.get('remote_applied'):
            return False
        if item['server'] not in self.servers():
            return False
        item.update(state='cancelled', dismissed=True, delivery_pending=True)
        self.save(data)
        return True

    def requests(self, data):
        visible = []
        for rid in data['requests']:
            item = self.request(data, rid)
            self.dismiss_connected(data, item)
            if item['state'] in (*ACTIVE, 'rejected', 'expired') and not item.get('cleared'):
                visible.append(self.view(item))
        return visible

    def maintain(self):
        """Persist expiry and retry one notification; local API-off pauses delivery."""
        with self.guard():
            self.ready()
            data = self.load()
            self.requests(data)
            if not self.api_enabled(data):
                return
            now = int(time.time())
            due = [
                item
                for item in data['requests'].values()
                if item.get('delivery_pending') and item.get('next_delivery_at', 0) <= now
            ]
            if due:
                # An unreachable first peer must not monopolize each minute's
                # delivery slot. Prefer requests that have waited the longest.
                item = min(due, key=lambda candidate: candidate.get('next_delivery_at', 0))
                item['next_delivery_at'] = now + INVITATION_INTERVAL
                self.save(data)
                self.retry(data, item['id'])

    def peer(self, data, server):
        doc = protocol.discover(server, self.transport)
        old = data['peers'].get(server)
        pinned = [old['document']] if old else []
        pinned.extend(
            item['document']
            for item in data['requests'].values()
            if item['server'] == server and item['state'] in ACTIVE
        )
        if any(
            saved[k] != doc[k] for saved in pinned for k in ('instance_id', 'key_id', 'bot_mxid')
        ):
            raise protocol.ProtocolError('peer_identity_changed')
        return doc

    @staticmethod
    def require_invitation_support(document):
        if document.get('request_ttl', 604800) < protocol.REQUEST_TTL:
            raise protocol.ProtocolError('peer_upgrade_required')
        if 'capabilities' not in document:
            raise protocol.ProtocolError('peer_upgrade_required')
        if protocol.INVITATION_CAPABILITY not in document['capabilities']:
            raise protocol.ProtocolError('invitation_unsupported')

    def invitation_candidate(self, data, server, document):
        """Describe eligibility without creating a request or posting an envelope."""
        result = {'supported': False, 'ready': False, 'expires_after': protocol.REQUEST_TTL}
        try:
            self.require_invitation_support(document)
            result['supported'] = True
            self.require_api(data)
            if server == data['server_name']:
                raise protocol.ProtocolError('self_invitation')
            if server in self.servers():
                raise protocol.ProtocolError('peer_already_connected')
            active = self.active_for(data, server)
            if active and active['direction'] == 'incoming':
                raise protocol.ProtocolError('pending_request_exists')
            if not active and len(data['requests']) >= 1000:
                raise protocol.ProtocolError('request_capacity')
            if (
                not active
                and int(time.time()) - data.get('last_invitation_at', 0) < INVITATION_INTERVAL
            ):
                raise protocol.ProtocolError('invitation_rate_limit')
            result['ready'] = True
        except protocol.ProtocolError as error:
            result['code'] = error.code
        return result

    def send(self, data, item, action):
        self.require_api(data)
        body = {
            'action': action,
            'request_id': item['id'],
            'expires_at': item['expires_at'],
            'approved': bool(item.get('local_approval')),
            'applied': item['local_applied'],
        }
        remote = item['document']
        message = protocol.envelope(self.doc(data), self.key, remote, body)
        response = self.transport(item['server'], protocol.API, message)
        # Responses are bound to this exact nonce, recipient and request id.
        value = protocol.authenticate(response, self.doc(data), remote, {})
        reply = value['body']
        if (
            not isinstance(reply, dict)
            or type(reply.get('approved')) is not bool
            or type(reply.get('applied')) is not bool
            or reply.get('state') not in (*ACTIVE, 'connected', *TERMINAL)
            or reply.get('reply_to') != message['payload']['nonce']
            or reply.get('request_id') != item['id']
        ):
            raise protocol.ProtocolError('invalid_response')
        if reply.get('approved'):
            item['remote_approval'] = True
        # These are saga milestones. A delayed signed status must not regress
        # application or erase the checkpoint referenced by a connected peer.
        item['remote_applied'] = item['remote_applied'] or reply['applied']
        if reply.get('state') in TERMINAL and item['state'] in ACTIVE:
            item['state'] = reply['state']
        item.pop('safe_error', None)
        self.save(data)
        return reply

    def local_apply(self, data, item):
        if (
            item['state'] in TERMINAL
            or not item.get('local_approval')
            or not item.get('remote_approval')
        ):
            return
        # Revocation/blocked status is rechecked before deferred execution.
        self.authorize(item['actor'])
        if not item['local_applied']:
            item['state'] = 'applying'
            self.save(data)
            self.change(item['server'], True, item['actor'])
            item['local_applied'] = True
            item['state'] = 'partially_applied'
            self.save(data)
        if item['remote_applied']:
            item['state'] = 'connected'
            data['peers'][item['server']] = {
                'document': item['document'],
                'approved': True,
                'request_id': item['id'],
            }
            self.save(data)

    def invite(self, data, server, actor):
        server = schema.domain(server)
        if server == data['server_name']:
            raise protocol.ProtocolError('self_invitation')
        self.require_api(data)
        if server in self.servers():
            for old in data['requests'].values():
                if old['server'] == server:
                    self.dismiss_connected(data, old)
            raise protocol.ProtocolError('peer_already_connected')
        old = self.active_for(data, server)
        if old:
            if old['direction'] == 'incoming':
                raise protocol.ProtocolError('pending_request_exists')
            return self.retry(data, old['id'])
        now = int(time.time())
        if now - data.get('last_invitation_at', 0) < INVITATION_INTERVAL:
            raise protocol.ProtocolError('invitation_rate_limit')
        if len(data['requests']) >= 1000:
            raise protocol.ProtocolError('request_capacity')
        doc = self.peer(data, server)
        self.require_invitation_support(doc)
        rid = 'MB-' + secrets.token_hex(16)
        item = {
            'id': rid,
            'server': server,
            'document': doc,
            'direction': 'outgoing',
            'actor': actor,
            'expires_at': now + protocol.REQUEST_TTL,
            'created_at': now,
            'state': 'pending',
            'local_approval': True,
            'remote_approval': False,
            'local_applied': False,
            'remote_applied': False,
            'entry_preexisted': server in self.servers(),
        }
        data['requests'][rid] = item
        data['last_invitation_at'] = now
        self.save(data)
        return self.retry(data, rid)

    def retry(self, data, rid):
        item = self.request(data, rid)
        try:
            if item['state'] in TERMINAL:
                # Unknown cancellations are tombstones at the recipient too:
                # a delayed delivery of the original invite cannot resurrect it.
                if item.get('delivery_pending'):
                    terminal = item['state']
                    if terminal == 'expired':
                        terminal = 'cancelled' if item['direction'] == 'outgoing' else 'rejected'
                    self.send(data, item, terminal)
                    item.pop('delivery_pending', None)
                    item.pop('next_delivery_at', None)
                    self.save(data)
                return self.view(item)
            self.send(
                data,
                item,
                'invite'
                if item['direction'] == 'outgoing' and not item.get('remote_approval')
                else 'status',
            )
            self.local_apply(data, item)
            if item.get('local_approval') and item.get('remote_approval'):
                self.send(data, item, 'decision')
                self.local_apply(data, item)
        except protocol.ProtocolError as error:
            item['safe_error'] = error.code
            self.save(data)
            return {
                **self.view(item),
                'code': error.code,
                'next_action': 'sudo misbot federation requests retry --request ' + rid,
            }
        return self.view(item)

    def receive(self, message):
        with self.guard():
            self.ready()
            data = self.load()
            self.require_api(data)
            own = self.doc(data)
            try:
                sender = message['payload']['sender']
                if not isinstance(sender, str):
                    raise ValueError()
                server = schema.domain(sender)
            except (ValueError, KeyError, TypeError):
                raise protocol.ProtocolError('invalid_request') from None
            remote = self.peer(data, server)
            data['nonces'] = {k: v for k, v in data['nonces'].items() if v > time.time() - 600}
            value = protocol.authenticate(message, own, remote, data['nonces'])
            body = value['body']
            if not isinstance(body, dict) or set(body) - {
                'action',
                'request_id',
                'expires_at',
                'approved',
                'applied',
            }:
                raise protocol.ProtocolError('invalid_request')
            rid = body.get('request_id')
            action = body.get('action')
            if not isinstance(rid, str) or not REQUEST.fullmatch(rid):
                raise protocol.ProtocolError('invalid_request_id')
            if action not in ('invite', 'status', 'decision', 'cancelled', 'rejected'):
                raise protocol.ProtocolError('remote_operation_denied')
            if type(body.get('approved')) is not bool or type(body.get('applied')) is not bool:
                raise protocol.ProtocolError('invalid_request')
            data['nonces'][value['nonce']] = int(time.time())
            self.save(data)
            item = data['requests'].get(rid)
            if action in ('invite', 'cancelled') and item is None:
                expiration = body.get('expires_at')
                # Retired requests can have an elapsed deadline. An unknown
                # cancellation records a tombstone before a delayed invite.
                if (
                    type(expiration) is not int
                    or not 0 < expiration <= value['timestamp'] + protocol.REQUEST_TTL
                    or (action == 'invite' and expiration <= time.time())
                ):
                    raise protocol.ProtocolError('invalid_expiration')
                if len(data['requests']) >= 1000:
                    raise protocol.ProtocolError('request_capacity')
                now = int(time.time())
                received = data.setdefault('incoming_invitation_at', {})
                if action == 'invite' and now - received.get(server, 0) < INVITATION_INTERVAL:
                    raise protocol.ProtocolError('invitation_rate_limit')
                old = self.active_for(data, server)
                if action == 'invite' and old:
                    if (
                        old['direction'] != 'outgoing'
                        or old['state'] != 'pending'
                        or old.get('local_applied')
                        or old.get('remote_applied')
                        or old.get('remote_approval')
                    ):
                        raise protocol.ProtocolError('pending_request_exists')
                    # Crossed requests converge to one ID without transferring
                    # consent. The receiver must explicitly approve the winner.
                    if old['id'] < rid:
                        raise protocol.ProtocolError('pending_request_exists')
                    old.update(state='cancelled', superseded_by=rid)
                    self.save(data)
                connected = server in self.servers()
                item = {
                    'id': rid,
                    'server': server,
                    'document': remote,
                    'direction': 'incoming',
                    'actor': None,
                    'expires_at': expiration,
                    'state': 'cancelled' if action == 'cancelled' or connected else 'pending',
                    'local_approval': False,
                    'remote_approval': body['approved'],
                    'local_applied': False,
                    'remote_applied': False,
                    'entry_preexisted': connected,
                }
                data['requests'][rid] = item
                if action == 'invite':
                    received[server] = now
                self.save(data)
            item = self.request(data, rid)
            if (
                item['server'] != server
                or item['document']['instance_id'] != remote['instance_id']
                or item['document']['key_id'] != remote['key_id']
            ):
                raise protocol.ProtocolError('request_owner')
            if action == 'rejected' and item['direction'] != 'outgoing':
                raise protocol.ProtocolError('invalid_transition')
            if action == 'invite' and item['direction'] != 'incoming':
                raise protocol.ProtocolError('invalid_transition')
            self.dismiss_connected(data, item)
            if item['state'] in ACTIVE:
                if action in ('cancelled', 'rejected'):
                    # Removing policy is a SEPARATE local admin operation, never implicit rollback.
                    item['state'] = action
                else:
                    if body['approved']:
                        item['remote_approval'] = True
                    item['remote_applied'] = item['remote_applied'] or body['applied']
                    self.save(data)
                    self.local_apply(data, item)
            self.save(data)
            reply = {
                'request_id': rid,
                'reply_to': value['nonce'],
                'state': item['state'],
                'approved': bool(item.get('local_approval')),
                'applied': item['local_applied'],
            }
            return protocol.envelope(own, self.key, remote, reply)

    def operate(self, action, argument=None, actor='root', event_id=None):
        if action not in ACTIONS:
            raise protocol.ProtocolError('unknown_operation')
        with self.guard():
            self.ready(
                action
                in (
                    'recover',
                    'rollback',
                    'operations',
                    'requests',
                    'request-status',
                    'peers',
                    'servers',
                    'api-status',
                    'api-enable',
                    'api-disable',
                    'clear-rejected',
                )
            )
            self.authorize(actor)
            data = self.load()
            if event_id:
                if not isinstance(event_id, str) or len(event_id) > 255:
                    raise protocol.ProtocolError('invalid_event')
                digest = hashlib.sha256(event_id.encode()).hexdigest()
                if digest in data['events']:
                    return {'status': 'already_processed'}
                # Persist before side effects: replay can only use explicit retry/recovery.
                if len(data['events']) >= 100000:
                    raise protocol.ProtocolError('event_capacity')
                data['events'][digest] = {'action': action, 'time': int(time.time())}
                self.save(data)
            if action == 'api-status':
                return {'enabled': self.api_enabled(data)}
            if action in ('api-enable', 'api-disable'):
                enabled = action == 'api-enable'
                changed = self.api_enabled(data) != enabled
                data['api_enabled'] = enabled
                self.save(data)
                return {'enabled': enabled, 'status': 'changed' if changed else 'unchanged'}
            if action == 'clear-rejected':
                items = (
                    [self.request(data, argument)] if argument else list(data['requests'].values())
                )
                if argument and items[0]['state'] not in ('rejected', 'expired'):
                    raise protocol.ProtocolError('invalid_transition')
                count = 0
                for item in items:
                    if item['state'] in ('rejected', 'expired') and not item.get('cleared'):
                        item.update(cleared=True, cleared_at=int(time.time()))
                        count += 1
                # Keep replay tombstones, pending delivery and connected peer pins.
                self.save(data)
                return {'status': 'cleared', 'count': count}
            if action == 'servers':
                return {'servers': self.servers()}
            if action in ('add', 'remove'):
                return self.change(argument, action == 'add', actor)
            if action in ('recover', 'rollback'):
                return self.recover_policy(action == 'rollback')
            if action == 'check':
                server = schema.domain(argument)
                result = {
                    'server': server,
                    'input': 'valid',
                    'policy': server in self.servers(),
                    'federation_exchange': 'not_verified',
                }
                try:
                    discovery = self.transport(server, '/.well-known/matrix/server')
                    target = discovery['m.server']
                    host, separator, port = target.partition(':')
                    if separator and port != '443':
                        raise protocol.ProtocolError('matrix_discovery_port_unsupported')
                    host = schema.domain(host)
                    self.transport(host, '/_matrix/federation/v1/version')
                    result.update(matrix_discovery='available', matrix_https='available')
                except protocol.ProtocolError as error:
                    result.update(matrix_discovery='unconfirmed', matrix_code=error.code)
                except (KeyError, TypeError, ValueError):
                    result.update(
                        matrix_discovery='unconfirmed', matrix_code='invalid_matrix_discovery'
                    )
                try:
                    document = self.peer(data, server)
                    result.update(
                        misbot_discovery=document,
                        https='available',
                        invitation=self.invitation_candidate(data, server, document),
                    )
                except protocol.ProtocolError as error:
                    result.update(
                        https='unconfirmed',
                        code=error.code,
                        invitation={
                            'supported': False,
                            'ready': False,
                            'code': error.code,
                            'expires_after': protocol.REQUEST_TTL,
                        },
                    )
                return result
            if action == 'invite':
                return self.invite(data, argument, actor)
            if action == 'requests':
                return {'requests': self.requests(data)}
            if action == 'operations':
                return {
                    'operations': [
                        {'id': k, 'state': v['state'], 'result': v.get('result', {})}
                        for k, v in data.get('jobs', {}).items()
                    ]
                }
            if action == 'peers':
                return {
                    'peers': [
                        {
                            'server': s,
                            'bot_mxid': p['document']['bot_mxid'],
                            'approved': p['approved'],
                            'state': data['requests']
                            .get(p['request_id'], {})
                            .get('state', 'unconfirmed'),
                        }
                        for s, p in data['peers'].items()
                    ]
                }
            if action in ('approve', 'reject', 'cancel', 'retry', 'request-status'):
                item = self.request(data, argument)
                self.dismiss_connected(data, item)
                if action == 'request-status':
                    return self.view(item)
                if action == 'retry':
                    return self.retry(data, argument)
                if item['server'] in self.servers():
                    raise protocol.ProtocolError('peer_already_connected')
                if action == 'approve':
                    if item['direction'] != 'incoming' or item['state'] in TERMINAL:
                        raise protocol.ProtocolError('invalid_transition')
                    item.update(local_approval=True, actor=actor, state='accepted')
                    self.save(data)
                    return self.retry(data, argument)
                if action == 'reject' and item['direction'] != 'incoming':
                    raise protocol.ProtocolError('invalid_transition')
                if action == 'cancel' and item['direction'] != 'outgoing':
                    raise protocol.ProtocolError('invalid_transition')
                if (
                    item['state'] == 'connected'
                    or item.get('local_applied')
                    or item.get('remote_applied')
                ):
                    raise protocol.ProtocolError('request_in_progress')
                if item['state'] in TERMINAL:
                    return self.retry(data, argument)
                item.update(
                    state='rejected' if action == 'reject' else 'cancelled', delivery_pending=True
                )
                self.save(data)
                return self.retry(data, argument)
            raise protocol.ProtocolError('unknown_operation')


def backend(root, **kwargs):
    if (
        json.loads((Path(root) / 'installation.json').read_text()).get('backend')
        == 'existing-compose'
    ):
        from existing_features import Federation

        return Federation(root, **kwargs)
    return Backend(root, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('action', choices=sorted(ACTIONS))
    parser.add_argument('argument', nargs='?')
    args = parser.parse_args()
    try:
        state_store.require_root()
        __import__('command_wrapper').trusted_installation(args.directory)
        result = backend(args.directory).operate(args.action, args.argument)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get('code') else 0
    except protocol.ProtocolError as error:
        print(json.dumps(failure(error.code, args.directory, args.action), ensure_ascii=False))
        return 1
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(
            json.dumps(
                failure('local_state_or_input', args.directory, args.action), ensure_ascii=False
            )
        )
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
