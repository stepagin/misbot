"""Portable referral bot configuration, provisioning and image preparation."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tarfile
import tempfile
import state_store

CLIENT_ID = '01K5WZ00000000000000000004'
DEVICE_ID = 'MATRIX-REFERRAL-01'
IMAGES = {'referral-bot': 'matrix-stack/referral-bot:1.0.0',
          'referral-control': 'matrix-stack/referral-control:1.0.0'}


def private(path, value):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Unexpected symlink in referral configuration')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_store.write_json(path,value)


def validate_saved(root, metadata):
    root = Path(root)
    if metadata.get('referral_provisioned'):
        for name in ['config/referral-bot.json', 'secrets/referral-session.json',
                     'data/referral-control/session.json', 'data/referral-control/data/bot-control.sqlite3']:
            if not (root / name).is_file():
                raise RuntimeError('Saved referral bot state is missing; restore it from backup')
        config = json.loads((root / 'config/referral-bot.json').read_text())
        journal = json.loads((root / 'data/referral-control/session.json').read_text())
        if any(config.get(k) != journal['config'].get(k) for k in ['access_token', 'user_id', 'device_id']):
            raise RuntimeError('Saved referral session does not match the original; restore it from backup')
    if metadata.get('referral_crypto_initialized'):
        for name in ['data/referral-bot/cross-signing.json', 'data/referral-bot/state.json']:
            if not (root / name).is_file():
                raise RuntimeError('Saved referral encryption keys or checkpoint are missing; restore from backup')
        if not any(p.is_file() and p.open('rb').read(16) == b'SQLite format 3\x00'
                   for p in (root / 'data/referral-bot/crypto').rglob('*') if not p.name.endswith(('-wal','-shm'))):
            raise RuntimeError('Saved referral crypto store is missing; restore from backup')


def configure(root, metadata, settings, preview=False):
    root = Path(root)
    validate_saved(root, metadata)
    domain = metadata['domain']
    path = root / 'secrets/referral-client.json'
    if not path.exists():
        if metadata.get('referral_seeded'):
            raise RuntimeError('Saved referral service credential is missing; restore from backup')
        private(path, {'client_id': CLIENT_ID,
                       'client_secret': 'REDACTED_REFERRAL_SECRET' if preview else secrets.token_urlsafe(48)})
    client = json.loads(path.read_text())
    if client['client_id'] != CLIENT_ID or not client.get('client_secret'):
        raise RuntimeError('Invalid referral service credentials')
    metadata['referral_seeded'] = True
    policy = {'invitation_lifetime_seconds': int(settings.get('INVITATION_LIFETIME_SECONDS', '604800')),
              'invitation_usage_limit': int(settings.get('INVITATION_USAGE_LIMIT', '1')),
              'registration_required': settings.get('REGISTRATION_TOKEN_REQUIRED', 'true') == 'true',
              'federation_servers': []}
    control = root / 'data/referral-control'
    for directory in [control, control / 'data', root / 'data/referral-bot']:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    socket = control / 'bot-run'
    socket.mkdir(mode=0o750, exist_ok=True)
    socket.chmod(0o750)
    private(control / 'bot-config.json', {'servers': {domain: {
        'mas_url': 'http://mas:8080', 'synapse_url': 'http://synapse:8008',
        'client_id': client['client_id'], 'client_secret': client['client_secret']}}})
    private(control / 'bot-policy.json', {'servers': {domain: policy}})
    config = root / 'config/referral-bot.json'
    if not config.exists():
        private(config, {'pending_provisioning': True, 'user_id': '@referral-bot:' + domain})
    if not preview and os.geteuid() == 0:
        os.chown(socket, 0, 10001)
        os.chown(root / 'data/referral-bot', 10002, 10001)
        os.chown(config, 10002, 10001)
    return client, policy


def services():
    common = {'restart': 'unless-stopped', 'read_only': True, 'cap_drop': ['ALL'],
              'security_opt': ['no-new-privileges:true'], 'pids_limit': 64,
              'tmpfs': ['/tmp:size=8m'],
              'logging': {'driver': 'json-file', 'options': {'max-size': '3m', 'max-file': '2'}}}
    return {
        'referral-control': {**common, 'image': IMAGES['referral-control'], 'user': '0:10001',
            'mem_limit': '64m', 'memswap_limit': '128m',
            'volumes': ['./data/referral-control:/config'],
            'healthcheck': {'test': ['CMD', 'python', '-c',
                "from common import bridge_call; bridge_call({'action':'servers'},path='/config/bot-run/control.sock',timeout=2)"],
                'interval': '20s', 'timeout': '5s', 'retries': 3}},
        'referral-bot': {**common, 'image': IMAGES['referral-bot'], 'user': '10002:10001',
            'mem_limit': '160m', 'memswap_limit': '320m',
            'environment': {'NODE_OPTIONS': '--max-old-space-size=96'},
            'volumes': ['./config/referral-bot.json:/config/referral.json:ro',
                        './data/referral-control/bot-run:/run/control:ro', './data/referral-bot:/data'],
            'depends_on': {'referral-control': {'condition': 'service_healthy'}},
            'healthcheck': {'test': ['CMD', 'node', '-e',
                "fetch('http://127.0.0.1:8080/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"],
                'interval': '30s', 'timeout': '5s', 'retries': 3}},
    }


def prepare_images(root, run, emit):
    root = Path(root)
    bases = json.loads((root / 'versions.json').read_text())['build_bases']
    lock_path = root / 'referral-images.lock.json'
    previous = json.loads(lock_path.read_text()) if lock_path.exists() else {}
    locks = {}
    for role, tag in IMAGES.items():
        folder = root / role
        if (folder / 'Dockerfile').read_text().splitlines()[0] != 'FROM ' + bases[role]:
            raise RuntimeError('Referral Docker base differs from the pinned image lock')
        source = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(folder.iterdir()) if p.is_file()}
        saved = previous.get(role, {})
        try:
            image_id = run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']).strip()
        except RuntimeError:
            image_id = ''
        reused = bool(image_id and saved.get('image_id') == image_id and
                      saved.get('base') == bases[role] and saved.get('source_sha256') == source)
        if not reused:
            run(['docker', 'build', '-t', tag, str(folder)])
            image_id = run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']).strip()
        locks[role] = {'image_id': image_id, 'base': bases[role], 'source_sha256': source}
        emit('referral_image_ready', role=role, image_id=image_id, reused=reused)
    private(lock_path, locks)
    import yaml
    path = root / 'compose.yaml'
    compose = yaml.safe_load(path.read_text())
    for role, lock in locks.items():
        compose['services'][role]['image'] = lock['image_id']
    path.write_text(yaml.safe_dump(compose, sort_keys=False))


def provision(root, dc, metadata):
    root = Path(root)
    validate_saved(root, metadata)
    # The admin credential stays inside the controller. Capture the one private
    # provisioning response, never print it or put it in a stage journal.
    result = json.loads(dc('exec', '-T', 'referral-control', 'python', 'provision.py'))
    config = result['config']
    if config['user_id'] != '@referral-bot:' + metadata['domain'] or config['device_id'] != DEVICE_ID:
        raise RuntimeError('Referral provisioning identity mismatch')
    private(root / 'config/referral-bot.json', config)
    os.chown(root / 'config/referral-bot.json', 10002, 10001)
    private(root / 'secrets/referral-session.json', result['metadata'])
    metadata['referral_provisioned'] = True


def note_ready(root, metadata):
    root = Path(root)
    for name in ['data/referral-bot/cross-signing.json', 'data/referral-bot/state.json']:
        if not (root / name).is_file():
            raise RuntimeError('Referral encryption initialization incomplete')
    metadata['referral_crypto_initialized'] = True
    validate_saved(root, metadata)


def verify_archive(archive):
    with tarfile.open(archive,'r:gz') as tar:
        metadata=json.load(tar.extractfile('installation.json'))
        if not metadata.get('referral_provisioned'):
            return
        required=['config/referral-bot.json','secrets/referral-session.json','data/referral-control/session.json',
                  'data/referral-control/data/bot-control.sqlite3']
        if metadata.get('referral_crypto_initialized'):
            required += ['data/referral-bot/cross-signing.json','data/referral-bot/state.json']
        for name in required:
            member=tar.getmember(name)
            if not member.isfile():
                raise RuntimeError('Missing referral backup file')
        config=json.load(tar.extractfile('config/referral-bot.json'))
        session=json.load(tar.extractfile('data/referral-control/session.json'))
        if any(config[k]!=session['config'][k] for k in ['access_token','user_id','device_id']):
            raise RuntimeError('Referral backup session mismatch')
        with tempfile.TemporaryDirectory(prefix='matrix-referral-restore-') as tmp:
            directory=Path(tmp)
            for member in tar.getmembers():
                if not member.name.startswith(('data/referral-bot/crypto/','data/referral-control/data/')):
                    continue
                if member.isdir():
                    continue
                if not member.isfile() or Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                    raise RuntimeError('Unsafe referral database archive member')
                target=directory/member.name
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
                target.chmod(0o600)
            count=0
            for path in directory.rglob('*'):
                if not path.is_file() or path.name.endswith(('-wal','-shm')):
                    continue
                with path.open('rb') as handle:
                    signature=handle.read(16)
                if signature!=b'SQLite format 3\x00':
                    continue
                with sqlite3.connect(path) as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                        raise RuntimeError('Referral backup database integrity failed')
                count+=1
            if count < (2 if metadata.get('referral_crypto_initialized') else 1):
                raise RuntimeError('Referral backup databases missing')
