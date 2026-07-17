# Bartosz Dev Bridge

Lokalny Bridge dla ChatGPT Plus i GitHuba, rozwinięty od POC-0 do trwałego runtime’u z bezpieczną pętlą lokalnego workspace.

Aktualna faza:

```text
Local Workspace Loop — bounded context, tested edits, rollback repair and verified local promotion
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
- bezpieczny, opt-in i crash-recoverable cleanup wyłącznie sesji `COMPLETED`;
- immutable repository snapshots, tracked files, Python symbols i outline;
- statyczne importy, references, callers, dependency graph i deterministyczne search;
- bounded context pack oraz large-repository gate;
- canonical multi-file patch planning z exact before/after bytes;
- trwały Journal v9, crash-recoverable batch apply/rollback i session-scoped recovery;
- finalna bramka `multi_file_patch` z bounded profilem, commitem przy sukcesie i pełnym rollbackiem przy failure;
- trwały Journal v10 dla immutable profile outcome;
- osobny, walidowany wynik batchu publikowany przez wspólny durable outbox;
- bounded lokalny snapshot dla Native Hosta bez ujawniania ścieżek absolutnych;
- kompaktowe karty akcji oraz bounded, opt-in AUTO w rozszerzeniu;
- automatyczna pętla naprawcza wyłącznie po potwierdzonym rollbacku;
- idempotentny promoter: dokładny commit i wyłącznie `git merge --ff-only` do czystego checkoutu;
- trwałe receipts promocji z commitem, plikami i hashami;
- operator Windows `Prepare`, `Start`, `Status`, `Stop` dla trwałych aliasów projektów.

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

bdb bridge edit status --config <path> --command-id <session-uuid:sequence> [--json]

bdb bridge repo index --config <path> [--ref HEAD] [--json]
bdb bridge repo status --config <path> [--ref HEAD] [--json]
bdb bridge repo files --config <path> [--ref HEAD] [--json]
bdb bridge repo outline --config <path> --path <posix-path> [--ref HEAD] [--json]
bdb bridge repo analyze --config <path> [--ref HEAD] [--json]
bdb bridge repo search --config <path> [--ref HEAD] --query <text> [--kind all|file|symbol] [--limit 50] [--json]
bdb bridge repo references --config <path> [--ref HEAD] (--symbol-id <id> | --path <path> --qualified-name <name>) [--direction incoming|outgoing] [--kind <kind>] [--limit 100] [--json]
bdb bridge repo callers --config <path> [--ref HEAD] (--symbol-id <id> | --path <path> --qualified-name <name>) [--limit 100] [--json]
bdb bridge repo dependencies --config <path> [--ref HEAD] --path <path> [--direction incoming|outgoing] [--depth 1] [--edge-kind all|import|call|reference] [--max-nodes 200] [--json]
bdb bridge repo context --config <path> [--ref HEAD] (--query <text> | --symbol-id <id> | --path <path> [--qualified-name <name>]) [--direction incoming|outgoing|both] [--depth 2] [--max-files 20] [--max-bytes 65536] [--max-excerpt-lines 80] [--json]
bdb bridge repo gate --config <path> [--ref HEAD] [--max-files 200000] [--max-symbols 2000000] [--max-relationships 5000000] [--json]
```

Indeks repozytorium (GHB1-A) opisuje dokładny commit Git wskazany przez `--ref` w `fixture_repo_path`. Szczegóły: [docs/GHB1A_REPOSITORY_INDEX.md](docs/GHB1A_REPOSITORY_INDEX.md).

Relacje kodu (GHB1-B) są budowane wyłącznie na immutable snapshotach GHB1-A. Szczegóły: [docs/GHB1B_CODE_RELATIONSHIPS.md](docs/GHB1B_CODE_RELATIONSHIPS.md).

Context pack i końcowa bramka większego repozytorium (GHB1-C) są opisane w [docs/GHB1C_CONTEXT_PACK.md](docs/GHB1C_CONTEXT_PACK.md).

Trwały checkpoint, fizyczny batch apply, rollback, commit CAS i recovery są opisane w [docs/GHB2C_DURABLE_BATCH_RECOVERY.md](docs/GHB2C_DURABLE_BATCH_RECOVERY.md).

Finalna aktywacja `multi_file_patch`, durable profile outcome, wynik batchu i operator status są opisane w [docs/GHB2D_FINAL_EDITING_GATE.md](docs/GHB2D_FINAL_EDITING_GATE.md).

## Local Workspace Loop

Jednorazowe podłączenie czystego lokalnego repo:

```powershell
.\scripts\Invoke-BDBWorkspaceLoop.ps1 `
  -Action Prepare `
  -Root "$env:LOCALAPPDATA\BartoszDevBridge\workspaces\calculator" `
  -Repo "C:\Projekty\Kalkulator test" `
  -Alias "calculator" `
  -AllowedPath @("*.py", "tests/*.py", "README.md", ".gitignore")
```

Codzienny lifecycle:

```powershell
.\scripts\Invoke-BDBWorkspaceLoop.ps1 -Action Start  -Root <workspace-loop-root>
.\scripts\Invoke-BDBWorkspaceLoop.ps1 -Action Status -Root <workspace-loop-root>
.\scripts\Invoke-BDBWorkspaceLoop.ps1 -Action Stop   -Root <workspace-loop-root>
```

Po `READY` ChatGPT może w jednej bounded pętli pobrać lokalny kontekst, wykonać dokładny odczyt, zmienić kilka plików, uruchomić allowlistowany profil, przeanalizować bezpiecznie wycofaną porażkę, ponowić poprawkę i zakończyć dopiero po receipt potwierdzającym fast-forward właściwego lokalnego checkoutu.

Pełny kontrakt, przykłady akcji, reguły AUTO, promocji i bezpieczeństwa: [docs/LOCAL_WORKSPACE_LOOP.md](docs/LOCAL_WORKSPACE_LOOP.md).

## Lokalny end-to-end POC

Na Windows pełny syntetyczny POC można uruchomić jedną komendą:

```powershell
.\scripts\Invoke-BDBLocalE2E.ps1
```

Bramka automatycznie używa `.venv\Scripts\python.exe`, tworzy wyłącznie tymczasowe repozytoria i sprawdza lokalny transport Git, finalny `multi_file_patch`, rollback, durable recovery oraz foreground lifecycle. Szczegóły: [docs/LOCAL_E2E_POC.md](docs/LOCAL_E2E_POC.md).

## Trwały pilot operatorski

Po zielonym lokalnym POC można uruchomić trwały, zachowywany przebieg poza checkoutem Bridge:

```powershell
.\scripts\Invoke-BDBPersistentPilot.ps1
```

Pilot uruchamia prawdziwy proces `bdb`, osobne repo źródłowe, osobny bare remote Git, finalny `multi_file_patch`, profil `poc_pytest`, publikację wyniku i graceful stop. Pozostawia worktree, Journal, logi i `pilot-report.json`; nie dotyka repozytoriów biznesowych ani `bartosz-dev-poc-control`. Szczegóły: [docs/PERSISTENT_OPERATOR_PILOT.md](docs/PERSISTENT_OPERATOR_PILOT.md).

## Prywatny transport GitHub

Po zielonym trwałym pilocie można uruchomić Bridge w tle przeciwko osobnemu prywatnemu repozytorium `commands/results`:

```powershell
.\scripts\Invoke-BDBGitHubPilot.ps1
```

Bootstrap tworzy wyłącznie sztuczne repo źródłowe, klonuje prywatny kanał GitHub, generuje kanoniczny manifest i `multi_file_patch`, zapisuje ich dokładne ścieżki oraz uruchamia zwykły proces użytkownika w stanie `RUNNING`. Komenda jest następnie dostarczana przez konektor GitHub, a wynik odczytywany z gałęzi `results`. Szczegóły: [docs/GITHUB_REMOTE_PILOT.md](docs/GITHUB_REMOTE_PILOT.md).

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

GHB2-D rozszerza recovery o trwały batch checkpoint, profile outcome, rollback i staging wyniku. Profile nie jest wykonywany ponownie po zapisaniu Journal v10, a committed batch nie zwiększa revision drugi raz.

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
