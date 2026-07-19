# ADR 0017: Bounded session history without inferred repair links

- Status: Accepted
- Date: 2026-07-19

## Context

Control Center needs readable history of completed sessions, durable results, checkpoints and promotion receipts. The current runtime does not store a generic durable identifier linking one failed session to a separate later repair session. Inferring that relationship from time, alias, filenames or ordering could create a false audit history.

## Decision

1. Add bounded, read-only `OperatorApi.sessions(workspace_root, limit)`.
2. Open SQLite only with `mode=ro` and `PRAGMA query_only=ON`.
3. Read results and receipts only from canonical roots declared by project configuration.
4. Enforce limits for sessions, attempts and file bytes.
5. Reject symlinks, path escapes, irregular files and unsupported schemas.
6. Validate receipt identity, result SHA-256, changed files and Git commits.
7. Keep sessions visible when evidence is missing or invalid, with a warning.
8. Open validated local artifacts only after an explicit user click.
9. Present sessions independently with `repair_relationships_inferred=false`.
10. Reject GUI payloads that claim inferred cross-session repair links.

## Consequences

Users can inspect completed work without raw SQLite or manual receipt discovery. The GUI stays a thin read-only layer and corrupted evidence remains visible. The limitation is that Control Center cannot yet render one combined failed-session → repair-session timeline.

## Rejected alternatives

- Time-based or filename-based matching: not audit-safe.
- Runtime-wide JSON scanning: scope is too broad.
- Automatic opening of local paths: not explicit.
- Storing guessed links in GUI state: GUI is not execution truth.

## Follow-up

A later versioned contract may add a durable correlation ID written by the execution layer. Only that explicit ID may enable a combined cross-session repair timeline.
