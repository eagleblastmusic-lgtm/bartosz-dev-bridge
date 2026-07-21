# Bartosz Dev Bridge browser extension 0.3.0

This Manifest V3 extension implements bounded ASSISTED and explicit opt-in AUTO Direct Lane modes.

- It runs only on `https://chatgpt.com/*`.
- Project Creator never opens a competing ChatGPT tab. A queued prompt may be claimed only by the currently visible, focused `/c/...` conversation with an empty composer.
- It recognizes only explicit JSON code blocks using `bdb-action-v1`.
- Before Native Host submission, mutating actions receive client preflight for safe repository paths, the exact local `allowed_paths`, canonical Base64 and every declared `content_sha256`.
- ASSISTED remains manual: `BDB: Wykonaj` sends one action to `com.bartosz.dev_bridge` and the extension never clicks Send for an ordinary ASSISTED action.
- AUTO runs only after the operator explicitly enables it and remains bounded by configured iteration and time limits.
- AUTO continuation is sent only after the current result is completed, required promotion is observed, and the exact composer submission is confirmed.
- Duplicate ChatGPT rerenders share one durable replay claim; in-flight duplicates wait, failed claims are released, and abandoned claims expire after a bounded lease.
- The active conversation is durably correlated with `repo_alias`, `launch_id`, `session_id` and `command_id` in extension-local storage.
- Failed mutating actions preserve an explicit `bdb-repair-correlation-v1`. `Napraw i uruchom ponownie` corrects deterministic hash metadata locally or returns the exact error to the same conversation; the next corrected action uses either the still-unbound initial session or a new repair session with an exact predecessor.
- A result can always be copied or inserted manually when the composer DOM no longer matches the bounded selector set.
- Repository paths, aliases and policy remain controlled by the local Native Host configuration. The extension does not silently widen an existing workspace allowlist.

Load the directory as an unpacked extension only after installing the Native Host and registering the extension's exact ID in `allowed_origins`.
