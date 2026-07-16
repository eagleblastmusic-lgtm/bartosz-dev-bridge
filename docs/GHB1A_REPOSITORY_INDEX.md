# GHB1-A — Repository Index

## Cel i granice

GHB1-A dodaje pierwszy trwały moduł inteligencji kodowej Bartosz Dev Bridge:

- deterministyczny indeks niezmiennego commita Git;
- inwentarz wszystkich śledzonych plików;
- ekstrakcję symboli Python (`ast` only);
- kanoniczny outline pojedynczego pliku;
- atomowy zapis snapshotu w Journal (migracja v7);
- komendy CLI `bdb bridge repo …`.

Poza zakresem GHB1-A: embeddings, semantic search, LLM, watcher, background indexing, blob cache, parsery innych języków, pełna treść źródeł w SQLite, GUI oraz indeksowanie repozytorium control.

## Źródło danych

Indeks budowany jest wyłącznie z obiektów Git wskazanego commita (`config.fixture_repo_path` + `--ref`, domyślnie `HEAD`):

1. `git rev-parse --verify --end-of-options <ref>^{commit}`
2. tree SHA commita
3. `git ls-tree -r -z --long`
4. bajty blobów przez `git cat-file`

Working tree (staged/unstaged/untracked/ignored) nie wpływa na snapshot. Indekser nie wykonuje `checkout`, `fetch`, `pull`, `reset` ani kodu z indeksowanego repozytorium. Ref zaczynający się od `-` jest odrzucany, a argumenty Git są przekazywane przez `subprocess` z `shell=False`.

Symlink i submodule/gitlink są metadata-only. Dla symlinku hash obejmuje dokładne bajty blobu celu linku zapisanego w Git. Gitlink nie jest klasyfikowany jako tekst, nie ma `line_count`, a jego deterministyczna tożsamość opiera się na SHA wskazanego commita.

## Journal v7

Migracja: `journal_v7_repository_index`.

Tabele:

- `repository_snapshots` — klucz `(repository_id, commit_sha)`
- `repository_files` — klucz `(repository_id, commit_sha, path)`
- `repository_symbols` — klucz `(repository_id, commit_sha, symbol_id)`

Snapshot jest widoczny atomowo albo wcale. Ponowne indeksowanie tego samego `(repository_id, commit_sha)` jest idempotentne; konflikt niezmiennej zawartości zgłasza `journal_conflict`. Brak publicznego API kasowania snapshotów. Snapshoty różnych commitów współistnieją.

## Klasyfikacja plików

- tekst = brak `NUL` + strict UTF-8;
- `content_sha256` z dokładnych bajtów regularnego blobu albo symlinku; gitlink używa deterministycznych bajtów SHA commita jako metadanych;
- `line_count` tylko dla tekstu;
- submodule/gitlink zawsze `metadata_only`, `is_text=false`, `line_count=null`;
- języki po rozszerzeniu (python, markdown, json, yaml, toml, javascript, typescript, css, html, liquid, powershell, shell, plain_text, unknown);
- symbole tylko dla Python;
- limit parsowania: `MAX_PARSE_BYTES = 1 MiB` → `parse_status=too_large`.

`parse_status`: `ok`, `unsupported_language`, `syntax_error`, `too_large`, `binary`, `metadata_only`.

## Symbole Python v1

Parser używa wyłącznie `ast.parse`. Rodzaje: `class`, `function`, `async_function`, `method`, `async_method`, `nested_function`, `nested_async_function`, `nested_class`.

Definicje są odkrywane także w ciałach instrukcji złożonych, takich jak `if`, `for`, `while`, `with`, `try` i `match`, bez wykonywania kodu. `qualified_name` odzwierciedla hierarchię (`Outer.Inner.method`). `symbol_id` to SHA-256 kanonicznego zestawu identity + zakres źródłowy. Outline jest preorder według położenia w źródle. Błąd składni nie przerywa snapshotu.

Nazwy dekoratorów są ograniczone liczbowo i długościowo przed zapisem, aby zachować ograniczony, bezpieczny rekord Journal.

## Semantyka idempotencji

Jeśli snapshot już istnieje i recomputed inventory (tree, liczniki, pliki, symbole) jest identyczny, CLI/API zwraca istniejący snapshot z `idempotent=true` bez nadpisywania. Różnica niezmiennych pól → `journal_conflict`.

## Kontrakt CLI

Wszystkie komendy GHB1-A wymagają `OFFLINE` + instance lock (spójna polityka operatorska).

```text
bdb bridge repo index --config <config.json> [--ref HEAD] [--json]
bdb bridge repo status --config <config.json> [--ref HEAD] [--json]
bdb bridge repo files --config <config.json> [--ref HEAD] [--json]
bdb bridge repo outline --config <config.json> --path <posix-path> [--ref HEAD] [--json]
```

JSON: jeden obiekt + `\n`, `sort_keys=True`, separators `(',', ':')`, bez absolutnych ścieżek lokalnych, sekretów i tracebacków.

## Bezpieczeństwo

- ścieżki walidowane jako repo-relative POSIX (`validate_repo_relative_path`);
- diagnostyki przez `sanitize_diagnostics`;
- lock i Journal zamykane w `finally`;
- brak usuwania worktree/Journal/repozytorium;
- brak wykonywania indeksowanego kodu;
- brak importowania modułów analizowanego projektu;
- brak mutujących komend Git w indeksowanym repozytorium.

## Znane ograniczenia / kolejne etapy

- brak incremental cache i watchera;
- brak symboli poza Python;
- brak embeddings / semantic search;
- brak automatycznego indeksowania w `BridgeService`;
- brak publicznego cleanupu snapshotów.
