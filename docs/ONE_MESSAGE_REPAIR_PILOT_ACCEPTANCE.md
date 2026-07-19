# One-message repair pilot acceptance checklist

The candidate is accepted only when the dedicated Windows workflow proves all items below on one exact commit:

- baseline synthetic `calculator2` tests pass;
- first Native Host action reaches a failed pytest result;
- expected zero-division test is present in bounded failure evidence;
- first checkpoint is `rolled_back` and reports `rollback_performed=true`;
- failed worktree and trusted source checkout return to clean baseline bytes;
- repair is submitted automatically without user intervention;
- repaired profile exits zero and commits its checkpoint;
- durable successful Direct Lane result exists;
- existing `WorkspacePromoter` creates a single-parent commit and ff-only promotion;
- final source HEAD equals the promotion receipt commit;
- final pytest passes;
- final source checkout is clean;
- report, Journal, direct results, logs and promotion receipt are uploaded as workflow evidence.

Passing this checklist proves the synthetic local execution loop. It does not authorize merge to `main`, production deploy, installer, signing or GitHub Release.
