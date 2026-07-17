# Windows Native Messaging Host

The Native Messaging Host is a thin local transport adapter between the browser extension and the existing Direct Lane. It does not analyze code, choose repositories, execute arbitrary processes, or bypass Bridge policy.

## Host identity

```text
com.bartosz.dev_bridge
```

The browser host manifest uses:

- `type: stdio`;
- exact `allowed_origins` entries;
- the installed `bdb-native-host.exe` console entrypoint;
- per-user HKCU registration for Chrome and Microsoft Edge.

The host reads and writes UTF-8 JSON messages prefixed by one unsigned 32-bit native-order byte length. BDB applies a stricter 1 MiB limit in both directions.

## Trusted configuration

The default host configuration is:

```text
%LOCALAPPDATA%\BartoszDevBridge\native-host.json
```

Schema:

```json
{
  "schema": "bdb-native-host-config-v1",
  "bridge_config_path": "C:\\trusted\\bridge-config.json",
  "allowed_origins": [
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop/"
  ],
  "state_path": "C:\\Users\\user\\AppData\\Local\\BartoszDevBridge\\native-host-arm.json",
  "max_wait_seconds": 30,
  "max_message_bytes": 1048576
}
```

The configuration is installed locally. Browser messages cannot supply or override:

- the Bridge config path;
- runtime, spool, result, repository, or executable paths;
- extension origins;
- message size or wait limits;
- execution profiles or local allowlists.

## Explicit arm gate

The host starts fail-closed. Submits and result polling require a non-expired local arm state.

```powershell
bdb bridge native-host arm --minutes 10
bdb bridge native-host status --json
bdb bridge native-host disarm
```

Arm duration is limited to 1–60 minutes. Expiry is checked for every `submit` and `result` request. A `status` request remains available while disarmed.

## Request protocol

### Submit

```json
{
  "schema": "bdb-native-request-v1",
  "request_id": "turn-0001",
  "action": "submit",
  "filename": "turn-0001.json",
  "wait_seconds": 30,
  "envelope": {
    "schema": "bdb-local-envelope-v1",
    "submitted_at": "2026-07-17T03:00:00Z",
    "manifest": {},
    "command": {}
  }
}
```

The host verifies that `command_id` exactly matches `session_id` and `sequence`, publishes the envelope atomically through `LocalSpoolWriter`, signals the existing Windows named event, and waits only within the configured bound.

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

The host resolves only the canonical durable result path:

```text
sessions/<session_id>/results/<sequence>.json
```

## Responses

Every framed response uses `bdb-native-response-v1` and echoes a bounded safe `request_id`.

Possible statuses:

- `accepted` — the envelope is durable, but no result appeared within the requested wait;
- `pending` — a result poll completed without a result;
- `completed` — the exact durable local result is included;
- `status` — the current arm state;
- `failed` — a bounded error code and message.

## Installation

After installing the Python package so that `bdb-native-host.exe` exists:

```powershell
.\scripts\Install-BDBNativeHost.ps1 `
  -HostExecutable (Get-Command bdb-native-host).Source `
  -BridgeConfig C:\path\to\config.json `
  -ChromeExtensionId abcdefghijklmnopabcdefghijklmnop `
  -EdgeExtensionId ponmlkjihgfedcbaponmlkjihgfedcba
```

The installer:

1. writes UTF-8-without-BOM host and BDB configuration files under `%LOCALAPPDATA%\BartoszDevBridge`;
2. writes exact extension origins;
3. registers `com.bartosz.dev_bridge` under the Chrome and Edge HKCU NativeMessagingHosts keys;
4. does not require an open port or administrator-level machine registration.

Enterprise browser policy may disable user-level native hosts or require a host allowlist. That is an explicit deployment condition, not a reason to weaken the host checks.

## Non-goals

- no browser DOM automation in this stage;
- no AUTO conversation loop;
- no arbitrary shell or process launch;
- no remote network listener;
- no direct mutation of Bridge policy or repository aliases;
- no automatic Git commit, push, PR, or merge.
