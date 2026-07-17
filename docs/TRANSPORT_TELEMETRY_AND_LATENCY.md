# Transport telemetry and latency reduction

This increment measures the durable command path and removes two avoidable waits without weakening recovery or transport backoff.

## Durable timing report

`bridge edit status --json` includes a `timing` object built from existing journal records. No schema migration is required.

Timestamps:

- `remote_created_at` — timestamp declared by the validated command envelope;
- `first_seen_at` — first durable observation in `command_ingestion`;
- `validated_at` — `command.validated` journal event;
- `claimed_at` — `command.claimed` journal event;
- `execution_started_at` and `execution_finished_at` — immutable result timestamps when available, otherwise command state events;
- `result_staged_at` — durable result creation time;
- `result_published_at` — durable outbox publication acknowledgement.

Derived durations are reported in milliseconds:

- inbound transport;
- validation;
- scheduler queue;
- pre-execution preparation;
- execution;
- result staging;
- result publication;
- full remote-created-to-published end-to-end time.

A missing phase is represented by `null`. Status therefore remains valid for in-progress commands and older durable records.

## Immediate publication

After execution and atomic result staging, `ResultCoordinator` immediately asks the existing outbox processor to publish the exact staged bytes.

The crash boundary `AFTER_STAGE_COMMIT_BEFORE_PUBLISH` remains unchanged. A crash at that point leaves the command in `result_staged`, and recovery performs publication only. Execution is not repeated.

If publication is unavailable, the normal outbox retry record and exponential backoff remain authoritative. The command stays durable in `result_staged`.

## Productive-cycle draining

The service skips `idle_poll_seconds` only after durable progress, including:

- a command or manifest was discovered, validated, rejected, or expired;
- a command finished in a state other than `result_staged`;
- a result was published or a collision was durably recorded.

The service still waits normally after:

- an unchanged transport snapshot;
- transport-unavailable ingestion;
- a staged result whose publication was scheduled for retry;
- an idle cycle.

This permits consecutive commands and session handoffs to drain without an artificial one-second pause while preserving retry backoff and preventing a busy loop.

## Non-goals

- no polling thread or parallel worker;
- no webhook or local transport in this increment;
- no arbitrary shell execution;
- no automatic cleanup;
- no business repository connection;
- no change to result collision or manual reconciliation semantics.
