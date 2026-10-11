"""Internal contracts; persisted dictionaries and public records keep their formats."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal, NotRequired, Required, TypedDict


class InstallationState(TypedDict, total=False):
    domain: Required[str]
    migration_frozen: bool
    restore_pending: bool


class BackupPauseRecord(TypedDict):
    format: Literal[1]
    # Older/malformed journals still reach the identity check in recovery.
    server_name: NotRequired[object]
    writers: list[str]
    keep_stopped: bool
    pid: int
    started: str


class OperationStage(str, Enum):
    CONFIGURATION = 'configuration'
    BACKUP_RECOVERY = 'backup_recovery'


class OperationStatus(str, Enum):
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'


@dataclass(frozen=True)
class OperationResult:
    """Messages are deliberate public guidance, never raw driver exceptions."""

    code: str
    stage: OperationStage
    message: str
    next_actions: tuple[str, ...] = ()
    status: OperationStatus = OperationStatus.SUCCEEDED


class ConfigurationFailure(TypedDict):
    stage: str
    status: str
    code: str
    file: str
    field: str
    explanation: str
    next_actions: list[str]
