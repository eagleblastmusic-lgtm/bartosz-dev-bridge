# One-message failure → repair pilot

Status: **candidate pilot gate**

## Purpose

This pilot verifies one bounded execution chain for the synthetic `calculator2` alias:

```text
one task
→ Native Host
→ Direct Lane
→ isolated worktree
→ initial multi-file patch
→ pytest failure
→ durable failure result
→ rollback verification
→ bounded failure analysis
→ repaired multi-file patch in a fresh session
→ pytest success
→ promotion commit
→ ff-only update of the trusted local checkout
→ final pytest
→ promotion receipt and pilot report
```

No user action is required between the first attempt and the repair attempt.

## Deliberate failure

The initial candidate adds `safe_divide()` without handling division by zero. The submitted tests require `safe_divide(9, 0) is None`, so the first profile must fail. The pilot accepts that result only when it proves:

- `status=failed` and a non-zero exit code;
- the expected pytest test identifier is present in bounded output;
- checkpoint state is `rolled_back`;
- `rollback_performed=true`;
- `changed_files=[]`;
- both the failed isolated worktree and the trusted source checkout are clean and still contain baseline bytes.

## Repair and promotion

The second candidate is submitted as a fresh sequence-1 session. It adds the zero-division guard, the tests and a small pilot evidence file. Automatic promotion remains constrained by the existing `WorkspacePromoter` contract:

- successful zero-exit `multi_file_patch` only;
- allowlisted paths only;
- exact durable result path;
- clean source checkout;
- detached registered worktree;
- single-parent commit;
- `git merge --ff-only` only;
- clean source checkout and exact final HEAD after promotion;
- atomic promotion receipt.

## Run

Use a new, non-existing directory outside the BDB implementation checkout:

```powershell
.\scripts\Invoke-BDBOneMessageRepairPilot.ps1 `
  -Python (Get-Command python).Source `
  -Root "C:\Temp\bdb-one-message-repair-pilot" `
  -TimeoutSeconds 180
```

The durable report is written to:

```text
<root>\one-message-repair-report.json
```

A successful report uses schema `bdb-one-message-repair-pilot-report-v1` and includes both command IDs, rollback evidence, bounded failure analysis, changed files, final tests, promoted commit, receipt path and Journal path.

## Boundaries

This pilot does not add arbitrary shell execution, new profiles, global dependency installation, secrets, remote pushes, PR creation, merge to a business repository, deploy, GitHub Release, installer or signing. It proves the local execution and promotion chain on a synthetic repository only.
