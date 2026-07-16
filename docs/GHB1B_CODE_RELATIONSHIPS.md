# GHB1-B — Code relationships

## Cel i zakres

GHB1-B dodaje deterministyczną statyczną analizę relacji Python dla immutable snapshotu GHB1-A. Analiza nie czyta working tree, nie importuje modułów projektu i nie wykonuje kodu źródłowego.

Zakres v1:

- importy lokalne, względne, aliasy i zależności zewnętrzne;
- references: `call`, `name_read`, `attribute_read`, `decorator`, `base_class`, `annotation`;
- incoming/outgoing references i callers;
- file-level oraz symbol-level dependency edges;
- bounded, cycle-safe traversal grafu;
- deterministyczne wyszukiwanie plików i symboli;
- trwała, atomowa analiza w Journal v8.

Poza zakresem: LSP, Tree-sitter, runtime tracing, ogólne type inference, dynamiczne importy, wildcard resolution, JavaScript/TypeScript relationships, embeddings i background watcher.

## Źródło danych

Analiza wymaga wcześniej utworzonego snapshotu:

```text
bdb bridge repo index --config <path> --ref <commit> --json
```

Następnie analizuje dokładnie ten sam commit:

```text
bdb bridge repo analyze --config <path> --ref <commit> --json
```

Bajty Python są odczytywane przez immutable Git object reader z blobów zapisanych w snapshotcie. Zmiany staged, unstaged, ignored i untracked nie wpływają na wynik.

## Journal v8

Migracja:

```text
journal_v8_code_relationships
```

Tabele:

- `repository_analyses`;
- `repository_imports`;
- `repository_symbol_references`;
- `repository_dependency_edges`.

Klucz analizy:

```text
(repository_id, commit_sha, analysis_version)
```

Zapis jest atomowy. Identyczny replay jest idempotentny, a inna niezmienna zawartość pod tym samym kluczem powoduje `analysis_conflict`. Snapshoty i analizy różnych commitów współistnieją. Relacje mają foreign keys do snapshotu, plików i symboli GHB1-A.

## Resolver Python v1

Resolver oznacza każdą relację statusem:

```text
resolved | unresolved | ambiguous | external | dynamic | unsupported
```

oraz pewnością:

```text
exact | high | heuristic | none
```

Rozwiązywane są między innymi:

- funkcje i klasy w tym samym module i zakresie leksykalnym;
- `from pkg.mod import symbol as alias`;
- `import pkg.mod as alias` i `alias.symbol()`;
- importy względne;
- `self.method()` oraz `cls.method()` przy jednoznacznym celu;
- lokalne/importowane dekoratory, klasy bazowe i proste annotations.

Jako dynamiczne, nierozwiązane lub unsupported pozostają między innymi `getattr`, wildcard import, unknown instance attributes, runtime reassignment, monkeypatching i string annotations wymagające ewaluacji. Parametry i lokalne przypisania zasłaniają zewnętrzne bindingi.

## Wyszukiwanie

Search używa wyłącznie trwałych metadanych GHB1-A: path, symbol name, qualified name, signature i docstring summary. Nie przechowuje ani nie przeszukuje pełnej treści źródła.

Ranking:

1. exact qualified name;
2. exact symbol name;
3. exact path;
4. prefix qualified name;
5. prefix symbol name;
6. prefix path;
7. substring qualified name;
8. substring symbol name;
9. substring path;
10. signature/docstring.

Porównanie używa Unicode `casefold`, a wyniki mają stabilny tie-break.

## References, callers i graf

`callers` zawiera wyłącznie incoming references rodzaju `call` o statusie `resolved`.

Traversal zależności jest BFS, posiada limity:

```text
depth: 1..10
max_nodes: 1..1000
```

Cykle nie powodują nieskończonej pętli. Wynik zawiera `cycle` i `truncated`.

## CLI

```text
bdb bridge repo analyze --config <path> [--ref HEAD] [--json]

bdb bridge repo search --config <path> [--ref HEAD] \
  --query <text> [--kind all|file|symbol] [--limit 50] [--json]

bdb bridge repo references --config <path> [--ref HEAD] \
  (--symbol-id <id> | --path <path> --qualified-name <name>) \
  [--direction incoming|outgoing] [--kind all|call|name_read|attribute_read|decorator|base_class|annotation] \
  [--limit 100] [--json]

bdb bridge repo callers --config <path> [--ref HEAD] \
  (--symbol-id <id> | --path <path> --qualified-name <name>) \
  [--limit 100] [--json]

bdb bridge repo dependencies --config <path> [--ref HEAD] --path <path> \
  [--direction incoming|outgoing] [--depth 1] \
  [--edge-kind all|import|call|reference] [--max-nodes 200] [--json]
```

Cała grupa `repo` zachowuje politykę operatorską `OFFLINE + instance lock`. JSON jest kanoniczny: jeden obiekt, stabilne listy, `sort_keys=True`, bez tracebacków, absolutnych lokalnych ścieżek i pełnej treści źródeł.

## Bezpieczeństwo i ograniczenia

- standardowe `ast`, bez runtime dependencies;
- brak `eval`, `exec`, importowania analizowanego kodu i `shell=True`;
- brak mutujących operacji Git w analizowanym repozytorium;
- bounded diagnostics i query limits;
- brak publicznego API kasowania analiz;
- statyczny resolver v1 nie obiecuje pełnej semantyki Pythona.

Kolejny etap GHB1-C zbuduje context pack i końcową bramkę większego repozytorium na podstawie GHB1-A/B.
