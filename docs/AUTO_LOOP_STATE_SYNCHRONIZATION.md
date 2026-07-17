# AUTO loop state synchronization

## Problem

The original browser AUTO state was stored under:

```text
bdbAuto:<tabId>:<loop_id>
```

That made the iteration counter depend on the sender tab identity. A later ChatGPT turn could carry the same `loop_id` and the correct next iteration, but after a tab identity change the service worker loaded an empty state with `lastIteration = 0`. Iteration 2 was then rejected as `non_sequential_iteration` even though iteration 1 had completed.

## Current contract

The canonical state key is now:

```text
bdbAuto:<loop_id>
```

The loop identity, not the transient browser tab, owns:

- `startedAt`;
- `lastIteration`;
- `status`;
- `lastCommandId`;
- `updatedAt`.

`lastTabId` is diagnostic metadata only. Changing or refreshing a tab does not reset the loop counter.

The state remains in `chrome.storage.session`, so it survives MV3 service-worker restarts in the same browser session without becoming permanent cross-session authority.

## Legacy migration

On the next AUTO action the entry worker scans session storage for old keys ending in the same `loop_id`.

If a canonical state does not exist, it conservatively selects the legacy state with:

1. the highest completed iteration;
2. then the newest timestamp.

It writes that state to the canonical key and removes the obsolete tab-scoped keys. A terminal legacy status remains terminal.

## Replay and concurrency safety

This change does not replace or weaken `claimAutoReplay`.

The durable replay guard remains in `chrome.storage.local` and is still keyed by:

```text
<loop_id>:<iteration>
```

Consequences:

- the same iteration cannot execute twice after a rerender;
- two tabs cannot claim the same iteration;
- a higher iteration cannot start until the shared loop state records the previous one;
- a fresh `loop_id` starts independently;
- existing Native Host, allowlist, profile, rollback and promotion gates are unchanged.

A repeated already-completed iteration is reported as `iteration_already_processed`. A genuine gap remains `non_sequential_iteration`.

Every AUTO decision returns `expectedIteration`. The extension popup displays the latest loop, status, completed iteration and expected next iteration.

## Runtime regression test

`tests/test_browser_auto_loop_runtime.py` executes the actual MV3 worker through a Node VM with stubbed Chrome storage and Native Messaging. It covers:

1. iteration 1 in tab A;
2. service-worker restart;
3. iteration 2 in tab B;
4. another restart and iteration 3 in tab C;
5. rerender of iteration 3 without a second native request;
6. a fresh loop starting at iteration 1.

The test first failed on the old implementation with the second action rejected. The fixed worker must pass this scenario on every CI platform.

## Manual verification

After updating the unpacked extension:

1. reload the extension in the browser;
2. enable AUTO in the popup;
3. start a new loop with a unique `loop_id` and iteration 1;
4. allow the result to create the next ChatGPT turn;
5. verify that iteration 2 runs without an ASSISTED click;
6. inspect the popup and confirm the displayed expected iteration;
7. refresh the ChatGPT tab before a later iteration and confirm continuation;
8. confirm that rerendering an old action does not send a second Native Messaging request.

The real Local Workspace Loop acceptance test still requires one initial user message, automatic context, mutation, tests, promotion receipt and final response without manual interaction.
