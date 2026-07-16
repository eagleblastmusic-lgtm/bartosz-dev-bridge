from .config import BridgeConfig
from .ingestion import CommandIngestor
from .journal import Journal
from .recovery_gate_hooks import (
    install_command_collision_diagnostics,
    install_command_ingestor_fault_hook,
)

install_command_ingestor_fault_hook(CommandIngestor)
install_command_collision_diagnostics(Journal)
from .workspace_lifecycle_migration import install_workspace_lifecycle_migration
from .repository_index_migration import install_repository_index_migration
from .code_relationship_migration import install_code_relationship_migration
from .multi_file_patch_migration import install_multi_file_patch_migration
from .multi_file_patch_runtime_migration import install_multi_file_patch_runtime_migration

install_workspace_lifecycle_migration(Journal)
install_repository_index_migration(Journal)
install_code_relationship_migration(Journal)
install_multi_file_patch_migration(Journal)
install_multi_file_patch_runtime_migration(Journal)

from .models import (
    BridgeErrorCode, CommandIngestionRecord, CommandRecord, CommandState, ExecutionOutcome,
    IngestionIssue, IngestionReport, JournalEvent, Operation, OperationEffectRecord,
    OperationPlanRecord, OutboxProcessOutcome, OutboxProcessState, OutboxRecord, OutboxState,
    PollReport, ProfileRunOutcome, PromotionOutcome, PublishAttempt, PublishAttemptState,
    RecoveryDecision, RemoteResult, RemoteResultState, ResultCoordinationOutcome, ResultRecord,
    ResultStatus, SessionIngestionRecord, SessionRecord, SessionState, StagedResult,
    TransportRetryRecord, WorkspaceRecord, ServiceInstanceState, ServiceStatus,
    ServiceInstanceRecord, ServiceStatusSnapshot, BridgeCycleReport, ServiceRunOutcome,
    StopRequestOutcome, BackgroundStartOutcome,
)
from .recovery_journal import (
    compute_operation_effect_sha256, compute_operation_plan_sha256,
    install_journal_recovery_api, sha256_bytes,
)
from .outbox_journal import install_journal_outbox_api
from .service_journal import install_journal_service_api
from .workspace_lifecycle_journal import install_journal_workspace_lifecycle_api
from .repository_index_journal import install_journal_repository_index_api
from .code_relationship_journal import install_journal_code_relationship_api
from .multi_file_patch_journal import (
    compute_multi_file_checkpoint_sha256,
    install_journal_multi_file_patch_api,
)
from .multi_file_patch_hardening import (
    install_journal_multi_file_patch_hardening,
    install_multi_file_patch_executor_hardening,
)
from .multi_file_patch_temp_identity import install_multi_file_patch_temp_identity
from .multi_file_patch_runtime_journal import install_journal_multi_file_patch_runtime_api
from .multi_file_patch_gate import install_multi_file_patch_command_gate

install_journal_recovery_api(Journal)
install_journal_outbox_api(Journal)
install_journal_service_api(Journal)
install_journal_workspace_lifecycle_api(Journal)
install_journal_repository_index_api(Journal)
install_journal_code_relationship_api(Journal)
install_journal_multi_file_patch_api(Journal)
install_journal_multi_file_patch_hardening(Journal)
install_journal_multi_file_patch_runtime_api(Journal)
install_multi_file_patch_command_gate()

from .workspace_manager import WorkspaceManager
from .instance_lock import InstanceLock
from .service_status import ServiceStatusReader, is_pid_alive
from .heartbeat import HeartbeatWorker
from .service import BridgeService
from .execution import ExecutionCoordinator, RecoveryAssessment
from .protocol import (
    BridgeError, COMMAND_PATH_RE, MANIFEST_PATH_RE, SCHEMA_VERSION, SESSION_RE,
    command_id_for, command_path_for, manifest_path_for, parse_command_path,
    parse_manifest_path, parse_git_ref, sanitize_diagnostics, path_matches,
    require_int, require_string, result_path_for, validate_base_sha,
    validate_path_pattern, validate_repo_relative_path, validate_session_id,
)
from .result_outbox import OutboxProcessor, ResultCoordinator
from .result_staging import EXECUTOR_VERSION, ResultBuildInput, ResultStager
from .result_transport import GitResultTransport, ResultTransport
from .scheduler import SingleQueueScheduler
from .serializers import MAX_RESULT_BYTES, MAX_TAIL_CHARS, canonical_json, finalize_result, sha256_text, tail
from .transport import CommandSnapshot, CommandTransport, RemoteDocument
from .git_command_transport import GitCommandTransport
from .workspace_types import (
    WorkspaceCleanupOutcome, WorkspaceDisposition, WorkspaceEligibility,
    WorkspaceLifecycleRecord, WorkspaceLifecycleState, WorkspaceStatusSnapshot,
)
from .workspace_lifecycle import WorkspaceLifecycleCoordinator
from .workspace_lifecycle_errors import install_workspace_lifecycle_error_mapping
from .session_finalization import SessionFinalizationOutcome, SessionFinalizer
from .code_relationship_models import (
    ANALYSIS_VERSION, AnalysisImport, AnalysisPersistOutcome, Confidence,
    DependencyEdge, EdgeKind, ImportKind, ReferenceKind, RepositoryAnalysis,
    ResolutionStatus, SearchResult, SymbolReference,
)
from .code_relationship_service import RepositoryRelationshipService
from .multi_file_patch_executor import MultiFilePatchExecutor
from .multi_file_patch_recovery_models import (
    MultiFileCheckpointBundle, MultiFileCheckpointPath, MultiFileCheckpointRecord,
    MultiFileCheckpointState, MultiFileRecoveryOutcome,
)
from .multi_file_patch_runtime_models import (
    MultiFilePatchProfileRecord, MultiFilePatchRuntimeResult,
)
from .multi_file_patch_runtime import MultiFilePatchRuntimeCoordinator
from .multi_file_patch_result import install_multi_file_patch_result_support
from .multi_file_patch_checkpoint_hook import (
    install_multi_file_patch_checkpoint_hook_boundary,
)
from .multi_file_patch_lifecycle import (
    install_multi_file_patch_lifecycle_bootstrap,
)

install_multi_file_patch_executor_hardening(MultiFilePatchExecutor)
install_multi_file_patch_temp_identity(MultiFilePatchExecutor)
install_multi_file_patch_checkpoint_hook_boundary(MultiFilePatchExecutor)
install_multi_file_patch_lifecycle_bootstrap(MultiFilePatchRuntimeCoordinator)
install_multi_file_patch_result_support(ResultCoordinator)
install_workspace_lifecycle_error_mapping(WorkspaceLifecycleCoordinator)

__all__ = [
    "ANALYSIS_VERSION", "AnalysisImport", "AnalysisPersistOutcome",
    "BridgeConfig", "BridgeError", "BridgeErrorCode", "BridgeService", "COMMAND_PATH_RE",
    "CommandIngestor", "CommandIngestionRecord", "CommandRecord", "CommandSnapshot",
    "CommandState", "CommandTransport", "Confidence", "DependencyEdge", "EdgeKind",
    "ExecutionCoordinator", "ExecutionOutcome", "EXECUTOR_VERSION", "GitResultTransport",
    "ImportKind", "IngestionIssue", "IngestionReport", "GitCommandTransport", "InstanceLock",
    "Journal", "JournalEvent", "MANIFEST_PATH_RE", "MAX_RESULT_BYTES", "MAX_TAIL_CHARS",
    "MultiFileCheckpointBundle", "MultiFileCheckpointPath", "MultiFileCheckpointRecord",
    "MultiFileCheckpointState", "MultiFilePatchExecutor", "MultiFileRecoveryOutcome",
    "MultiFilePatchProfileRecord", "MultiFilePatchRuntimeResult",
    "MultiFilePatchRuntimeCoordinator",
    "Operation", "OperationEffectRecord", "OperationPlanRecord", "OutboxProcessOutcome",
    "OutboxProcessState", "OutboxProcessor", "OutboxRecord", "OutboxState", "PollReport",
    "ProfileRunOutcome", "PromotionOutcome", "PublishAttempt", "PublishAttemptState",
    "RecoveryAssessment", "RecoveryDecision", "ReferenceKind", "RemoteDocument", "RemoteResult",
    "RemoteResultState", "RepositoryAnalysis", "RepositoryRelationshipService",
    "ResolutionStatus", "ResultBuildInput", "ResultCoordinator", "ResultCoordinationOutcome",
    "ResultRecord", "ResultStager", "ResultStatus", "ResultTransport", "SCHEMA_VERSION",
    "SESSION_RE", "SearchResult", "SessionIngestionRecord", "SessionRecord", "SessionState",
    "SingleQueueScheduler", "StagedResult", "SymbolReference", "TransportRetryRecord",
    "WorkspaceManager", "WorkspaceRecord", "ServiceInstanceState", "ServiceStatus",
    "ServiceInstanceRecord", "ServiceStatusSnapshot", "ServiceStatusReader", "is_pid_alive",
    "HeartbeatWorker", "BridgeCycleReport", "ServiceRunOutcome", "StopRequestOutcome",
    "BackgroundStartOutcome", "WorkspaceCleanupOutcome", "WorkspaceDisposition",
    "WorkspaceEligibility", "WorkspaceLifecycleRecord", "WorkspaceLifecycleState",
    "WorkspaceStatusSnapshot", "WorkspaceLifecycleCoordinator", "SessionFinalizationOutcome",
    "SessionFinalizer", "canonical_json", "command_id_for", "command_path_for",
    "compute_multi_file_checkpoint_sha256", "compute_operation_effect_sha256",
    "compute_operation_plan_sha256", "finalize_result", "install_journal_outbox_api",
    "install_journal_workspace_lifecycle_api", "install_multi_file_patch_migration",
    "install_multi_file_patch_runtime_migration", "install_journal_multi_file_patch_hardening",
    "install_multi_file_patch_executor_hardening", "install_multi_file_patch_temp_identity",
    "install_multi_file_patch_checkpoint_hook_boundary",
    "install_multi_file_patch_lifecycle_bootstrap",
    "install_journal_multi_file_patch_runtime_api", "install_multi_file_patch_command_gate",
    "install_multi_file_patch_result_support", "install_workspace_lifecycle_migration",
    "manifest_path_for", "parse_command_path", "parse_manifest_path", "path_matches",
    "require_int", "require_string", "result_path_for", "sha256_bytes", "sha256_text",
    "tail", "validate_base_sha", "validate_path_pattern", "validate_repo_relative_path",
    "validate_session_id", "parse_git_ref", "sanitize_diagnostics",
]

from .ghb07_cli import install_cli
install_cli()
