"""Internal contracts; persisted dictionaries and public records keep their formats."""

from dataclasses import dataclass
from enum import Enum
from typing import Literal, NotRequired, Required, TypedDict

BACKUP_WRITERS = ('federation-bot', 'referral-bot', 'referral-control', 'synapse', 'mas', 'jwt')


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


class OperationError(RuntimeError):
    def __init__(self, result: OperationResult) -> None:
        self.result = result
        super().__init__(result.message)


@dataclass(frozen=True)
class BackupRecoveryResult:
    operation: OperationResult
    changed: bool = False
    writers: tuple[str, ...] = ()
    kept_stopped: bool = False

    def legacy_fields(self) -> dict[str, object]:
        """Keep the existing lifecycle JSON record, including omitted no-op fields."""
        fields: dict[str, object] = {'pending': False, 'changed': self.changed}
        if self.changed:
            fields.update(
                writers=list(self.writers),
                kept_stopped=self.kept_stopped,
                next_action=self.operation.next_actions[0],
            )
        return fields


class ConfigurationFailure(TypedDict):
    stage: str
    status: str
    code: str
    file: str
    field: str
    explanation: str
    next_actions: list[str]
