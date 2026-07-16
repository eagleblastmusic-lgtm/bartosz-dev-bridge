# GHB1-C — Context pack i large-repository gate

## Cel

GHB1-C zamyka fazę repository intelligence. Dodaje deterministyczny, ograniczony budżetem context pack oraz końcową bramkę większego repozytorium opartą wyłącznie na immutable snapshotach GHB1-A i relacjach GHB1-B.

Context pack nie czyta working tree, nie importuje modułów projektu i nie wykonuje kodu. Źródła są pobierane z dokładnych blobów Git wskazanych przez snapshot.

## Wymagany stan

Dla tego samego commita muszą istnieć:

```text
bdb bridge repo index --config <path> --ref <commit> --json
bdb bridge repo analyze --config <path> --ref <commit> --json
```

Brak snapshotu zwraca `snapshot_not_found`; brak analizy — `analysis_not_found`.

## Context pack v1

Wersja kontraktu:

```text
ghb1c-v1
```

Seed jest dokładnie jednym z:

- query;
- symbol ID;
- repo-relative path;
- repo-relative path + dokładny qualified name.

Przykłady:

```text
bdb bridge repo context --config <path> --ref <commit> \
  --query "WorkspaceLifecycle" --direction both --depth 2 \
  --max-files 20 --max-bytes 65536 --max-excerpt-lines 80 --json

bdb bridge repo context --config <path> --ref <commit> \
  --path bdb_bridge/service.py --qualified-name BridgeService.run \
  --direction incoming --depth 2
```

Domyślne wyjście jest deterministycznym Markdownem. `--json` zwraca jeden kanoniczny obiekt JSON.

## Selekcja

Kolejność kandydatów jest stabilna:

1. plik seeda;
2. dopasowania search według rankingu GHB1-B;
3. incoming/outgoing references wybranego symbolu;
4. file dependency edges do wskazanej głębokości.

Remisy są rozstrzygane przez repo-relative POSIX path. Traversal korzysta z przygotowanych map adjacency, więc nie skanuje całego grafu osobno dla każdego odwiedzonego węzła.

## Excerpts i budżety

Limity v1:

```text
depth: 0..3
max_files: 1..50
max_bytes: 1024..262144
max_excerpt_lines: 1..200
```

`max_bytes` ogranicza łączną liczbę bajtów UTF-8 fragmentów źródła. Metadane packu nie są liczone do tego budżetu.

Fragmenty:

- zaczynają się od zakresu seeda lub dopasowanego symbolu;
- obejmują miejsca incoming references;
- obejmują definicje jednoznacznych outgoing targets;
- dla pliku bez dokładnego hintu używają pierwszych symboli outline albo początku pliku;
- są numerowane liniami;
- mają deterministyczną kolejność.

Pełne źródło nie jest zapisywane do Journalu. Context pack powstaje on demand i nie ma migracji Journal v9.

Pliki binary, symlink i submodule są metadata-only. Regularny plik większy niż 1 MiB również nie jest wczytywany do packu. Typowe committed secret paths (`.env*`, klucze prywatne, keystore oraz jawne pliki credentials/service-account) są zawsze metadata-only z `omitted_reason=sensitive_path`. Każdy pozostały odczytany blob jest weryfikowany względem trwałego `content_sha256` snapshotu.

## Identyczność

Pack nie zawiera timestampu. `pack_sha256` jest SHA-256 tej samej kanonicznej reprezentacji JSON (`sort_keys=True`, zwarte separatory, standardowe escaping Unicode), której używa CLI, dla wszystkich pól packu poza samym `pack_sha256`.

Ten sam repository ID, commit, seed i limity dają identyczny wynik na Windows i Linux. Zmiany staged, unstaged, ignored oraz untracked nie wpływają na wynik.

## Large-repository gate

Komenda:

```text
bdb bridge repo gate --config <path> --ref <commit> --json
```

Gate sprawdza:

- spójność deklarowanych i rzeczywistych liczników snapshotu;
- spójność liczników analizy;
- limity liczby plików, symboli i relacji;
- możliwość utworzenia deterministycznego przykładowego context packu;
- dotrzymanie budżetu sample packu;
- poprawność sample `pack_sha256`.

Domyślne safety caps:

```text
files: 200000
symbols: 2000000
relationships: 5000000
sample files: 20
sample source bytes: 32768
```

Progi można obniżyć przez CLI, aby użyć gate jako polityki konkretnego repozytorium. Niespełniony check daje `passed=false` i exit code 1. Błąd braku danych lub konfiguracji pozostaje controlled error.

Gate nie wykonuje benchmarku zależnego od szybkości współdzielonego runnera. Sprawdza deterministyczność, spójność i bounded behavior, dzięki czemu nie wprowadza kruchego limitu czasu do CI.

## Bezpieczeństwo

- wyłącznie immutable Git blobs;
- strict UTF-8 i kontrola content SHA-256;
- brak `eval`, `exec`, runtime import i `shell=True`;
- brak operacji mutujących analizowane repozytorium;
- brak pełnego źródła w SQLite;
- brak absolutnych lokalnych ścieżek w output;
- `OFFLINE + instance lock` dla `context` i `gate`;
- bounded selector, depth, file, byte i line limits.

## Zakres dalszy

GHB1-C nie dodaje semantycznego LLM search, embeddings, vector database, LSP ani watchera. Następna faza GHB-2 wykorzysta context pack jako kontrolowane wejście dla operacji edycyjnych.
