# Bartosz Dev Bridge

Lokalny Bridge dla ChatGPT Plus i GitHuba, rozwijany etapami na podstawie potwierdzonego POC-0.

Aktualna faza:

```text
GHB0-5 — Result staging i durable outbox
```

## Zakres GHB0-5

Rdzeń zachowuje protokół `1.1`, recovery worktree z GHB0-4 oraz dotychczasowe granice bezpieczeństwa. Nowa warstwa wyników obejmuje:

- `ResultStager`, który buduje deterministyczny wynik wyłącznie z trwałych rekordów session, command, plan, effect oraz kontrolowanego `ExecutionOutcome`;
- `finalize_result()` jako jedyny finalny serializer, bez zmiany limitu 16 KiB ani end markera;
- exact UTF-8 bytes bez BOM, dopisywania newline i normalizacji końców linii;
- SHA-256 liczony z pełnych exact staged bytes;
- atomowe `result + outbox + RESULT_STAGED + events` w jednym `BEGIN IMMEDIATE`;
- tabelę SQLite `outbox` z persisted attempt count, next-attempt time, bounded diagnostic oraz trwałymi stanami `pending`, `published` i `collision`;
- deterministyczny backoff `1, 2, 4, 8…`, ograniczony do 60 sekund, bez jittera i bez `sleep`;
- `OutboxProcessor`, którego pojedyncze wywołanie wykonuje najwyżej jedną próbę publikacji jednego wpisu;
- `ResultTransport` i `GitResultTransport` publikujące dokładnie jeden result path na branchu `results`, bez force-pusha;
- ponowny odczyt exact remote bytes po pushu zamiast zaufania samemu exit code;
- idempotentne rozpoznanie identycznego istniejącego remote resultu;
- trwałe `RESULT_COLLISION` i przejście command/session do `MANUAL_RECONCILIATION_REQUIRED` bez nadpisania zdalnej treści;
- recovery po crashu po pushu, ale przed lokalnym ACK, bez drugiego remote commita;
- preservation session worktree oraz brak ponownego patcha po awarii publikacji.

## Recovery contract

```text
EFFECT_RECORDED + brak staged result
  → bez ponownego patcha dokończ profil/result
  → atomowo stage result i enqueue outbox

RESULT_STAGED
  → nie uruchamiaj operacji ani profilu
  → publikuj wyłącznie immutable staged result

remote path absent
  → jedna próba publikacji exact bytes

remote path identical
  → RESULT_PUBLISHED bez kolejnego pushu

remote path different
  → outbox collision
  → command/session MANUAL_RECONCILIATION_REQUIRED

push success + crash przed lokalnym ACK
  → po restarcie odczytaj remote path
  → identyczny hash oznacza RESULT_PUBLISHED bez ponownego commita
```

## Granice

GHB0-5 nie implementuje jeszcze:

- daemona ani pętli działającej stale;
- CLI `start`, `stop` i `status`;
- instance lock, PID lub heartbeat;
- ACK protocol ani przejścia do `ACKNOWLEDGED`;
- cleanupu session worktree;
- uploadu artefaktów;
- GUI, LSP, Browser Lab ani Hermesa;
- integracji z GicleeApp;
- nowych operacji lub ogólnego terminala;
- podłączenia nowego runtime do legacy `PocBridge`.

Legacy POC-0 pozostaje dostępny przez `bdb_poc` i `poc_bridge.py`, ale nie używa durable outboxu GHB0-5.

Repozytorium nie może zawierać tokenów, sekretów, plików `.env` ani prywatnych danych użytkownika.
