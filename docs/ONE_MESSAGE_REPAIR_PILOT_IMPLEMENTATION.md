# One-message repair pilot implementation

Status: **candidate implementation on stacked branch**

## Scope

This implementation is intentionally a thin pilot coordinator over existing BDB components. It does not add a new executor or a general-purpose agent loop.

The coordinator uses:

- the trusted `calculator2` alias;
- Native Host `submit_action`;
- Direct Local Lane durable command and result transport;
- the fixed `poc_pytest` profile;
- existing multi-file checkpoint and rollback;
- bounded pytest failure analysis;
- a fresh sequence-1 repair session;
- existing `WorkspacePromoter` for commit, `git merge --ff-only`, clean-checkout verification and receipt.

## Safety properties

- Synthetic repository created under a new pilot root only.
- Exact allowlist of `src/calculator.py`, `tests/test_calculator.py` and `PILOT_RESULT.md`.
- No arbitrary program, shell command or profile input.
- No secret, token, remote GitHub push, business repository mutation, PR merge, deploy, installer, signing or Release.
- The first candidate is accepted as a repair input only after durable proof of a failed test and complete rollback.
- The repair is promotable only after a successful zero-exit result from the existing runtime.
- The trusted source checkout must be clean before and after promotion.

## Evidence gate

The dedicated Windows workflow must produce an uploaded pilot root containing:

- `one-message-repair-report.json`;
- Journal database;
- exact direct results for both attempts;
- promotion receipt;
- bridge stdout and stderr;
- isolated worktrees and synthetic source checkout.

The report must prove exactly two attempts, zero user interventions between them, failed first profile, rolled-back first checkpoint, bounded failure analysis, successful repaired profile, promoted commit, final green pytest and clean source checkout.
