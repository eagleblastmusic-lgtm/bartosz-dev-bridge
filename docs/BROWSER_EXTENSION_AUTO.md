# Browser extension — AUTO mode

AUTO is an explicit opt-in layer above the default ASSISTED mode.

## Dual authorization

An action executes automatically only when both conditions are true:

1. the extension popup has `AUTO enabled`;
2. the action contains:

```json
{
  "automation": {
    "mode": "auto",
    "loop_id": "repair-cart-001",
    "iteration": 1
  }
}
```

Without both conditions, the normal `BDB: Wykonaj` button remains.

## Limits and state

The background worker stores per-tab/per-loop state in `chrome.storage.session`:

- start time;
- last accepted iteration;
- last command ID;
- running or terminal status.

User-configured limits are bounded to 1–8 iterations and 1–30 minutes. Iterations must arrive exactly in sequence. Duplicate, skipped, expired or already terminal loops do not execute.

## Automatic continuation

After an exact durable `completed` result, and only when no terminal status is found:

1. the content script requires the ChatGPT composer to be empty;
2. it writes a unique `BDB_AUTO_RESULT:<loop_id>:<iteration>` marker and `BDB_RESULT`;
3. it requires the exact `button[data-testid='send-button']` inside the same form;
4. it verifies the marker is still present and the button is enabled;
5. it performs one click.

Any mismatch leaves the result visible and falls back to ASSISTED. The extension never searches broadly for buttons by label and never overwrites an existing draft.

## Hard stops

Recursive bounded result inspection stops AUTO for:

- `DONE`;
- `NEEDS_USER`;
- `POLICY_DENIED`;
- `MANUAL_RECONCILIATION_REQUIRED`;
- `FAILED`;
- `CANCELLED`;
- `ABORTED`.

A Native Host response other than `completed`, a time/iteration violation, missing composer, non-empty draft, missing exact send button or extension/native error also stops automatic continuation.

AUTO does not weaken Native Host ARMED TTL, repository aliases, Direct Lane policy, fixed profiles, worktree isolation, checkpoint, rollback or recovery.
