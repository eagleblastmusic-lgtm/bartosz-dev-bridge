# Bartosz Dev Bridge

Lokalny Bridge dla ChatGPT Plus i GitHuba, rozwinięty od POC-0 do trwałego, pojedynczego runtime’u z procesową bramką recovery.

Aktualna faza:

```text
GHB-0 — recovery gate closed
```

## Działający zakres

- durable Git ingestion i immutable command identity;
- single queue i jeden aktywny worker;
- izolowane session worktree;
- atomic exact-byte patching;
- plan/effect recovery bez podwójnej revision;
- immutable result staging i durable outbox;
- fast-forward result publication i collision detection;
- service lifecycle `RUNNING → STOPPING → OFFLINE`;
- wspólny OS lock i heartbeat;
- Windows foreground i background jako zwykły proces użytkownika;
- siedmiosesyjna recovery gate A–G z rzeczywistymi restartami;
- jawne session finalization;
- persisted `preserve` jako polityka domyślna;
- bezpieczny, opt-in i crash-recoverable cleanup wyłącznie sesji `COMPLETED`.

## CLI

```text
bdb bridge start --config <path> --foreground
bdb bridge start --config <path> --background   # Windows
bdb bridge stop --config <path>
bdb bridge status --config <path> [--json]

bdb bridge session finalize --config <path> --session-id <uuid>

bdb bridge workspace status --config <path> --session-id <uuid> [--json]
bdb bridge workspace preserve --config <path> --session-id <uuid>
bdb bridge workspace cleanup --config <path> --session-id <uuid> --confirm-session-id <uuid>

bdb bridge repo index --config <path> [--ref HEAD] [--json]
bdb bridge repo status --config <path> [--ref HEAD] [--json]
bdb bridge repo files --config <path> [--ref HEAD] [--json]
bdb bridge repo outline --config <path> --path <posix-path> [--ref HEAD] [--json]
```

Indeks repozytorium (GHB1-A) opisuje dokładny commit Git wskazany przez `--ref` w `fixture_repo_path`. Szczegóły: [docs/GHB1A_REPOSITORY_INDEX.md](docs/GHB1A_REPOSITORY_INDEX.md).

Tryb background nie tworzy Windows Service, Scheduled Task ani procesu administracyjnego. Child sam zdobywa platformowy lock i prowadzi graceful lifecycle.

## Kolejność service loop

```text
recovery → pending outbox → ingestion → execution → wait
```

Pomiędzy bezpiecznymi fazami sprawdzany jest trwały stop. Żądanie zapisane podczas execution nie przerywa patcha ani profilu w połowie; faza kończy się bezpiecznie, a następnie service przechodzi do końcowego `OFFLINE` bez dodatkowego pełnego idle delay.

## Recovery gate

Macierz wykonuje świeże, procesowe scenariusze:

```text
A  DISCOVERED przed validation
B  CLAIMED
C  temp write przed atomic replace
D  atomic replace przed EFFECT_RECORDED
E  EFFECT_RECORDED przed profile/result
F  RESULT_STAGED przed publish
G  remote push przed local publication ACK
```

Każdy case korzysta z nowego syntetycznego fixture repo, bare/control repo, Journalu, worktree, session ID i procesu foreground. Po fault exit uruchamiany jest nowy proces, a po sukcesie kolejny restart sprawdza no-op. Bramka potwierdza pojedynczy patch, revision, plan, effect, result, outbox i publish oraz exact remote bytes/hash/path.

Dodatkowe scenariusze obejmują persisted transport retry, command collision, result collision, divergent workspace i drugi proces blokowany przez OS lock.

Windows gate:

```powershell
.\scripts\Invoke-GHB0RecoveryGate.ps1
.\scripts\Invoke-GHB0RecoveryGate.ps1 -Python ".venv\Scripts\python.exe"
```

## Workspace lifecycle v6

Journal v6 dodaje trwały rekord lifecycle z immutable identity: session, exact absolute path, base SHA, revision i state hash.

Dyspozycje:

```text
preserve | cleanup
```

Stany:

```text
preserved | cleanup_requested | removing | removed | blocked
```

Domyślna polityka to `preserve`. Restart, stop, collision, transport error i manual reconciliation nie usuwają worktree.

## Session finalization

Finalizacja jest jawna i transakcyjna:

```text
ACTIVE → COMPLETING → COMPLETED
```

Wymaga service `OFFLINE`, wspólnego OS locka i braku unresolved command, pending/collision outbox, blocking ingestion issue oraz manual reconciliation. `RESULT_PUBLISHED` pozostaje bez protocol ACK. Finalizacja zapisuje `preserve` i nie usuwa worktree ani Journalu.

## Safe cleanup

Cleanup wymaga exact confirmation tego samego UUID, stanu `COMPLETED`, service `OFFLINE`, zdobytego wspólnego locka i pełnej eligibility. Sprawdzane są m.in. exact path, brak reparse points, source cleanliness, dokładnie jedna detached registration na exact base SHA, brak unauthorized/temp paths oraz zgodność physical state hash.

Jedyna operacja fizyczna:

```text
git -C <fixture_repo> worktree remove --force <exact_workspace_path>
```

Bridge nie używa `shutil.rmtree`, `Remove-Item -Recurse`, `rmdir /s`, `git reset`, `git clean` ani `git worktree prune`. Cleanup jest odzyskiwany po awarii przed startem, po `removing` oraz po fizycznym remove przed local DB ACK.

## Granice bezpieczeństwa

GHB-0 nadal nie dodaje:

- protocol ACK ani automatycznego `ACKNOWLEDGED`;
- automatic cleanup lub retention;
- cleanupu aktywnych/manual sessions;
- Windows Service, Scheduled Task, GUI, tray, installer lub autostart;
- produkcyjnego execution;
- arbitrary shell lub `shell=True`;
- wielu workerów i równoległych sesji;
- HTTP/WebSocket remote control;
- Hermesa, GicleeApp, Browser Lab, Playwright lub LSP;
- zależności runtime `bdb_bridge → bdb_poc`.

Legacy POC-0A i POC-0B pozostają regresjami przez `poc_bridge.py` oraz `bdb_poc.PocBridge`.

Dokumentacja operatorska:

- `docs/GHB0_WINDOWS_RUNBOOK.md`;
- `docs/GHB0_RECOVERY_GATE.md`.
