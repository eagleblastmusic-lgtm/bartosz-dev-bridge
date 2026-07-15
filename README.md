# Bartosz Dev Bridge

Minimalna implementacja **POC-0** lokalnego Bridge'a dla ChatGPT Plus i GitHuba, zgodna z dokumentacją projektową v1.1.

Aktualna faza:

```text
GHB0-2 — SQLite Journal v1
```

Zakres obejmuje:

- pakiet `bdb_bridge` ze stabilnymi granicami protokołu v1.1 (walidacja, serializacja, konfiguracja, modele);
- **SQLite Journal v1** — trwały, transakcyjny moduł `bdb_bridge.journal` z wersjonowanymi migracjami, idempotency komend i wyników, compare-and-swap workspace oraz append-only event logiem;
- warstwę wykonawczą `bdb_poc` z zachowaną kompatybilnością importów POC-0;
- jednorazowy `poc_bridge.py`;
- syntetyczne repozytorium `bdb-poc-fixture`;
- polling branchu `commands`;
- publikację małych wyników na branch `results`;
- operacje `open_read` i `replace_exact_and_test`;
- stały lokalny profil `python -m pytest -q`;
- jedno worktree i jedną aktywną sesję;
- testy jednostkowe/integracyjne oraz GitHub Actions.

Journal v1 jest niezależnym fundamentem danych i **nie jest jeszcze podłączony** do działającego `PocBridge`, pollingu GitHuba ani worktree runtime.

SQLite Journal:

```python
from bdb_bridge import Journal

journal = Journal.open("path/to/journal.db")
journal.create_session(session_id, repository_id, base_sha)
journal.record_command(session_id, command_id, sequence, command_dict)
journal.store_result(command_id, result_json, remote_path)
journal.close()
```

Schemat obejmuje tabele: `schema_migrations`, `sessions`, `commands`, `workspaces`, `results`, `events`.

Poza zakresem pozostają daemon, recovery, outbox, automatyczne wznawianie komend, GUI, LSP, Browser Lab, Hermes, wielosesyjność, prawdziwe repozytoria GicleeApp oraz operacje produkcyjne.

Instrukcja uruchomienia:

```text
POC_0_WINDOWS_START.md
```

Repozytorium nie może zawierać tokenów, sekretów, plików `.env` ani prywatnych danych użytkownika.
