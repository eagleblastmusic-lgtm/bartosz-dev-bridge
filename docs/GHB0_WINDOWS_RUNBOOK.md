# GHB-0 — Windows Runbook

Ten runbook dotyczy lokalnego Bridge uruchamianego jako zwykły proces użytkownika. Nie używa Windows Service, Scheduled Task, uprawnień administratora ani zewnętrznego mechanizmu zabijania PID.

## Instalacja

```powershell
cd C:\Projekty\DevMaster\bartosz-dev-bridge
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Nie usuwaj istniejącej `.venv`. Jeżeli środowisko już istnieje, wykonaj tylko instalację pakietu.

## Lifecycle

```powershell
.venv\Scripts\bdb bridge start --config C:\...\config.json --foreground
.venv\Scripts\bdb bridge start --config C:\...\config.json --background
.venv\Scripts\bdb bridge status --config C:\...\config.json --json
.venv\Scripts\bdb bridge stop --config C:\...\config.json
```

Stany publiczne:

- `RUNNING` — aktywny proces posiada wspólny OS lock i aktualizuje heartbeat;
- `STOPPING` — zapisano trwałe żądanie graceful stop;
- `STALE` — PID, heartbeat, rekord instancji i lock nie tworzą spójnego aktywnego procesu;
- `OFFLINE` — brak aktywnej instancji, a wspólny lock jest wolny.

Nie kasuj lock file ani Journalu. Nie używaj `taskkill`, `TerminateProcess`, `Stop-Process` ani ręcznego zabijania childa jako procedury operatorskiej.

## Finalizacja sesji

Finalizacja jest jawna i nie implementuje protocol ACK. `RESULT_PUBLISHED` pozostaje bez przejścia do `ACKNOWLEDGED`.

```powershell
.venv\Scripts\bdb bridge session finalize --config C:\...\config.json --session-id <uuid>
```

Finalizacja wymaga stanu `OFFLINE`, wspólnego OS locka, sesji `ACTIVE`, braku recoverable command, pending/collision outbox, blocking ingestion issue i manual reconciliation. Przejście jest transakcyjne:

```text
ACTIVE → COMPLETING → COMPLETED
```

Po finalizacji worktree pozostaje zachowany.

## Workspace status i preserve

```powershell
.venv\Scripts\bdb bridge workspace status --config C:\...\config.json --session-id <uuid> --json
.venv\Scripts\bdb bridge workspace preserve --config C:\...\config.json --session-id <uuid>
```

Domyślną polityką jest `preserve`. `preserve` jest idempotentne, nie usuwa plików i nie zmienia stanu command/session. Dla manual reconciliation jest jedyną bezpieczną dyspozycją.

## Jawny cleanup

```powershell
.venv\Scripts\bdb bridge workspace cleanup --config C:\...\config.json --session-id <uuid> --confirm-session-id <uuid>
```

Cleanup jest opt-in i wymaga dokładnie identycznego UUID w obu argumentach. Jest dozwolony wyłącznie dla kwalifikującej się sesji `COMPLETED`, gdy service jest `OFFLINE` i wspólny OS lock został realnie zdobyty.

Stany cleanupu:

- `cleanup_requested` — żądanie zapisane, fizyczne usuwanie jeszcze się nie rozpoczęło;
- `removing` — eligibility zostało ponownie sprawdzone i rozpoczęto kontrolowaną operację;
- `removed` — brak target path i registration został potwierdzony, a local ACK zapisany;
- `blocked` — co najmniej jeden warunek bezpieczeństwa nie przeszedł; dyspozycja wraca do `preserve`.

Jedyną dozwoloną operacją fizyczną jest:

```text
git -C <fixture_repo> worktree remove --force <exact_workspace_path>
```

Bridge nie używa `rmtree`, `Remove-Item -Recurse`, `rmdir /s`, `git clean`, `git reset` ani `git worktree prune`.

## Recovery i publikacja

- `RESULT_STAGED` oznacza, że wynik i outbox są trwałe, ale publikacja może jeszcze oczekiwać;
- pending outbox jest przetwarzany po restarcie przed nowym ingestion;
- collision resultu przechodzi do manual reconciliation i zachowuje worktree;
- divergent workspace nie jest nadpisywany i zostaje oznaczony `preserve`;
- `cleanup_requested` i `removing` są odzyskiwane idempotentnie po restarcie.

Bezpieczna inspekcja divergent workspace:

```powershell
.venv\Scripts\bdb bridge workspace status --config C:\...\config.json --session-id <uuid> --json
git -C C:\...\fixture-repo worktree list --porcelain
git -C C:\...\worktrees\<uuid> status --porcelain=v1
git -C C:\...\worktrees\<uuid> diff --
```

Nie rozwiązuj divergence przez `reset --hard`, `git clean`, ręczne kasowanie katalogu ani prune.

## Recovery gate

```powershell
.\scripts\Invoke-GHB0RecoveryGate.ps1
.\scripts\Invoke-GHB0RecoveryGate.ps1 -Python ".venv\Scripts\python.exe"
```

Skrypt kompiluje runtime, wykonuje siedem świeżych sesji recovery A–G, targeted lifecycle, regresje POC-0A/POC-0B oraz pełną suite. Canonical JSON report jest zapisywany do `artifacts\ghb0-gate\recovery-gate.json` i nie jest commitowany.
