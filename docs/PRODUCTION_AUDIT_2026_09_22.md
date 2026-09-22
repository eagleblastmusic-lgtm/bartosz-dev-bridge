# Production audit — 2026-09-22

Work in progress. Verdict: NOT_PRODUCTION_READY. No synthetic Premium Calculator result is permitted.

## Baseline and evidence

- Fetched origin before changes: `2d5925a23909820d7e163d82086141a0d4f4beed`, merged PR #135. No open AUTO delivery PR.
- Branch: `codex/production-audit-auto-hardening`.
- Installed client and M9b source: `b3cb1407d46d666f526bb7a48a5931966d166731`; Bootstrap activation `m11c-maint-m11c-b3cb140-20260920`. Source HEAD is not deployment evidence.
- Real project `0c62f1b8-2ce1-48d3-bae9-c3c32b9a84b6`: P3 / P3-03, binding `binding-7e6cce3a424341b98ad173d0e3436397`, launch `cad1032b-2164-41cd-908d-28dd29dbfbc3`, conversation `6aadb22f-d5b8-83eb-b470-335b0b5ba593`. Outbox ACKNOWLEDGED with auto_send=false; handoff PENDING; binding ACTIVE.

## Requirements, implementation, verification map

| Area | Classification | Finding / required delta | Evidence |
| --- | --- | --- | --- |
| Existing continue synchronization | ALREADY_CLOSED in source only | PR #135 calls ensure_auto_current_launch; not installed | Source read and installed manifests |
| Rearm transaction | DELTA_REQUIRED | EXECUTION_LAUNCH_REARMED was absent from event allowlist; transaction always rolled back | Original regression failed with event_type_invalid |
| Manual-to-AUTO recovery | DELTA_REQUIRED | Rearm preserved auto_send=false; AUTO poller ignores it | Extended integration regression |
| Queue recovery | DELTA_REQUIRED | Only PENDING recovered; expired PUBLISHED projection disappeared | Restart/expiry regression |
| Orphan handling | DELTA_REQUIRED | Read errors caused destructive orphan classification; clear raced claims/replacement | Authority failure and queue CAS regressions |
| Native claim | DELTA_REQUIRED | Conversation written before acquiring transport lease | Framed Native process restart regression |
| AUTO ACK | DELTA_REQUIRED | AUTO ACK without confirmed-send metadata allowed | Framed Native process restart regression |
| GUI start / STOP | DELTA_REQUIRED | Swallowed synchronization errors | Real GUI click and injected disk-write failure regression |
| Tests / CI | DELTA_REQUIRED | PR #135 GUI fixtures crash on missing memory.attention; portable CI excludes these tests | Local test failures; CI workflow read |
| Browser send/restart | Under audit | Actual vNext bundle must be tested; PR #135 changed legacy browser_extension files | Source identity verified |
| Deployment / real E2E | Pending | Canonical maintenance, ACTIVE/PREVIOUS, installed Chrome, real composer and result | No PASS claimed |

All local test fixtures are isolated from production memory. Framed Native Messaging process tests prove IPC behavior, not installed Chrome delivery. DOM harnesses do not prove real composer behavior.

## Additional verified deltas

- Next AUTO binding inherits canonical conversation from the prior accepted binding; automatic polling cannot choose an unbound conversation from tab visibility.
- Browser manual ACK cache no longer suppresses a re-armed AUTO prompt. Uncertain AUTO ACK still fails closed; SENT recovery does not repopulate the composer.
- Native checks exact outbox prompt and auto_send bytes, rejects AUTO ACK without SENT metadata, and exposes durable v2 STOP after a partial GUI operation.
- Native result composition now uses the same existing ResilientProjectWorkflow as the GUI so accepted repository alignment is enforced before a next launch.
- CI now installs GUI and Node dependencies and runs real GUI orchestration, Native subprocess IPC, outbox recovery and vNext browser harnesses on Windows and Linux.

## Verification and limits

Initial broad run: 222 passed, 6 failed. Five failures were source-bound gates requiring a committed clean worktree; one fixture incorrectly requested a mutation after loading an OFF/read-only snapshot. The fixture now models admitted production state; source-bound gates must be rerun after commit. Subsequent focused milestone, result and Browser run: 48 passed. Native restart/ownership/tampered-payload/partial-STOP regression: passed.

Computer Use refused the Chrome observation because it could not confidently determine the current browser URL. No UI bypass was attempted. Real Chrome E2E and production acceptance remain unverified. Canonical maintenance requires a real Browser byte-verification witness; this must not be forged from a Python test or a source manifest. Premium Calculator Memory has not been modified by this audit.
