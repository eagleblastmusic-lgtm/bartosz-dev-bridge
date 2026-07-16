# GHB2-A — Kanoniczne operacje strukturalne

## Cel

GHB2-A ustanawia bezpieczny, deterministyczny format i single-operation engine dla czterech operacji na plikach:

```text
create_file
delete_file
rename_file
move_file
```

Pakiet nie aktywuje jeszcze tych operacji w zdalnym `ExecutionCoordinator`. Istniejący runtime GHB0 nadal wykonuje wyłącznie `replace_exact_and_test`. Trwałe checkpointy, rollback i crash recovery dla operacji strukturalnych należą do GHB2-C; wcześniejsze włączenie zdalnego wykonania tworzyłoby niepełny kontrakt odzyskiwania.

## Schema

Każdy dokument operacji ma:

```json
{
  "schema": "bdb-edit-operation-v1",
  "kind": "..."
}
```

Parser wymaga dokładnego zestawu kluczy dla danego rodzaju. Brakujące i dodatkowe pola są odrzucane. Hash operacji jest SHA-256 kanonicznego, znormalizowanego JSON-u.

## Create

```json
{
  "schema": "bdb-edit-operation-v1",
  "kind": "create_file",
  "path": "pkg/new.bin",
  "content_base64": "AAEC",
  "content_sha256": "sha256:<64 lowercase hex>"
}
```

Wymagania:

- ścieżka jest repo-relative POSIX path;
- plik docelowy nie istnieje;
- katalog nadrzędny już istnieje;
- treść używa kanonicznego RFC 4648 base64;
- hash zgadza się z dokładnymi decoded bytes;
- treść ma maksymalnie 1 MiB;
- engine nie tworzy katalogów i nie nadpisuje pliku.

## Delete

```json
{
  "schema": "bdb-edit-operation-v1",
  "kind": "delete_file",
  "path": "pkg/obsolete.bin",
  "expected_sha256": "sha256:<64 lowercase hex>"
}
```

Usunięcie wymaga istniejącego regularnego pliku o dokładnie oczekiwanym hash. Plan przechowuje exact before bytes, aby przyszły checkpoint GHB2-C mógł odtworzyć plik.

## Rename

```json
{
  "schema": "bdb-edit-operation-v1",
  "kind": "rename_file",
  "source_path": "pkg/old.py",
  "destination_path": "pkg/new.py",
  "expected_source_sha256": "sha256:<64 lowercase hex>"
}
```

`rename_file` wymaga tego samego katalogu nadrzędnego. Zmiana katalogu jest `move_file`.

## Move

```json
{
  "schema": "bdb-edit-operation-v1",
  "kind": "move_file",
  "source_path": "pkg/old.py",
  "destination_path": "archive/old.py",
  "expected_source_sha256": "sha256:<64 lowercase hex>"
}
```

`move_file` wymaga innego katalogu nadrzędnego. Katalog docelowy musi istnieć.

## Polityka ścieżek

Wszystkie source i destination paths przechodzą przez istniejący `WorkspaceManager`:

- lokalne `config.allowed_paths`;
- `manifest.allowed_paths`;
- repo-relative POSIX validation;
- containment w exact session worktree;
- blokadę symlinków, junctions i reparse components.

Parser dodatkowo blokuje operacje na typowych committed secret paths:

```text
.env
.env.*
id_rsa
id_ed25519
credentials.json
service-account.json
*.pem
*.key
*.p12
*.pfx
*.jks
*.keystore
```

## Plan

`StructuralEditEngine.plan()`:

1. rozwiązuje ścieżki przez `WorkspaceManager`;
2. potwierdza istnienie/nieistnienie source i destination;
3. odczytuje bounded exact bytes źródła;
4. sprawdza oczekiwany SHA-256;
5. wylicza exact after bytes;
6. tworzy immutable `StructuralEditPlan` i `plan_sha256`.

Planowanie nie modyfikuje workspace.

Źródło większe niż 1 MiB jest odrzucane, ponieważ GHB2-A zachowuje exact bytes potrzebne do przyszłego rollbacku.

## Apply

`StructuralEditEngine.apply()` najpierw weryfikuje integralność planu, a następnie ponownie sprawdza wszystkie preconditions.

Zasady:

- create zapisuje exact sibling temp, wykonuje flush/fsync, reread verification i atomic promotion;
- delete ponownie porównuje exact bytes przed `unlink`;
- rename/move ponownie porównują source bytes i wymagają absent destination;
- destination nigdy nie jest celowo nadpisywane;
- parent directories są fsyncowane tam, gdzie system to wspiera;
- wynik jest weryfikowany przez exact bytes;
- zwracany `StructuralEditOutcome` ma deterministyczny `outcome_sha256`;
- engine nie wykonuje test profile i nie tworzy commita Git.

Jeżeli exact wewnętrzny temp już istnieje, engine nie usuwa go i przechodzi do `manual_reconciliation_required`.

## Granica trwałości

GHB2-A nie zapisuje planu do Journalu i nie rozszerza migracji. Caller musi zapewnić pojedyncze wykonanie pod istniejącym process lockiem.

Awaria procesu po fizycznej mutacji, ale przed przyszłym durable ACK, nie jest jeszcze automatycznie odzyskiwana. Dlatego:

- operacje nie są włączone do remote runtime;
- GHB2-B może budować multi-file patch i scope validation nad tym formatem;
- GHB2-C doda durable checkpoint, rollback i recovery;
- dopiero GHB2-D zamknie editing gate i dopuści pełny przepływ.

## Poza zakresem

GHB2-A nie dodaje:

- nadpisywania destination;
- tworzenia/usuwania katalogów;
- chmod i zmian symlinków;
- wildcard/glob operations;
- batch/multi-file atomicity;
- patch hunks;
- Git commit/push;
- zdalnego arbitrary shell;
- Journal v9;
- checkpointu, rollbacku i recovery.
