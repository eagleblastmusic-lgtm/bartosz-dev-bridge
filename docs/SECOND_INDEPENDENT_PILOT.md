# Second Independent Pilot

Status: review candidate until CI and artifact verification complete.

## Purpose

Prove that the one-message repair loop is not coupled to the calculator2 fixture or the pytest profile.

The independent pilot uses:

- synthetic repository alias `inventory2`;
- a root-level `inventory/` package instead of `src/`;
- the fixed standard-library profile `poc_unittest`;
- creation of several new files rather than replacement of existing feature files;
- a distinct allowlist and failure analyzer.

## Scenario

The task creates stock-line parsing and inventory summarization. The first candidate accepts a negative quantity. Standard-library unittest discovery must report the failing test `test_rejects_negative_quantity`.

BDB must then prove:

1. the first batch is rolled back completely;
2. all newly created candidate files disappear from the failed worktree;
3. the failed worktree and trusted source checkout remain clean;
4. the bounded analyzer identifies the expected unittest failure;
5. a fresh repaired sequence-1 session is submitted without user intervention;
6. unittest succeeds;
7. the existing WorkspacePromoter creates a local commit and uses only ff-only promotion;
8. the final source checkout is clean and a durable receipt exists.

## Fixed execution profile

`poc_unittest` maps only to:

```text
python -m unittest discover -s tests -p test_*.py -v
```

The profile registry contains no user-provided command, shell text, or variable argument list. Unknown profile identifiers are denied.

## Allowlist

- `inventory/parser.py`
- `inventory/report.py`
- `tests/test_inventory_report.py`
- `SECOND_PILOT_RESULT.md`

## Boundaries

- synthetic local repository only;
- no business repository mutation;
- no arbitrary shell;
- no secrets or global dependency installation;
- no remote push of the synthetic result;
- no automatic PR merge, deploy, installer, signing, tag, or GitHub Release.

## Acceptance gate

- full pytest matrix passes on Ubuntu Python 3.11/3.12 and Windows Python 3.11/3.12/3.14;
- focused fixed-profile and lifecycle contracts pass;
- real Windows/Python 3.14 inventory2 pilot passes;
- evidence artifact is downloaded and independently read;
- report proves one unittest failure, rollback, repair success, zero user interventions, ff-only promotion, clean checkout and receipt.
