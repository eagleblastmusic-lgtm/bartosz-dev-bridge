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

## Atomic publication

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

## Configuration

The lane is enabled by default:

```json
{
  "direct_spool_enabled": true
}
```

The default inbox is:

```text
<runtime_dir>/direct_spool/inbox
```

A custom `direct_spool_dir` must remain inside `runtime_dir`.

## Operator commands

```powershell
bdb bridge local submit `
  --config C:\path\to\config.json `
  --envelope C:\path\to\action.json `
  --filename action-000001.json

bdb bridge local status --config C:\path\to\config.json --json
```

This stage does not yet automate browser integration. Native Messaging and the browser extension must consume this contract rather than introducing a second execution path.
