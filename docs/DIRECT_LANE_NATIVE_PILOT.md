# Direct Lane Native pilot

This Windows pilot is the system-level gate for the local browser-to-Bridge path. It does not rely on GitHub for command delivery or for the first durable result.

## Proven path

```text
bdb-action-v1
→ Native Messaging framing
→ trusted repository alias
→ Direct Spool
→ Windows wake event
→ Journal validation and scheduling
→ isolated worktree
→ multi_file_patch
→ poc_pytest
→ durable local result
```

The pilot then restores the intentionally unavailable Git transport and proves that the existing outbox publishes the already staged exact result.

## Two required phases

### Phase A — Git unavailable

The pilot renames its local bare control remote before sending the Native Messaging request. A successful Phase A requires:

- Native Host response status `completed`;
- result status `success`;
- the expected workspace patch bytes;
- passing `poc_pytest`;
- a durable local result before the Git remote is restored;
- exact command state `result_staged`.

`result_published` is not accepted during this phase. This prevents the gate from accidentally passing through the old Git publication path.

### Phase B — Git restored

After the local result is observed, the pilot restores the same bare remote and waits for:

- exact command state `result_published`;
- the same command ID and workspace;
- a clean, unchanged source checkout;
- the report marker `git_fallback_published_without_reexecution=true`.

The checked runner validates the transition `result_staged → result_published`. Since execution and result construction are already complete before restoration, the second phase exercises only retry/reconciliation/publication of the existing outbox record.

## Windows CI invocation

The full gate runs only in the Windows / Python 3.14 matrix job:

```powershell
$root = Join-Path $env:RUNNER_TEMP "bdb-direct-lane-pilot"
.\scripts\Invoke-BDBDirectLanePilot.ps1 `
  -Python (Get-Command python).Source `
  -Root $root `
  -TimeoutSeconds 120
```

The root must not already exist and must stay outside the implementation checkout. Pilot artifacts and `direct-lane-report.json` remain available in the runner workspace until job cleanup.

## Harness constraints

`run_direct_lane_pilot_checked.py` is deliberately narrow:

- it invokes the existing Native Host `main()` through a controlled Python shim because `native_host.py` is primarily a console entrypoint module;
- it normalizes only the finite known successful plain-text responses from `bridge stop`;
- it does not alter Bridge, Native Host, Journal, execution, result, or outbox semantics;
- it fails closed for every unknown output or missing evidence field.
