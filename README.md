# Bartosz Dev Bridge

Lokalny Bridge dla ChatGPT Plus i GitHuba, rozwijany etapami na podstawie potwierdzonego POC-0.

Aktualna faza:

```text
GHB0-6 — Service lifecycle i pojedyncza instancja
```

## CLI i Cykl Życia

Usługa działa w pętli obsługującej cztery fazy (Recovery, Outbox, Ingestion, Execution) i jest zarządzana za pomocą CLI:

- `bdb bridge start --config <path> [--foreground] [--background]` - Uruchomienie usługi w tle lub pierwszoplanowo.
- `bdb bridge stop --config <path>` - Wysłanie żądania bezpiecznego i graceful zatrzymania usługi.
- `bdb bridge status --config <path> [--json]` - Odpytanie o stan usługi (OFFLINE, RUNNING, STOPPING, STALE).

### Stany Usługi

- `OFFLINE`: Usługa nie działa i lock jest wolny.
- `RUNNING`: Usługa działa i regularnie odświeża heartbeat.
- `STOPPING`: Wysłano żądanie zatrzymania, usługa kończy aktualny cykl.
- `STALE`: Lock jest trzymany, ale proces o danym PID nie żyje lub nie odświeżał heartbeatu przez ustalony czas.

### Gwarancje Jednej Instancji

- Wykorzystanie platformowych blokad plików (`InstanceLock` z `msvcrt` na Windows i `flock` na POSIX).
- Kontrola kolizji blokad (contention mapowana na `INSTANCE_ALREADY_RUNNING`, uszkodzenia filesystemu na `INSTANCE_LOCK_FAILED`).
- `HeartbeatWorker` działa w osobnym wątku (`daemon=False`) z deterministycznym startem i bezpieczną procedurą `join()`.

### Błędy SQLite i Invarianty

- Wszystkie odczyty z publicznych getterów bazy są zabezpieczone przed propagacją surowych `sqlite3.Error`.
- Wykrycie niespójności w bazie danych (np. >1 aktywnej instancji) mapuje rekord na `JOURNAL_CORRUPT` i zatrzymuje usługę.
- Transient błędy sieciowe/transportowe nie przerywają pętli głównej, podczas gdy fatal błędy (np. korupcja bazy, błędy stanów) natychmiast wyłączają usługę i zwalniają lock.
