"""Versioned opaque secret-file snapshots; local files remain the runtime cache."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

import preflight
import state_store

FIELDS = {
    'VAULT_ADDR',
    'VAULT_KV_MOUNT',
    'VAULT_SECRET_PATH',
    'VAULT_ROLE_ID_FILE',
    'VAULT_SECRET_ID_FILE',
    'VAULT_CA_FILE',
}
LIMIT = 1024 * 1024
RECOVERY_IDS = {0, 991, 999, 10001, 10002, 10003, 65532}


def private_bytes(path):
    path = Path(path)
    info = path.lstat()
    if (
        path.is_symlink()
        or not path.is_file()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise RuntimeError(
            'Vault authentication requires owned private regular files, mode 600, without symlinks'
        )
    if info.st_size > 4096:
        raise RuntimeError('Vault authentication file is too large')
    return path.read_bytes().strip()


def connection(path):
    values = preflight.private_settings_file(path)
    if set(values) - FIELDS or not (FIELDS - {'VAULT_CA_FILE'}) <= set(values):
        raise RuntimeError(
            'Use all required Vault parameter names from vault.md, without unknown names'
        )
    address = urllib.parse.urlsplit(values['VAULT_ADDR'])
    try:
        valid_port = address.port is None or 1 <= address.port <= 65535
    except ValueError:
        valid_port = False
    if (
        address.scheme != 'https'
        or not address.hostname
        or address.username
        or address.password
        or address.path not in ('', '/')
        or address.query
        or address.fragment
        or not valid_port
    ):
        raise RuntimeError('VAULT_ADDR must be an HTTPS origin without credentials, query or path')
    for key in ('VAULT_KV_MOUNT', 'VAULT_SECRET_PATH'):
        value = values[key]
        if not value or not all(re.fullmatch(r'[A-Za-z0-9_-]+', part) for part in value.split('/')):
            raise RuntimeError(
                'Vault mount/path must be relative names without empty, dot or encoded segments'
            )
    for key in ('VAULT_ROLE_ID_FILE', 'VAULT_SECRET_ID_FILE'):
        if not Path(values[key]).is_absolute():
            raise RuntimeError('Use absolute paths to private Vault authentication files')
    return values


class Client:
    def __init__(self, values):
        self.values = values
        context = ssl.create_default_context(cafile=values.get('VAULT_CA_FILE') or None)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            NoRedirect(),
        )
        self.token = None
        login = self.request(
            'auth/approle/login',
            {
                'role_id': private_bytes(values['VAULT_ROLE_ID_FILE']).decode(),
                'secret_id': private_bytes(values['VAULT_SECRET_ID_FILE']).decode(),
            },
        )
        self.token = login['auth']['client_token']
        self.path = values['VAULT_KV_MOUNT'] + '/data/' + values['VAULT_SECRET_PATH']

    def request(self, path, data=None, missing=False):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['X-Vault-Token'] = self.token
        payload = json.dumps(data).encode() if data is not None else None
        try:
            with self.opener.open(
                urllib.request.Request(
                    self.values['VAULT_ADDR'].rstrip('/') + '/v1/' + path,
                    data=payload,
                    headers=headers,
                ),
                timeout=15,
            ) as response:
                raw = response.read(LIMIT + 65537)
                if len(raw) > LIMIT + 65536:
                    raise RuntimeError('Vault response exceeds the supported snapshot size')
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            if error.code == 503:
                raise RuntimeError(
                    'Vault is unavailable or sealed. Restore the service or ask its administrator to unseal it, then retry vault-sync; local private files are retained'
                ) from None
            raise RuntimeError(
                'Vault request refused; check AppRole policy, credentials, CAS version and server availability privately'
            ) from None
        except (OSError, ValueError):
            raise RuntimeError(
                'Vault connection failed; check TLS, DNS, availability and private credentials, then retry vault-sync'
            ) from None

    def read(self, version=None):
        response = self.request(
            self.path + ('?version=' + str(version) if version else ''), missing=True
        )
        if response is None:
            return None
        return response['data']['metadata']['version'], response['data']['data']

    def write(self, payload, version):
        return self.request(self.path, {'options': {'cas': version}, 'data': payload})['data'][
            'version'
        ]


def descriptor(root):
    return Path(root) / 'config/vault-state.json'


def mark_dirty(root):
    path = descriptor(root)
    if path.exists():
        value = json.loads(path.read_text())
        value['dirty'] = True
        state_store.write_json(path, value)


def secret_files(root):
    root = Path(root)
    metadata = json.loads((root / 'installation.json').read_text())
    names = (
        ['settings/' + p.name for p in (root / 'settings').iterdir()]
        if (root / 'settings').is_dir()
        else ['settings.env']
    ) + [
        'config/mas-signing.pem',
        'config/mas-defaults.yaml',
        'config/referral-bot.json',
        'config/external-postgres-owner.json',
        'data/referral-bot/cross-signing.json',
        'config/federation-bot.json',
        'config/welcome-bot.json',
        'config/admin-panel-identity.json',
        'data/federation-bot/welcome/cross-signing.json',
        'data/federation-bot/cross-signing.json',
        'data/federation/identity.pem',
        'data/synapse/' + metadata['domain'] + '.signing.key',
    ]
    names += [
        str(path.relative_to(root))
        for path in (root / 'secrets').rglob('*')
        if path.is_file() or path.is_symlink()
    ]
    return sorted(name for name in names if (root / name).exists() or (root / name).is_symlink())


def snapshot(root, owner):
    files, manifest = {}, {}
    total = 0
    for name in secret_files(root):
        path = root / name
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or path.resolve() != path.absolute():
            raise RuntimeError(
                'Secret snapshot paths must be regular files without symlink parents'
            )
        if info.st_mode & 0o007 or info.st_mode & 0o070 not in (0, 0o040):
            raise RuntimeError('Close broad secret-file access before exporting to Vault')
        total += info.st_size
        if total > LIMIT // 2:
            raise RuntimeError(
                'Vault snapshot exceeds 512 KiB of source files; use full encrypted backups for data'
            )
        opaque = path.read_bytes()
        files[name] = base64.b64encode(opaque).decode('ascii')
        manifest[name] = {
            'sha256': hashlib.sha256(opaque).hexdigest(),
            'size': len(opaque),
            'uid': info.st_uid,
            'gid': info.st_gid,
            'mode': info.st_mode & 0o777,
        }
    return {
        'format': 1,
        'owner': owner['owner'],
        'server_name': owner['server_name'],
        'files': files,
        'manifest': manifest,
    }


def sync(root, service=None):
    path = descriptor(root)
    saved = json.loads(path.read_text())
    mark_dirty(root)
    values = connection(root / 'config/vault-connection.env')
    service = service or Client(values)
    payload = snapshot(root, saved)
    remote = service.read()
    version = remote[0] if remote else 0
    if remote and (
        remote[1].get('owner') != saved['owner']
        or remote[1].get('server_name') != saved['server_name']
    ):
        raise RuntimeError(
            'Another installation owns this Vault path; use a dedicated empty path, never overwrite it'
        )
    if remote and remote[1] == payload:
        pass  # Lost commit reply or unchanged repeat: verify, without making another version.
    else:
        if version != saved.get('version', 0):
            raise RuntimeError(
                'Vault version changed independently; keep local cache and resolve ownership before retrying'
            )
        version = service.write(payload, version)
    verified = service.read(version)
    if not verified or verified != (version, payload):
        raise RuntimeError(
            'Vault snapshot readback failed; local cache retained, repeat vault-sync'
        )
    saved.update(version=version, manifest=payload['manifest'], dirty=False)
    state_store.write_json(path, saved)
    return len(payload['files']), version


def sync_optional(root, emit):
    if not descriptor(root).exists():
        return
    try:
        count, version = sync(root)
        emit('vault_snapshot', passed=True, files=count, version=version, contents_omitted=True)
    except state_store.OperationInterrupted:
        raise
    except Exception:
        emit(
            'vault_snapshot',
            passed=False,
            local_cache_retained=True,
            next_action='Keep local private files. Restore Vault connectivity and run: sudo bash '
            + str(root / 'install.sh')
            + ' vault-sync',
        )


def connect(root, settings):
    values = connection(settings)
    # Validate authentication before recording a connection; never adopt an occupied path.
    service = Client(values)
    path = descriptor(root)
    if path.exists():
        if connection(root / 'config/vault-connection.env') != values:
            raise RuntimeError(
                'Vault destination is fixed; use vault-auth for authentication changes or a dedicated migration'
            )
    else:
        if service.read() is not None:
            raise RuntimeError('Vault path is occupied; choose a dedicated empty path')
        domain = json.loads((root / 'installation.json').read_text())['domain']
        state_store.write_text(root / 'config/vault-connection.env', Path(settings).read_text())
        state_store.write_json(
            path,
            {
                'format': 1,
                'owner': uuid.uuid4().hex,
                'server_name': domain,
                'version': 0,
                'dirty': True,
            },
        )
    return sync(root, service)


def recover(root, service=None):
    saved = json.loads(descriptor(root).read_text())
    if saved.get('dirty') or not saved.get('version'):
        raise RuntimeError(
            'Vault snapshot is stale or unverified; recover from the full server backup, not older credentials'
        )
    import lifecycle

    if any(
        row['State'] in ('running', 'restarting')
        for row in lifecycle.service_rows()
        if row['Service'] != 'postgres'
    ):
        raise RuntimeError(
            'Stop server services before restoring secret files from Vault; then retry vault-recover'
        )
    service = service or Client(connection(root / 'config/vault-connection.env'))
    remote = service.read(saved['version'])
    if (
        not remote
        or remote[1].get('owner') != saved['owner']
        or remote[1].get('server_name') != saved['server_name']
        or remote[1].get('manifest') != saved['manifest']
        or remote[1].get('format') != 1
        or set(remote[1].get('files', {})) != set(saved['manifest'])
    ):
        raise RuntimeError(
            'Vault snapshot identity or inventory differs; use the verified full backup'
        )
    prepared = []
    for name, expected in saved['manifest'].items():
        path = root / name
        if (
            Path(name).is_absolute()
            or '..' in Path(name).parts
            or path.resolve() != path.absolute()
        ):
            raise RuntimeError('Unsafe Vault recovery path; target unchanged')
        if (
            not path.parent.is_dir()
            or expected['uid'] not in RECOVERY_IDS
            or expected['gid'] not in RECOVERY_IDS
            or expected['mode'] & 0o007
            or expected['mode'] & 0o070 not in (0, 0o040)
        ):
            raise RuntimeError('Unsupported Vault recovery owner/path; use full server restore')
        opaque = base64.b64decode(remote[1]['files'][name], validate=True)
        if (
            len(opaque) != expected['size']
            or hashlib.sha256(opaque).hexdigest() != expected['sha256']
        ):
            raise RuntimeError('Vault secret-file checksum differs; no files restored')
        if path.exists():
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected['sha256']
            ):
                raise RuntimeError(
                    'Existing secret differs; refusing to replace newer credentials with an older Vault version'
                )
        else:
            prepared.append((path, opaque, expected))
    for path, opaque, expected in prepared:
        descriptor_fd, temporary = tempfile.mkstemp(prefix='.vault-recover-', dir=path.parent)
        try:
            with os.fdopen(descriptor_fd, 'wb') as output:
                os.fchmod(output.fileno(), expected['mode'])
                if os.geteuid() == 0:
                    os.fchown(output.fileno(), expected['uid'], expected['gid'])
                output.write(opaque)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, path)  # Never replace a file that appeared since validation.
        finally:
            os.unlink(temporary)
    return len(prepared)


def assert_change_allowed(root, metadata, action):
    if any(
        metadata.get(key)
        for key in ('migration_frozen', 'media_transition_pending', 'credential_transition_pending')
    ):
        raise RuntimeError(
            'Finish installation recovery or migration before changing Vault storage'
        )
    if metadata.get('restore_pending'):
        marker = Path(root) / 'restore-journal.json'
        if (
            action != 'disconnect'
            or not marker.is_file()
            or marker.is_symlink()
            or json.loads(marker.read_text()).get('stage') != 'ready'
        ):
            raise RuntimeError(
                'Finish the original restore/restore-local command before changing Vault; '
                'only vault-disconnect is allowed for a ready staged target'
            )
        import lifecycle

        if any(
            row['Service'] != 'postgres' and row['State'] in ('running', 'restarting', 'paused')
            for row in lifecycle.service_rows()
        ):
            raise RuntimeError(
                'Staged target services are still running; stop the target with install.sh stop '
                'and repeat vault-disconnect. Local files and remote Vault versions were not changed'
            )


def disconnect(root):
    """Detach the local descriptor first; repeated cleanup never contacts Vault."""
    paths = (descriptor(root), Path(root) / 'config/vault-connection.env')
    for path in paths:
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise RuntimeError(
                    'Vault disconnect files must be private owned regular files; '
                    'retain the local cache, correct permissions privately and repeat vault-disconnect'
                )
    changed = any(path.exists() for path in paths)
    if paths[0].exists():
        saved = json.loads(paths[0].read_text())
        missing = [name for name in saved.get('manifest', {}) if not (Path(root) / name).is_file()]
        if missing:
            # restore-local deliberately retires this external cluster connector.
            # A stale archived Vault inventory must not require reattaching it.
            metadata = json.loads((Path(root) / 'installation.json').read_text())
            marker = Path(root) / 'restore-journal.json'
            local_restore = (
                missing == ['config/external-postgres-owner.json']
                and metadata.get('restore_pending') is True
                and metadata.get('database_mode') == 'local'
                and marker.is_file()
                and not marker.is_symlink()
            )
            if local_restore:
                info = marker.stat()
                progress = json.loads(marker.read_text())
                values = preflight.settings(root)
                local_restore = (
                    info.st_uid == os.geteuid()
                    and not info.st_mode & 0o077
                    and progress.get('stage') == 'ready'
                    and progress.get('local_postgres') is True
                    and values.get('POSTGRES_MODE') == 'local'
                    and not values.get('POSTGRES_HOST')
                )
            if not local_restore:
                raise RuntimeError(
                    'Restore missing secret files before disconnecting Vault; '
                    'only a retired external PostgreSQL ownership connector in a ready restore-local target may be absent'
                )
            assert_change_allowed(root, metadata, 'disconnect')
        snapshot(root, saved)  # Validate cache; do not contact Vault or print its contents.
    # Once descriptor is absent no optional sync can publish into the source path.
    paths[0].unlink(missing_ok=True)
    paths[1].unlink(missing_ok=True)
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('connect', 'sync', 'recover', 'auth', 'disconnect'))
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--settings', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Run Vault operations as root')
    root = args.directory.resolve(strict=True)
    import lifecycle

    lifecycle.configure(root, 'vault-' + args.action)
    with state_store.operation_guard(root):
        state_store.assert_update_allowed(root)
        lifecycle.ownership_check()
        metadata = lifecycle.state()
        assert_change_allowed(root, metadata, args.action)
        if args.action == 'recover':
            count = recover(root)
            lifecycle.emit('vault_recovered', passed=True, files=count, contents_omitted=True)
        elif args.action == 'disconnect':
            changed = disconnect(root)
            lifecycle.emit(
                'vault_disconnected',
                passed=True,
                changed=changed,
                local_files_retained=True,
                remote_versions_retained=True,
            )
        else:
            if args.action == 'auth':
                values = connection(args.settings)
                old = connection(root / 'config/vault-connection.env')
                if any(
                    values[key] != old[key]
                    for key in ('VAULT_ADDR', 'VAULT_KV_MOUNT', 'VAULT_SECRET_PATH')
                ):
                    raise RuntimeError('vault-auth cannot change the snapshot destination')
                service = Client(values)
                saved = json.loads(descriptor(root).read_text())
                remote = service.read(saved['version'])
                if not remote or remote[1].get('owner') != saved['owner']:
                    raise RuntimeError(
                        'New AppRole cannot read this installation snapshot; old access configuration retained'
                    )
                previous = (root / 'config/vault-connection.env').read_text()
                if metadata.get('databases_initialized'):
                    lifecycle.backup()
                    lifecycle.restore_check()
                try:
                    state_store.write_text(
                        root / 'config/vault-connection.env', Path(args.settings).read_text()
                    )
                    count, version = sync(root, service)
                except Exception:
                    state_store.write_text(root / 'config/vault-connection.env', previous)
                    raise RuntimeError(
                        'Vault authentication change failed; previous access configuration restored. Correct the private file and retry vault-auth'
                    ) from None
            elif args.action == 'connect':
                if metadata.get('databases_initialized') and not descriptor(root).exists():
                    lifecycle.backup()
                    lifecycle.restore_check()
                count, version = connect(root, args.settings)
            else:
                count, version = sync(root)
            lifecycle.emit(
                'vault_snapshot', passed=True, files=count, version=version, contents_omitted=True
            )


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    'check': 'vault',
                    'passed': False,
                    'message': str(error)
                    if isinstance(error, RuntimeError)
                    else 'Vault operation failed; retain private local files, check authentication/TLS and retry the same command',
                }
            )
        )
        raise SystemExit(1)
