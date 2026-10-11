"""Ordinary user password reset with a private forward recovery checkpoint."""

import argparse
import json
import os
from pathlib import Path
import re
import signal
import stat

import credentials
import lifecycle
import state_store
import vault_cache


def private_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding='utf-8') as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or info.st_size > 16384
        ):
            raise RuntimeError(
                'Use an owned private regular JSON file, mode600, without symlinks or hardlinks'
            )
        return json.load(handle)


def validate(values):
    if not isinstance(values, dict) or set(values) != {'username', 'password'}:
        raise RuntimeError('Private JSON must contain exactly username and password strings')
    name, password = values['username'], values['password']
    if (
        not isinstance(name, str)
        or not re.fullmatch(r'[a-z0-9][a-z0-9._=-]{0,254}', name)
        or name == 'matrix-admin'
    ):
        raise RuntimeError(
            'Use an existing ordinary username without @ or domain; use admin-password for matrix-admin'
        )
    if (
        not isinstance(password, str)
        or not 16 <= len(password) <= 1024
        or any(ord(c) < 32 for c in password)
    ):
        raise RuntimeError('Use a password of 16–1024 characters without control characters')


def checkpoint(root):
    folder = root / 'secrets'
    info = folder.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or folder.resolve() != folder
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise RuntimeError('Password recovery requires the private owned secrets directory')
    return folder / 'user-password-pending.json'


def api(root, action, values):
    client = private_json(root / 'secrets/referral-client.json')
    response = lifecycle.dc(
        'exec',
        '-T',
        'synapse',
        'python',
        '-c',
        (root / 'user_password_api.py').read_text(),
        stdin=json.dumps({'action': action, 'credentials': values, 'client': client}),
    )
    identity = json.loads(response)
    if (
        not isinstance(identity, dict)
        or set(identity) != {'username', 'user_id'}
        or identity['username'] != values['username']
        or not isinstance(identity['user_id'], str)
        or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity['user_id'])
        or ('user_id' in values and identity['user_id'] != values['user_id'])
    ):
        raise RuntimeError('Password API identity was not confirmed')
    return identity


def pending_values(root, path):
    saved = private_json(path)
    if (
        not isinstance(saved, dict)
        or set(saved) != {'format', 'server_name', 'credentials'}
        or saved['format'] != 1
        or type(saved['format']) is not int
        or saved['server_name'] != lifecycle.state()['domain']
        or not isinstance(saved['credentials'], dict)
        or set(saved['credentials']) != {'username', 'password', 'user_id'}
    ):
        raise RuntimeError(
            'Invalid user password checkpoint; retain it for private administrator review'
        )
    values = saved['credentials']
    validate({k: values[k] for k in ('username', 'password')})
    if not isinstance(values['user_id'], str) or not re.fullmatch(
        r'[A-Za-z0-9_-]{1,128}', values['user_id']
    ):
        raise RuntimeError('Invalid user identity in password checkpoint')
    return values


def finish(root, path, values, recovered=False):
    try:
        vault_cache.mark_dirty(root)
        api(root, 'password', values)
        credentials.wait_services()
    except Exception:
        raise RuntimeError(
            'User password result is not confirmed. The new password may already be active. '
            'Keep private input and checkpoint, restore MAS/services, then run: sudo bash '
            + str(root / 'install.sh')
            + ' user-password-recover'
        ) from None
    path.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    vault_cache.sync_optional(root, lifecycle.emit)
    lifecycle.emit(
        'user_password_recovered' if recovered else 'user_password_changed',
        passed=True,
        password_omitted=True,
        next_action='Confirm a new password login; existing sessions are managed separately',
    )


def reset(root, input_file):
    path = checkpoint(root)
    if path.exists() or path.is_symlink():
        raise RuntimeError(
            'A user password operation is pending; run sudo bash '
            + str(root / 'install.sh')
            + ' user-password-recover before another reset'
        )
    values = private_json(input_file)
    validate(values)
    try:
        identity = api(root, 'inspect', {'username': values['username']})
    except Exception:
        raise RuntimeError(
            'Existing active ordinary user was not confirmed. No password changed. '
            'Check the username, account status and MAS connectivity, then repeat user-password with the private file'
        ) from None
    lifecycle.backup()
    lifecycle.restore_check()
    credentials.wait_services()
    values = dict(values, user_id=identity['user_id'])
    vault_cache.mark_dirty(root)
    state_store.write_json(
        path, {'format': 1, 'server_name': lifecycle.state()['domain'], 'credentials': values}
    )
    finish(root, path, values)


def recover(root):
    path = checkpoint(root)
    if not path.exists() and not path.is_symlink():
        raise RuntimeError(
            'No pending ordinary user password change; confirm login or start user-password with a private file'
        )
    finish(root, path, pending_values(root, path), recovered=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--password-file', type=Path)
    parser.add_argument('action', choices=('reset', 'recover'))
    args = parser.parse_args()
    state_store.require_root('Run user password operations with sudo on the installed VPS')

    def interrupted(signum, frame):
        raise state_store.OperationInterrupted(
            'User password operation interrupted; retain private input/checkpoint'
        )

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    root = args.directory.resolve(strict=True)
    lifecycle.configure(root, 'user-password-' + args.action)
    with state_store.operation_guard(root):
        state_store.assert_update_allowed(root)
        lifecycle.ownership_check()
        if any(
            lifecycle.state().get(k)
            for k in (
                'credential_transition_pending',
                'restore_pending',
                'migration_frozen',
                'media_transition_pending',
            )
        ):
            raise RuntimeError(
                'Finish administrator password, restore, migration or media recovery before user password operations'
            )
        if args.action == 'recover':
            recover(root)
        elif args.password_file:
            reset(root, args.password_file)
        else:
            raise RuntimeError('Specify a private JSON with username and password')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    'check': 'user_password',
                    'passed': False,
                    'message': str(error)
                    if isinstance(error, RuntimeError)
                    else 'User password operation failed; retain private input/checkpoint and inspect them privately',
                }
            )
        )
        raise SystemExit(1)
