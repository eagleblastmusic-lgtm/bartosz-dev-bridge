# Local browser pilot — Windows

This is the first real operator pilot using the ChatGPT browser extension, Native Messaging and Direct Lane on a local Windows machine.

It is intentionally isolated from business repositories. The bootstrap creates a new synthetic Git repository with a three-path allowlist:

```text
src/clamp.py
tests/test_clamp.py
PILOT_RESULT.md
```

It does not reference or modify Giclée Art, `bartosz-dev-poc-control`, production data or any existing repository alias.

## Safety boundary

The bootstrap:

- requires a clean Bridge implementation checkout;
- creates the pilot outside the implementation checkout;
- refuses to overwrite an existing Native Host config, manifest or HKCU registration;
- installs only a per-user Native Messaging registration;
- opens no network port and requires no administrator rights;
- starts one ordinary background Bridge process;
- arms only the synthetic `pilot` alias for a bounded 1–60 minute window;
- preserves the fixture, worktree, Journal, logs and registration when stopped;
- never runs `git reset`, `git clean`, recursive deletion or automatic cleanup.

## Validated checkpoint

The reviewed bootstrap was validated by Bridge CI on Windows/Python 3.14 with the exact lifecycle:

```text
Setup → RUNNING → ARMED → Status → DISARM → graceful Stop → OFFLINE
```

The same exact-head run also passed the existing local E2E, persistent operator, Direct Lane Native and private GitHub smoke gates. The operator must still verify the real browser extension ID and local machine state before setup.

## Prerequisites

- Windows 10 or Windows 11;
- Git available in `PATH`;
- Python 3.11 or newer available as `python`;
- Chrome or Microsoft Edge;
- a clean local checkout of `bartosz-dev-bridge` on the reviewed `main` commit.

The expected checkout used by the current project is:

```text
C:\Projekty\DevMaster\bartosz-dev-bridge
```

## 1. Load the unpacked extension and copy its ID

The exact extension ID is required by the Native Messaging allowlist, so the extension must be loaded once before running the bootstrap.

Chrome:

```text
chrome://extensions
```

Edge:

```text
edge://extensions
```

Enable **Developer mode**, choose **Load unpacked** and select:

```text
C:\Projekty\DevMaster\bartosz-dev-bridge\browser_extension
```

Copy the 32-character lowercase extension ID. Until the Native Host is installed, the extension may report that the host is unavailable. That is expected.

## 2. Prepare and start the isolated pilot

From PowerShell in the Bridge checkout:

```powershell
git switch main
git pull --ff-only

.\scripts\Invoke-BDBLocalBrowserPilot.ps1 `
  -Action Setup `
  -Browser Chrome `
  -ExtensionId <PASTE_32_CHARACTER_EXTENSION_ID>
```

For Edge, use `-Browser Edge`.

The bootstrap creates `.venv` when needed, installs the reviewed checkout in editable mode, prepares the synthetic fixture and local control remote, installs the Native Host, starts Bridge in background mode, waits for `RUNNING` and arms the `pilot` alias for 10 minutes.

Default preserved pilot root:

```text
%LOCALAPPDATA%\BartoszDevBridge\local-browser-pilot
```

The command returns JSON containing:

- the exact Bridge implementation SHA;
- extension directory and ID;
- Bridge and Native Host status;
- paths to two ready test actions;
- the active safety guarantees.

## 3. Confirm status before using ChatGPT

```powershell
.\scripts\Invoke-BDBLocalBrowserPilot.ps1 -Action Status
```

Required state:

```text
bridge.status = RUNNING
native_host.armed = true
repo_alias = pilot
```

Do not enable browser AUTO yet. The first real operator pass is ASSISTED and read-only.

## 4. First browser action — read only

Open `actions\01-open-read.json` from the pilot root. The generated action is:

```json
{
  "schema": "bdb-action-v1",
  "repo_alias": "pilot",
  "operation": "open_read",
  "expected_revision": 0,
  "payload": {
    "path": "src/clamp.py"
  }
}
```

Have ChatGPT return that object in a JSON code block. The extension should add **BDB: Wykonaj**. Click it once.

Pass criteria:

- the extension reaches `com.bartosz.dev_bridge`;
- the Native Host resolves only alias `pilot`;
- the response is `completed` or a bounded `accepted` followed by a successful result poll;
- the result contains the synthetic `src/clamp.py` content;
- no file is changed.

If the button does not appear, reload the extension and the ChatGPT tab. If the host is unavailable, compare the extension ID with `%LOCALAPPDATA%\BartoszDevBridge\native-host.json` and run `-Action Status`.

## 5. Second browser action — isolated edit and pytest

Only after the read-only pass, open `actions\02-multi-file-patch.json` and have ChatGPT return it as a JSON code block. Click **BDB: Wykonaj** once.

The action:

- edits only `src/clamp.py` inside a detached session worktree;
- creates only `PILOT_RESULT.md`;
- runs the fixed `poc_pytest` profile;
- leaves the original synthetic source checkout clean;
- uses the trusted initial CAS hash supplied by the Native Host;
- publishes a durable local result through the normal Journal, scheduler, checkpoint and outbox path.

Pass criteria:

```text
result.status = success
profile.status = success
checkpoint = committed
source checkout = clean
```

Do not point the alias at a business repository during this pilot.

## 6. Stop without cleanup

```powershell
.\scripts\Invoke-BDBLocalBrowserPilot.ps1 -Action Stop
```

Stop disarms the Native Host and requests a graceful Bridge stop. It intentionally preserves all pilot artifacts and the Native Messaging registration for inspection. No worktree, Journal, repository, config or registry entry is deleted.

## Re-running

The default setup is single-use and fail-closed. A second `Setup` refuses to overwrite the existing pilot or Native Host registration.

Before any migration from the synthetic alias to a real repository, perform a separate review of:

- exact local repository path and clean HEAD;
- repository-specific `allowed_paths`;
- fixed test profiles;
- expected branch and worktree policy;
- recovery and rollback procedure;
- whether ASSISTED remains the only enabled browser mode.

A successful synthetic pilot is evidence that the transport works. It is not authorization to attach a business repository.
