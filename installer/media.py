#!/usr/bin/env python3
"""Explicit media-storage operations; retain local originals during S3 adoption."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import lifecycle
import media_transfer
import preflight
import s3_config
import state_store


def replace_settings(root,values):
    path=root/'settings.env'
    found=set()
    lines=path.read_text().splitlines()
    for index,line in enumerate(lines):
        if '=' not in line or line.lstrip().startswith('#'):continue
        key=line.split('=',1)[0].strip()
        if key in values:
            lines[index]=key+'='+values[key]
            found.add(key)
    lines.extend(key+'='+value for key,value in values.items() if key not in found)
    state_store.write_text(path,'\n'.join(lines)+'\n')


def local(root):
    metadata=lifecycle.state()
    values=preflight.settings(root)
    if metadata.get('storage_committed'):
        raise RuntimeError('Local fallback for an existing S3 installation requires verification of every local original; this operation currently supports only an uncommitted installation')
    if metadata.get('restore_pending') or metadata.get('migration_frozen'):
        raise RuntimeError('Storage changes are disabled on staged or frozen migration machines')
    replace_settings(root,{'MEDIA_MODE':'local'})
    lifecycle.emit('media_local_selected',passed=True,data_preserved=True,
        next_action='Run install.sh up in the same directory. Later: install.sh media-s3 PRIVATE_S3_ENV')


def s3(root,settings_path):
    metadata=lifecycle.state()
    if metadata.get('restore_pending') or metadata.get('migration_frozen'):
        raise RuntimeError('Storage changes are disabled on staged or frozen migration machines')
    if metadata.get('media_mode','local')!='local':
        raise RuntimeError('This operation adopts S3 from local media; changing an existing S3 destination requires a separate migration')
    values=s3_config.read_profile(settings_path)
    if set(values)-media_transfer.FIELDS:
        raise RuntimeError('The private S3 file must contain only S3 parameter names from the template')
    try:
        service=media_transfer.client(values)
        service.head_bucket(Bucket=values['S3_BUCKET'])
    except Exception:
        raise RuntimeError('S3 connection failed; current local storage is unchanged. Correct the private S3 file and repeat install.sh media-s3 PRIVATE_S3_ENV') from None
    if not metadata.get('databases_initialized'):
        replace_settings(root,{**values,'MEDIA_MODE':'s3'})
        lifecycle.emit('media_s3_selected',passed=True,next_action='Run install.sh up in the same directory')
        return
    if metadata.get('media_transition_pending'):
        raise RuntimeError('Interrupted media transition detected; run install.sh media-recover in the same directory, then repeat media-s3 with the same private file')
    lifecycle.backup()
    lifecycle.restore_check()
    folder=root/'stages'/'media-transfers'
    folder.mkdir(mode=0o700,exist_ok=True)
    previous=(root/'settings.env').read_bytes()
    original=lifecycle.state()
    saved=folder/(time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:6])
    saved.mkdir(mode=0o700)
    (saved/'settings.env').write_bytes(previous)
    state_store.write_json(saved/'installation.json',original)
    metadata=dict(original,media_transition_pending=True,media_transition_dir=str(saved.relative_to(root)))
    state_store.write_json(root/'installation.json',metadata)
    try:
        lifecycle.dc('stop','-t','30','synapse')
        destination_hash=hashlib.sha256(json.dumps(media_transfer.identity(values),sort_keys=True).encode()).hexdigest()[:16]
        checkpoint=folder/('checkpoint-'+destination_hash+'.json')
        result=media_transfer.transfer(root/'data/synapse/media_store',values,checkpoint,service)
        replace_settings(root,{**values,'MEDIA_MODE':'s3'})
        metadata=dict(original,media_mode='s3',s3_prefix=values['S3_PREFIX'],
                      s3_identity=media_transfer.identity(values))
        state_store.write_json(root/'installation.json',metadata)
        lifecycle.up()
        lifecycle.emit('media_s3_connected',passed=True,**result,
            checkpoint=str(checkpoint),previous_settings=str(saved/'settings.env'))
    except BaseException:
        state_store.write_text(root/'settings.env',previous.decode())
        state_store.write_json(root/'installation.json',original)
        try:
            lifecycle.up()
        except Exception:
            lifecycle.emit('media_local_recovery',passed=False,
                next_action='Original settings restored. Correct the cause and retry install.sh up; retain local media')
        raise RuntimeError('S3 adoption failed; original local settings restored and local media retained. '
                           'Correct the resource and repeat media-s3 with the same file; inspect private checkpoint '+str(folder)) from None


def recover(root):
    metadata=lifecycle.state()
    if not metadata.get('media_transition_pending'):
        raise RuntimeError('No interrupted media transition exists; use install.sh up to resume a committed configuration')
    relative=Path(metadata.get('media_transition_dir',''))
    folder=root/relative
    if (relative.is_absolute() or '..' in relative.parts or
            relative.parts[:2]!=('stages','media-transfers') or folder.is_symlink()):
        raise RuntimeError('Unsafe media recovery checkpoint; inspect the private installation state')
    original=json.loads((folder/'installation.json').read_text())
    if original.get('domain')!=metadata.get('domain') or original.get('media_mode','local')!='local':
        raise RuntimeError('Media checkpoint does not match this installation; do not overwrite server identity')
    state_store.write_text(root/'settings.env',(folder/'settings.env').read_text())
    state_store.write_json(root/'installation.json',original)
    lifecycle.up()
    lifecycle.emit('media_local_recovery',passed=True,local_files_retained=True,
        next_action='Repeat media-s3 with the original private file when S3 is available')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,type=Path)
    parser.add_argument('--settings',type=Path)
    parser.add_argument('action',choices=('local','s3','recover'))
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('Run media operations as root')
    root=args.directory.resolve(strict=True)
    lifecycle.configure(root,'media-'+args.action)
    with state_store.operation_guard(root):
        state_store.assert_update_allowed(root)
        lifecycle.ownership_check()
        if args.action=='local':local(root)
        elif args.action=='recover':recover(root)
        elif args.settings:s3(root,args.settings)
        else:raise RuntimeError('Specify the private S3 settings file')


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'check':'media_storage','passed':False,
            'message':str(error) if isinstance(error,RuntimeError) else
            'Media operation failed; keep local files and private settings, then retry the same command'}))
        raise SystemExit(1)
