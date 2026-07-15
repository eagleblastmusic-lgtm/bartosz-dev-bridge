# Bartosz Dev Bridge

Minimalna implementacja **POC-0** lokalnego Bridge'a dla ChatGPT Plus i GitHuba, zgodna z dokumentacją projektową v1.1.

Aktualna faza:

```text
GHB0-3 — Durable ingestion i pojedyncza kolejka
```

Zakres obejmuje:

- pakiet `bdb_bridge` ze stabilnymi granicami protokołu v1.1 (walidacja, serializacja, konfiguracja, modele);
- **SQLite Journal v2** — trwały moduł `bdb_bridge.journal` z wersjonowanymi migracjami (v1 + v2), idempotency komend i wyników, compare-and-swap workspace, append-only event log oraz warstwą ingestion;
- **durable command ingestion** — `CommandIngestor` zapisuje manifesty i komendy lokalnie przed wykonaniem, wznawia walidację po restarcie, obsługuje TTL i kolizje;
- **immutable transport snapshots** — `GitCommandTransport` rozwiązuje jeden `snapshot_sha` i odczytuje dokumenty oraz per-document commit SHA z tego samego snapshotu;
- **persisted transport backoff** — retry transportu z exponential backoff zapisany w `ingestion_sources`;
- **single active session** i **atomic command claim** — `SingleQueueScheduler` / `Journal.claim_next_command()` w jednej transakcji SQLite;
- warstwę wykonawczą `bdb_poc` z zachowaną kompatybilnością importów POC-0 (`PocBridge` bez integracji z nowym ingestorem);
- jednorazowy `poc_bridge.py`;
- syntetyczne repozytorium `bdb-poc-fixture`;
- polling branchu `commands` (POC-0);
- publikację małych wyników na branch `results` (POC-0);
- operacje `open_read` i `replace_exact_and_test` (POC-0);
- stały lokalny profil `python -m pytest -q`;
- testy jednostkowe/integracyjne oraz GitHub Actions.

Journal v2 jest fundamentem danych dla ingestion i schedulera. **Nie wykonuje jeszcze operacji komend**, nie tworzy worktree, nie publikuje wyników przez outbox i nie jest podłączony do działającego `PocBridge`.

Przykład ingestion + scheduler:

```python
from bdb_bridge import CommandIngestor, Journal, SingleQueueScheduler
from bdb_poc.transport import GitCommandTransport

journal = Journal.open("path/to/journal.db")
ingestor = CommandIngestor(journal, GitCommandTransport(control_repo_path))
ingestor.poll_once()
claimed = SingleQueueScheduler(journal).claim_next()
journal.close()
```

Schemat obejmuje tabele: `schema_migrations`, `sessions`, `commands`, `workspaces`, `results`, `events`, `ingestion_sources`, `session_ingestion`, `command_ingestion`, `ingestion_issues`.

Poza zakresem pozostają wykonywanie operacji komend, recovery worktree, durable result outbox, daemon, automatyczne wznawianie w runtime, GUI, LSP, Browser Lab, Hermes, prawdziwe repozytoria GicleeApp oraz operacje produkcyjne.

Instrukcja uruchomienia:

```text
POC_0_WINDOWS_START.md
```

Repozytorium nie może zawierać tokenów, sekretów, plików `.env` ani prywatnych danych użytkownika.
