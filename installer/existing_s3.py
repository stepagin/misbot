"""S3 primitives for adopted installations; no credentials in diagnostics."""
import hashlib
import json
from pathlib import Path
import re
import stat
import uuid

import media_transfer
import s3_config
import s3_diagnostics


def separate(media, backup):
    same = all(media.get(k, '') == backup.get(k, '') for k in
               ('S3_BUCKET', 'S3_ENDPOINT_URL', 'S3_REGION'))
    if same and (media['S3_PREFIX'].startswith(backup['S3_PREFIX']) or
                 backup['S3_PREFIX'].startswith(media['S3_PREFIX'])):
        raise RuntimeError('Live media and backups require separate S3 prefixes')


def conditional(service, operation, **arguments):
    """Require exclusive publication with both current and distro SDKs."""
    event = 'before-sign.s3.' + operation
    fields = service.meta.service_model.operation_model(operation).input_shape.members
    def condition(request, **kwargs):
        request.headers['If-None-Match'] = '*'
    if 'IfNoneMatch' in fields:
        arguments['IfNoneMatch'] = '*'
    else:
        service.meta.events.register(event, condition)
    try:
        method = re.sub(r'(?<!^)(?=[A-Z])', '_', operation).lower()
        return getattr(service, method)(**arguments)
    finally:
        if 'IfNoneMatch' not in fields:
            service.meta.events.unregister(event, condition)


class Multipart:
    """Keep only one 8 MiB part in memory; complete only after tar succeeds."""
    part_size = 8 * 1024 * 1024

    def __init__(self, values, catalog, save, service=None):
        self.values = values
        self.service = service or media_transfer.client(values)
        self.catalog, self.save = Path(catalog), save
        self.owner = uuid.uuid4().hex
        self.key = values['S3_PREFIX'] + self.owner + '.tar.gz.aesgcm'
        self.record = {'format': 'misbot-existing-s3-v1', 'storage': media_transfer.identity(values),
                       'owner': self.owner, 'key': self.key, 'verified': False,
                       'phase': 'creating', 'upload_id': None}
        self.save(self.catalog, self.record)
        response = self.service.create_multipart_upload(Bucket=values['S3_BUCKET'], Key=self.key,
            ContentType='application/octet-stream', Metadata={'misbot-backup': self.owner})
        self.record.update(upload_id=response['UploadId'], phase='uploading')
        self.save(self.catalog, self.record)
        self.buffer, self.parts = bytearray(), []
        self.hash, self.size = hashlib.sha256(), 0

    def write(self, data):
        self.hash.update(data)
        self.size += len(data)
        self.buffer.extend(data)
        while len(self.buffer) >= self.part_size:
            self.part(bytes(self.buffer[:self.part_size]))
            del self.buffer[:self.part_size]
        return len(data)

    def part(self, data):
        if len(self.parts) >= 10000:
            raise RuntimeError('Multipart part limit exceeded; incomplete upload retained')
        number = len(self.parts) + 1
        response = self.service.upload_part(Bucket=self.values['S3_BUCKET'], Key=self.key,
            UploadId=self.record['upload_id'], PartNumber=number, Body=data)
        self.parts.append({'PartNumber': number, 'ETag': response['ETag']})

    def complete(self):
        if self.buffer:
            self.part(bytes(self.buffer))
            self.buffer.clear()
        self.record.update(phase='completing', sha256=self.hash.hexdigest(), bytes=self.size)
        self.save(self.catalog, self.record)
        conditional(self.service, 'CompleteMultipartUpload', Bucket=self.values['S3_BUCKET'],
                    Key=self.key, UploadId=self.record['upload_id'], MultipartUpload={'Parts': self.parts})
        self.record.update(phase='uploaded')
        self.save(self.catalog, self.record)
        return self.record

    def abort(self):
        # Abort only this invocation's known multipart ID, never a complete object.
        try:
            self.service.abort_multipart_upload(Bucket=self.values['S3_BUCKET'], Key=self.key,
                                                 UploadId=self.record['upload_id'])
            self.record['multipart_aborted'] = True
            self.save(self.catalog, self.record)
        except Exception:
            pass


class HashedReader:
    def __init__(self, source):
        self.source, self.hash, self.size = source, hashlib.sha256(), 0

    def read(self, size=-1):
        data = self.source.read(size)
        self.hash.update(data)
        self.size += len(data)
        return data

    def close(self):
        self.source.close()


class ObjectStream:
    """Publish bounded encrypted parts using conditional PUT on S3 clones."""
    part_size = 8 * 1024 * 1024

    def __init__(self, values, catalog, save, service=None):
        self.values, self.catalog, self.save = values, Path(catalog), save
        self.service = service or media_transfer.client(values)
        self.owner = uuid.uuid4().hex
        self.key = values['S3_PREFIX'] + self.owner + '.manifest.json'
        self.record = {'format': 'misbot-existing-s3-chunks-v1', 'storage': media_transfer.identity(values),
                       'owner': self.owner, 'key': self.key, 'verified': False,
                       'phase': 'uploading', 'chunks': []}
        self.buffer, self.hash, self.size = bytearray(), hashlib.sha256(), 0
        self.save(self.catalog, self.record)

    def write(self, data):
        self.hash.update(data)
        self.size += len(data)
        self.buffer.extend(data)
        while len(self.buffer) >= self.part_size:
            self.part(bytes(self.buffer[:self.part_size]))
            del self.buffer[:self.part_size]
        return len(data)

    def part(self, data):
        number = len(self.record['chunks'])
        if number >= 10000:
            raise RuntimeError('Encrypted part limit exceeded; catalog retained')
        key = self.values['S3_PREFIX'] + self.owner + '/' + f'{number:05d}'
        chunk = {'key': key, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        # Intent is retained if the server accepted PUT but the response was lost.
        self.record['pending_chunk'] = chunk
        self.save(self.catalog, self.record)
        conditional(self.service, 'PutObject', Bucket=self.values['S3_BUCKET'], Key=key,
                    Body=data, ContentType='application/octet-stream',
                    Metadata={'misbot-backup': self.owner})
        self.record['chunks'].append(chunk)
        self.record.pop('pending_chunk')
        self.save(self.catalog, self.record)

    def complete(self):
        if self.buffer:
            self.part(bytes(self.buffer))
            self.buffer.clear()
        self.record.update(phase='completing', sha256=self.hash.hexdigest(), bytes=self.size)
        self.save(self.catalog, self.record)
        payload = {k: self.record[k] for k in ('format', 'storage', 'owner', 'key', 'chunks', 'sha256', 'bytes')}
        conditional(self.service, 'PutObject', Bucket=self.values['S3_BUCKET'], Key=self.key,
                    Body=json.dumps(payload, sort_keys=True).encode(), ContentType='application/json',
                    Metadata={'misbot-backup': self.owner})
        self.record['phase'] = 'uploaded'
        self.save(self.catalog, self.record)
        return self.record

    def abort(self):
        # Retain encrypted parts and the exact catalog for an explicit cleanup.
        self.record['incomplete_parts_retained'] = True
        self.save(self.catalog, self.record)


class ChunkReader:
    def __init__(self, service, values, record):
        self.service, self.values, self.record = service, values, record
        self.index, self.body = 0, None
        self.part_hash, self.part_bytes = None, 0

    def read(self, size=-1):
        # Crypto framing asks for bounded reads. Never load a full archive here.
        if size < 0:
            raise RuntimeError('Backup reading requires bounded reads')
        output = bytearray()
        while len(output) < size:
            if self.body is None:
                if self.index == len(self.record['chunks']):
                    break
                chunk = self.record['chunks'][self.index]
                response = self.service.get_object(Bucket=self.values['S3_BUCKET'], Key=chunk['key'])
                metadata = {k.lower(): v for k, v in response.get('Metadata', {}).items()}
                self.body = response['Body']
                if metadata.get('misbot-backup') != self.record['owner']:
                    self.close()
                    raise RuntimeError('Encrypted part ownership differs')
                self.part_hash, self.part_bytes = hashlib.sha256(), 0
            data = self.body.read(size - len(output))
            self.part_hash.update(data)
            self.part_bytes += len(data)
            output.extend(data)
            if not data:
                chunk = self.record['chunks'][self.index]
                self.close()
                if self.part_bytes != chunk['bytes'] or self.part_hash.hexdigest() != chunk['sha256']:
                    raise RuntimeError('Encrypted part checksum differs')
                self.index += 1
        return bytes(output)

    def close(self):
        if self.body is not None:
            self.body.close()
            self.body = None


def open_backup(values, record, service=None):
    owner = record.get('owner', '')
    chunked = record.get('format') == 'misbot-existing-s3-chunks-v1'
    suffix = '.manifest.json' if chunked else '.tar.gz.aesgcm'
    if (record.get('format') not in ('misbot-existing-s3-v1', 'misbot-existing-s3-chunks-v1') or not re.fullmatch('[0-9a-f]{32}', owner)
            or record.get('storage') != media_transfer.identity(values)
            or record.get('key') != values['S3_PREFIX'] + owner + suffix):
        raise RuntimeError('Backup catalog does not match this S3 profile')
    service = service or media_transfer.client(values)
    response = service.get_object(Bucket=values['S3_BUCKET'], Key=record['key'])
    metadata = {name.lower(): value for name, value in response.get('Metadata', {}).items()}
    if metadata.get('misbot-backup') != owner:
        response['Body'].close()
        raise RuntimeError('Remote backup ownership differs; no object changed')
    if chunked:
        try:
            body = response['Body'].read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise RuntimeError('Backup manifest exceeds its limit')
            manifest = json.loads(body)
        finally:
            response['Body'].close()
        expected = {k: record[k] for k in ('format', 'storage', 'owner', 'key', 'chunks', 'sha256', 'bytes')}
        if manifest != expected or not 1 <= len(record['chunks']) <= 10000:
            raise RuntimeError('Backup manifest differs from the private catalog')
        total = 0
        for number, chunk in enumerate(record['chunks']):
            if (chunk['key'] != values['S3_PREFIX'] + owner + '/' + f'{number:05d}'
                    or not re.fullmatch('[0-9a-f]{64}', chunk['sha256'])
                    or not 1 <= chunk['bytes'] <= ObjectStream.part_size):
                raise RuntimeError('Backup part path or size is invalid')
            total += chunk['bytes']
        if total != record['bytes']:
            raise RuntimeError('Backup parts have an invalid total size')
        return HashedReader(ChunkReader(service, values, record))
    return HashedReader(response['Body'])


def verify_extension(root, container, image):
    folder = root / 'synapse/extensions/s3'
    if folder.resolve() != folder:
        raise RuntimeError('S3 extension must not be a symlink')
    for path in [folder, folder / 'manifest.json', *(folder / 'dependencies').rglob('*')]:
        info = path.lstat()
        if path.resolve() != path or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('S3 extension ownership or path is unsafe')
    manifest = json.loads((folder / 'manifest.json').read_text())
    if (manifest.get('format') != 1 or manifest.get('base_image') != image
            or manifest.get('versions', {}).get('provider_version') != '1.7.0'):
        raise RuntimeError('Stage a tested S3 extension for this exact Synapse image first')
    files = manifest.get('files', {})
    actual = {p.relative_to(folder / 'dependencies').as_posix()
              for p in (folder / 'dependencies').rglob('*') if not p.is_dir()}
    if not files or actual != set(files):
        raise RuntimeError('S3 extension files differ from its manifest')
    for name, checksum in files.items():
        path = folder / 'dependencies' / name
        if path.resolve() != path or not path.is_relative_to(folder):
            raise RuntimeError('S3 extension contains an unsafe path')
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('S3 extension must be root owned and protected from writes')
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise RuntimeError('S3 extension checksum differs')
    return folder


def profile(path, purpose):
    return s3_config.read_profile(path, purpose=purpose)


def copy_media(directory, values, checkpoint, save, progress, service=None):
    """Verify every file by GET; never overwrite different remote content."""
    directory, checkpoint = Path(directory), Path(checkpoint)
    if directory.resolve() != directory:
        raise RuntimeError('Media directory must not contain symlinks')
    service = service or media_transfer.client(values)
    record = json.loads(checkpoint.read_text()) if checkpoint.exists() else {
        'format': 1, 'identity': media_transfer.identity(values), 'files': {}}
    if record.get('format') != 1 or record.get('identity') != media_transfer.identity(values):
        raise RuntimeError('Media checkpoint belongs to another S3 destination')
    files, total, copied = sorted(directory.rglob('*')), 0, 0
    if any(p.is_symlink() for p in files):
        raise RuntimeError('Media symlink found; no outside files uploaded')
    for path in files:
        if not path.is_file():
            continue
        name = path.relative_to(directory).as_posix()
        with path.open('rb') as source:
            checksum = media_transfer.stream_digest(source)
        key = values['S3_PREFIX'] + name
        found = media_transfer.remote_digest(service, values['S3_BUCKET'], key)
        if found is not None and found != checksum:
            raise RuntimeError('Different media already exists at the S3 key; nothing overwritten')
        if found is None:
            record['files'][name] = {'sha256': checksum, 'status': 'uploading'}
            save(checkpoint, record)
            with path.open('rb') as source:
                try:
                    conditional(service, 'PutObject', Bucket=values['S3_BUCKET'], Key=key, Body=source,
                                Metadata={'sha256': checksum})
                except Exception as error:
                    if getattr(error, 'response', {}).get('Error', {}).get('Code') not in ('PreconditionFailed', '412'):
                        raise
                    # Synapse may concurrently store the same immutable file.
                    if media_transfer.remote_digest(service, values['S3_BUCKET'], key) != checksum:
                        raise RuntimeError('Concurrent S3 media differs; nothing overwritten') from None
            copied += 1
            if media_transfer.remote_digest(service, values['S3_BUCKET'], key) != checksum:
                raise RuntimeError('Uploaded media checksum differs; local originals retained')
        with path.open('rb') as source:
            if media_transfer.stream_digest(source) != checksum:
                raise RuntimeError('Local media changed during copying; retry, originals retained')
        record['files'][name] = {'sha256': checksum, 'status': 'verified'}
        save(checkpoint, record)
        total += 1
        if total % 50 == 0:
            progress(files_verified=total, files_uploaded=copied)
    return {'files_verified': total, 'files_uploaded': copied, 'local_files_retained': True}
