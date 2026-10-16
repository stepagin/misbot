"""One backup writer pause/recovery, with explicit installation and runtime dependencies."""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict

import state_store
from operation_models import (
    BACKUP_WRITERS,
    BackupPauseRecord,
    BackupRecoveryResult,
    InstallationState,
    OperationError,
    OperationResult,
    OperationStage,
    OperationStatus,
)


class ServiceRow(TypedDict):
    Service: str
    State: str
    Health: NotRequired[str]


class Compose(Protocol):
    def __call__(self, *arguments: str) -> object: ...


class Emit(Protocol):
    def __call__(self, check: str, **values: object) -> None: ...


@dataclass(frozen=True)
class WriterRuntime:
    root: Path
    state: Callable[[], InstallationState]
    services: Callable[[], Sequence[ServiceRow]]
    compose: Compose
    wait_ready: Callable[[list[str]], None]
    emit: Emit


def running_writers(rows: Sequence[ServiceRow]) -> list[str]:
    running = {row['Service'] for row in rows if row['State'] in ('running', 'restarting')}
    return [name for name in BACKUP_WRITERS if name in running]


def pause_record(
    metadata: InstallationState, writers: list[str], leave_stopped: bool, pid: int, started: str
) -> BackupPauseRecord:
    return {
        'format': 1,
        'server_name': metadata['domain'],
        'writers': writers,
        'keep_stopped': bool(leave_stopped or metadata.get('migration_frozen')),
        'pid': pid,
        'started': started,
    }


class BackupWriters:
    def __init__(self, runtime: WriterRuntime) -> None:
        self.runtime = runtime
        self.paused = False

    @property
    def root(self) -> Path:
        return self.runtime.root

    @contextmanager
    def pause(self, leave_stopped: bool = False) -> Iterator[None]:
        """Persist intent before stopping; a nested pause shares the outer intent."""
        if self.paused:
            yield
            return
        state_store.assert_backup_allowed(self.root)
        writers = running_writers(self.runtime.services())
        record = pause_record(
            self.runtime.state(),
            writers,
            leave_stopped,
            os.getpid(),
            state_store.process_identity(os.getpid())[1],
        )
        journal = self.root / 'backup-writers.json'
        state_store.write_json(journal, record)
        self.paused = True
        try:
            if writers:
                self.runtime.compose('stop', '-t', '30', *writers)
            yield
        finally:
            try:
                if (
                    writers
                    and not record['keep_stopped']
                    and not self.runtime.state().get('migration_frozen')
                ):
                    self.runtime.compose('up', '-d', *writers)
                    self.runtime.wait_ready(writers)
                journal.unlink()
            except BaseException:
                self.runtime.emit(
                    'backup_recovery_pending',
                    command='sudo bash ' + str(self.root / 'install.sh') + ' backup-recover',
                )
                raise
            finally:
                self.paused = False

    def recover(self) -> BackupRecoveryResult:
        state_store.assert_database_allowed(self.root)
        record = state_store.backup_pause_record(self.root)
        if record is None:
            return BackupRecoveryResult(
                OperationResult(
                    'backup_recovery_not_needed',
                    OperationStage.BACKUP_RECOVERY,
                    'No interrupted backup needs recovery',
                )
            )
        if record.get('server_name') != self.runtime.state()['domain']:
            raise OperationError(
                OperationResult(
                    'backup_identity_mismatch',
                    OperationStage.BACKUP_RECOVERY,
                    'Backup recovery journal belongs to another Matrix identity',
                    ('Keep the journal and use the matching installation',),
                    OperationStatus.FAILED,
                )
            )
        try:
            alive = state_store.process_identity(record['pid'])[1] == record['started']
        except OSError:
            alive = False
        if alive:
            raise OperationError(
                OperationResult(
                    'backup_owner_active',
                    OperationStage.BACKUP_RECOVERY,
                    'The backup process still exists; wait for it to finish before backup-recover',
                    ('Wait for the backup process to finish before backup-recover',),
                    OperationStatus.FAILED,
                )
            )
        writers = record['writers']
        metadata = self.runtime.state()
        keep_stopped = record['keep_stopped'] or bool(
            metadata.get('migration_frozen') or metadata.get('restore_pending')
        )
        if writers:
            if keep_stopped:
                self.runtime.compose('stop', '-t', '30', *writers)
            else:
                self.runtime.compose('up', '-d', *writers)
                self.runtime.wait_ready(writers)
        (self.root / 'backup-writers.json').unlink()
        operation = OperationResult(
            'backup_writers_kept_stopped' if keep_stopped else 'backup_writers_resumed',
            OperationStage.BACKUP_RECOVERY,
            'Backup writers remain stopped'
            if keep_stopped
            else 'Previously running backup writers resumed',
            (
                'Use move-abort only if cancelling migration'
                if keep_stopped
                else 'Run install.sh check, then retry the backup',
            ),
        )
        return BackupRecoveryResult(
            operation, changed=True, writers=tuple(writers), kept_stopped=keep_stopped
        )
