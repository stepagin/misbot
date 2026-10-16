"""Optional native Telegram worker with a separate privileged host controller."""

import hashlib
import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import tarfile
import tempfile

import configuration
import state_store

WORKER_CONFIG = 'config/telegram-worker.json'
CONTROL = 'data/telegram-control'
WORKER_DATA = 'data/telegram-worker'
CONTROL_DB = CONTROL + '/data/bot-control.sqlite3'
WORKER_DB = WORKER_DATA + '/worker.sqlite3'
IMAGE_LOCK = 'telegram-images.lock.json'
BACKUP_RECORD = 'telegram-runtime-backup.json'
SOURCES = ('Dockerfile', 'requirements.txt', 'worker.py', 'common.py')
FILES = (WORKER_CONFIG, CONTROL + '/bot-config.json', CONTROL + '/bot-policy.json', IMAGE_LOCK)
MEMBERS = (*FILES, CONTROL_DB, WORKER_DB)


def enabled(metadata):
    record = metadata.get('telegram_runtime')
    if record is None:
        return False
    if (
        not isinstance(record, dict)
        or type(record.get('format')) is not int
        or record.get('format') != 1
        or record.get('server_name') != metadata['domain']
        or set(record) - {'format', 'server_name', 'identity_sha256'}
    ):
        raise RuntimeError('Unknown native Telegram runtime; retain its private state')
    return True


def metadata(root):
    return configuration.read_private_json(Path(root) / 'installation.json')


def regular(path, owner, directory=False, ownership=True):
    path = Path(path)
    info = path.lstat()
    if (
        path.resolve() != path.absolute()
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or (not directory and info.st_nlink != 1)
        or (ownership and (info.st_uid != owner or info.st_gid != owner))
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
    ):
        raise RuntimeError('Unsafe native Telegram configuration or state; retain original files')
    return path


def private_json(path, owner=None, ownership=True):
    owner = os.geteuid() if owner is None else owner
    path = regular(path, owner, ownership=ownership)
    if path.stat().st_size > 131072:
        raise RuntimeError('Native Telegram configuration exceeds its supported size')
    return json.loads(path.read_text())


def worker_sidecars(path, ownership=True):
    for suffix in ('-wal', '-shm'):
        try:
            descriptor = os.open(str(path) + suffix, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue
        except OSError:
            raise RuntimeError('Unsafe Telegram SQLite sidecar; retain original state') from None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or (ownership and (info.st_uid not in (0, 10001) or info.st_gid not in (0, 10001)))
            ):
                raise RuntimeError('Unsafe Telegram SQLite sidecar; retain original state')
            if ownership and os.geteuid() == 0:
                os.fchown(descriptor, 10001, 10001)
        finally:
            os.close(descriptor)


def database(path, domain, worker=False, ownership=True):
    regular(path, 10001 if worker else 0, ownership=ownership)
    if worker:
        worker_sidecars(path, ownership)
    try:
        sqlite_state(path, domain, worker)
    finally:
        if worker:
            # Even a read-only WAL connection can create owner-root sidecars.
            worker_sidecars(path, ownership)


def sqlite_state(path, domain, worker):
    with closing(
        sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro', uri=True, timeout=30)
    ) as db:
        if (
            db.execute('PRAGMA user_version').fetchone()[0] != 0
            or db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
        ):
            raise RuntimeError('Unknown or damaged Telegram SQLite; migration is required')
        expected = (
            {'state': ('key', 'value'), 'users': ('uid', 'server', 'waiting')}
            if worker
            else {'authorizations': ('server', 'uid', 'user_id', 'expires')}
        )
        for table, columns in expected.items():
            if tuple(row[1] for row in db.execute('PRAGMA table_info(' + table + ')')) != columns:
                raise RuntimeError('Unknown Telegram SQLite layout; retain the original runtime')
        if worker:
            binding = db.execute("SELECT value FROM state WHERE key='server_binding'").fetchone()
            if not binding or json.loads(binding[0]) != {'format': 1, 'server_name': domain}:
                raise RuntimeError(
                    'Telegram worker server binding differs; no state reset performed'
                )
            if db.execute(
                "SELECT 1 FROM users WHERE server IS NOT NULL AND server<>'' AND server<>? LIMIT 1",
                (domain,),
            ).fetchone():
                raise RuntimeError('Telegram worker contains another server binding')
        else:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in tables:
                columns = {
                    row[1]
                    for row in db.execute(
                        'PRAGMA table_info(' + '"' + table.replace('"', '""') + '"' + ')'
                    )
                }
                if (
                    'server' in columns
                    and db.execute(
                        'SELECT 1 FROM "' + table.replace('"', '""') + '" WHERE server<>? LIMIT 1',
                        (domain,),
                    ).fetchone()
                ):
                    raise RuntimeError('Telegram control contains state for another server')
            for table in ('auth_requests', 'authorizations', 'telegram_accounts'):
                foreign = (
                    db.execute('SELECT uid FROM ' + table + " WHERE typeof(uid)<>'integer'")
                    if table in tables
                    else ()
                )
                if any(
                    table != 'telegram_accounts'
                    or not isinstance(row[0], str)
                    or not re.fullmatch(r'retired:[0-9a-f]{32}', row[0])
                    for row in foreign
                ):
                    raise RuntimeError(
                        'Separate Matrix and Telegram authorization state before import'
                    )


def validate(root, state=None, ownership=None):
    root = Path(root)
    state = state if state is not None else metadata(root)
    if not enabled(state):
        return None
    if ownership is None:
        ownership = not state.get('preview', False)
    regular(root / CONTROL, 0, directory=True, ownership=ownership)
    regular(root / (CONTROL + '/data'), 0, directory=True, ownership=ownership)
    regular(root / WORKER_DATA, 10001, directory=True, ownership=ownership)
    worker = private_json(root / WORKER_CONFIG, 10001, ownership)
    config = private_json(root / (CONTROL + '/bot-config.json'), ownership=ownership)
    policy = private_json(root / (CONTROL + '/bot-policy.json'), ownership=ownership)
    domain = state['domain']
    if (
        worker.get('server_name') != domain
        or not isinstance(worker.get('token'), str)
        or not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', worker['token'])
        or not isinstance(worker.get('username'), str)
        or not worker['username']
        or str(worker.get('client_id')) != worker['token'].split(':', 1)[0]
        or set(config.get('servers', {})) != {domain}
        or set(policy.get('servers', {})) != {domain}
    ):
        raise RuntimeError(
            'Telegram credentials, policy and server binding must match this installation'
        )
    server = config['servers'][domain]
    if server.get('federation_backend') != 'misbot':
        raise RuntimeError('Native Telegram requires federation_backend=misbot')
    if any(
        not isinstance(server.get(key), str) or not server[key]
        for key in ('client_id', 'client_secret', 'mas_url', 'synapse_url')
    ):
        raise RuntimeError('Retain the existing Telegram MAS client credentials and URLs')
    identity = hashlib.sha256(
        json.dumps(
            {
                'server_name': domain,
                'token': worker['token'],
                'username': worker['username'],
                'client_id': str(worker['client_id']),
                'mas_client_id': server['client_id'],
                'mas_client_secret': server['client_secret'],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if state['telegram_runtime'].get('identity_sha256', identity) != identity:
        raise RuntimeError('Native Telegram identity changed; restore the original private files')
    database(root / CONTROL_DB, domain, ownership=ownership)
    database(root / WORKER_DB, domain, worker=True, ownership=ownership)
    matrix = root / 'data/referral-control/data/bot-control.sqlite3'
    if matrix.exists():
        if matrix.samefile(root / CONTROL_DB):
            raise RuntimeError('Matrix and Telegram controllers must use separate SQLite files')
        with closing(sqlite3.connect(matrix.absolute().as_uri() + '?mode=ro', uri=True)) as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in ('auth_requests', 'authorizations', 'telegram_accounts'):
                if (
                    table in tables
                    and db.execute(
                        'SELECT 1 FROM ' + table + " WHERE typeof(uid)='integer' LIMIT 1"
                    ).fetchone()
                ):
                    raise RuntimeError(
                        'Telegram authorizations must not be polled by the Matrix controller'
                    )
    return identity


def configure(root, state, preview=False):
    identity = validate(root, state)
    if identity is None:
        return
    root = Path(root)
    config = private_json(root / (CONTROL + '/bot-config.json'), ownership=not preview)
    server = config['servers'][state['domain']]
    server['stack_root'] = str(root)
    server['synapse_url'] = 'http://127.0.0.1:8008'
    if state.get('mas_mode', 'local') == 'local':
        server['mas_url'] = 'http://127.0.0.1:8080'
    state_store.write_json(root / (CONTROL + '/bot-config.json'), config)
    state['telegram_runtime']['identity_sha256'] = identity


def read_image_lock(path):
    value = private_json(path)
    if (
        value.get('format') != 1
        or not isinstance(value.get('image_id'), str)
        or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['image_id'])
    ):
        raise RuntimeError('Unknown native Telegram image pin; retain the accepted checkpoint')
    return value


def socket_directory(root):
    return Path('/run') / (
        'misbot-telegram-' + hashlib.sha256(str(Path(root).absolute()).encode()).hexdigest()[:16]
    )


def unit_name(root):
    return socket_directory(root).name + '.service'


def service(root, state):
    if not enabled(state):
        return {}
    lock = Path(root) / IMAGE_LOCK
    image = (
        read_image_lock(lock)['image_id']
        if lock.exists() or lock.is_symlink()
        else 'matrix-stack/telegram-worker:unprepared'
    )
    return {
        'telegram-worker': {
            'image': image,
            'pull_policy': 'never',
            'restart': 'unless-stopped',
            'user': '10001:10001',
            'read_only': True,
            'cap_drop': ['ALL'],
            'security_opt': ['no-new-privileges:true'],
            'pids_limit': 64,
            'mem_limit': '96m',
            'memswap_limit': '192m',
            'tmpfs': ['/tmp:rw,size=8m'],
            'environment': {'MISBOT_CONTROL_SOCKET': '/run/control/control.sock'},
            'volumes': [
                './' + WORKER_CONFIG + ':/config/worker.json:ro',
                './' + WORKER_DATA + ':/data',
                str(socket_directory(root)) + ':/run/control:ro',
            ],
            'logging': {'driver': 'json-file', 'options': {'max-size': '3m', 'max-file': '2'}},
            'healthcheck': {
                'test': [
                    'CMD',
                    'python',
                    '-c',
                    "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)",
                ],
                'interval': '20s',
                'timeout': '5s',
                'retries': 3,
            },
        }
    }


def prepare_image(root, run):
    root = Path(root)
    if not enabled(metadata(root)):
        return
    validate(root)
    manifest = dict(
        line.split('  ', 1)[::-1] for line in (root / 'SHA256SUMS').read_text().splitlines()
    )
    hashes = {}
    for name in SOURCES:
        path = root / 'telegram-bot' / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if manifest.get('telegram-bot/' + name) != digest or path.is_symlink():
            raise RuntimeError('Telegram build requires the complete verified public source bundle')
        hashes[name] = digest
    base = json.loads((root / 'versions.json').read_text())['build_bases']['referral-control']
    if (root / 'telegram-bot/Dockerfile').read_text().splitlines()[0] != 'FROM ' + base:
        raise RuntimeError('Telegram build base differs from the pinned runtime')
    path = root / IMAGE_LOCK
    old = read_image_lock(path) if path.exists() or path.is_symlink() else {}
    image = old.get('image_id', '')
    present = ''
    if re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        present = run(
            ['docker', 'image', 'inspect', image, '--format', '{{.Id}}'], allowed=(0, 1)
        ).strip()
    if (
        present != image
        or old.get('source_sha256') != hashes
        or old.get('base') != base
        or not image
    ):
        (root / 'stages').mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='telegram-build-', dir=root / 'stages') as folder:
            folder = Path(folder)
            context = folder / 'context'
            context.mkdir(mode=0o700)
            for name in SOURCES:
                shutil.copyfile(root / 'telegram-bot' / name, context / name)
            iid = folder / 'image-id'
            run(
                ['docker', 'build', '--pull=false', '--iidfile', str(iid), str(context)],
                allowed=(0,),
            )
            image = iid.read_text().strip()
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
                raise RuntimeError('Telegram build did not return an immutable image ID')
            run(
                [
                    'docker',
                    'run',
                    '--rm',
                    '--pull',
                    'never',
                    '--network',
                    'none',
                    '--read-only',
                    '--cap-drop',
                    'ALL',
                    '--security-opt',
                    'no-new-privileges:true',
                    '--pids-limit',
                    '64',
                    '--memory',
                    '96m',
                    '--tmpfs',
                    '/tmp:rw,size=8m',
                    '--entrypoint',
                    'python',
                    image,
                    'worker.py',
                    '--help',
                ]
            )
        state_store.write_json(
            path, {'format': 1, 'image_id': image, 'source_sha256': hashes, 'base': base}
        )
    import yaml

    compose = yaml.safe_load((root / 'compose.yaml').read_text())
    compose['services']['telegram-worker']['image'] = image
    state_store.write_text(root / 'compose.yaml', yaml.safe_dump(compose, sort_keys=False))


def controller(root, run, start=True, system=None, remove=False):
    root = Path(root).absolute()
    if root.resolve() != root or any(ord(char) < 32 for char in str(root)):
        raise RuntimeError('Unsafe native Telegram installation directory')
    if not enabled(metadata(root)):
        return
    path = Path(system or '/etc/systemd/system') / unit_name(root)
    marker = '# Misbot Telegram controller ' + str(root) + '\n'
    if path.is_symlink() or (path.exists() and not path.read_text().startswith(marker)):
        raise RuntimeError('Another installation owns the Telegram controller unit')
    if not start:
        if path.exists():
            run(['systemctl', 'disable', '--now', path.name])
            if remove:
                path.unlink()
                run(['systemctl', 'daemon-reload'])
                folder = socket_directory(root)
                if folder.exists():
                    info = folder.lstat()
                    children = list(folder.iterdir())
                    if (
                        folder.resolve() != folder
                        or not stat.S_ISDIR(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or any(
                            p.name != 'control.sock' or not stat.S_ISSOCK(p.lstat().st_mode)
                            for p in children
                        )
                    ):
                        raise RuntimeError('Unregistered Telegram runtime files retained')
                    for child in children:
                        child.unlink()
                    folder.rmdir()
        return
    if metadata(root).get('restore_pending') or metadata(root).get('migration_frozen'):
        raise RuntimeError('Staged/frozen installation cannot start Telegram')
    if importlib.util.find_spec('requests') is None:
        raise RuntimeError('Install python3-requests on this host, then retry misbot start')
    validate(root)
    folder = socket_directory(root)
    folder.mkdir(mode=0o750, exist_ok=True)
    info = folder.lstat()
    if folder.resolve() != folder or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError('Unsafe native Telegram socket directory')
    os.chown(folder, 0, 10001)
    folder.chmod(0o750)

    def quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'

    state_store.write_text(
        path,
        marker
        + """[Unit]
Description=Misbot native Telegram controller
After=docker.service network-online.target
[Service]
Type=simple
Group=0
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
RuntimeDirectory="""
        + folder.name
        + """
RuntimeDirectoryMode=0750
RuntimeDirectoryPreserve=yes
ExecStart=/usr/bin/python3 """
        + quote(root / 'referral-control/bridge.py')
        + ' --config '
        + quote(root / CONTROL / 'bot-config.json')
        + ' --socket-directory '
        + quote(folder)
        + """
[Install]
WantedBy=multi-user.target
""",
        mode=0o644,
    )
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', path.name])
    run(['systemctl', 'restart', path.name])


def rpc(root, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(60)
        client.connect(str(socket_directory(root) / 'control.sock'))
        client.sendall(json.dumps(request).encode() + b'\n')
        with client.makefile('rb') as response:
            answer = json.loads(response.readline(32769))
    if not answer.get('ok'):
        raise RuntimeError('Telegram controller did not confirm its snapshot/readiness')
    return answer['result']


def ready(root):
    try:
        return rpc(root, {'action': 'servers'}) is not None
    except (
        FileNotFoundError,
        ConnectionRefusedError,
        ConnectionResetError,
        BrokenPipeError,
        TimeoutError,
    ):
        raise RuntimeError('Telegram controller transport is not ready') from None


def check(root, state=None):
    state = state if state is not None else metadata(root)
    if not enabled(state):
        return
    validate(root, state)
    if set(rpc(root, {'action': 'servers'})) != {state['domain']}:
        raise RuntimeError('Telegram controller is bound to another server')


def assert_stopped(root, run, state=None):
    if enabled(state if state is not None else metadata(root)):
        active = run(['systemctl', 'is-active', unit_name(root)], allowed=(0, 3, 4)).strip()
        if active not in ('inactive', 'failed', 'unknown', ''):
            raise RuntimeError(
                'Staged Telegram controller is still active; stop it before activation'
            )


def snapshot_database(source, output):
    with closing(
        sqlite3.connect(Path(source).absolute().as_uri() + '?mode=ro', uri=True, timeout=30)
    ) as live:
        with closing(sqlite3.connect(output)) as saved:
            live.backup(saved)
            if saved.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Telegram SQLite snapshot integrity failed')
    Path(output).chmod(0o600)


def add_backup(tar, root, state, run):
    """Archive standalone snapshots, never the live control DB or worker WAL."""
    if not enabled(state):
        return
    root = Path(root)
    validate(root, state)
    read_image_lock(root / IMAGE_LOCK)
    import backup_archive

    with tempfile.TemporaryDirectory(prefix='telegram-snapshot-', dir=root / 'backups') as folder:
        folder = Path(folder)
        active = run(['systemctl', 'is-active', unit_name(root)], allowed=(0, 3, 4)).strip()
        if active == 'active':
            response = rpc(root, {'action': 'backup_snapshot'})
            source = regular(root / CONTROL / 'data/backup.sqlite3', os.geteuid())
            if hashlib.sha256(source.read_bytes()).hexdigest() != response.get('sha256'):
                raise RuntimeError('Telegram controller snapshot checksum differs')
            shutil.copyfile(source, folder / 'control.sqlite3')
            (folder / 'control.sqlite3').chmod(0o600)
            source.unlink()
        elif active in ('inactive', 'failed', 'unknown', ''):
            snapshot_database(root / CONTROL_DB, folder / 'control.sqlite3')
        else:
            raise RuntimeError('Wait for the Telegram controller transition before backup')
        snapshot_database(root / WORKER_DB, folder / 'worker.sqlite3')
        snapshots = {CONTROL_DB: folder / 'control.sqlite3', WORKER_DB: folder / 'worker.sqlite3'}
        hashes = {
            name: hashlib.sha256((snapshots.get(name) or root / name).read_bytes()).hexdigest()
            for name in MEMBERS
        }
        state_store.write_json(folder / 'record.json', {'format': 1, 'sha256': hashes})
        for name in (CONTROL, CONTROL + '/data', WORKER_DATA):
            backup_archive.add_tree(tar, root / name, name, recursive=False)
        for name in (CONTROL + '/bot-config.json', CONTROL + '/bot-policy.json'):
            backup_archive.add_tree(tar, root / name, name)
        for name, source in {**snapshots, BACKUP_RECORD: folder / 'record.json'}.items():
            info = tar.gettarinfo(str(source), arcname=name)
            info.uid = info.gid = 10001 if name == WORKER_DB else 0
            info.mode = 0o600
            with source.open('rb') as content:
                tar.addfile(info, content)


def verify_archive(archive):
    with tarfile.open(archive, 'r:gz') as tar:
        state = json.load(tar.extractfile('installation.json'))
        if not enabled(state):
            if any(member.name in (*MEMBERS, BACKUP_RECORD) for member in tar.getmembers()):
                raise RuntimeError('Unregistered native Telegram backup payload')
            return
        with tempfile.TemporaryDirectory(prefix='telegram-verify-') as folder:
            root = Path(folder)
            record = json.load(tar.extractfile(BACKUP_RECORD))
            if record.get('format') != 1 or set(record.get('sha256', {})) != set(MEMBERS):
                raise RuntimeError('Incomplete native Telegram backup')
            for name in MEMBERS:
                members = [member for member in tar.getmembers() if member.name == name]
                if len(members) != 1 or not members[0].isfile() or members[0].islnk():
                    raise RuntimeError('Invalid native Telegram archive member')
                path = root / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with tar.extractfile(members[0]) as source, path.open('wb') as target:
                    shutil.copyfileobj(source, target)
                path.chmod(0o600)
                if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256'][name]:
                    raise RuntimeError('Native Telegram backup checksum differs')
            validate(root, state, ownership=False)
            read_image_lock(root / IMAGE_LOCK)
