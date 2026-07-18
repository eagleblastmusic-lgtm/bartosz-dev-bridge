# AUTO loop composer reconciliation — 0.2.5

## Acceptance finding

The strict 0.2.4 browser acceptance run completed the local operation successfully:

- workspace context was read;
- `README.md` was changed in an isolated worktree;
- the allowlisted pytest profile passed (`7 passed`);
- the checkpoint was committed;
- the result was promoted by verified fast-forward;
- the source checkout was clean and the promotion receipt matched the command.

The loop still required a manual click at the final continuation step. The visible composer already contained the exact `BDB_AUTO_RESULT` payload, while the action panel reported:

```text
BDB AUTO → ASSISTED (composer_unavailable_or_not_empty)
```

## Root cause

`content_auto_send.js` validated the same composer object immediately after dispatching the input event. ChatGPT can reconcile its React tree and replace the contenteditable node after insertion. The payload may therefore be visible in the new live composer while the extension still inspects the detached predecessor.

## 0.2.5 behavior

AUTO now:

1. distinguishes a missing composer from a non-empty user draft;
2. inserts only into an initially empty composer;
3. repeatedly reacquires the current live composer;
4. waits until the exact marker is observable in that live node;
5. resolves the send button relative to the reacquired composer;
6. retries bounded clicks only while the exact marker remains present;
7. reports separate reasons for missing, occupied, unobserved, lost, or unconfirmed composer states.

The runtime regression replaces the composer object immediately after insertion and requires AUTO to reacquire the replacement before sending.

## Loop identity

A runtime contract verifies exact character-for-character preservation of an identifier containing digits, hyphens, underscores, dots, and a colon across:

- `automationMetadata`;
- canonical session-state keys;
- replay keys;
- `BDB_AUTO_RESULT` markers.

No loop identifier normalization or replacement is permitted inside the extension.

## Safe STALE recovery

The workspace operator previously called `bridge stop` and then always waited for `OFFLINE`. A dead Bridge with a free lock and an abandoned active Journal row reports `STALE`, so no living process could complete that transition.

For the exact safe condition:

```text
status = STALE
lock_held = false
pid_alive = false
```

`Start` and `Stop` now use the existing service recovery path:

1. start a temporary background Bridge instance;
2. let service startup mark the abandoned instance stale;
3. reach `RUNNING` with a new instance;
4. for `Stop`, request graceful stop and wait for `OFFLINE`;
5. preserve Journal, worktrees, results, receipts, and logs.

The operator still refuses ambiguous STALE states and does not use `Stop-Process`, `taskkill`, `git reset`, `git clean`, recursive deletion, or worktree pruning.

## Strict acceptance criterion

0.2.5 is accepted only when one initial user message completes context, mutation, tests, promotion, receipt verification, and final ChatGPT continuation without a manual click, manual Enter, or page refresh after the run begins.
