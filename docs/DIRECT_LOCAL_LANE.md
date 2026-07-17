# Direct Local Lane

The Direct Local Lane removes GitHub from the command critical path while preserving the existing Git transport as a fallback.

## Boundary

The local lane changes transport only. It does not bypass:

- manifest and command schema validation;
- local repository allowlists;
- fixed execution profiles;
- the single-worker scheduler;
- isolated Git worktrees and exact `base_sha` checks;
- checkpoint, rollback, recovery, result staging, or the durable Journal.

Local Git remains part of Bridge isolation and recovery. GitHub remains available for fallback transport, audit, CI, and final branch/PR publication.

## Envelope

A producer atomically publishes one UTF-8 JSON file using schema `bdb-local-envelope-v1`:

```json
{
  "schema": "bdb-local-envelope-v1",
  "submitted_at": "2026-07-17T03:00:00Z",
  "manifest": {
    "schema_version": "1.1",
    "session_id": "018f3f66-6cb3-4f66-9f2e-3d7647d1b701",
    "repository_id": "bdb-poc-fixture",
    "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "created_at": "2026-07-17T03:00:00Z",
    "expires_at": "2026-07-17T03:05:00Z"
  },
  "command": {
    "schema_version": "1.1",
    "session_id": "018f3f66-6cb3-4f66-9f2e-3d7647d1b701",
    "command_id": "018f3f66-6cb3-4f66-9f2e-3d7647d1b701:000001",
    "sequence": 1,
    "operation": "open_read",
    "created_at": "2026-07-17T03:00:00Z",
    "expires_at": "2026-07-17T03:05:00Z",
    "expected_revision": 0,
    "expected_state_hash": null,
    "payload": {"path": "src/clamp.py"}
  }
}
```

The outer envelope is transport framing. The inner manifest and command continue through the canonical Bridge validators.

## Atomic command publication

The supported writer performs:

1. exclusive creation of a temporary file in the inbox;
2. complete write and file `fsync`;
3. atomic `os.replace` to a safe `.json` basename;
4. directory `fsync` where supported.

The reader ignores temporary files, rejects symlinks, bounds file count and byte size, and verifies that a file did not change during reading. Files are intentionally preserved as operator evidence. Re-reading exact bytes is idempotent through the Journal.

## Priority and fallback

The service polls `local-spool` before `commands`:

1. local durable work or a local error stops the ingestion phase for that cycle;
2. an empty local snapshot falls through to the existing Git command transport;
3. each source has an independent Journal retry row, so Git backoff cannot delay local commands.

## Immediate wake

On Windows the running service owns one manual-reset named event derived from the exact resolved `runtime_dir`. After a successful atomic submit, the operator or Native Host opens that existing event and signals it.

- no TCP or HTTP listener is opened;
- no public endpoint is created;
- a submit made while Bridge is offline remains durable in the spool and normal polling recovers it later;
- the existing `idle_poll_seconds` timeout remains a fallback;
- the service keeps the single-worker invariant.

Other platforms use the existing in-process waiter for tests and development. The production cross-process wake contract is Windows Native Event.

## Durable local result

Before the unchanged Git result publication path begins, the outbox mirrors exact staged result bytes to:

```text
<direct_result_dir>/sessions/<session_id>/results/<sequence>.json
```

The mirror uses exclusive temporary creation, file `fsync`, atomic replace, strict canonical path validation and exact-byte collision detection. If the local mirror cannot be made durable, publication fails closed and the command stays `RESULT_STAGED` for recovery.

This means Native Messaging may return the local result without waiting for GitHub. GitHub publication continues as the existing audit/fallback path and retains its current retry, collision and reconciliation semantics.

## Configuration

The lane is enabled by default:

```json
{
  "direct_spool_enabled": true
}
```

Default directories:

```text
<runtime_dir>/direct_spool/inbox
<runtime_dir>/direct_spool/results
```

Custom `direct_spool_dir` and `direct_result_dir` values must be separate dedicated directories contained within `runtime_dir`.

## Operator commands

```powershell
bdb bridge local submit `
  --config C:\path\to\config.json `
  --envelope C:\path\to\action.json `
  --filename action-000001.json

bdb bridge local status --config C:\path\to\config.json --json
```

The submit response reports whether the running Windows named event was signaled. A false value does not invalidate the durable submit; it means polling or a later service start will process it.

Native Messaging and the browser extension must consume this contract rather than introducing a second execution path.
