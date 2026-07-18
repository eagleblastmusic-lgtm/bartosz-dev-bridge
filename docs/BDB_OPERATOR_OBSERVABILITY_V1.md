# BDB Operator Observability v1

Status: **P04 / proposed implementation**

## Cel

P04 dostarcza widoki potrzebne przyszłemu Control Center bez zmiany modelu wykonania BDB:

- wersjonowane zdarzenia `bdb-event-v1`;
- snapshot bieżącej operacji `bdb-current-operation-v1`;
- ograniczony snapshot istniejących logów `bdb-log-snapshot-v1`.

Są to projekcje tylko do odczytu. Journal pozostaje źródłem prawdy, a eventy nie stają się nowym event store.

## Źródła

### Journal

Ścieżka Journalu pochodzi z istniejącego `bridge-config.json`. Połączenie SQLite jest otwierane jako:

```text
mode=ro
PRAGMA query_only=ON
```

Warstwa operatorska:

- nie uruchamia migracji;
- nie tworzy tabel;
- nie wykonuje `INSERT`, `UPDATE` ani `DELETE`;
- nie zmienia lifecycle ani stanów komend;
- nie interpretuje braku eventu jako braku trwałego skutku.

### Logi

Czytane są wyłącznie ścieżki zapisane przez preparer w `workspace-loop-state.json`:

- `promoter_stdout`;
- `promoter_stderr`.

Każde źródło jest ograniczone do maksymalnie 65 536 bajtów i 500 linii. Domyślny odczyt zwraca do 200 linii. Brak pliku jest poprawnym, pustym stanem; błąd systemowy odczytu jest raportowany stabilnym kodem.

## Publiczne operacje

### `events`

Parametry:

- `after_event_id` — kursor liczbowy Journalu, domyślnie `0`;
- `limit` — `1..500`, domyślnie `100`;
- opcjonalne `session_id` i `command_id`.

Wynik zawiera eventy w rosnącej kolejności oraz kursor `next_after_event_id`. Pobierany jest jeden dodatkowy rekord, aby obliczyć `has_more` bez osobnego zapytania licznikowego.

Każdy event ma stabilne ID:

```text
journal:<project_alias>:<event_id>
```

`payload_json` jest dekodowany do obiektu. Niepoprawny lub zbyt duży payload nie zatrzymuje całego widoku — event otrzymuje ostrzeżenie i bounded reprezentację.

### `current-operation`

Widok wybiera najnowszą komendę w stanie nieterminalnym:

```text
discovered
validated
claimed
executing
effect_recorded
result_staged
result_published
```

Zwraca wyłącznie podsumowanie: identyfikatory, stan, operację, target, profil, rewizję workspace i wynik. Nie zwraca pełnego patcha, before/after content ani pełnego `command_json`.

### `logs`

Zwraca dwa bounded źródła logów z informacją o istnieniu, rozmiarze, czasie modyfikacji i truncation.

## CLI

```powershell
bdb-operator events --root <workspace> --after-event-id 0 --limit 100
bdb-operator current-operation --root <workspace>
bdb-operator logs --root <workspace> --max-lines 200
```

Operacje są read-only i dostępne niezależnie od platformowego adaptera mutacji Windows.

## Kody błędów P04

- `observability_config_missing`;
- `observability_config_invalid`;
- `journal_missing`;
- `journal_unavailable`;
- `log_read_failed`.

Istniejące `invalid_argument`, `workspace_state_missing`, `workspace_state_invalid` oraz `internal_error` pozostają obowiązujące.

## Bezpieczeństwo i prywatność

- brak listenera sieciowego;
- brak automatycznej publikacji eventów;
- brak monitorowania w tle;
- brak odczytu dowolnej ścieżki podanej bezpośrednio przez użytkownika;
- logi pochodzą tylko z przygotowanego stanu workspace;
- payloady i logi są ograniczone rozmiarem;
- pełny command payload i treści plików nie są częścią `current-operation`.

## Poza zakresem P04

- GUI i polling GUI;
- tray oraz powiadomienia;
- trwały cache eventów;
- nowa tabela eventów;
- modyfikacja Journalu;
- live streaming lub file watching;
- logi rozszerzenia Chrome;
- zdalne przesyłanie telemetrii.

## Bramka wyjścia

- schematy v1 są zapisane;
- wszystkie odczyty Journalu są wymuszone jako read-only;
- eventy mają stabilną tożsamość i kursor;
- snapshot operacji nie ujawnia pełnego command payloadu;
- odczyt logów jest bounded;
- pełna macierz CI pozostaje zielona;
- nie dodano GUI ani procesu działającego w tle.
