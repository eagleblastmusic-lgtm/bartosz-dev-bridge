# Source commit latency and incremental command snapshots

## Why the metric changed

A command envelope contains a user-supplied `created_at` timestamp. That timestamp can precede the actual Git commit by an arbitrary amount of time, so it must not be interpreted as transport latency.

`bridge edit status --json` now reports both concepts:

- `document_created_at`: the timestamp declared inside the command document;
- `source_commit_at`: the committer timestamp of the Git object referenced by `document_commit_sha`;
- `first_seen_at`: the durable local ingestion timestamp.

The existing fields `remote_created_at`, `inbound_transport_ms`, and `end_to_end_ms` remain for compatibility. Their semantics are unchanged and are document-time based.

The preferred transport measurements are:

- `source_commit_to_first_seen_ms`;
- `source_commit_to_result_ms`.

If the source commit object is unavailable locally, source-commit fields are `null` and status remains readable.

## Incremental snapshot cache

`GitCommandTransport` still performs a read-only `git fetch` on every due poll.

After the fetch:

1. an unchanged commands SHA returns the immutable in-memory snapshot without rereading documents;
2. a fast-forward reads the name-status diff and refreshes only added, modified, type-changed, or deleted command/manifest paths;
3. a non-fast-forward performs a complete snapshot rebuild;
4. an incremental decoding or transport error falls back to the complete snapshot path where safe;
5. all document content remains pinned to one resolved snapshot SHA.

The cache is process-local and is intentionally discarded on service restart. The first poll after restart performs a complete read.

## Safety properties

- no schema migration;
- no branch checkout or working-tree mutation;
- no arbitrary shell;
- `subprocess` remains `shell=False`;
- no parallel workers;
- transport failure still uses the existing durable retry/backoff;
- cached content is reused only when the immutable snapshot SHA is unchanged;
- non-fast-forward movement never trusts the incremental cache.
