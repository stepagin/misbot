"""Atomic private checkpoints; preserve file owners used by container mounts."""

import errno
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import secrets
import shlex
import stat
import subprocess
import tempfile
import cli_commands
from typing import cast
from operation_models import BACKUP_WRITERS, BackupPauseRecord


class OperationInterrupted(Exception):
    """Signal exceptions must bypass InterruptedError syscall retry handling."""


class S3CredentialRecoveryRequired(RuntimeError):
    """Public recovery guidance; contains no provider exception or credentials."""


def write_text(path, text, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('Refusing to replace a symlink in a managed checkpoint')
    saved = path.stat() if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), mode)
            if saved and os.geteuid() == 0:
                os.fchown(output.fileno(), saved.st_uid, saved.st_gid)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(directory)
            except OSError as error:
                if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def require_root(message='Run installer commands as root'):
    if os.geteuid() != 0:
        raise RuntimeError(message)


def create_rsa_key(path):
    """Publish a newly generated key only after its producer has succeeded."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise RuntimeError('RSA key path is already occupied; no key replaced')
    descriptor, name = tempfile.mkstemp(prefix='.rsa-publish-', dir=path.parent)
    temporary = Path(name)
    os.close(descriptor)
    try:
        try:
            subprocess.run(
                [
                    'openssl',
                    'genpkey',
                    '-algorithm',
                    'RSA',
                    '-pkeyopt',
                    'rsa_keygen_bits:3072',
                    '-out',
                    str(temporary),
                ],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **child_process_options(),
            )
        except subprocess.SubprocessError:
            raise RuntimeError(
                'RSA key generation failed before publication. Keep the installation directory, correct the resource error, then retry the original init/generate command'
            ) from None
        descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or not info.st_size
            ):
                raise RuntimeError(
                    'RSA generator did not produce a complete private file; retry initialization'
                )
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if path.exists() or path.is_symlink():
            raise RuntimeError('RSA key path became occupied during generation; no key replaced')
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            try:
                os.fsync(descriptor)
            except OSError as error:
                if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def process_identity(pid):
    """Return parent and start ticks; a reused PID cannot inherit an operation."""
    fields = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()
    return int(fields[1]), fields[19]


def related_owner(owner):
    try:
        pid = os.getpid()
        visited = set()
        while pid > 0 and pid not in visited:
            visited.add(pid)
            parent, started = process_identity(pid)
            if pid == owner['pid']:
                return started == owner['started']
            pid = parent
    except (OSError, ValueError, KeyError):
        pass
    return False


def operation_lock_path(root):
    root = Path(root).resolve()
    name = hashlib.sha256(str(root).encode()).hexdigest()[:24]
    return root.parent / ('.matrix-installer-operation-' + name + '.lock')


def child_process_options():
    """Keep the operation's open lock alive in children if their parent is killed."""
    try:
        descriptor = int(os.environ['MATRIX_INSTALLER_OPERATION_FD'])
        if descriptor < 3:
            return {}
        info = os.fstat(descriptor)
        path = operation_lock_path(os.environ['MATRIX_INSTALLER_OPERATION_ROOT'])
        expected = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            return {}
        return {'pass_fds': (descriptor,)}
    except (OSError, ValueError, KeyError):
        return {}


def inherited_operation_descriptor(root):
    options = child_process_options()
    if not options or os.environ.get('MATRIX_INSTALLER_OPERATION_ROOT') != str(root):
        return None
    descriptor = options['pass_fds'][0]
    try:
        owner = json.loads(os.pread(descriptor, 4096, 0))
        if not secrets.compare_digest(
            os.environ.get('MATRIX_INSTALLER_OPERATION_TOKEN', ''), owner['token']
        ):
            return None
        # Only an inherited open file description can share an existing flock.
        # A separate descriptor with copied environment values remains blocked.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except (OSError, ValueError, KeyError, TypeError):
        return None


def assert_update_allowed(
    root,
    allow_restore_generation=False,
    allow_federation=False,
    allow_configuration=False,
    allow_homepage=False,
):
    reject_existing_backend(root)
    if (Path(root) / 'remove-journal.json').exists():
        raise RuntimeError(
            'Удаление сервера прервано: sudo misbot remove; запуск и другие изменения запрещены.'
        )
    state_path = Path(root) / 'installation.json'
    if state_path.exists() and json.loads(state_path.read_text()).get('decommissioned'):
        raise RuntimeError(
            'Сервер удалён частично. Для полного удаления повторите sudo misbot remove.'
        )
    if not allow_homepage and (
        (Path(root) / 'homepage-transition.json').exists()
        or (Path(root) / 'homepage-transition.json').is_symlink()
    ):
        raise RuntimeError(
            'Главная страница требует восстановления: sudo misbot --directory '
            + shlex.quote(str(root))
            + ' homepage recover; прежние файлы и checkpoint сохраните'
        )
    if not allow_federation and (Path(root) / 'federation-transition.json').exists():
        raise RuntimeError(
            'Federation change needs recovery: sudo misbot --directory '
            + shlex.quote(str(root))
            + ' federation recover; keep the checkpoint'
        )
    import configuration

    if not allow_configuration:
        configuration.pending(root)
    assert_restore_complete(root, allow_restore_generation)
    assert_s3_credentials_allowed(root)
    assert_database_allowed(root)
    assert_backup_allowed(root)
    journal = Path(root) / 'update-journal.json'
    if not journal.exists():
        return
    if journal.is_symlink():
        raise RuntimeError('Unsafe update journal; inspect the private installation state')
    info = journal.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RuntimeError(
            'Update journal must be private and owned by the current user; recover from the trusted installer'
        )
    record = json.loads(journal.read_text())
    if os.environ.get('MATRIX_INSTALLER_UPDATE_STAGE') != record.get('stage') or not related_owner(
        record
    ):
        raise RuntimeError(
            'An interrupted update needs recovery. From a trusted extracted installer run: '
            'sudo bash install.sh update-recover '
            + str(root)
            + '. Or: sudo misbot --directory '
            + shlex.quote(str(root))
            + ' update recover --source /root/matrix-installer. Keep data, secrets and update checkpoints'
        )


def assert_restore_complete(root, allow_generation=False):
    path = Path(root) / 'restore-journal.json'
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise RuntimeError(
            'Restore journal must be a private owned regular file; keep target/archive and inspect it privately'
        )
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        raise RuntimeError(
            'Restore journal is unreadable; keep the target and trusted archive, no writers started'
        ) from None
    if (
        not isinstance(record, dict)
        or record.get('stage') not in ('new', 'extracting', 'files_ready', 'importing', 'ready')
        or type(record.get('local_postgres', False)) is not bool
    ):
        raise RuntimeError('Invalid restore journal; keep the target/archive, no writers started')
    if record['stage'] != 'ready':
        if (
            allow_generation
            and isinstance(record.get('archive_sha256'), str)
            and os.environ.get('MATRIX_INSTALLER_RESTORE_ARCHIVE') == record['archive_sha256']
            and type(record.get('pid')) is int
            and isinstance(record.get('started'), str)
            and related_owner(record)
            and inherited_operation_descriptor(root) is not None
        ):
            return
        action = 'restore-local' if record.get('local_postgres') else 'restore'
        raise RuntimeError(
            'Restore is incomplete; writers remain disabled. From the trusted extracted installer repeat install.sh '
            + action
            + ' with the original archive, directory, SHA256 and public IP; see backup.md. Keep data and the restore journal'
        )


def database_transition_record(root):
    root = Path(root)
    path = root / 'database-transition.json'
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise RuntimeError(
            'Database transition journal must be a private owned regular file; retain checkpoints'
        )
    record = json.loads(path.read_text())
    phases = ('prepared', 'snapshot', 'importing', 'committed', 'activating', 'aborting')
    writers = record.get('writers')
    imported = record.get('imported')
    if (
        record.get('format') != 1
        or record.get('phase') not in phases
        or type(record.get('activated')) is not bool
        or type(record.get('pid')) is not int
        or record['pid'] <= 0
        or not isinstance(record.get('started'), str)
        or not isinstance(record.get('stage'), str)
        or not isinstance(record.get('target'), str)
        or not isinstance(writers, list)
        or len(set(writers)) != len(writers)
        or any(n not in BACKUP_WRITERS for n in writers)
        or not isinstance(imported, list)
        or len(set(imported)) != len(imported)
        or any(n not in ('synapse', 'mas') for n in imported)
        or record.get('server_name')
        != json.loads((root / 'installation.json').read_text()).get('domain')
    ):
        raise RuntimeError(
            'Invalid database transition journal; retain both datasets and inspect checkpoints privately'
        )
    client = record.get('active_client')
    if client is not None and (
        not isinstance(client, str) or not re.fullmatch(r'matrix-dbcopy-[0-9a-f]{32}', client)
    ):
        raise RuntimeError('Invalid owned database client name; retain checkpoints')
    moment = record.get('freeze_started_unix')
    if moment is not None and (
        type(moment) not in (int, float) or not math.isfinite(moment) or moment < 0
    ):
        raise RuntimeError('Invalid database freeze timestamp; retain checkpoints')
    return record


def assert_database_allowed(root):
    assert_s3_credentials_allowed(root)
    assert_database_password_allowed(root)
    record = database_transition_record(root)
    if record is None:
        return
    if os.environ.get('MATRIX_INSTALLER_DATABASE_STAGE') != record['stage'] or not related_owner(
        record
    ):
        raise RuntimeError(
            'A database storage transition is pending. Keep both datasets and private checkpoints; run: sudo bash '
            + str(Path(root) / 'install.sh')
            + ' database-recover. CLI: '
            + cli_commands.recovery_command('database-recover', root)
            + '. database-abort is allowed only before destination activation; do not start an independent writer'
        )


def assert_s3_credentials_allowed(root):
    root = Path(root)
    path = root / 's3-credentials-transition.json'
    if not path.exists() and not path.is_symlink():
        return
    from s3_credentials import private_json

    try:
        record = private_json(path)
        valid = (
            isinstance(record, dict)
            and record.get('format') == 1
            and isinstance(record.get('stage'), str)
            and re.fullmatch(r'stages/s3-credentials/[0-9a-f]{32}', record['stage'])
            and type(record.get('pid')) is int
            and record['pid'] > 0
            and isinstance(record.get('started'), str)
        )
    except (OSError, ValueError, RuntimeError):
        valid = False
    if (
        not valid
        or os.environ.get('MATRIX_INSTALLER_S3_CREDENTIAL_STAGE') != record['stage']
        or not related_owner(record)
    ):
        raise S3CredentialRecoveryRequired(
            'An S3 credential change needs recovery. Keep private checkpoints and verified backup; run: sudo bash '
            + str(root / 'install.sh')
            + ' s3-credentials-recover. CLI: '
            + cli_commands.recovery_command('s3-credentials-recover', root)
            + '. Do not start Synapse manually'
        )


def assert_database_password_allowed(root):
    root = Path(root)
    path = root / 'database-password-transition.json'
    if not path.exists() and not path.is_symlink():
        return
    from pg_credentials import private_json

    try:
        record = private_json(path)
        valid = (
            isinstance(record, dict)
            and record.get('format') == 1
            and isinstance(record.get('stage'), str)
            and re.fullmatch(r'stages/database-passwords/[0-9a-f]{32}', record['stage'])
            and type(record.get('pid')) is int
            and record['pid'] > 0
            and isinstance(record.get('started'), str)
        )
    except (OSError, ValueError, RuntimeError):
        valid = False
    if (
        not valid
        or os.environ.get('MATRIX_INSTALLER_DATABASE_PASSWORD_STAGE') != record['stage']
        or not related_owner(record)
    ):
        raise RuntimeError(
            'A database password change needs recovery. Keep private checkpoints and the verified backup; run: sudo bash '
            + str(root / 'install.sh')
            + ' database-password-recover. CLI: '
            + cli_commands.recovery_command('database-password-recover', root)
            + '. Do not start writers manually'
        )


def backup_pause_record(root: str | Path) -> BackupPauseRecord | None:
    journal = Path(root) / 'backup-writers.json'
    if not journal.exists() and not journal.is_symlink():
        return None
    info = journal.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        raise RuntimeError(
            'Backup recovery journal must be a private regular file owned by the current user'
        )
    record = json.loads(journal.read_text())
    writers = record.get('writers')
    if (
        record.get('format') != 1
        or not isinstance(writers, list)
        or any(not isinstance(name, str) for name in writers)
        or len(writers) != len(set(writers))
        or any(name not in BACKUP_WRITERS for name in writers)
        or type(record.get('pid')) is not int
        or record['pid'] <= 0
        or not isinstance(record.get('started'), str)
        or type(record.get('keep_stopped')) is not bool
    ):
        raise RuntimeError(
            'Invalid backup recovery journal; retain files and inspect the installation privately'
        )
    return cast(BackupPauseRecord, record)


def assert_backup_allowed(root):
    if backup_pause_record(root) is not None:
        raise RuntimeError(
            'An interrupted backup may have stopped writers. Run: sudo bash '
            + str(Path(root) / 'install.sh')
            + ' backup-recover. Keep data, secrets and backup files'
        )


def reject_existing_backend(root):
    path = Path(root) / 'installation.json'
    if path.is_file() and json.loads(path.read_text()).get('backend') == 'existing-compose':
        raise RuntimeError(
            'Existing Compose installations must use misbot; portable generation and lifecycle operations are disabled'
        )


@contextmanager
def operation_guard(root):
    """Serialize mutations; admit only descendants of the current owning process.

    The lock lives beside the installation so an interrupted first init or restore
    does not make an otherwise empty destination unusable. Never unlink a busy lock.
    """
    root = Path(root).resolve()
    reject_existing_backend(root)
    descriptor = os.open(operation_lock_path(root), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    acquired = False
    previous = {
        key: os.environ.get(key)
        for key in (
            'MATRIX_INSTALLER_OPERATION_ROOT',
            'MATRIX_INSTALLER_OPERATION_TOKEN',
            'MATRIX_INSTALLER_OPERATION_FD',
        )
    }
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise RuntimeError(
                'Installer operation lock must be a private regular file owned by the current user'
            )
        inherited_descriptor = inherited_operation_descriptor(root)
        if inherited_descriptor is not None:
            borrowed = os.dup(inherited_descriptor)
            os.close(descriptor)
            descriptor = borrowed
        else:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                inherited = False
                try:
                    owner = json.loads(os.pread(descriptor, 4096, 0))
                    inherited = (
                        os.environ.get('MATRIX_INSTALLER_OPERATION_ROOT') == str(root)
                        and secrets.compare_digest(
                            os.environ.get('MATRIX_INSTALLER_OPERATION_TOKEN', ''), owner['token']
                        )
                        and related_owner(owner)
                    )
                except (ValueError, KeyError, TypeError):
                    pass
                if not inherited:
                    raise RuntimeError(
                        'Another installer operation is running for this directory. '
                        'Use sudo misbot --directory '
                        + shlex.quote(str(root))
                        + ' status (technical entry: install.sh status), wait for that operation, then retry the same command; do not remove the lock file'
                    ) from None
        if acquired:
            token = secrets.token_hex(32)
            owner = {
                'pid': os.getpid(),
                'started': process_identity(os.getpid())[1],
                'token': token,
            }
            os.ftruncate(descriptor, 0)
            os.write(descriptor, json.dumps(owner).encode())
            os.fsync(descriptor)
            os.environ['MATRIX_INSTALLER_OPERATION_ROOT'] = str(root)
            os.environ['MATRIX_INSTALLER_OPERATION_TOKEN'] = token
            os.environ['MATRIX_INSTALLER_OPERATION_FD'] = str(descriptor)
        yield
    finally:
        if acquired:
            os.ftruncate(descriptor, 0)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        os.close(descriptor)
