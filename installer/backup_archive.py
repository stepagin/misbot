"""Validate and extract private backup payloads without trusting tar paths."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile


def add_tree(tar, source, arcname, recursive=True):
    """Write independent regular payloads, without following source links."""
    source = Path(source)
    observed = source.lstat()
    if (stat.S_ISSOCK(observed.st_mode) and
            str(PurePosixPath(arcname)) == 'data/referral-control/bot-run/control.sock'):
        # The control service recreates this IPC endpoint; it has no payload.
        return
    if stat.S_ISDIR(observed.st_mode):
        tar.add(source, arcname=arcname, recursive=False)
        if recursive:
            for child in sorted(source.iterdir()):
                add_tree(tar, child, str(PurePosixPath(arcname) / child.name))
        return
    if not stat.S_ISREG(observed.st_mode):
        raise RuntimeError('Backup source contains a symlink or special file; no outside data copied. Retain the installation and inspect the managed directories privately before retrying the original backup command')
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as incoming:
        actual = os.fstat(incoming.fileno())
        if (not stat.S_ISREG(actual.st_mode) or
                (actual.st_dev, actual.st_ino) != (observed.st_dev, observed.st_ino)):
            raise RuntimeError('Backup source changed while opening a file; retain existing copies and retry when writers and configuration edits are stopped')
        member = tar.gettarinfo(str(source), arcname=arcname, fileobj=incoming)
        # A killed S3 publication can leave original and scratch sharing an inode.
        # Both must remain recoverable even when one TarInfo would be a hardlink.
        member.type = tarfile.REGTYPE
        member.linkname = ''
        member.size = actual.st_size
        tar.addfile(member, incoming)


def extract(archive, destination, owners=False):
    destination = Path(destination).resolve(strict=True)
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        seen = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.name
                    or member.name in seen or not path.parts
                    or not (member.isdir() or member.isfile())):
                raise RuntimeError('Unsafe or duplicate backup member; no files extracted')
            seen.add(member.name)
            target = destination.joinpath(*path.parts)
            if not target.resolve().is_relative_to(destination):
                raise RuntimeError('Backup destination escapes its private directory')
            if target.exists() or target.is_symlink():
                raise RuntimeError('Backup extraction requires an empty destination')
            if owners and (member.uid not in (0, 999, 991, 10001, 10002, 10003, 65532)
                    or member.gid not in (0, 999, 991, 10001, 10002, 10003, 65532)):
                raise RuntimeError('Unexpected numeric owner in backup; inspect privately')
        # All member paths and types are checked before writing any member.
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
            if owners:
                import os
                os.chown(target, member.uid, member.gid)
                target.chmod(member.mode & 0o777)
            else:
                target.chmod(0o700 if member.isdir() else 0o600)


def verify_snapshot(directory):
    entries = list(Path(directory).iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        raise RuntimeError('Unexpected backup layout')
    snapshot = entries[0]
    digests = json.loads((snapshot / 'SHA256.json').read_text())
    required = {'synapse.dump', 'mas.dump', 'roles.sql', 'files.tar.gz'}
    if not isinstance(digests, dict) or not required.issubset(digests):
        raise RuntimeError('Backup lacks required checksum entries')
    if (snapshot / 'backup-format.json').exists():
        if not {'backup-format.json', 'mode.txt'}.issubset(digests):
            raise RuntimeError('Backup metadata lacks checksums')
    for name, digest in digests.items():
        if (not isinstance(name, str) or PurePosixPath(name).name != name
                or name in ('', '.', '..') or '\\' in name):
            raise RuntimeError('Unsafe checksum filename')
        with (snapshot / name).open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != digest:
                raise RuntimeError('Backup checksum mismatch; obtain the intact source archive')
    return snapshot
