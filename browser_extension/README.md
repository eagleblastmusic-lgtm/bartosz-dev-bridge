# Bartosz Dev Bridge browser extension

This Manifest V3 extension implements the bounded ASSISTED and explicit opt-in AUTO Direct Lane modes.

- It runs only on `https://chatgpt.com/*`.
- It recognizes only explicit JSON code blocks using `bdb-action-v1`.
- ASSISTED remains manual: `BDB: Wykonaj` sends one action to `com.bartosz.dev_bridge` and the extension never clicks Send for an ASSISTED action.
- AUTO runs only after the operator explicitly enables it and remains bounded by configured iteration and time limits.
- AUTO continuation is sent only after the current result is completed, required promotion is observed, and the exact composer submission is confirmed.
- Duplicate ChatGPT rerenders share one durable replay claim; in-flight duplicates wait, failed claims are released, and abandoned claims expire after a bounded lease.
- A result can always be copied or inserted manually when the composer DOM no longer matches the bounded selector set.
- Repository paths, aliases and policy remain controlled by the local Native Host configuration.

Load the directory as an unpacked extension only after installing the Native Host and registering the extension's exact ID in `allowed_origins`.
