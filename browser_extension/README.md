# Bartosz Dev Bridge browser extension 0.4.4

This Manifest V3 extension implements bounded ASSISTED and explicit opt-in AUTO Direct Lane modes.

- It runs only on `https://chatgpt.com/*`.
- Project Creator never opens a competing ChatGPT tab. A queued prompt may be claimed only by the currently visible, focused `/c/...` conversation with an empty composer.
- It recognizes only explicit JSON code blocks using `bdb-action-v1`.
- Before Native Host submission, mutating actions receive client preflight for safe repository paths, the exact local `allowed_paths`, canonical Base64 and every declared `content_sha256`.
- ASSISTED remains manual: `BDB: Wykonaj` sends one action to `com.bartosz.dev_bridge` and the extension never clicks Send for an ordinary ASSISTED action.
- AUTO runs only after the operator explicitly enables it and remains bounded by configured iteration and time limits.
- AUTO continuation is sent only after the current result is completed, required promotion is observed, and the exact composer submission is confirmed.
- AUTO result transport targets 12 KiB, permits at most 16 KiB on the single-replacement contenteditable fast path, and retains a 4 KiB ceiling for the legacy insertion fallback. `inspect_bundle` adapts its profile while preserving query counts, paths and bounded excerpts.
- A hard-coded content build handshake detects a ChatGPT tab that survived an extension update and asks for an explicit tab reload.
- The task controller keeps a bounded local ledger, sanitized decision diagnostics, metrics and undelivered-result checkpoints. It stores no source code in diagnostic exports.
- Unsafe model-generated `loop_id` values are deterministically normalized before the AUTO state machine sees them; the effective identity is returned in the decision receipt.
- Exact read actions are cached only while the trusted local Git `HEAD` still matches. Exact mutating actions are deduplicated for five minutes against the same post-promotion commit.
- Optional `bdb-acceptance-v1` assertions verify result status, changed paths, promotion, tests and bounded post-action searches before recommending completion.
- Visual tasks may set `manual_visual_confirmation_required`; AUTO then stops with `needs_confirmation` after automated checks instead of claiming completion without a human review.
- Explicit resume grants the same task a fresh bounded iteration window in the active ChatGPT tab and immediately retries the already visible expected action.
- File delete/move/rename operations never run in AUTO; they fall back to the explicit ASSISTED button.
- Shadow mode records the decision, risk and estimated complexity without executing the action.
- The popup exposes health, task stop/resume, cache control, an explicit end-to-end AUTO self-test and a one-file sanitized diagnostics ZIP.
- Duplicate ChatGPT rerenders share one durable replay claim; in-flight duplicates wait, failed claims are released, and abandoned claims expire after a bounded lease.
- The active conversation is durably correlated with `repo_alias`, `launch_id`, `session_id` and `command_id` in extension-local storage.
- Failed mutating actions preserve an explicit `bdb-repair-correlation-v1`. `Napraw i uruchom ponownie` corrects deterministic hash metadata locally or returns the exact error to the same conversation; the next corrected action uses either the still-unbound initial session or a new repair session with an exact predecessor.
- A result can always be copied or inserted manually when the composer DOM no longer matches the bounded selector set.
- Repository paths, aliases and policy remain controlled by the local Native Host configuration. The extension does not silently widen an existing workspace allowlist.

Load the directory as an unpacked extension only after installing the Native Host and registering the extension's exact ID in `allowed_origins`.
