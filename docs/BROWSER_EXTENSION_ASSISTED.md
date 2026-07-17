# Browser extension — ASSISTED mode

ASSISTED is the first browser integration boundary. It deliberately keeps one explicit user confirmation before execution and one explicit user send after a result.

## Flow

1. ChatGPT produces an explicit JSON code block with schema `bdb-action-v1`.
2. The isolated content script adds `BDB: Wykonaj` beside that block.
3. The user clicks the button.
4. The MV3 service worker sends `submit_action` to `com.bartosz.dev_bridge`.
5. The Native Host resolves the trusted alias, binds exact local Git state, writes Direct Spool and returns a durable local result.
6. The extension displays the result.
7. `Przygotuj kontynuację` inserts `BDB_RESULT` into the composer when a bounded selector matches. It never submits the message.
8. If insertion is unavailable, the result is copied for manual paste.

## Permissions

The extension requests only:

- `nativeMessaging`;
- `storage` for the preferred repository alias;
- host access to `https://chatgpt.com/*`.

It does not request `tabs`, `<all_urls>`, downloads, clipboard-write permission, debugger, webRequest or remote code.

## Fail-closed boundaries

- no automatic execution while scanning;
- one in-flight action per sender tab;
- only `bdb-action-v1` objects under 1 MiB;
- no full envelope construction in the browser;
- no absolute local paths;
- no automatic click on the ChatGPT send button;
- generic Native Host errors only;
- clipboard fallback when the composer DOM changes.

AUTO mode must be a separate stage with its own iteration, time and state-transition limits.
