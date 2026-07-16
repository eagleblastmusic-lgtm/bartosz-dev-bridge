# GHB2-D — Final multi-file editing gate

## Purpose

GHB2-D is the first stage that exposes the previously planned and durable `bdb-multi-file-patch-v1` operation to the normal Bridge command lifecycle.

It connects:

```text
Git command ingestion
  -> single queue claim
  -> immutable multi-file plan
  -> Journal v9 checkpoint
  -> crash-recoverable physical apply
  -> bounded test profile
  -> commit or rollback
  -> durable Journal v10 profile outcome
  -> immutable result staging
  -> durable outbox publication
```

GHB2-D preserves the existing `replace_exact_and_test` path. The dispatcher selects the dedicated multi-file runtime only when the durable command operation is exactly `multi_file_patch`.

## Command contract

The command envelope keeps schema version `1.1` and the normal session, sequence, expiry, revision and state-hash fields.

The operation is:

```json
{
  "operation": "multi_file_patch",
  "expected_revision": 0,
  "expected_state_hash": "sha256:<64 lowercase hex>",
  "payload": {
    "profile_id": "poc_pytest",
    "patch": {
      "schema": "bdb-multi-file-patch-v1",
      "operations": []
    }
  }
}
```

The final gate requires payload keys to be exactly:

```text
profile_id
patch
```

The only enabled profile is `poc_pytest`. Arbitrary profile IDs, extra keys, unsupported schemas, unsafe paths, sensitive paths, internal `.bdb_*` paths, alias collisions and out-of-scope operations fail before execution.

The patch may contain the canonical operations implemented in GHB2-A and GHB2-B:

- exact file replacement;
- create;
- delete;
- rename;
- move;
- mixed multi-file batches.

All per-file and batch limits remain enforced by both the planner and the durable Journal checkpoint.

## Exclusive ownership

Every mutating or recovery operation requires the exact process-owned canonical lock:

```text
<runtime_dir>/bridge.instance.lock
```

The same `InstanceLock` object acquired by the foreground/background service composition root is passed to `ResultCoordinator`, the GHB2-D runtime and the GHB2-C executor.

The workspace must have a durable lifecycle record with:

```text
disposition = preserve
state = preserved
```

A fresh Bridge-owned worktree receives this default record once. Existing operator disposition is never overwritten.

## Durable state machines

### Command

```text
CLAIMED
  -> EXECUTING
  -> EFFECT_RECORDED
  -> RESULT_STAGED
  -> RESULT_PUBLISHED
```

Unsafe pre-plan states may terminate as:

```text
STALE_REVISION
STATE_MISMATCH
POLICY_DENIED
MANUAL_RECONCILIATION_REQUIRED
```

### Checkpoint

Success:

```text
planned -> applying -> applied -> committed
```

Profile failure:

```text
planned -> applying -> applied -> rolling_back -> rolled_back
```

Unexpected physical state:

```text
non-terminal -> blocked
```

### Workspace revision

Physical apply does not advance the durable revision.

A successful profile atomically binds:

```text
checkpoint applied -> committed
workspace revision N -> N+1
workspace state hash BEFORE -> AFTER
command EXECUTING -> EFFECT_RECORDED
```

A failed, timed-out or internally failed profile must complete rollback first. The workspace revision and durable state hash remain at BEFORE, then the command moves to `EFFECT_RECORDED` with a durable failed result.

## Journal v10

Migration:

```text
version: 10
name: journal_v10_multi_file_patch_runtime
checksum: 6ba6a3338f95ff66679025a177c7a2d95adb75901c22f724d3bddf89ce5fd0fe
```

Table:

```text
multi_file_patch_profile_runs
```

The immutable row stores:

- command and profile identity;
- status: `success`, `failed`, `timeout` or `internal_error`;
- exit code;
- bounded stdout/stderr tails;
- full strict-UTF-8 output hashes;
- duration;
- started/finished timestamps.

Update and delete are rejected by SQLite triggers. Re-recording the exact same outcome is idempotent; a different outcome for the same command is an effect collision.

Checksums v1 through v9 remain frozen.

## Profile execution

The profile runs only after every path is physically at the checkpoint AFTER state.

The default runner reuses the existing bounded profile implementation:

```text
poc_pytest
```

A crash after the process finishes but before the next stage cannot cause a second profile run once the Journal v10 row has been committed.

## Commit and rollback

### Success

Before final commit the runtime verifies:

- all checkpoint paths are exact AFTER;
- all internal temporary files are absent;
- the physical workspace state hash equals the predicted AFTER hash;
- workspace and checkpoint CAS identities still match.

Only then are workspace revision, workspace state hash, checkpoint state and command state committed atomically.

### Failure

A non-success profile causes rollback through the existing GHB2-C executor. Result staging is forbidden until the checkpoint is fully `rolled_back` and the physical workspace matches BEFORE.

The failed result reports:

```text
changed_files = []
diff = ""
rollback_performed = true
checkpoint_state = rolled_back
```

## Result staging and publication

GHB2-D uses the existing immutable result table and durable outbox. It adds a separate multi-file result validator rather than pretending that the batch has a single-file operation plan/effect.

The result is cross-checked against:

- durable command/session identity;
- Journal v9 checkpoint identity;
- Journal v10 profile outcome;
- exact terminal checkpoint state;
- workspace revision/state hashes;
- attempted and changed paths;
- strict timestamps and output hashes;
- canonical remote result path.

Publication remains fast-forward-only and idempotent. A restart after remote push but before local ACK uses the existing outbox reconciliation path.

## Startup and in-process recovery

The service still executes phases in order:

```text
recovery -> pending outbox -> ingestion -> execution -> wait
```

`get_recoverable_command()` returns the durable command in `CLAIMED`, `EXECUTING` or `EFFECT_RECORDED` and the normal `ResultCoordinator` dispatches it back into the dedicated multi-file runtime.

Recovery boundaries covered by GHB2-C/GHB2-D include:

1. after checkpoint, before command `EXECUTING`;
2. after command `EXECUTING`;
3. after temp write;
4. after any individual path promotion/deletion;
5. after full apply;
6. after durable profile outcome;
7. during rollback;
8. after rollback, before command finalization;
9. after atomic successful commit, before result build;
10. after result build, before staging;
11. after staging, before publish;
12. after remote publish, before local ACK.

The profile is not run twice after its durable row exists. Workspace revision is not increased twice after a committed checkpoint exists.

## Operator status

Read-only status:

```text
bdb bridge edit status \
  --config <path> \
  --command-id <session-uuid:sequence> \
  [--json]
```

The output includes:

- command/session/sequence;
- command state;
- checkpoint state and hash;
- revision before/after;
- profile status and ID;
- result status;
- outbox state;
- durable checkpoint error, when present.

The command does not acquire a mutation lock and does not change Journal or workspace state.

## Manual reconciliation

The runtime fails closed and preserves the worktree for operator review when it encounters:

- an unexpected target or temporary byte sequence;
- pre-existing internal temp collision;
- symlink/reparse substitution;
- workspace lifecycle drift or cleanup disposition;
- checkpoint/session mismatch;
- corrupted durable JSON, profile or result records;
- failed workspace/checkpoint CAS;
- physical hash drift before commit;
- result/outbox identity collision.

No reset, clean, stash, rebase, automatic worktree deletion or source-checkout mutation is performed.

## Explicit exclusions

GHB2-D does not add:

- arbitrary shell execution;
- arbitrary profile selection;
- multiple concurrent workers;
- parallel mutation sessions;
- HTTP/WebSocket remote control;
- automatic cleanup or retention;
- Windows Service, Scheduled Task, installer or autostart;
- mutation of the control repository outside normal command/result transport;
- direct changes to user source checkout.
