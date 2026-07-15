# Bartosz Dev Bridge

Lokalny Bridge dla ChatGPT Plus i GitHuba, rozwijany etapami na podstawie potwierdzonego POC-0.

Aktualna faza:

```text
GHB0-4 — Workspace recovery i effect journal
```

## Zakres GHB0-4

Rdzeń zachowuje protokół `1.1` oraz dotychczasowe granice bezpieczeństwa. Nowa warstwa wykonawcza obejmuje:

- kontrolowany `WorkspaceManager` dla detached worktree z exact base SHA;
- trwały, immutable operation plan zapisany przed zmianą pliku;
- exact-byte SHA-256 dla treści przed i po edycji;
- atomowy file effect: temp w katalogu targetu, flush, `fsync`, `os.replace` i ponowny odczyt bytes;
- atomowy effect journal: workspace CAS, effect row, przejście command i event w jednym `BEGIN IMMEDIATE`;
- idempotentne replaye planu i effectu z kontrolą wszystkich immutable pól;
- recovery rozróżniający stany `BEFORE`, `PLANNED-AFTER`, `EFFECT` i `DIVERGED`;
- atomowe przejście command/session do `MANUAL_RECONCILIATION_REQUIRED`;
- zachowanie worktree i plików diagnostycznych przy failure lub divergence;
- jedyny profil wykonawczy `poc_pytest`, uruchamiany jako `<python_executable> -m pytest -q` bez `shell=True`;
- testy fault-injection, migration golden checksums oraz regresje Windows/Ubuntu.

## Recovery contract

```text
BEFORE
  target i physical state odpowiadają planowi before
  → wykonaj lub dokończ zweryfikowany własny temp artifact

PLANNED-AFTER
  target odpowiada planned-after, ale effect nie jest jeszcze zapisany
  → zapisz effect bez ponownego patcha

EFFECT
  effect, Journal i physical state są zgodne
  → nie stosuj patcha ponownie; profil może zostać ponowiony

DIVERGED
  dowolna inna kombinacja stanu, obcy plik, zły HEAD lub mismatch Journalu
  → MANUAL_RECONCILIATION_REQUIRED, bez cleanupu i bez usuwania dowodów
```

Własny temp artifact jest rozpoznawany wyłącznie po ścieżce wynikającej z persisted plan hash. Jego bytes i hash muszą dokładnie odpowiadać `planned_after_content`. Obce temp/untracked pliki nie są usuwane.

## Granice

GHB0-4 nie implementuje jeszcze:

- result outbox ani zdalnej publikacji wyników nowego runtime;
- daemona, service lifecycle lub instance lock;
- cleanupu worktree;
- GUI, LSP, Browser Lab ani Hermesa;
- integracji z GicleeApp;
- podłączenia `ExecutionCoordinator` do legacy `PocBridge`;
- nowych operacji ani ogólnego terminala.

Legacy POC-0 pozostaje dostępny przez `bdb_poc` i `poc_bridge.py`, ale nowy execution runtime nie jest z nim jeszcze połączony.

Repozytorium nie może zawierać tokenów, sekretów, plików `.env` ani prywatnych danych użytkownika.
