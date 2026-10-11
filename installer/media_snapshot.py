"""Restore missing S3 originals locally before a consistent full archive."""

import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile

import media_transfer
import state_store


def hydrate(directory, values, inventory, service=None):
    directory = Path(directory).absolute()
    if directory.resolve(strict=True) != directory:
        raise RuntimeError('Media directory must not contain symlinks; no outside files copied')
    service = service or media_transfer.client(values)
    identity = media_transfer.identity(values)
    bucket, prefix = values['S3_BUCKET'], values['S3_PREFIX']
    files = {}
    downloaded = 0
    owner = directory.stat()
    for page in service.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
        for record in page.get('Contents', []):
            key = record['Key']
            if not key.startswith(prefix):
                raise RuntimeError(
                    'S3 returned an object outside the configured prefix; no object removed'
                )
            relative = key[len(prefix) :]
            if relative.endswith('/') or not relative:
                continue
            path = PurePosixPath(relative)
            if (
                path.is_absolute()
                or '..' in path.parts
                or '\\' in relative
                or path.as_posix() != relative
                or relative in files
            ):
                raise RuntimeError(
                    'Unsafe or duplicate S3 media path; inspect privately before backing up'
                )
            target = directory.joinpath(*path.parts)
            if target.is_symlink() or not target.resolve().is_relative_to(directory):
                raise RuntimeError(
                    'S3 media path conflicts with a local symlink; nothing overwritten'
                )
            parent = directory
            for part in path.parts[:-1]:
                parent = parent / part
                if parent.is_symlink():
                    raise RuntimeError(
                        'Local media directory contains a symlink; nothing overwritten'
                    )
                if not parent.exists():
                    parent.mkdir(mode=0o750)
                    if os.geteuid() == 0:
                        os.chown(parent, owner.st_uid, owner.st_gid)
            size = int(record['Size'])
            # Existing originals also need scratch space for the verified download.
            if shutil.disk_usage(directory).free < size + 64 * 1024**2:
                raise RuntimeError(
                    'Insufficient free disk for S3 originals; retain S3/local data and free space before retrying backup-external'
                )
            fd, name = tempfile.mkstemp(prefix='.matrix-s3-', dir=target.parent)
            temporary = Path(name)
            digest = hashlib.sha256()
            count = 0
            body = None
            try:
                response = service.get_object(Bucket=bucket, Key=key)
                body = response['Body']
                with os.fdopen(fd, 'wb') as output:
                    fd = None
                    while True:
                        chunk = body.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        count += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if count != size:
                    raise RuntimeError(
                        'S3 media changed during the snapshot; no local original overwritten'
                    )
                checksum = digest.hexdigest()
                if target.exists():
                    if not target.is_file():
                        raise RuntimeError(
                            'S3 media conflicts with a local non-file; nothing overwritten'
                        )
                    with target.open('rb') as existing:
                        if media_transfer.stream_digest(existing) != checksum:
                            raise RuntimeError(
                                'S3 and local media differ; keep both copies and inspect privately before retrying'
                            )
                else:
                    temporary.chmod(0o640)
                    if os.geteuid() == 0:
                        os.chown(temporary, owner.st_uid, owner.st_gid)
                    os.link(temporary, target)
                    downloaded += 1
                files[relative] = {'sha256': checksum, 'bytes': count}
            finally:
                if fd is not None:
                    os.close(fd)
                if body is not None:
                    body.close()
                temporary.unlink(missing_ok=True)
    state_store.write_json(inventory, {'format': 1, 'identity': identity, 'files': files})
    return {'s3_objects_verified': len(files), 'missing_local_objects_restored': downloaded}
