"""One durable, single-use invitation, independent of administrator bootstrap."""

import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import urllib.parse
import re

import configuration
import lifecycle
import state_store

BASE = '/api/admin/v1/user-registration-tokens'


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
    with state_store.operation_guard(root):
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
        record['state'] = (
            'revoked'
            if attr.get('revoked_at')
            else 'used'
            if attr.get('times_used', 0) >= 1
            else 'expired'
            if datetime.fromisoformat(record['expires_at']) <= datetime.now(timezone.utc)
            else 'valid'
        )
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
            'times_used': attr.get('times_used', 0),
            'private_file': str(path),
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument(
        'action', choices=('ensure', 'status', 'show', 'revoke', 'new', 'admin-show')
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
            print('matrix-admin: ' + record['password'])
            return 0
        result = manage(args.directory, args.action)
        if args.action == 'show' and result['state'] == 'valid':
            print(
                'Токен: '
                + configuration.read_private_json(
                    args.directory / 'secrets/first-registration.json'
                )['token']
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        print(
            'Invitation could not be verified safely; retain private checkpoint and retry users invitation ensure',
            file=sys.stderr,
        )
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
