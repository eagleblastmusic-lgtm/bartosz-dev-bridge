# AUTO loop state synchronization

## Problem 1: tab-scoped loop state

The original browser AUTO state was stored under:

```text
bdbAuto:<tabId>:<loop_id>
```

That made the iteration counter depend on the sender tab identity. A later ChatGPT turn could carry the same `loop_id` and the correct next iteration, but after a tab identity change the service worker loaded an empty state with `lastIteration = 0`. Iteration 2 was then rejected as `non_sequential_iteration` even though iteration 1 had completed.

## Current state contract

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

On the next AUTO action the entry worker scans session storage for exact old keys using the form:

```text
bdbAuto:<numeric-tabId>:<loop_id>
```

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

## Problem 2: ChatGPT rerender removes the panel

ChatGPT may reconcile an assistant message by removing extension-owned DOM children while preserving the original `<code>` element. The original content script remembered processed code nodes in a `WeakSet`. If the `.bdb-assisted` panel disappeared but the same code node remained, later scans skipped that node permanently. Reloading the page created a new code node and only masked the problem.

Extension version `0.2.2` loads `content_rerender.js` after the mature `content.js` scanner. Before each scan, the reconciliation layer checks remembered code nodes against the live DOM:

- when the direct BDB panel still exists, nothing changes;
- when the panel disappeared, only that code node is removed from `processedBlocks`;
- the existing scanner then recreates the panel and calls the background AUTO decision again;
- the durable replay guard remains the authority preventing duplicate execution.

The reconciliation layer never submits Native Messaging actions directly. A focused `MutationObserver` watches child removals only to rescan the affected live element; all parsing, execution decisions and replay claims remain delegated to the original scanner and background worker.

## Problem 3: streaming local scan finishes too early

A strict local acceptance run on `0.2.2` proved a second failure mode. The popup correctly retained `lastIteration = 1` and `expectedIteration = 2`, while the completed iteration-2 JSON remained unenhanced in the ChatGPT page. This showed that the background state and replay guard were healthy, but the content script never submitted the second action.

During streaming, an immediate local scan can observe a code block while its JSON is still incomplete. Parsing correctly fails closed. If later React mutations do not produce a useful scan root containing that same block, the now-complete action can remain plain JSON indefinitely.

Extension version `0.2.3` adds a bounded settle-reconciliation path:

- code-related character or child-list mutations schedule one delayed reconciliation;
- the delayed callback scans the whole document after 200 ms instead of relying on the original local mutation root;
- continuous streaming can schedule later bounded scans, so the final complete JSON is eventually observed;
- the scanner, AUTO decision and Native Messaging submission remain the mature implementations in `content.js` and the background worker;
- the companion script still sends no runtime or Native Messaging request directly;
- duplicate execution remains blocked by the durable `<loop_id>:<iteration>` replay claim.

## Runtime regression tests

`tests/test_browser_auto_loop_runtime.py` executes the actual MV3 worker through a Node VM with stubbed Chrome storage and Native Messaging. It covers:

1. iteration 1 in tab A;
2. service-worker restart;
3. iteration 2 in tab B;
4. another restart and iteration 3 in tab C;
5. rerender of iteration 3 without a second native request;
6. a fresh loop starting at iteration 1.

`tests/test_browser_content_rerender_runtime.py` executes the content-script stack declared by the manifest. It covers:

1. initial enhancement of an AUTO action;
2. removal of the BDB panel while retaining the same `<code>` node;
3. a later scan restoring the panel;
4. reconsideration through the background replay guard rather than direct execution.

`tests/test_browser_content_settle_reconciliation_runtime.py` reproduces the streaming timing boundary:

1. the code node initially contains incomplete `bdb-action-v1` JSON;
2. the immediate scan fails closed and creates no panel;
3. a relevant mutation schedules delayed whole-document reconciliation;
4. the JSON becomes complete before the scheduled callback;
5. the delayed scan enhances it and re-enters AUTO exactly through the normal decision path.

`tests/test_browser_content_rerender_observer_contract.py` locks the removed-node observer boundary and asserts that the companion script contains no direct runtime messaging.

The runtime regressions were introduced red against the preceding implementations before their respective fixes.

## Manual verification

After updating the unpacked extension:

1. reload the extension in the browser and confirm version `0.2.3`;
2. reload the ChatGPT tab once before beginning the acceptance run so the new content script is active;
3. use one ChatGPT tab for the acceptance run;
4. enable AUTO in the popup;
5. start a new loop with a unique `loop_id` and iteration 1;
6. allow the result to create the next ChatGPT turn;
7. verify that the mutation runs without an ASSISTED click;
8. verify that the final receipt-validation action is detected without `Ctrl+R`;
9. inspect the popup and confirm the displayed expected iteration;
10. confirm that rerendering or refreshing an old action does not send a second Native Messaging request.

The Local Workspace Loop acceptance criterion remains strict: one initial user message, automatic context, mutation, tests, promotion receipt and final response without a manual click or page refresh after the run begins.
