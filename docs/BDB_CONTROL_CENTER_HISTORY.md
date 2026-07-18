# BDB Control Center — P09 historia Journalu

Status: IMPLEMENTED ON BRANCH

## Cel

P09 zastępuje placeholder `History` rzeczywistym, stronicowanym widokiem zdarzeń BDB. Warstwa GUI korzysta wyłącznie z publicznego:

```text
OperatorApi.events(workspace_root, after_event_id, limit, session_id, command_id)
```

Nie otwiera Journalu, nie wykonuje SQL i nie modyfikuje trwałego stanu.

## Ograniczenia odczytu

Każde żądanie ma:

- `after_event_id >= 0`;
- limit od 1 do 500;
- opcjonalny exact `session_id`;
- opcjonalny exact `command_id`.

Domyślny limit strony wynosi 100. GUI nie udostępnia operacji „wczytaj wszystko”.

## Kontrakty

Operator API zwraca `bdb-event-v1`. GUI mapuje stronę do:

- `bdb-gui-history-v1`;
- `bdb-gui-event-v1`.

Snapshot ma zawsze:

- `read_only=true`;
- `mutation_operations_invoked=0`;
- typowany kursor;
- dokładne filtry;
- identyfikator operacji Operator API.

GUI odrzuca odpowiedź, gdy:

- sekwencje nie są ściśle rosnące;
- kursor wejściowy różni się od żądania;
- `next_after_event_id` nie odpowiada ostatniemu zdarzeniu;
- odpowiedź zmienia filtry;
- payload albo wydarzenie mają niepoprawny typ.

## Widok

Tabela pokazuje:

- sequence;
- czas;
- severity;
- event type;
- session ID;
- command ID.

Wybrany wiersz pokazuje pełny, już ograniczony przez Operator API dokument wydarzenia w panelu tylko do odczytu.

## Paginacja

`Odśwież historię`:

- zaczyna od kursora 0;
- zastępuje bieżącą listę;
- używa aktualnych filtrów i limitu.

`Wczytaj więcej`:

- jest aktywne tylko przy `has_more=true`;
- używa dokładnie `next_after_event_id`;
- dopisuje nowe sekwencje;
- nie duplikuje już widocznych eventów.

## Brak pollingu

Historia jest pobierana wyłącznie po jawnym kliknięciu. P09 nie dodaje timera, watchera, listenera ani strumienia sieciowego.

## Serializacja

Worker historii jest objęty tym samym mechanizmem `one active task` co bootstrap, status, sterowanie i bieżąca operacja. W czasie odczytu wszystkie pozostałe akcje są zablokowane.

## Poza zakresem

- modyfikacja albo usuwanie eventów;
- arbitralne zapytania SQL;
- eksport pakietu diagnostycznego — P10;
- automatyczny polling;
- zdalny transport;
- pełnotekstowe wyszukiwanie payloadów.
