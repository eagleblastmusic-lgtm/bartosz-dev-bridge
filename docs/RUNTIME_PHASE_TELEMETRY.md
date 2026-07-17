# Runtime phase telemetry

This document defines the diagnostic timing fields exposed by:

```text
bridge edit status --command-id <session-id>:<sequence> --json
```

The implementation reads timestamps that the Bridge already stores durably. It does not add a new database migration, change command execution order, weaken policy checks, or add telemetry writes to the runtime path.

## Phase boundaries

For multi-file patch commands the following existing durable records are used:

- `command.claimed`
- `multi_file_patch.checkpoint_recorded`
- `multi_file_patch.applying`
- `multi_file_patch.applied`
- profile `started_at` and `finished_at`
- `multi_file_patch.profile_recorded`
- `multi_file_patch.execution_recorded`
- result `created_at`
- outbox `published_at`

The status payload adds these timestamps when available:

- `checkpoint_recorded_at`
- `patch_applying_at`
- `patch_applied_at`
- `profile_recorded_at`
- `execution_recorded_at`

Historical or non-multi-file commands may report `null` for phase fields that do not exist.

## Durations

- `workspace_and_plan_checkpoint_ms`: claim to durable checkpoint creation. This intentionally remains a combined bucket because the current journal has no command-specific durable boundary between workspace attachment and planning.
- `checkpoint_activation_ms`: checkpoint creation to the durable `applying` transition.
- `patch_apply_ms`: durable `applying` to durable `applied`.
- `profile_startup_ms`: durable `applied` to profile start.
- `execution_ms`: profile start to profile finish.
- `profile_recording_ms`: profile finish to durable profile record.
- `checkpoint_finalize_ms`: durable profile record to durable execution record.
- `result_build_and_stage_ms`: durable execution record to durable staged result.
- `runtime_to_stage_ms`: command claim to durable staged result.

Compatibility fields remain unchanged:

- `pre_execution_ms` is still claim to profile start.
- `result_staging_ms` is still profile finish to staged result.
- `inbound_transport_ms` and `end_to_end_ms` retain their document-time semantics.
- source-commit metrics retain the semantics introduced in PR #23.

## Invalid or partial timelines

A duration is `null` when either boundary is missing, invalid, or out of order. Timing is diagnostic and must not make command status unavailable when an optional multi-file extension record cannot be read.

## Safety properties

- no schema migration;
- no arbitrary shell;
- no new runtime subprocesses;
- no new runtime journal writes;
- no parallel worker;
- no cleanup;
- no policy or allowlist changes;
- no changes to recovery, checkpoint, staging, or publication behavior.
