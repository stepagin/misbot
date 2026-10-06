"""Replace live-media S3 credentials without changing its destination or data."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import uuid

import lifecycle
import media
import media_transfer
import preflight
import state_store
import s3_config
import vault_cache

JOURNAL = 's3-credentials-transition.json'
KEYS = ('S3_ACCESS_KEY_ID', 'S3_SECRET_ACCESS_KEY')
PROBE = b'FICTIONAL_INSTALLER_CREDENTIAL_CHECK'


def private_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 32768):
            raise RuntimeError('S3 checkpoint must be an owned private regular file, without links, at most 32 KiB')
        body = stream.read(32769)
        if len(body) > 32768:
            raise RuntimeError('S3 checkpoint is too large; preserve recovery files')
    return body


def private_json(path):
    return json.loads(private_bytes(path))


def private_profile(path):
    return s3_config.read_profile(path)


def current(root):
    metadata = lifecycle.state()
    values = preflight.settings(root)
    if (not metadata.get('databases_initialized') or not metadata.get('storage_committed')
            or not metadata.get('network_committed') or metadata.get('media_mode') != 's3'
            or any(metadata.get(k) for k in ('restore_pending', 'migration_frozen',
                'media_transition_pending', 'credential_transition_pending'))
            or (root / 'secrets/user-password-pending.json').exists()):
        raise RuntimeError('An active S3 installation is required; finish existing recovery/migration first')
    if media_transfer.identity(values) != metadata.get('s3_identity'):
        raise RuntimeError('S3 identity differs from the installed destination; restore its original settings before changing credentials')
    return {key: values.get(key, '') for key in media_transfer.FIELDS}


def validate(root, values):
    if (set(values) != media_transfer.FIELDS or any(not isinstance(v, str) for v in values.values())
            or media_transfer.identity(values) != media_transfer.identity(current(root))):
        raise RuntimeError('Use the same S3 bucket, region, HTTPS endpoint and prefix; this operation changes only the two credentials')
    return media_transfer.client(values)


def record(root):
    saved = private_json(root / JOURNAL)
    if (not isinstance(saved, dict) or saved.get('format') != 1
            or saved.get('server_name') != lifecycle.state()['domain']
            or saved.get('destination') != media_transfer.identity(current(root))
            or not isinstance(saved.get('stage'), str)
            or not re.fullmatch(r'stages/s3-credentials/[0-9a-f]{32}', saved['stage'])
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', saved.get('image_id', ''))
            or type(saved.get('pid')) is not int or saved['pid'] <= 0
            or not isinstance(saved.get('started'), str)):
        raise RuntimeError('S3 checkpoint does not match this installation; retain all settings, backups and recovery files')
    folder = root / saved['stage']
    if folder.resolve() != folder or not folder.is_dir():
        raise RuntimeError('Unsafe S3 recovery directory; preserve the checkpoint')
    return saved


def claim(root, saved):
    saved.update(pid=os.getpid(), started=state_store.process_identity(os.getpid())[1])
    state_store.write_json(root / JOURNAL, saved)
    os.environ['MATRIX_INSTALLER_S3_CREDENTIAL_STAGE'] = saved['stage']


def read_access(service, values):
    service.head_bucket(Bucket=values['S3_BUCKET'])
    reply = service.list_objects_v2(Bucket=values['S3_BUCKET'], Prefix=values['S3_PREFIX'], MaxKeys=1)
    for item in reply.get('Contents', []):
        if not item['Key'].startswith(values['S3_PREFIX']):
            raise RuntimeError('S3 returned an object outside the prefix')
        response = service.get_object(Bucket=values['S3_BUCKET'], Key=item['Key'])
        body = response['Body']
        try:
            while body.read(1024 * 1024):
                pass
        finally:
            body.close()


def probe_write(root, folder, service, values):
    path = folder / 'probe.json'
    if path.exists():
        probe = private_json(path)
    else:
        probe = {'format': 1, 'server_name': lifecycle.state()['domain'],
                 'destination': media_transfer.identity(values),
                 'object_key': values['S3_PREFIX'] + '_installer_credential_checks/' + uuid.uuid4().hex,
                 'deleted': False, 'verified_write': False, 'version_id': None}
        state_store.write_json(path, probe)  # Ownership before a possibly lost PUT response.
    validate_probe(root, probe, values)
    if probe['deleted'] and probe['verified_write']:
        return
    probe['deleted'] = False
    state_store.write_json(path, probe)
    try:
        created = service.put_object(Bucket=values['S3_BUCKET'], Key=probe['object_key'], Body=PROBE)
        probe['version_id'] = created.get('VersionId')
        state_store.write_json(path, probe)
        response = service.get_object(Bucket=values['S3_BUCKET'], Key=probe['object_key'])
        body = response['Body']
        try:
            if body.read(len(PROBE) + 1) != PROBE:
                raise RuntimeError('S3 credential write/read verification failed')
        finally:
            body.close()
        probe['verified_write'] = True
        state_store.write_json(path, probe)
    finally:
        try:
            version = {'VersionId': probe['version_id']} if probe['version_id'] else {}
            service.delete_object(Bucket=values['S3_BUCKET'], Key=probe['object_key'], **version)
            probe['deleted'] = True
            state_store.write_json(path, probe)
        except Exception:
            lifecycle.emit('s3_credential_probe_cleanup', passed=False, credentials_omitted=True,
                next_action='A private test object may remain. Keep the private probe catalog; after fixing DeleteObject permission run install.sh s3-credentials-cleanup')


def validate_probe(root, probe, values):
    prefix = values['S3_PREFIX'] + '_installer_credential_checks/'
    if (not isinstance(probe, dict) or probe.get('format') != 1
            or probe.get('server_name') != lifecycle.state()['domain']
            or probe.get('destination') != media_transfer.identity(values)
            or not isinstance(probe.get('object_key'), str)
            or not probe['object_key'].startswith(prefix)
            or not re.fullmatch(r'[0-9a-f]{32}', probe['object_key'][len(prefix):])
            or type(probe.get('deleted')) is not bool
            or type(probe.get('verified_write')) is not bool
            or not (probe.get('version_id') is None or isinstance(probe['version_id'], str))):
        raise RuntimeError('S3 test object catalog is not owned by this installation; no remote object deleted')


def finish(root, saved):
    folder = root / saved['stage']
    values = private_json(folder / 'replacement.json')
    service = validate(root, values)
    read_access(service, values)
    probe_write(root, folder, service, values)
    lifecycle.dc('stop', '-t', '30', 'synapse')
    if any(row['Service'] == 'synapse' and row['State'] in ('running', 'restarting', 'paused')
           for row in lifecycle.service_rows()):
        raise RuntimeError('Synapse did not stop; keep the S3 recovery checkpoint')
    vault_cache.mark_dirty(root)
    media.replace_settings(root, {key: values[key] for key in KEYS})
    lifecycle.generate()
    # Use the recorded existing image: credential changes never update components.
    override = folder / 'image.yaml'
    state_store.write_text(override, 'services:\n  synapse:\n    image: ' + saved['image_id'] + '\n')
    # Recover can follow a full `down`: bring back the existing stack as well
    # as Synapse, using this recorded image and the unchanged component lock.
    lifecycle.run(lifecycle.COMPOSE + ['-f', str(override), 'up', '-d'])
    writers = ['synapse', 'mas', 'jwt', 'referral-bot', 'referral-control',
               'livekit', 'tls-router', 'caddy', 'admin', 'web']
    if not lifecycle.external_database():
        writers.append('postgres')
    lifecycle.wait_writers(writers)
    lifecycle.certificate_watch()
    lifecycle.check()
    (root / JOURNAL).unlink()
    vault_cache.sync_optional(root, lifecycle.emit)
    lifecycle.emit('s3_credentials_changed', passed=True, changed=True, destination_retained=True, credentials_omitted=True,
        next_action='Run install.sh check; verify old/new attachments before revoking the old provider key. Backup S3 credentials use their own private env file')


def rotate(root, path):
    if (root / JOURNAL).exists() or (root / JOURNAL).is_symlink():
        raise RuntimeError('S3 credentials change is pending; run install.sh s3-credentials-recover')
    old = current(root)
    values = private_profile(path)
    values.setdefault('S3_ENDPOINT_URL', '')
    service = validate(root, values)
    read_access(service, values)  # Reject wrong credentials before backup or local changes.
    if all(values[key] == old[key] for key in KEYS):
        lifecycle.emit('s3_credentials_changed', passed=True, changed=False, credentials_omitted=True,
            next_action='The supplied key pair is already configured; no services restarted. Use a new valid provider key pair for a rotation')
        return
    lifecycle.check()
    identifiers = lifecycle.dc('ps', '-q', 'synapse').split()
    if len(identifiers) != 1:
        raise RuntimeError('Require one running owned Synapse before changing credentials')
    image_id = lifecycle.run(['docker', 'inspect', '--format', '{{.Image}}', identifiers[0]]).strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        raise RuntimeError('Cannot identify the installed Synapse image; no credentials changed')
    lifecycle.backup()
    lifecycle.restore_check()
    parent = root / 'stages/s3-credentials'
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.resolve() != parent:
        raise RuntimeError('Unsafe S3 checkpoint directory; no credentials changed')
    parent.chmod(0o700)
    folder = parent / uuid.uuid4().hex
    folder.mkdir(mode=0o700)
    state_store.write_text(folder / 'original-settings.env', (root / 'settings.env').read_text())
    state_store.write_json(folder / 'replacement.json', values)
    saved = {'format': 1, 'stage': str(folder.relative_to(root)), 'server_name': lifecycle.state()['domain'],
             'destination': media_transfer.identity(old), 'image_id': image_id}
    claim(root, saved)
    finish(root, saved)


def recover(root):
    if not (root / JOURNAL).exists() and not (root / JOURNAL).is_symlink():
        lifecycle.emit('s3_credential_recovery', pending=False, changed=False)
        return
    saved = record(root)
    claim(root, saved)
    finish(root, saved)


def cleanup(root):
    values = current(root)
    service = media_transfer.client(values)
    count = 0
    parent = root / 'stages/s3-credentials'
    if parent.resolve() != parent:
        raise RuntimeError('Unsafe S3 checkpoint parent; no remote object deleted')
    for folder in sorted(parent.glob('*')):
        if folder.is_symlink() or folder.resolve() != folder or not re.fullmatch(r'[0-9a-f]{32}', folder.name):
            raise RuntimeError('Unowned S3 checkpoint directory; no additional deletion attempted')
        path = folder / 'probe.json'
        if not path.exists():
            continue
        probe = private_json(path)
        validate_probe(root, probe, values)
        if not probe['deleted']:
            version = {'VersionId': probe['version_id']} if probe['version_id'] else {}
            service.delete_object(Bucket=values['S3_BUCKET'], Key=probe['object_key'], **version)
            probe['deleted'] = True
            state_store.write_json(path, probe)
            count += 1
    lifecycle.emit('s3_credential_probe_cleanup', passed=True, deleted=count, credentials_omitted=True)


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--settings', type=Path)
    parser.add_argument('action', choices=('rotate', 'recover', 'cleanup'))
    return parser.parse_args()


def main(args=None):
    args = args or parse_arguments()
    state_store.require_root()
    root = args.directory.resolve(strict=True)
    lifecycle.configure(root, 's3-credentials-' + args.action)
    def interrupted(signum, frame):
        raise state_store.OperationInterrupted('S3 credential operation interrupted')
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    with state_store.operation_guard(root):
        if args.action == 'recover' and (root / JOURNAL).exists():
            claim(root, record(root))
        state_store.assert_update_allowed(root)
        lifecycle.ownership_check()
        if args.action == 'rotate':
            if not args.settings:
                raise RuntimeError('Specify the private S3 env file; see credentials.md')
            rotate(root, args.settings)
        else:
            (recover if args.action == 'recover' else cleanup)(root)


def run_cli():
    args = None
    try:
        args = parse_arguments()
        main(args)
    except Exception:
        root = args.directory.absolute() if args else Path('/opt/matrix-stack')
        command = 'sudo bash ' + shlex.quote(str(root / 'install.sh'))
        pending = (root / JOURNAL).exists() or (root / JOURNAL).is_symlink()
        if pending:
            next_action = command + ' s3-credentials-recover'
        elif args and args.action == 'cleanup':
            next_action = command + ' s3-credentials-cleanup'
        elif args and args.action == 'rotate' and args.settings:
            next_action = command + ' s3-credentials ' + shlex.quote(str(args.settings.absolute()))
        else:
            next_action = command + ' status'
        print(json.dumps({'check': 's3_credentials', 'passed': False,
            'next_action': next_action,
            'message': 'S3 credential operation did not complete. Keep original settings, data, verified backup and private checkpoints. Correct the private S3 input or provider permissions/connectivity, then run: ' + next_action + '. Do not change the bucket/prefix, delete checkpoints or start Synapse manually.'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(run_cli())
