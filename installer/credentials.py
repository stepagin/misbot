"""Administrator password rotation with a private recovery checkpoint."""
import argparse
import json
import os
from pathlib import Path
import signal
import stat
import time
import uuid

import lifecycle
import state_store
import vault_cache


def private_json(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != 0:
        raise RuntimeError('Password input must be a regular root-owned file with mode 600')
    return json.loads(path.read_text())


def apply(root, credentials):
    payload = {'action': 'password', 'credentials': credentials,
               'client': json.loads((root / 'secrets/referral-client.json').read_text())}
    lifecycle.dc('exec', '-T', 'synapse', 'python', '-c', (root / 'mas_admin.py').read_text(),
                 stdin=json.dumps(payload))


def wait_services():
    expected = 10 if lifecycle.state().get('database_mode', 'local') == 'external' else 11
    def ready():
        rows = lifecycle.service_rows()
        return len(rows) == expected and all(row['State'] == 'running' and
            row.get('Health', '') in ('', 'healthy') for row in rows)
    lifecycle.wait(ready, service='postgres synapse mas referral-control referral-bot jwt web admin caddy tls-router livekit')


def checkpoint(root, metadata):
    relative = Path(metadata.get('credential_transition_dir', ''))
    if (len(relative.parts) != 3 or relative.parts[:2] != ('stages', 'credentials') or
            relative.is_absolute() or '..' in relative.parts):
        raise RuntimeError('Unsafe credential recovery checkpoint; inspect private installation state')
    folder = root / relative
    if folder.is_symlink() or folder.resolve() != folder:
        raise RuntimeError('Unsafe credential recovery directory')
    return folder


def recover(root):
    metadata = lifecycle.state()
    if not metadata.get('credential_transition_pending'):
        raise RuntimeError('No interrupted administrator password change exists')
    folder = checkpoint(root, metadata)
    previous = private_json(folder / 'previous.json')
    if previous.get('username') != 'matrix-admin':
        raise RuntimeError('Recovery checkpoint has an unexpected administrator identity')
    apply(root, previous)
    state_store.write_json(root / 'secrets/admin-access.json', previous)
    wait_services()
    metadata.pop('credential_transition_pending', None)
    metadata.pop('credential_transition_dir', None)
    state_store.write_json(root / 'installation.json', metadata)
    vault_cache.sync_optional(root, lifecycle.emit)
    lifecycle.emit('administrator_password_recovered', passed=True, password_omitted=True,
                   next_action='Sign in using the original password stored in secrets/admin-access.json')


def rotate(root, password_file):
    values = private_json(password_file)
    if set(values) != {'password'} or not isinstance(values['password'], str):
        raise RuntimeError('Private JSON must contain exactly one string field: password')
    password = values['password']
    if len(password) < 16 or len(password) > 1024 or any(ord(c) < 32 for c in password):
        raise RuntimeError('Use a password of 16–1024 characters without control characters')
    metadata = lifecycle.state()
    if metadata.get('credential_transition_pending'):
        raise RuntimeError('Interrupted password change: run install.sh admin-password-recover before trying again')
    previous = private_json(root / 'secrets/admin-access.json')
    if previous.get('username') != 'matrix-admin':
        raise RuntimeError('Unexpected administrator identity; no password changed')
    if password == previous['password']:
        raise RuntimeError('The new password equals the saved password; no change requested')
    lifecycle.backup()
    lifecycle.restore_check()
    wait_services()
    parent = root / 'stages/credentials'
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.is_symlink():
        raise RuntimeError('Unsafe credential checkpoint directory')
    parent.chmod(0o700)
    folder = parent / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:6])
    folder.mkdir(mode=0o700)
    state_store.write_json(folder / 'previous.json', previous)
    replacement = dict(previous, password=password)
    state_store.write_json(folder / 'replacement.json', replacement)
    metadata = lifecycle.state()
    metadata.update(credential_transition_pending=True,
                    credential_transition_dir=str(folder.relative_to(root)))
    state_store.write_json(root / 'installation.json', metadata)
    vault_cache.mark_dirty(root)
    try:
        apply(root, replacement)
        state_store.write_json(root / 'secrets/admin-access.json', replacement)
        wait_services()
        metadata.pop('credential_transition_pending', None)
        metadata.pop('credential_transition_dir', None)
        state_store.write_json(root / 'installation.json', metadata)
    except Exception:
        try:
            recover(root)
        except Exception:
            if not lifecycle.state().get('credential_transition_pending'):
                raise RuntimeError('Original password restored, but services are not ready. '
                    'Keep private credentials, then retry: sudo bash '+str(root / 'install.sh')+' up') from None
            raise RuntimeError('Password change was interrupted and recovery is pending. Keep private checkpoints. '
                'Restore MAS connectivity, then run: sudo bash ' + str(root / 'install.sh') + ' admin-password-recover') from None
        raise RuntimeError('Password change failed; original password restored. Correct the private input and retry admin-password') from None
    vault_cache.sync_optional(root, lifecycle.emit)
    lifecycle.emit('administrator_password_changed', passed=True, password_omitted=True,
                   credential_file=str(root / 'secrets/admin-access.json'),
                   next_action='Sign in with the new password; manage existing sessions separately')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--password-file', type=Path)
    parser.add_argument('action', choices=('rotate', 'recover'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run password operations as root')
    def interrupted(signum, frame):
        raise state_store.OperationInterrupted('Password operation interrupted; keep private input and checkpoints')
    for signum in (signal.SIGTERM, signal.SIGINT): signal.signal(signum, interrupted)
    root = args.directory.resolve(strict=True)
    lifecycle.configure(root, 'admin-password-' + args.action)
    with state_store.operation_guard(root):
        state_store.assert_update_allowed(root)
        lifecycle.ownership_check()
        metadata = lifecycle.state()
        if any(metadata.get(k) for k in ('restore_pending', 'migration_frozen', 'media_transition_pending')):
            raise RuntimeError('Finish restoration, migration or media recovery before changing credentials')
        if args.action == 'recover':
            recover(root)
        elif args.password_file:
            rotate(root, args.password_file)
        else:
            raise RuntimeError('Provide a private JSON password file')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'check': 'administrator_password', 'passed': False,
            'message': str(error) if isinstance(error, RuntimeError) else
            'Credential operation failed; retain private input and checkpoints, inspect them privately'}))
        raise SystemExit(1)
