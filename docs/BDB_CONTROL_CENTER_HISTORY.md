# BDB Control Center — historia zdarzeń, sesji i receipts

Status: IMPLEMENTED ON BRANCH

## Cel

Ekran `History` udostępnia dwa rozdzielone, ręcznie odświeżane widoki:

1. `Zdarzenia Journalu` — techniczna, stronicowana historia eventów;
2. `Sesje i receipts` — bounded podsumowania sesji, prób, trwałych wyników, checkpointów, promocji i jawnych grup naprawczych.

Oba widoki korzystają wyłącznie z publicznego Operator API. GUI nie otwiera SQLite, nie wykonuje SQL i nie modyfikuje trwałego stanu.

## Historia zdarzeń Journalu

GUI korzysta z:

```text
OperatorApi.events(workspace_root, after_event_id, limit, session_id, command_id)
```

### Ograniczenia

Każde żądanie ma:

- `after_event_id >= 0`;
- limit od 1 do 500;
- opcjonalny exact `session_id`;
- opcjonalny exact `command_id`.

Domyślny limit strony wynosi 100. GUI nie udostępnia operacji „wczytaj wszystko”.

### Kontrakty

Operator API zwraca `bdb-event-v1`. GUI mapuje stronę do:

- `bdb-gui-history-v1`;
- `bdb-gui-event-v1`.

Snapshot ma zawsze:

- `read_only=true`;
- `mutation_operations_invoked=0`;
- typowany kursor;
- dokładne filtry;
- identyfikator operacji Operator API.

GUI odrzuca odpowiedź, gdy sekwencje nie są ściśle rosnące, kursory lub filtry różnią się od żądania albo payload ma niepoprawny typ.

### Widok i paginacja

Tabela pokazuje sequence, czas, severity, event type, session ID i command ID. Wybrany wiersz pokazuje pełny, już ograniczony dokument wydarzenia.

`Odśwież historię` zaczyna od kursora 0. `Wczytaj więcej` jest aktywne tylko przy `has_more=true`, używa dokładnie `next_after_event_id` i nie duplikuje widocznych eventów.

## Historia sesji i receipts

GUI korzysta z:

```text
OperatorApi.sessions(workspace_root, limit)
```

Operacja `sessions` nie uruchamia PowerShella ani żadnego procesu. Operator otwiera Journal przez SQLite `mode=ro` i `PRAGMA query_only=ON`.

### Bounded zakres

- maksymalnie 100 sesji na żądanie;
- maksymalnie 20 prób na sesję;
- manifest sesji do 64 KiB;
- trwały wynik do 64 KiB;
- receipt do 2 MiB;
- tylko kanoniczne ścieżki zadeklarowane przez konfigurację projektu;
- odrzucenie symlinków, plików nieregularnych i wyjścia poza runtime root;
- walidacja schematu, tożsamości sesji/próby, SHA-256 wyniku, changed files oraz commitów receipt.

Uszkodzony albo brakujący plik nie usuwa sesji z widoku. Próba pozostaje widoczna z ostrzeżeniem i wyłączoną akcją otwarcia.

### Schematy

Operator zwraca:

- `bdb-session-history-v1`;
- `bdb-session-summary-v1`;
- `bdb-session-attempt-v1`;
- `bdb-repair-correlation-v1` — jawna relacja przypisana do sesji;
- `bdb-repair-group-v1` — bounded, zweryfikowana grupa sesji.

GUI mapuje je do:

- `bdb-gui-session-history-v1`;
- `bdb-gui-session-summary-v1`;
- `bdb-gui-session-attempt-v1`;
- `bdb-gui-repair-group-v1`.

Każda próba może pokazać:

- command ID i sequence;
- operation, target path i profile ID;
- status wyniku i error code;
- exit code;
- checkpoint state;
- `rollback_performed`;
- changed files i SHA-256 wyniku;
- status pliku trwałego wyniku;
- status receipt;
- source commit, parent commit i promoted_at dla zweryfikowanej promocji.

### Jawne relacje naprawcze

Warstwa wykonawcza może przy utworzeniu sesji zapisać:

```json
{
  "schema": "bdb-repair-correlation-v1",
  "correlation_id": "<safe-id>",
  "role": "initial | repair",
  "predecessor_session_id": null
}
```

Dla roli `initial` predecessor musi być `null`, a dane `correlation_id` może zostać związane tylko z jedną sesją initial.

Dla roli `repair` predecessor jest wymagany i musi wskazywać inną sesję, która:

- już istnieje w tym samym Native Session Store;
- należy do tego samego `repo_alias` i `repository_id`;
- posiada jawne correlation;
- posiada dokładnie ten sam `correlation_id`.

Brakujący predecessor, drugi initial, różne correlation ID albo predecessor z innego repozytorium są odrzucane przed związaniem nowej sesji repair.

Correlation zostaje trwale związane z Native Session Store i manifestem sesji, a ingestion waliduje je ponownie przed zapisem manifestu w Journalu. Brak correlation pozostaje prawidłowy dla kompatybilności wstecznej i oznacza brak powiązania. Po związaniu correlation z sesją nie może zostać zmienione.

Operator buduje grupę wyłącznie z jawnych obiektów zapisanych w manifestach. Grupa otrzymuje `verified=true` tylko wtedy, gdy:

- ma dokładnie jedną sesję `initial`;
- ma co najmniej jedną sesję `repair`;
- każdy predecessor znajduje się w bounded odpowiedzi;
- każdy łańcuch repair dochodzi do initial;
- nie ma cyklu.

Jeżeli limit odpowiedzi odetnie część łańcucha albo manifest jest niespójny, grupa pozostaje widoczna jako niezweryfikowana z ostrzeżeniem. System nie uzupełnia brakujących elementów heurystycznie.

Control Center pokazuje w kolumnie `Łańcuch`:

- `START` — zweryfikowana sesja initial;
- `NAPRAWA` — zweryfikowana sesja repair;
- `NIEZWERYF.` — correlation istnieje, ale bounded grupa nie przeszła pełnej walidacji;
- `—` — brak jawnego correlation.

Operator i GUI zawsze publikują `repair_relationships_inferred=false` albo równoważne `relationship_inferred=false`. Czas, alias projektu, nazwy plików, podobieństwo diffu i kolejność nie tworzą relacji.

### Jawne otwieranie lokalnych artefaktów

Przyciski:

- `Otwórz wynik`;
- `Otwórz receipt`;
- `Otwórz katalog`;

są aktywne wyłącznie dla artefaktów, które Operator wcześniej zwalidował jako kanoniczne, istniejące i poprawne. Nic nie otwiera się automatycznie przy starcie, zmianie projektu ani odświeżeniu.

## Brak pollingu i serializacja

Oba widoki są pobierane wyłącznie po jawnym kliknięciu. Nie ma timera, watchera, listenera ani strumienia sieciowego.

Workery historii zdarzeń i sesji są objęte tym samym mechanizmem `one active task` co bootstrap, status, sterowanie i bieżąca operacja. Odczyty nie nakładają się na siebie ani na mutacje.

## Poza zakresem

- modyfikacja albo usuwanie zdarzeń, sesji, wyników i receipts;
- arbitralne zapytania SQL;
- arbitralne skanowanie katalogów;
- automatyczne otwieranie lokalnych ścieżek;
- pełnotekstowe wyszukiwanie payloadów;
- automatyczny polling;
- zdalny transport;
- inferowanie relacji naprawczych między sesjami;
- retroaktywne tworzenie correlation ID w istniejących historycznych danych;
- ręczna edycja correlation z poziomu GUI.
