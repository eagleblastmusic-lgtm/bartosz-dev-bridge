# Bartosz Dev Bridge

Lokalny Bridge dla ChatGPT Plus i GitHuba, rozwijany etapami na podstawie potwierdzonego POC-0.

Aktualna faza:

```text
GHB0-6 — Service lifecycle i pojedyncza instancja
```

## CLI

```text
bdb bridge start --config <path> --foreground
bdb bridge start --config <path> --background   # tylko Windows
bdb bridge stop --config <path>
bdb bridge status --config <path> [--json]
```

Tryb background na Windows uruchamia zwykły proces użytkownika. Nie tworzy Windows Service, Scheduled Task ani procesu administracyjnego. Child sam zdobywa platformowy OS lock i sam prowadzi graceful lifecycle.

## Cykl usługi

Każdy cykl zachowuje kolejność:

```text
recovery → pending outbox → ingestion → execution
```

Pomiędzy bezpiecznymi fazami sprawdzany jest trwały stan zatrzymania. Żądanie `STOPPING` zapisane podczas długiej fazy execution nie przerywa patcha ani profilu w połowie: faza kończy się bezpiecznie, po czym usługa przechodzi bezpośrednio do końcowego `OFFLINE`, bez dodatkowego pełnego `idle_poll_seconds`.

## Stany lifecycle

- `OFFLINE` — brak aktywnej instancji i OS lock jest wolny;
- `RUNNING` — aktywna instancja posiada lock, PID i aktualizowany heartbeat;
- `STOPPING` — trwałe żądanie graceful stop zostało zapisane i jest obserwowalne;
- `STALE` — baza, PID, heartbeat i stan locka nie tworzą spójnej aktywnej instancji.

Końcowe zatrzymanie zapisuje `STOPPED`, po czym publiczny status staje się `OFFLINE`. Journal, lock file, session worktree i inne durable markery nie są usuwane przez lifecycle.

## Pojedyncza instancja

`InstanceLock` korzysta z:

- `msvcrt.locking` na Windows;
- `fcntl.flock` na POSIX.

Contention jest mapowane na `INSTANCE_ALREADY_RUNNING`. Awaria backendu, ścieżki albo uprawnień jest mapowana na `INSTANCE_LOCK_FAILED` i nie jest traktowana jak zwykłe zajęcie locka. Drugi realny proces nie może rozpocząć działania, gdy lock należy do aktywnej instancji.

## Heartbeat

`HeartbeatWorker` działa jako osobny, niedaemonowy wątek i otwiera własne połączenie SQLite na podstawie ścieżki Journalu. Nie współdzieli głównego obiektu `Journal` usługi. Heartbeat jest aktualizowany także wtedy, gdy bezpieczna faza execution trwa przez kilka interwałów, a worker kończy się przez kontrolowany `stop()` i `join()`.

## Recovery po ponownym otwarciu

Po restarcie nowy Journal i nowy graph service/coordinator obsługują trwałe stany:

```text
CLAIMED         → recovery bez drugiego claimu
EXECUTING       → recovery bez ponownego patcha
EFFECT_RECORDED → dokończenie profilu/resultu bez ponownego patcha
RESULT_STAGED   → wyłącznie outbox/publication
```

Workspace revision rośnie dokładnie o jeden, plan i effect pozostają pojedyncze, a staged result/outbox są idempotentne. Recovery jest wykonywane przed pending outbox, a pending outbox przed nowym ingestion.

## Fault points

GHB0-6 zachowuje kontrolowane punkty awarii, między innymi:

- `AFTER_INSTANCE_LOCK_BEFORE_DB_START` — proces kończy się bez active service row, a OS zwalnia lock;
- `AFTER_EXECUTE_CLAIM` — trwały `CLAIMED` jest odzyskiwany po ponownym otwarciu bez drugiego claimu i bez podwójnego patcha/effectu.

## Background preflight

Przed uruchomieniem child process sprawdzane są Journal i publiczny status. Child nie jest uruchamiany przy:

- uszkodzonym lub nieobsługiwanym schemacie Journalu;
- błędzie status readera;
- `INSTANCE_LOCK_FAILED`;
- permission error;
- nieprawidłowej konfiguracji;
- stanie `RUNNING` lub `STOPPING`.

Tolerowany jest wyłącznie prawidłowy stan nieaktywnej instancji. Diagnostyka jest ograniczona i sanitizowana; błędy nie są ignorowane przez szerokie `except Exception: pass`.

## Granice bezpieczeństwa

GHB0-6 nie dodaje:

- `taskkill`, `TerminateProcess` ani zabijania procesu podczas graceful stop;
- PowerShella do sterowania usługą;
- Windows Service ani Scheduled Task;
- usuwania Journalu lub session worktree;
- daemonowego cleanupu;
- nowych operacji, ogólnego terminala, GUI, Hermesa ani integracji z GicleeApp;
- zależności `bdb_bridge → bdb_poc`.

Legacy POC-0 pozostaje dostępny przez `bdb_poc` i `poc_bridge.py`.
