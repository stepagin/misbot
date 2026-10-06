"""Resumable local-to-S3 copy; never delete local media or replace remote data."""
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import state_store
import s3_config

FIELDS={'S3_BUCKET','S3_REGION','S3_ENDPOINT_URL','S3_ACCESS_KEY_ID',
        'S3_SECRET_ACCESS_KEY','S3_PREFIX'}


def identity(values):
    return {key:values.get(key,'') for key in ('S3_BUCKET','S3_REGION','S3_ENDPOINT_URL','S3_PREFIX')}


def client(values):
    values=s3_config.validate({key:value for key,value in values.items() if key in FIELDS})
    import boto3
    from botocore.config import Config
    if not all(values.get(key) for key in FIELDS-{'S3_ENDPOINT_URL'}):
        raise RuntimeError('S3 requires bucket, region, both credentials and a dedicated prefix')
    endpoint=values.get('S3_ENDPOINT_URL','')
    if endpoint:
        parsed=urlsplit(endpoint)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
            raise RuntimeError('S3 endpoint must be a verified HTTPS origin without credentials or a path')
    prefix=values['S3_PREFIX']
    if not prefix.endswith('/') or prefix.startswith('/') or '..' in prefix.split('/'):
        raise RuntimeError('Use a dedicated relative S3 prefix ending in /')
    return boto3.client('s3',endpoint_url=endpoint or None,region_name=values['S3_REGION'],
        aws_access_key_id=values['S3_ACCESS_KEY_ID'],aws_secret_access_key=values['S3_SECRET_ACCESS_KEY'],
        config=Config(signature_version='s3v4',connect_timeout=8,read_timeout=15,
                      retries={'max_attempts':1},s3={'addressing_style':'path'}))


def stream_digest(stream):
    digest=hashlib.sha256()
    while True:
        chunk=stream.read(1024*1024)
        if not chunk:break
        digest.update(chunk)
    return digest.hexdigest()


def remote_digest(service,bucket,key):
    try:
        response=service.get_object(Bucket=bucket,Key=key)
    except Exception as error:
        details=getattr(error,'response',{})
        if details.get('Error',{}).get('Code') in ('NoSuchKey','404','NotFound'):
            return None
        raise
    stream=response['Body']
    try:return stream_digest(stream)
    finally:stream.close()


def transfer(directory,values,checkpoint,service=None):
    directory=Path(directory).absolute()
    if directory.resolve(strict=True)!=directory:
        raise RuntimeError('Media directory must not contain symlinks; no outside files uploaded')
    checkpoint=Path(checkpoint)
    service=service or client(values)
    service.head_bucket(Bucket=values['S3_BUCKET'])
    saved=json.loads(checkpoint.read_text()) if checkpoint.exists() else {
        'format':1,'identity':identity(values),'files':{}}
    if saved.get('format')!=1 or saved.get('identity')!=identity(values):
        raise RuntimeError('This transfer checkpoint belongs to another S3 destination; keep it and use a separate checkpoint')
    checkpoint.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    paths=sorted(directory.rglob('*'))
    if any(path.is_symlink() for path in paths):
        raise RuntimeError('Unexpected media symlink; local files and remote objects unchanged')
    copied=0
    verified=0
    for path in paths:
        if not path.is_file():continue
        relative=path.relative_to(directory).as_posix()
        with path.open('rb') as source:digest=stream_digest(source)
        key=values['S3_PREFIX']+relative
        found=remote_digest(service,values['S3_BUCKET'],key)
        if found is not None and found!=digest:
            raise RuntimeError('S3 already contains different data at a media key; nothing overwritten. Choose an empty dedicated prefix')
        previous=saved['files'].get(relative,{})
        if found is None:
            # Persist intent before PUT: a lost response must be recoverable.
            saved['files'][relative]={'sha256':digest,'status':'uploading','created_by_transfer':True}
            state_store.write_json(checkpoint,saved)
            with path.open('rb') as source:
                service.put_object(Bucket=values['S3_BUCKET'],Key=key,Body=source,
                    Metadata={'sha256':digest})
            copied+=1
        if remote_digest(service,values['S3_BUCKET'],key)!=digest:
            raise RuntimeError('S3 media checksum verification failed; keep local files and repeat the same transfer')
        with path.open('rb') as source:
            if stream_digest(source)!=digest:
                raise RuntimeError('Local media changed during copying; stop Synapse and repeat the transfer')
        saved['files'][relative]={'sha256':digest,'status':'verified',
            'created_by_transfer':found is None or previous.get('created_by_transfer',False)}
        state_store.write_json(checkpoint,saved)
        verified+=1
    return {'files_verified':verified,'files_uploaded':copied,'local_files_retained':True}
