# Private GitHub transport pilot

This pilot verifies the real transport boundary:

```text
ChatGPT GitHub connector
  -> private commands branch
  -> local Windows Bridge
  -> isolated synthetic worktree and bounded pytest profile
  -> private results branch
  -> ChatGPT GitHub connector
```

It does not attach GicléeApp or any business repository. The source repository is a fresh copy of `bdb-poc-fixture`, initialized inside a new pilot directory.

## Control repository

The default control repository is:

```text
eagleblastmusic-lgtm/bartosz-dev-bridge-pilot-control-private
```

It must be private and contain these branches:

```text
main
commands
results
```

The local Git credential helper must already be able to clone and push this repository. The setup used for the pilot is:

```powershell
gh auth login --hostname github.com --web --git-protocol https
gh auth setup-git --hostname github.com
```

Do not place tokens in `ControlUrl`, configuration files, console output, or committed files.

## Prepare and start

From the Bridge checkout:

```powershell
cd C:\Projekty\DevMaster\bartosz-dev-bridge
git pull --ff-only origin main
.\scripts\Invoke-BDBGitHubPilot.ps1
```

The command:

1. refuses to overwrite an existing pilot root;
2. requires the root to be outside the Bridge checkout;
3. verifies `main`, `commands`, and `results` through `git ls-remote`;
4. rejects embedded credentials and non-HTTPS GitHub URLs;
5. creates a synthetic local source repository;
6. clones the private control repository;
7. writes `config.json`, `github-pilot-request.json`, and `github-pilot-report.json`;
8. starts Bridge as a normal background user process;
9. verifies public service state `RUNNING`;
10. preserves every artifact and performs no cleanup.

Expected final marker:

```text
GITHUB REMOTE PILOT: READY
Service: RUNNING
```

The output includes the exact session ID, command ID, base SHA, timestamps, expected state hash, source hash, and the paths expected on the `commands` and `results` branches.

## ChatGPT delivery

After the operator pastes the READY output, ChatGPT uses the GitHub connector to add these files to the `commands` branch:

```text
sessions/<session-id>/manifest.json
sessions/<session-id>/commands/000001.json
```

The local Bridge polls `origin/commands`, validates immutable command identity, creates an isolated detached worktree, performs the final `multi_file_patch`, runs only `poc_pytest`, commits the successful batch, stages the durable result, and publishes:

```text
sessions/<session-id>/results/000001.json
```

ChatGPT then reads that exact result from the `results` branch.

## Safe inspection and stop

After the result is published:

```powershell
$Pilot = "C:\Projekty\DevMaster\bdb-github-pilot-..."
$Request = Get-Content "$Pilot\github-pilot-request.json" -Raw | ConvertFrom-Json

.\.venv\Scripts\python.exe -m bdb_bridge bridge edit status `
    --config "$Pilot\config.json" `
    --command-id $Request.command_id `
    --json

.\.venv\Scripts\python.exe -m bdb_bridge bridge stop `
    --config "$Pilot\config.json"

.\.venv\Scripts\python.exe -m bdb_bridge bridge status `
    --config "$Pilot\config.json" `
    --json
```

The expected terminal states are:

```text
command_state = result_published
checkpoint_state = committed
profile_status = success
result_status = success
outbox_state = published
service status = OFFLINE
```

Do not remove the Journal, lock file, worktree, or pilot root during validation. Do not use `taskkill`, `Stop-Process`, `git reset`, `git clean`, `git worktree prune`, force push, or automatic cleanup.

## CI preparation smoke

`-PrepareOnly` accepts a local control repository solely for CI preparation validation and does not start Bridge:

```powershell
.\scripts\Invoke-BDBGitHubPilot.ps1 `
    -ControlUrl C:\path\to\local-control.git `
    -Root C:\path\to\new-pilot-root `
    -PrepareOnly
```

A local control path is rejected during a real started pilot.
