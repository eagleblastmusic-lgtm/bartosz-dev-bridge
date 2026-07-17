# Windows Native Messaging Host

The Native Messaging Host is a thin local transport adapter between the browser extension and Direct Lane. It does not analyze code, execute arbitrary processes, or bypass Bridge policy.

## Host identity and framing

```text
com.bartosz.dev_bridge
```

The browser host manifest uses `type: stdio`, exact `allowed_origins`, the installed `bdb-native-host.exe` entrypoint, and per-user HKCU registration for Chrome and Microsoft Edge.

Messages are strict UTF-8 JSON objects prefixed by one unsigned 32-bit native-order byte length. BDB limits both directions to at most 1 MiB.

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

## Non-goals

- no browser DOM automation in this stage;
- no AUTO conversation loop;
- no arbitrary shell or process launch;
- no remote network listener;
- no browser mutation of repository aliases or Bridge policy;
- no automatic Git commit, push, PR or merge.
