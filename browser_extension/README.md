# Bartosz Dev Bridge browser extension

This Manifest V3 extension implements the ASSISTED Direct Lane mode.

- It runs only on `https://chatgpt.com/*`.
- It recognizes only explicit JSON code blocks using `bdb-action-v1`.
- It never executes an action automatically.
- `BDB: Wykonaj` sends the action to `com.bartosz.dev_bridge` through the service worker.
- A result can be copied or inserted into the ChatGPT composer, but the extension never clicks Send in ASSISTED mode.
- If the composer DOM no longer matches the bounded selector set, the extension falls back to clipboard copy.
- Repository paths, aliases and policy remain controlled by the local Native Host configuration.

Load the directory as an unpacked extension only after installing the Native Host and registering the extension's exact ID in `allowed_origins`.
