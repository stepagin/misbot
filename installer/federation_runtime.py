"""Lifecycle hooks for the narrow host controller and a non-admin Matrix account."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys

import configuration
import federation
import state_store

IMAGE='matrix-stack/federation-bot:1.0.0'
DEVICE='MATRIX-FEDERATION-01'


def identity(root, metadata):
    root=Path(root);path=root/'config/admin-panel-identity.json'
    if path.exists():
        saved=configuration.read_private_json(path)
    else:
        cfg=root/'config/federation-bot.json'
        session=root/'data/referral-control/federation-session.json'
        configured=json.loads(cfg.read_text()) if cfg.exists() else {}
        existing=json.loads(session.read_text())['config'] if session.exists() else configured
        admins=configured.get('administrator_mxids') or existing.get('administrator_mxids') or ['@matrix-admin:'+metadata['domain']]
        saved={'user_id':existing.get('user_id','@admin-panel:'+metadata['domain']), 'administrator_mxid':admins[0]}
        state_store.write_json(path,saved)
    for name in ('user_id','administrator_mxid'):
        if not re.fullmatch(r'@[a-z0-9][a-z0-9._=-]{0,63}:'+re.escape(metadata['domain']),saved[name]):
            raise RuntimeError('Invalid local admin panel identity')
    return saved


def configure(root, metadata, preview=False):
    root=Path(root);federation.setup(root,metadata,preview)
    folder=root/'data/federation-bot';folder.mkdir(mode=0o700,exist_ok=True)
    path=root/'config/federation-bot.json'
    saved=identity(root,metadata)
    if not path.exists():state_store.write_json(path,{'pending_provisioning':True,'user_id':saved['user_id']})
    welcome=root/'config/welcome-bot.json'
    if not welcome.exists():
        if metadata.get('welcome_provisioned'):raise RuntimeError('Welcome session missing; restore saved identity')
        state_store.write_json(welcome,{'pending_provisioning':True,'user_id':'@server-guide:'+metadata['domain']})
    __import__('admin_panel').Panel(root)
    if not preview and os.geteuid()==0:
        os.chown(folder,10003,10003);os.chown(path,10003,10003);os.chown(welcome,10003,10003)


def service():
    return {'image':IMAGE,'user':'10003:10003','restart':'unless-stopped','read_only':True,
        'cap_drop':['ALL'],'security_opt':['no-new-privileges:true'],'pids_limit':64,
        'mem_limit':'320m','memswap_limit':'640m','tmpfs':['/tmp:size=8m'],
        'environment':{'NODE_OPTIONS':'--max-old-space-size=192'},
        'volumes':['./config/federation-bot.json:/config/federation.json:ro',
                   './config/welcome-bot.json:/config/welcome.json:ro',
                   './run/federation:/run/control:ro','./data/federation-bot:/data'],
        'logging':{'driver':'json-file','options':{'max-size':'3m','max-file':'2'}},
        'healthcheck':{'test':['CMD','node','-e',"fetch('http://127.0.0.1:8080/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"],
                       'interval':'30s','timeout':'5s','retries':3}}


def prepare_image(root, run):
    root=Path(root);source=root/'federation-bot'
    import yaml
    bases=json.loads((root/'versions.json').read_text())['build_bases']
    if (source/'Dockerfile').read_text().splitlines()[0]!='FROM '+bases['referral-bot']:
        raise RuntimeError('Federation bot base differs from pinned SDK runtime')
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir() if p.is_file()}
    path=root/'federation-images.lock.json';old=configuration.read_private_json(path) if path.exists() else {}
    try: image=run(['docker','image','inspect',IMAGE,'--format','{{.Id}}']).strip()
    except RuntimeError:image=''
    if not image or old.get('image_id')!=image or old.get('source_sha256')!=hashes:
        run(['docker','build','-t',IMAGE,str(source)])
        image=run(['docker','image','inspect',IMAGE,'--format','{{.Id}}']).strip()
    state_store.write_json(path,{'image_id':image,'source_sha256':hashes,'base':bases['referral-bot']})
    compose=yaml.safe_load((root/'compose.yaml').read_text());compose['services']['federation-bot']['image']=image
    state_store.write_text(root/'compose.yaml',yaml.safe_dump(compose,sort_keys=False))


def provision(root, dc, metadata):
    root=Path(root);saved=identity(root,metadata)
    if metadata.get('federation_provisioned'):
        validate(root,metadata)
        cfg=json.loads((root/'config/federation-bot.json').read_text())
        cfg['administrator_mxids']=[saved['administrator_mxid']]
        state_store.write_json(root/'config/federation-bot.json',cfg)
    else:
        result=json.loads(dc('exec','-T','referral-control','python','provision.py','--federation','--username',saved['user_id'][1:].split(':')[0]))
        cfg=result['config']
        if cfg['user_id']!=saved['user_id'] or cfg['device_id']!=DEVICE:
            raise RuntimeError('Federation bot identity mismatch; keep saved session')
        cfg['administrator_mxids']=[saved['administrator_mxid']]
        state_store.write_json(root/'config/federation-bot.json',cfg)
        os.chown(root/'config/federation-bot.json',10003,10003)
        state_store.write_json(root/'secrets/federation-session.json',result['metadata'])
        metadata['federation_provisioned']=True
    if not metadata.get('welcome_provisioned'):
        result=json.loads(dc('exec','-T','referral-control','python','provision.py','--welcome'))
        if result['config']['user_id']!='@server-guide:'+metadata['domain'] or result['config']['device_id']!='MATRIX-WELCOME-01':
            raise RuntimeError('Welcome bot identity mismatch; keep saved session')
        state_store.write_json(root/'config/welcome-bot.json',result['config'])
        os.chown(root/'config/welcome-bot.json',10003,10003)
        state_store.write_json(root/'secrets/welcome-session.json',result['metadata'])
        metadata['welcome_provisioned']=True


def validate(root, metadata):
    root=Path(root)
    if metadata.get('federation_seeded'):
        for name in ('data/federation/identity.pem','data/federation/state.json'):
            configuration.read_private(root/name)
        saved=configuration.read_private_json(root/'data/federation/state.json')
        if saved['server_name']!=metadata['domain']:raise RuntimeError('Foreign federation state')
    if metadata.get('federation_provisioned'):
        cfg=json.loads((root/'config/federation-bot.json').read_text())
        journal=configuration.read_private_json(root/'data/referral-control/federation-session.json')['config']
        if any(cfg.get(k)!=journal.get(k) for k in ('user_id','device_id','access_token')):raise RuntimeError('Federation bot session differs; restore checkpoint')
        if cfg['device_id']!=DEVICE or cfg['user_id']!=identity(root,metadata)['user_id']:raise RuntimeError('Foreign federation bot')
    if metadata.get('welcome_provisioned'):
        cfg=json.loads((root/'config/welcome-bot.json').read_text())
        saved=configuration.read_private_json(root/'data/referral-control/welcome-session.json')['config']
        if any(cfg.get(k)!=saved.get(k) for k in ('user_id','device_id','access_token')) or cfg['user_id']!='@server-guide:'+metadata['domain']:
            raise RuntimeError('Welcome bot session differs; restore checkpoint')
    if metadata.get('welcome_crypto_initialized'):
        for name in ('state.json','cross-signing.json'):
            if not (root/'data/federation-bot/welcome'/name).is_file():raise RuntimeError('Welcome crypto missing; restore checkpoint')
        if not any(p.is_file() and p.open('rb').read(16)==b'SQLite format 3\x00' for p in (root/'data/federation-bot/welcome/crypto').rglob('*') if not p.name.endswith(('-wal','-shm'))):
            raise RuntimeError('Welcome crypto database missing; restore checkpoint')
    if metadata.get('federation_crypto_initialized'):
        for name in ('state.json','cross-signing.json'):
            if not (root/'data/federation-bot'/name).is_file():raise RuntimeError('Federation crypto state missing; restore backup')
        if not any(p.is_file() and p.open('rb').read(16)==b'SQLite format 3\x00' for p in (root/'data/federation-bot/crypto').rglob('*') if not p.name.endswith(('-wal','-shm'))):
            raise RuntimeError('Federation crypto store missing; restore backup')


def verify_archive(archive):
    import tarfile
    import sqlite3
    import tempfile
    with tarfile.open(archive,'r:gz') as tar:
        metadata=json.load(tar.extractfile('installation.json'))
        if not metadata.get('federation_seeded'):return
        required=['data/federation/identity.pem','data/federation/state.json']
        if metadata.get('federation_provisioned'):required+=['config/federation-bot.json','secrets/federation-session.json','data/referral-control/federation-session.json']
        if metadata.get('federation_crypto_initialized'):required+=['data/federation-bot/state.json','data/federation-bot/cross-signing.json']
        if metadata.get('welcome_provisioned'):required+=['config/welcome-bot.json','secrets/welcome-session.json','data/referral-control/welcome-session.json']
        if metadata.get('welcome_crypto_initialized'):required+=['data/federation-bot/welcome/state.json','data/federation-bot/welcome/cross-signing.json']
        for name in required:
            if not tar.getmember(name).isfile():raise RuntimeError('Federation backup is incomplete')
        state=json.load(tar.extractfile('data/federation/state.json'))
        if state['server_name']!=metadata['domain']:raise RuntimeError('Foreign federation backup')
        if metadata.get('federation_provisioned'):
            cfg=json.load(tar.extractfile('config/federation-bot.json'));saved=json.load(tar.extractfile('data/referral-control/federation-session.json'))['config']
            if any(cfg[k]!=saved[k] for k in ('user_id','device_id','access_token')):raise RuntimeError('Federation backup session mismatch')
        if metadata.get('welcome_provisioned'):
            cfg=json.load(tar.extractfile('config/welcome-bot.json'));saved=json.load(tar.extractfile('data/referral-control/welcome-session.json'))['config']
            if any(cfg[k]!=saved[k] for k in ('user_id','device_id','access_token')) or cfg['device_id']!='MATRIX-WELCOME-01':raise RuntimeError('Welcome archive session mismatch')
        if metadata.get('federation_crypto_initialized'):
            count=0
            with tempfile.TemporaryDirectory(prefix='misbot-federation-crypto-restore-') as folder:
                for member in tar.getmembers():
                    if not member.name.startswith('data/federation-bot/crypto/'):continue
                    relative=Path(member.name).relative_to('data/federation-bot/crypto')
                    if relative.is_absolute() or '..' in relative.parts or member.issym() or member.islnk():raise RuntimeError('Unsafe federation crypto backup path')
                    if not member.isfile():continue
                    path=Path(folder)/relative;path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    path.write_bytes(tar.extractfile(member).read());path.chmod(0o600)
                for path in Path(folder).rglob('*'):
                    if not path.is_file() or path.name.endswith(('-wal','-shm')) or path.open('rb').read(16)!=b'SQLite format 3\x00':continue
                    with sqlite3.connect(path) as database:
                        if database.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Federation restored crypto database integrity failed')
                    count+=1
            if not count:raise RuntimeError('Federation backup crypto missing')

    if metadata.get('welcome_crypto_initialized'):
        with tarfile.open(archive,'r:gz') as tar, tempfile.TemporaryDirectory(prefix='misbot-welcome-crypto-restore-') as folder:
            count=0
            for member in tar.getmembers():
                if not member.name.startswith('data/federation-bot/welcome/crypto/'):continue
                relative=Path(member.name).relative_to('data/federation-bot/welcome/crypto')
                if '..' in relative.parts or relative.is_absolute() or member.issym() or member.islnk():raise RuntimeError('Unsafe welcome crypto archive')
                if not member.isfile():continue
                path=Path(folder)/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(tar.extractfile(member).read())
            for path in Path(folder).rglob('*'):
                if not path.is_file() or path.name.endswith(('-wal','-shm')) or path.open('rb').read(16)!=b'SQLite format 3\x00':continue
                with sqlite3.connect(path) as database:
                    if database.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Welcome restored crypto integrity failed')
                count+=1
            if not count:raise RuntimeError('Welcome crypto archive incomplete')


def unit_name(root):
    domain=json.loads((Path(root)/'installation.json').read_text())['domain']
    return 'misbot-federation-'+hashlib.sha256(domain.encode()).hexdigest()[:16]+'.service'


def controller(root, run, start=True, system=None, remove=False):
    root=Path(root)
    if any(ord(c)<32 for c in str(root)):raise RuntimeError('Controller directory cannot contain control characters')
    path=Path(system or '/etc/systemd/system')/unit_name(root)
    # systemd argument syntax: quoted strings, no shell, double percent specifiers.
    def quote(v):return '"'+str(v).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
    marker='# Misbot federation controller '+str(root)+'\n'
    if path.exists() and not path.read_text().startswith(marker):raise RuntimeError('Another installation owns federation controller; refusing replacement')
    if not start:
        if path.exists():
            run(['systemctl','disable','--now',path.name])
            if remove:
                path.unlink();run(['systemctl','daemon-reload'])
        return
    metadata=json.loads((root/'installation.json').read_text())
    if metadata.get('restore_pending') or metadata.get('migration_frozen'):raise RuntimeError('Staged/frozen installation cannot start federation controller')
    state_store.write_text(path,marker+'''[Unit]
Description=Misbot narrow federation controller
After=docker.service network-online.target
[Service]
Type=simple
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
ExecStart=/usr/bin/python3 '''+quote(root/'federation_service.py')+' --directory '+quote(root)+'''
[Install]
WantedBy=multi-user.target
''',mode=0o644)
    run(['systemctl','daemon-reload']);run(['systemctl','enable',path.name]);run(['systemctl','restart',path.name])


def matrix_operation(root, action, argument):
    import lifecycle
    # Only fixed bot membership operations, no user impersonation or arbitrary paths.
    cfg=json.loads((Path(root)/'config/federation-bot.json').read_text())
    payload={'session':cfg,'action':action,'argument':argument}
    result=json.loads(lifecycle.dc('exec','-T','synapse','python','-c',
        (Path(root)/'federation_matrix_api.py').read_text(),stdin=json.dumps(payload)))
    if not result.get('ok'):raise federation.protocol.ProtocolError(result.get('code','matrix_unavailable'))
    return result['result']
