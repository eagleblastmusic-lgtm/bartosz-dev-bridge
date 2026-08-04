# Windows Native Messaging Host

The Native Messaging Host is a bounded local adapter between the browser extension and Direct Lane. Read-only search and inspection use immutable Git objects; mutations still pass through the durable Bridge queue, fixed profiles and local policy.

## Host identity and framing

```text
com.bartosz.dev_bridge
```

The browser host manifest uses `type: stdio`, exact `allowed_origins`, the installed `bdb-native-host.exe` entrypoint, and per-user HKCU registration for Chrome and Microsoft Edge.

Messages are strict UTF-8 JSON objects prefixed by one unsigned 32-bit native-order byte length. BDB limits both directions to at most 1 MiB.

The browser extension keeps one `connectNative` port and correlates concurrent responses by `request_id`. If a browser runtime does not expose ports, it falls back to one-shot Native Messaging. A submitted command is durably bound to its request ID, so one bounded reconnect with the same request recovers the original command result instead of creating another effect.

Extension `0.4.7` sends its version with every Native request and validates the `host_version` response. A mismatch stops the action with an explicit reload diagnostic. Before a mutating command is queued, Native Host also verifies that the active Bridge worker recorded the same runtime version. A missing or stale worker returns `bridge_restart_required` without writing a command.

Version 0.4.7 keeps the direct-spool inbox bounded without deleting evidence. Once a command has a durable local result, its immutable envelope is moved atomically from `direct_spool/inbox` to `direct_spool/archive/inbox`. Envelopes without results remain active, while result documents and the SQLite journal stay in their original locations.

## Trusted aliases

The default configuration is:

```text
%LOCALAPPDATA%\BartoszDevBridge\native-host.json
```

Example:

```json
{
  "schema": "bdb-native-host-config-v1",
  "repositories": {
    "gicleeart": {
      "bridge_config_path": "C:\\trusted\\gicleeart-bridge.json"
    }
  },
  "allowed_origins": [
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"
  ],
  "state_path": "C:\\Users\\user\\AppData\\Local\\BartoszDevBridge\\native-host-arm.json",
  "session_store_path": "C:\\Users\\user\\AppData\\Local\\BartoszDevBridge\\native-host-sessions.json",
  "max_wait_seconds": 30,
  "max_message_bytes": 1048576
}
```

Aliases are lowercase local names. Each alias resolves to one trusted Bridge config containing the repository checkout, runtime, allowlist, fixed profiles and Direct Lane directories. Browser messages cannot supply or override absolute repository, runtime, spool, result, executable or policy paths.

The host maintains an atomic local session store binding each generated `session_id` to one alias, `repository_id` and exact Git `base_sha`. A later command cannot move the session to another alias.

## Explicit arm gate

The host starts fail-closed. Submit and result requests require a non-expired local arm state:

```powershell
bdb bridge native-host arm --minutes 10
bdb bridge native-host status --json
bdb bridge native-host disarm
```

Arm duration is limited to 1–60 minutes. `status` and read-only repository `context` remain available while disarmed.

## Request protocol

Every request uses `bdb-native-request-v1` and a bounded `request_id`.

### Context

```json
{
  "schema": "bdb-native-request-v1",
  "request_id": "context-1",
  "action": "context",
  "repo_alias": "gicleeart"
}
```

The response contains only safe repository context: alias, `repository_id`, exact local `HEAD` SHA, relative allowlist patterns and sequence limit. No absolute local path is returned.

### Consolidated repository inspection

`inspect_bundle` combines up to eight searches, twenty explicit or top-match reads, a bounded tree, symbols, Git state and one mirror receipt under one exact `base_sha`:

```json
{
  "schema": "bdb-native-request-v1",
  "request_id": "inspect-1",
  "action": "inspect_bundle",
  "bdb_action": {
    "schema": "bdb-action-v1",
    "repo_alias": "gicleeart",
    "operation": "inspect_bundle",
    "payload": {
      "searches": [
        {"query": "ShopifyManager", "path_prefixes": ["cursor-api/Komponenty"]},
        {"query": "upload_image", "path_prefixes": ["cursor-api/Komponenty"]}
      ],
      "reads": [
        {"path": "cursor-api/Komponenty/example.py", "start_line": 1, "end_line": 400}
      ],
      "read_top_matches": 4
    }
  }
}
```

The operation performs mirror synchronization once, searches the pinned Git commit, batch-reads blobs with `git cat-file --batch`, applies the configured allowlist and returns explicit truncation and SHA-256 metadata. Independent searches run concurrently with at most four workers against that immutable commit. Search results use a bounded in-process LRU keyed by repository, exact `HEAD`, allowlist and normalized query. It is the preferred first action when a model would otherwise need several `workspace_context`, `search_text` and `open_read` turns.

For `automation.mode: auto` or `presentation.mode: compact`, the Native Host now returns a focused result capped at 20 KiB before mirror metadata. Full repository tree output is omitted by default, search matches are ranked and bounded, and up to six relevant excerpts share a 10 KiB content budget. Use `include_tree: true` only when a focused tree is genuinely needed; `include_symbols` can be disabled explicitly.

### Submit a model action

Preferred browser request:

```json
{
  "schema": "bdb-native-request-v1",
  "request_id": "turn-0001",
  "action": "submit_action",
  "wait_seconds": 30,
  "bdb_action": {
    "schema": "bdb-action-v1",
    "repo_alias": "gicleeart",
    "operation": "open_read",
    "expected_revision": 0,
    "payload": {
      "path": "src/example.py"
    }
  }
}
```

Supported operations are the existing Bridge gates only:

- direct bounded reads: `search_text`, `inspect_bundle`;
- `open_read`;
- `replace_exact_and_test`;
- `multi_file_patch`.

For a new action session, the host generates a UUID, reads immutable local Git `HEAD` using fixed `shell=False` commands, binds the alias/base durably and creates a canonical `bdb-local-envelope-v1`. Later actions provide the returned `session_id`, increasing `sequence`, expected revision/state and payload; the host reuses the originally bound base.

### Submit a full envelope

A trusted advanced client may submit an already complete `bdb-local-envelope-v1`, but it must also name a configured `repo_alias`. The manifest `repository_id` must match that alias. The host still validates command identity, publishes through `LocalSpoolWriter`, signals the Windows wake event and waits only within the configured bound.

### Result polling

```json
{
  "schema": "bdb-native-request-v1",
  "request_id": "turn-0001-result",
  "action": "result",
  "session_id": "018f3f66-6cb3-4f66-9f2e-3d7647d1b701",
  "sequence": 1,
  "wait_seconds": 10
}
```

For sessions created by `submit_action`, the host resolves the alias from the durable session binding. It reads only:

```text
sessions/<session_id>/results/<sequence>.json
```

## Responses

Every response uses `bdb-native-response-v1`. Possible statuses:

- `context` — safe local repository context;
- `accepted` — the action is durable but no result appeared within the wait;
- `pending` — a result poll completed without a result;
- `completed` — the exact durable local result is included;
- `status` — arm state and configured alias names;
- `failed` — a bounded error code with a generic message that does not expose local paths.

## Installation

```powershell
.\scripts\Install-BDBNativeHost.ps1 `
  -HostExecutable (Get-Command bdb-native-host).Source `
  -BridgeConfig C:\path\to\config.json `
  -RepositoryAlias gicleeart `
  -ChromeExtensionId abcdefghijklmnopabcdefghijklmnop `
  -EdgeExtensionId ponmlkjihgfedcbaponmlkjihgfedcba
```

The installer writes UTF-8-without-BOM config and host-manifest files under `%LOCALAPPDATA%\BartoszDevBridge`, records the trusted alias, writes exact origins and registers the host under Chrome and Edge HKCU NativeMessagingHosts keys. It opens no port and requires no machine-wide registration.

Enterprise browser policy may disable user-level native hosts or require a host allowlist. That is a deployment condition, not a reason to weaken host checks.

## Native Host non-goals

- no browser DOM access inside the Native Host process;
- no Native Host control over the user's AUTO opt-in;
- no arbitrary shell or process launch;
- no remote network listener;
- no browser mutation of repository aliases or Bridge policy;
- no automatic Git commit, push, PR or merge.
