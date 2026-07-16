# GHB2-C — Durable batch apply, rollback and recovery

## Status and boundary

GHB2-C adds the durable execution substrate for a previously validated `bdb-multi-file-patch-v1` plan. It does not expose that operation to remote command ingestion or the live execution coordinator. Runtime activation, test-profile orchestration, result staging and the final editing gate belong to the separate GHB2-D stage.

The implementation operates on one existing Bridge-owned session worktree and requires the caller to own the canonical Bridge instance lock.

## Safety preconditions

Every checkpoint, apply, rollback, commit and recovery operation requires all of the following:

- the canonical `<runtime_dir>/bridge.instance.lock` is acquired by the exact `InstanceLock` object passed to the executor;
- the workspace Journal record exists and matches the active `WorkspaceManager` identity;
- `workspace_lifecycle` exists with `disposition=preserve` and `state=preserved`;
- the plan revalidates against the current physical workspace;
- the physical state hash matches the durable workspace state before a new checkpoint;
- all paths remain inside the manifest scope and pass the existing sensitive/internal-path rules.

The lock serializes cooperating Bridge processes. A non-cooperating external process can still modify a file; therefore every mutation rechecks the expected bytes immediately before promotion or deletion and verifies the final bytes afterwards. Unexpected data fails closed and requires manual reconciliation.

## Journal v9

Migration `journal_v9_multi_file_patch_recovery` adds:

- `multi_file_patch_checkpoints`;
- `multi_file_patch_checkpoint_paths`.

Literal migration checksum:

```text
ff7019381e0c16588fc4871d0041bd44d08a74ee2dfe3f1387274f8715be3af3
```

Versions v1 through v8 remain byte-for-byte frozen.

A checkpoint stores the immutable plan identity, the exact workspace revision/state hash before execution, the predicted state hash after execution and exact before/after bytes for every changed path. Header identity and path rows are protected by SQLite triggers. Checkpoint deletion is not exposed and is rejected by the schema.

The Journal independently enforces the same bounded-data policy as the planner:

- at most 200 changed paths;
- at most 1 MiB for each before or after file image;
- at most 16 MiB for all before and after bytes combined;
- bounded path, roles, operation-index and diagnostic fields.

Persisted malformed states, JSON, hashes, counts, byte totals or checkpoint hashes are mapped to `JOURNAL_CORRUPT`; raw `ValueError`, `TypeError` and `JSONDecodeError` do not escape the Journal boundary.

## State machine

Normal completion:

```text
planned -> applying -> applied -> committed
```

Rollback:

```text
planned | applying | applied -> rolling_back -> rolled_back
```

Unexpected physical data:

```text
non-terminal state -> blocked
```

`blocked`, `rolled_back` and `committed` are terminal for this checkpoint. A blocked workspace is preserved for operator inspection.

## Revision rule

Physical `apply()` does not advance the workspace revision.

Only `applied -> committed` performs the Journal transaction that:

1. compares the durable workspace revision and state hash with the checkpoint BEFORE identity;
2. advances the workspace revision by exactly one;
3. writes the checkpoint AFTER state hash;
4. transitions the checkpoint to `committed`;
5. appends the commit event;
6. lets the existing workspace-lifecycle trigger synchronize the preserved lifecycle identity.

The transaction is idempotent. Repeating `commit()` after success returns the committed record without a second revision increase.

Rollback restores BEFORE bytes while the durable workspace revision remains unchanged.

## Atomic file operations

For each write, the executor:

1. classifies the target as exact BEFORE, exact AFTER or unexpected;
2. creates a deterministic Bridge-internal temporary file with `xb`;
3. writes and `fsync`s the exact target bytes;
4. rereads the temporary bytes;
5. rechecks the destination immediately before promotion;
6. uses `os.link` for an absent destination or `os.replace` for an existing destination;
7. performs best-effort parent-directory `fsync` through the existing workspace helper;
8. rereads the promoted target and verifies exact bytes.

Deletion verifies exact expected bytes immediately before `unlink`, performs parent-directory `fsync` and verifies absence.

The bounded temporary name is derived from the command identity, canonical path identity, ordinal and direction. It does not embed the target filename and does not depend on mutable workspace contents.

## Temporary-file ownership

Before creating a new checkpoint, the executor computes all possible apply/rollback temporary paths. Any pre-existing file or symlink at one of those internal paths is treated as a collision and is never adopted, overwritten or deleted.

After a checkpoint exists, an exact temporary file created by that checkpoint may be reused after a crash only when its bytes match the persisted target bytes. A different file, symlink or byte sequence blocks the checkpoint and preserves the workspace for manual reconciliation.

## Recovery

Recovery is scoped to the executor session. `recover_all()` queries only incomplete checkpoints whose `session_id` matches the active workspace and never applies a checkpoint through another session's worktree.

Recovery decisions are idempotent:

| Durable state | Physical expectation | Action |
|---|---|---|
| `planned` | all paths BEFORE | report ready to apply |
| `applying` | mix of BEFORE/AFTER plus owned exact temps | continue apply |
| `applied` | all paths AFTER | await commit or rollback |
| `rolling_back` | mix of BEFORE/AFTER plus owned exact temps | continue rollback |
| `rolled_back` | all paths BEFORE | no-op success |
| `committed` | all paths AFTER and workspace CAS committed | no-op success |
| any state with unexpected data | neither exact BEFORE nor AFTER | block and require reconciliation |

Covered crash boundaries include:

- after the durable checkpoint;
- after entering applying;
- after writing and syncing a temporary file;
- after promoting an individual path;
- before recording applied;
- after entering rolling back;
- after restoring/removing an individual path;
- before recording rolled back.

## Windows behavior

The final CI matrix validates Python 3.11 and 3.12 on Ubuntu and Windows. Windows coverage includes:

- instance-lock ownership;
- `os.link` for no-overwrite creation;
- `os.replace` for replacement;
- open-handle cleanup;
- file and parent-directory sync behavior;
- deterministic temporary cleanup;
- Unicode-capable repository paths;
- deletion and Journal/worktree reopen recovery.

Parent-directory `fsync` remains best-effort where the operating system does not support directory handles. File-content verification remains mandatory.

## Manual reconciliation

The executor fails closed with `MANUAL_RECONCILIATION_REQUIRED` when it encounters, among other cases:

- a pre-existing internal temporary collision;
- an unexpected target or temporary byte sequence;
- a symlink/reparse-point substitution;
- a missing parent directory;
- a checkpoint belonging to another session;
- lifecycle identity drift or cleanup disposition;
- external changes during apply, rollback or commit verification.

No automatic cleanup, reset, checkout-based discard or source-worktree mutation is performed.

## GHB2-D exclusions

GHB2-C deliberately does not add:

- a remotely accepted `multi_file_patch` command;
- ingestion or schema routing for that command;
- `ExecutionCoordinator` activation;
- automatic test-profile execution after apply;
- rollback policy for profile failures;
- result staging/publication for multi-file edits;
- startup service orchestration for this executor;
- operator CLI for live batch execution;
- a final remote editing safety gate.

Those changes require a new branch from the post-GHB2-C `main` checkpoint and belong exclusively to GHB2-D.
