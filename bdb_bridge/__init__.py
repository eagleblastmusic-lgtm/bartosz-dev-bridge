from .config import BridgeConfig
from .ingestion import CommandIngestor
from .journal import Journal
from .recovery_gate_hooks import install_command_ingestor_fault_hook

install_command_ingestor_fault_hook(CommandIngestor)
from .workspace_lifecycle_migration import install_workspace_lifecycle_migration

install_workspace_lifecycle_migration(Journal)

from .models import (
    BridgeErrorCode,
    CommandIngestionRecord,
    CommandRecord,
    CommandState,
    ExecutionOutcome,
    IngestionIssue,
    IngestionReport,
    JournalEvent,
    Operation,
    OperationEffectRecord,
    OperationPlanRecord,
    OutboxProcessOutcome,
    OutboxProcessState,
    OutboxRecord,
    OutboxState,
    PollReport,
    ProfileRunOutcome,
    PromotionOutcome,
    PublishAttempt,
    PublishAttemptState,
    RecoveryDecision,
    RemoteResult,
    RemoteResultState,
    ResultCoordinationOutcome,
    ResultRecord,
    ResultStatus,
    SessionIngestionRecord,
    SessionRecord,
    SessionState,
    StagedResult,
    TransportRetryRecord,
    WorkspaceRecord,
    ServiceInstanceState,
    ServiceStatus,
    ServiceInstanceRecord,
    ServiceStatusSnapshot,
    BridgeCycleReport,
    ServiceRunOutcome,
    StopRequestOutcome,
    BackgroundStartOutcome,
)
from .recovery_journal import (
    compute_operation_effect_sha256,
    compute_operation_plan_sha256,
    install_journal_recovery_api,
    sha256_bytes,
)
from .outbox_journal import install_journal_outbox_api
from .service_journal import install_journal_service_api
from .workspace_lifecycle_journal import install_journal_workspace_lifecycle_api

install_journal_recovery_api(Journal)
install_journal_outbox_api(Journal)
install_journal_service_api(Journal)
install_journal_workspace_lifecycle_api(Journal)

from .workspace_manager import WorkspaceManager
from .instance_lock import InstanceLock
from .service_status import ServiceStatusReader, is_pid_alive
from .heartbeat import HeartbeatWorker
from .service import BridgeService
from .execution import ExecutionCoordinator, RecoveryAssessment
from .protocol import (
    BridgeError,
    COMMAND_PATH_RE,
    MANIFEST_PATH_RE,
    SCHEMA_VERSION,
    SESSION_RE,
    command_id_for,
    command_path_for,
    manifest_path_for,
    parse_command_path,
    parse_manifest_path,
    parse_git_ref,
    sanitize_diagnostics,
    path_matches,
    require_int,
    require_string,
    result_path_for,
    validate_base_sha,
    validate_path_pattern,
    validate_repo_relative_path,
    validate_session_id,
)
from .result_outbox import OutboxProcessor, ResultCoordinator
from .result_staging import EXECUTOR_VERSION, ResultBuildInput, ResultStager
from .result_transport import GitResultTransport, ResultTransport
from .scheduler import SingleQueueScheduler
from .serializers import MAX_RESULT_BYTES, MAX_TAIL_CHARS, canonical_json, finalize_result, sha256_text, tail
from .transport import CommandSnapshot, CommandTransport, RemoteDocument
from .git_command_transport import GitCommandTransport
from .workspace_types import (
    WorkspaceCleanupOutcome,
    WorkspaceDisposition,
    WorkspaceEligibility,
    WorkspaceLifecycleRecord,
    WorkspaceLifecycleState,
    WorkspaceStatusSnapshot,
)
from .workspace_lifecycle import WorkspaceLifecycleCoordinator
from .session_finalization import SessionFinalizationOutcome, SessionFinalizer

__all__ = [
    "BridgeConfig", "BridgeError", "BridgeErrorCode", "BridgeService", "COMMAND_PATH_RE",
    "CommandIngestor", "CommandIngestionRecord", "CommandRecord", "CommandSnapshot",
    "CommandState", "CommandTransport", "ExecutionCoordinator", "ExecutionOutcome",
    "EXECUTOR_VERSION", "GitResultTransport", "IngestionIssue", "IngestionReport",
    "GitCommandTransport", "InstanceLock", "Journal", "JournalEvent", "MANIFEST_PATH_RE",
    "MAX_RESULT_BYTES", "MAX_TAIL_CHARS", "Operation", "OperationEffectRecord",
    "OperationPlanRecord", "OutboxProcessOutcome", "OutboxProcessState", "OutboxProcessor",
    "OutboxRecord", "OutboxState", "PollReport", "ProfileRunOutcome", "PromotionOutcome",
    "PublishAttempt", "PublishAttemptState", "RecoveryAssessment", "RecoveryDecision",
    "RemoteDocument", "RemoteResult", "RemoteResultState", "ResultBuildInput",
    "ResultCoordinator", "ResultCoordinationOutcome", "ResultRecord", "ResultStager",
    "ResultStatus", "ResultTransport", "SCHEMA_VERSION", "SESSION_RE",
    "SessionIngestionRecord", "SessionRecord", "SessionState", "SingleQueueScheduler",
    "StagedResult", "TransportRetryRecord", "WorkspaceManager", "WorkspaceRecord",
    "ServiceInstanceState", "ServiceStatus", "ServiceInstanceRecord", "ServiceStatusSnapshot",
    "ServiceStatusReader", "is_pid_alive", "HeartbeatWorker", "BridgeCycleReport",
    "ServiceRunOutcome", "StopRequestOutcome", "BackgroundStartOutcome",
    "WorkspaceCleanupOutcome", "WorkspaceDisposition", "WorkspaceEligibility",
    "WorkspaceLifecycleRecord", "WorkspaceLifecycleState", "WorkspaceStatusSnapshot",
    "WorkspaceLifecycleCoordinator", "SessionFinalizationOutcome", "SessionFinalizer",
    "canonical_json", "command_id_for", "command_path_for", "compute_operation_effect_sha256",
    "compute_operation_plan_sha256", "finalize_result", "install_journal_outbox_api",
    "install_journal_workspace_lifecycle_api", "install_workspace_lifecycle_migration",
    "manifest_path_for", "parse_command_path", "parse_manifest_path", "path_matches",
    "require_int", "require_string", "result_path_for", "sha256_bytes", "sha256_text", "tail",
    "validate_base_sha", "validate_path_pattern", "validate_repo_relative_path",
    "validate_session_id", "parse_git_ref", "sanitize_diagnostics",
]

from .ghb07_cli import install_cli
install_cli()
