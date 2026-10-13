"""One durable, single-use invitation, independent of administrator bootstrap."""

import argparse
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import urllib.parse
import re
import sqlite3
import stat

import configuration
import lifecycle
import state_store

BASE = '/api/admin/v1/user-registration-tokens'


def invitation_issuers(root, domain):
    """Read public issuer metadata without selecting the private token column."""
    result = {}
    path = root / 'data/referral-control/data/bot-control.sqlite3'
    if path.exists() or path.is_symlink():
        if path.resolve() != path:
            raise RuntimeError('Список приглашений не прочитан: путь метаданных содержит ссылку.')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise RuntimeError(
                    'Список приглашений не прочитан: проверьте владельца и приватные права файла метаданных.'
                )
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
                db.execute('PRAGMA query_only=ON')
                columns = {row[1] for row in db.execute('PRAGMA table_info(invitations)')}
                if not {'server', 'resource', 'uid'} <= columns:
                    raise RuntimeError(
                        'Схема метаданных приглашений не поддерживается. Сохраните базу и проверьте комплект Misbot.'
                    )
                name = 'inviter_name' if 'inviter_name' in columns else 'NULL'
                rows = db.execute(
                    'SELECT resource,uid,' + name + ' FROM invitations WHERE server=?', (domain,)
                )
                for resource, uid, label in rows:
                    if not resource:
                        continue
                    if isinstance(uid, str) and re.fullmatch(
                        r'@[a-z0-9._=/\-]{1,255}:' + re.escape(domain), uid
                    ):
                        issuer = uid
                    elif isinstance(uid, int) and not isinstance(uid, bool) and uid > 0:
                        issuer = (
                            label if isinstance(label, str) and label else 'Telegram ' + str(uid)
                        )
                    else:
                        issuer = 'не указан'
                    result[resource] = issuer
        finally:
            os.close(descriptor)
    first = root / 'secrets/first-registration.json'
    if first.exists() or first.is_symlink():
        record = configuration.read_private_json(first)
        if record.get('domain') != domain or record.get('format') != 1:
            raise RuntimeError(
                'Первое приглашение относится к другой установке. Проверьте выбранный каталог; данные сохранены.'
            )
        if record.get('resource'):
            result[record['resource']] = '@matrix-admin:' + domain
    return result


def invitation_state(attributes, now=None):
    """Use the same verified MAS state for list, preparation and secret display."""
    expires = attributes.get('expires_at')
    if expires is not None and not isinstance(expires, str):
        raise RuntimeError('Срок приглашения не прошёл проверку. Данные сохранены.')
    try:
        expiration = datetime.fromisoformat(expires.replace('Z', '+00:00')) if expires else None
    except ValueError:
        raise RuntimeError('Срок приглашения не прошёл проверку. Данные сохранены.') from None
    if expiration and expiration.tzinfo is None:
        raise RuntimeError('Для срока приглашения не указан часовой пояс. Данные сохранены.')
    used, limit = attributes.get('times_used'), attributes.get('usage_limit')
    if (
        (used is not None and (type(used) is not int or used < 0))
        or (limit is not None and (type(limit) is not int or limit < 0))
        or ('valid' in attributes and type(attributes['valid']) is not bool)
    ):
        raise RuntimeError(
            'Сведения об использовании приглашения не прошли проверку. Данные сохранены.'
        )
    if attributes.get('revoked_at'):
        return 'revoked'
    if limit is not None and used is not None and used >= limit:
        return 'used'
    if expiration and expiration <= (now or datetime.now(timezone.utc)):
        return 'expired'
    if attributes.get('valid') is False:
        return 'unavailable'
    if used is None and attributes.get('valid') is not True:
        return 'unknown'
    return 'valid'


def list_invitations(root, transport, now=None):
    domain = configuration.read_private_json(root / 'installation.json')['domain']
    issuers = invitation_issuers(root, domain)
    now = now or datetime.now(timezone.utc)
    invitations = []
    path = BASE
    seen_pages = set()
    seen_ids = set()
    for _ in range(10000):
        if path in seen_pages:
            raise RuntimeError(
                'Страница приглашений повторилась. Список не подтверждён; повторите просмотр позднее.'
            )
        seen_pages.add(path)
        response = transport(root, path)
        if response['status'] != 200 or not isinstance(response.get('body'), dict):
            raise RuntimeError(
                'Список приглашений недоступен. Проверьте состояние сервера и повторите просмотр.'
            )
        items = response['body'].get('data')
        if not isinstance(items, list):
            raise RuntimeError(
                'Формат списка приглашений не поддерживается. Проверьте совместимость MAS и Misbot.'
            )
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('attributes'), dict):
                raise RuntimeError(
                    'Формат списка приглашений не поддерживается. Проверьте совместимость MAS и Misbot.'
                )
            identifier = item['id']
            if not isinstance(identifier, str) or not re.fullmatch(
                '[0-9A-HJKMNP-TV-Z]{26}', identifier
            ):
                raise RuntimeError(
                    'Идентификатор приглашения не прошёл проверку. Список не подтверждён; данные сохранены.'
                )
            if identifier in seen_ids:
                continue
            seen_ids.add(identifier)
            attributes = item['attributes']
            state = invitation_state(attributes, now)
            # Only an explicit allowlist reaches CLI/UI. MAS also returns the
            # private token; it is never copied into this view or diagnostics.
            invitations.append(
                {
                    'id': identifier,
                    'issuer': issuers.get(identifier, 'не указан'),
                    'state': state,
                    'expires_at': attributes.get('expires_at'),
                    'times_used': attributes.get('times_used'),
                    'usage_limit': attributes.get('usage_limit'),
                }
            )
        links = response['body'].get('links', {})
        if not isinstance(links, dict):
            raise RuntimeError(
                'Формат страниц приглашений не поддерживается. Проверьте совместимость MAS и Misbot.'
            )
        next_path = links.get('next')
        if not next_path:
            return {'invitations': invitations}
        if not isinstance(next_path, str) or any(ord(character) < 32 for character in next_path):
            raise RuntimeError(
                'Адрес следующей страницы приглашений не прошёл проверку. Список не подтверждён; данные сохранены.'
            )
        parsed = urllib.parse.urlsplit(next_path)
        if parsed.scheme or parsed.netloc or parsed.path != BASE or parsed.fragment:
            raise RuntimeError(
                'Адрес следующей страницы приглашений не прошёл проверку. Список не подтверждён; данные сохранены.'
            )
        path = next_path
    raise RuntimeError(
        'Достигнут предел просмотра страниц приглашений. Список не подтверждён; данные сохранены.'
    )


def api(root, path, method='GET', body=None):
    payload = {
        'client': configuration.read_private_json(root / 'secrets/referral-client.json'),
        'path': path,
        'method': method,
        'body': body,
    }
    try:
        response = json.loads(
            lifecycle.dc(
                'exec',
                '-T',
                'synapse',
                'python',
                '-c',
                (root / 'registration_api.py').read_text(),
                stdin=json.dumps(payload),
            )
        )
        if response['status'] not in (200, 201, 404):
            raise ValueError()
        return response
    except (OSError, ValueError, KeyError, RuntimeError):
        raise RuntimeError(
            'MAS invitation service is unavailable. Keep private intent; retry: sudo misbot users invitation ensure'
        ) from None


def resource(root, record, transport, create=True):
    if record.get('resource'):
        if not re.fullmatch('[0-9A-HJKMNP-TV-Z]{26}', record['resource']):
            raise RuntimeError('Invalid saved invitation resource')
        response = transport(root, BASE + '/' + record['resource'])
        if response['status'] == 404:
            raise RuntimeError(
                'Saved invitation is absent in MAS; restore its database/checkpoint before creating another'
            )
        return response['body']['data']
    # A POST response can be lost. Match the saved random token before another POST.
    path = BASE
    for page in range(10000):
        response = transport(root, path)
        if response['status'] != 200:
            raise RuntimeError('Cannot inspect saved invitation; retry users invitation ensure')
        matches = [
            item
            for item in response['body']['data']
            if secrets.compare_digest(item['attributes']['token'], record['token'])
        ]
        if len(matches) > 1:
            raise RuntimeError('Ambiguous invitation ownership; retain private checkpoint')
        if matches:
            return matches[0]
        next_path = response['body'].get('links', {}).get('next')
        if not next_path:
            break
        parsed = urllib.parse.urlsplit(next_path)
        if parsed.scheme or parsed.netloc or parsed.path != BASE:
            raise RuntimeError('Unsafe MAS pagination; retain private checkpoint')
        path = next_path
    else:
        raise RuntimeError('MAS pagination limit reached; keep invitation intent')
    if not create:
        return None
    response = transport(
        root,
        BASE,
        'POST',
        {'token': record['token'], 'usage_limit': 1, 'expires_at': record['expires_at']},
    )
    if response['status'] != 201:
        raise RuntimeError(
            'Invitation creation not confirmed; retry users invitation ensure with the same private intent'
        )
    return response['body']['data']


def manage(root, action, transport=None):
    root = Path(root)
    transport = transport or api
    with state_store.operation_guard(root, wait_seconds=15):
        state_store.assert_update_allowed(root)
        lifecycle.configure(root, 'invitation-' + action)
        lifecycle.ownership_check()
        metadata = configuration.read_private_json(root / 'installation.json')
        if not metadata.get('databases_initialized') or any(
            metadata.get(k)
            for k in (
                'restore_pending',
                'migration_frozen',
                'media_transition_pending',
                'credential_transition_pending',
            )
        ):
            raise RuntimeError(
                'Finish initial start or pending recovery before managing invitations'
            )
        if action == 'list':
            return list_invitations(root, transport)
        if action == 'new':
            policy = configuration.load(root, True)
            if (
                policy['OPEN_REGISTRATION'] != 'true'
                or policy['REGISTRATION_TOKEN_REQUIRED'] != 'true'
            ):
                raise RuntimeError(
                    'Enable password registration with required tokens before replacing the invitation'
                )
        path = root / 'secrets/first-registration.json'
        if action in ('ensure', 'new'):
            __import__('vault_cache').mark_dirty(root)
        if not path.exists():
            if action in ('status', 'show', 'revoke'):
                return {
                    'state': 'absent',
                    'expires_at': None,
                    'usage_limit': 1,
                    'next_action': 'sudo misbot users invitation ensure',
                }
            model = configuration.load(root, True)
            if (
                model['OPEN_REGISTRATION'] != 'true'
                or model['REGISTRATION_TOKEN_REQUIRED'] != 'true'
            ):
                raise RuntimeError(
                    'Enable password registration with required tokens before creating a first invitation'
                )
            expires = datetime.now(timezone.utc) + timedelta(
                seconds=int(model['INVITATION_LIFETIME_SECONDS'])
            )
            record = {
                'format': 1,
                'domain': metadata['domain'],
                'token': 'MIS-' + secrets.token_urlsafe(32),
                'expires_at': expires.isoformat(),
                'state': 'pending',
            }
            state_store.write_json(path, record)
        record = configuration.read_private_json(path)
        if (
            record.get('format') != 1
            or record.get('domain') != metadata['domain']
            or not isinstance(record.get('token'), str)
        ):
            raise RuntimeError(
                'Invitation checkpoint belongs to another installation; keep it private'
            )
        if record.get('local_revoked') and action != 'new':
            return {'state': 'revoked', 'expires_at': record['expires_at'], 'usage_limit': 1}
        item = (
            resource(root, record, transport, create=action == 'ensure')
            if not record.get('local_revoked')
            else None
        )
        if item is None:
            if action in ('revoke', 'new'):
                record.update(state='revoked', local_revoked=True)
                state_store.write_json(path, record)
                if action == 'new':
                    model = configuration.load(root, True)
                    record.update(
                        token='MIS-' + secrets.token_urlsafe(32),
                        expires_at=(
                            datetime.now(timezone.utc)
                            + timedelta(seconds=int(model['INVITATION_LIFETIME_SECONDS']))
                        ).isoformat(),
                        state='pending',
                    )
                    record.pop('local_revoked', None)
                    state_store.write_json(path, record)
                    return manage(root, 'ensure', transport)
            return {
                'state': record.get('state', 'pending'),
                'expires_at': record['expires_at'],
                'usage_limit': 1,
                'next_action': 'sudo misbot users invitation ensure',
            }
        attr = item['attributes']
        if (
            not secrets.compare_digest(attr['token'], record['token'])
            or attr.get('usage_limit') != 1
            or datetime.fromisoformat(attr['expires_at'].replace('Z', '+00:00'))
            != datetime.fromisoformat(record['expires_at'])
        ):
            raise RuntimeError(
                'Invitation properties differ from saved intent; no second token issued'
            )
        record['resource'] = item['id']
        if action in ('revoke', 'new') and not attr.get('revoked_at'):
            response = transport(root, BASE + '/' + item['id'] + '/revoke', 'POST', {})
            if response['status'] != 200 or not response['body']['data']['attributes'].get(
                'revoked_at'
            ):
                raise RuntimeError(
                    'Revocation is not confirmed; retry users invitation revoke before issuing a replacement'
                )
            attr = response['body']['data']['attributes']
        record['state'] = invitation_state(attr)
        state_store.write_json(path, record)
        if action == 'new':
            # Durable replacement intent: after a lost response, `ensure` finishes the SAME token.
            model = configuration.load(root, True)
            history = configuration.directory(root / 'secrets/registration-history')
            state_store.write_json(history / (record['resource'] + '.json'), record)
            replacement = {
                'format': 1,
                'domain': metadata['domain'],
                'token': 'MIS-' + secrets.token_urlsafe(32),
                'expires_at': (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=int(model['INVITATION_LIFETIME_SECONDS']))
                ).isoformat(),
                'state': 'pending',
            }
            state_store.write_json(path, replacement)
            return manage(root, 'ensure', transport)
        return {
            'state': record['state'],
            'expires_at': record['expires_at'],
            'usage_limit': 1,
            'times_used': attr.get('times_used'),
            'private_file': str(path),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument(
        'action', choices=('ensure', 'status', 'show', 'revoke', 'new', 'admin-show', 'list')
    )
    args = parser.parse_args()
    try:
        state_store.require_root()
        import command_wrapper

        command_wrapper.trusted_installation(args.directory)
        if args.action in ('show', 'admin-show') and (
            not sys.stdin.isatty() or not sys.stdout.isatty()
        ):
            print(
                'Private credentials require an operator TTY: sudo misbot users invitation show / credentials admin show',
                file=sys.stderr,
            )
            return 2
        if args.action == 'admin-show':
            record = configuration.read_private_json(args.directory / 'secrets/admin-access.json')
            if record.get('username') != 'matrix-admin':
                raise RuntimeError('Unexpected administrator identity')
            if not record.get('password'):
                print(
                    'Пароль существующего администратора задаётся на внешнем MAS. Misbot его не менял и не сохранял.'
                )
            else:
                print('matrix-admin: ' + record['password'])
            return 0
        result = manage(args.directory, args.action)
        if args.action == 'show':
            if result['state'] != 'valid':
                states = {
                    'used': 'Этот код уже использован.',
                    'expired': 'Срок действия кода истёк.',
                    'revoked': 'Код отозван.',
                    'absent': 'Код ещё не создан.',
                }
                print(states.get(result['state'], 'Код пока не подтверждён.'))
                print('В UI выберите «Пользователи и приглашения» → «Пригласить пользователя».')
                return 1
            print(
                'Код приглашения: '
                + configuration.read_private_json(
                    args.directory / 'secrets/first-registration.json'
                )['token']
            )
            print('Передайте код человеку. Его нужно ввести в форме регистрации.')
            return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        print(
            'Список приглашений не удалось проверить безопасно. Сохраните данные и повторите users invitation list.'
            if args.action == 'list'
            else 'Invitation could not be verified safely; retain private checkpoint and retry users invitation ensure',
            file=sys.stderr,
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
