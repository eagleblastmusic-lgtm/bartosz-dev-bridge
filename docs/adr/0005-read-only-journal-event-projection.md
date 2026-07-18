# ADR-0005: Read-only projekcja Journalu dla zdarzeń operatorskich

- Status: **Accepted**
- Data: **2026-07-18**
- Etap: **P04**

## Kontekst

Control Center potrzebuje historii i bieżącej operacji. BDB ma już trwały Journal z append-only tabelą `events`, stanami komend, sesji, wyników i efektów. Utworzenie drugiego event store albo parsowanie przypadkowych logów stworzyłoby konkurencyjne źródło prawdy.

Jednocześnie zwykłe `Journal.open()` wykonuje kontrolę migracji. Warstwa prezentacyjna nie powinna modyfikować bazy ani blokować Bridge'a transakcją migracyjną.

## Decyzja

P04 implementuje osobną projekcję tylko do odczytu:

```text
existing Journal -> read-only SQLite projection -> bdb-event-v1 / current-operation
```

Połączenie:

- używa URI SQLite `mode=ro`;
- ustawia `PRAGMA query_only=ON`;
- nie wywołuje `Journal.open()` ani migracji;
- wykonuje wyłącznie zamknięte zapytania `SELECT`;
- ma krótki timeout;
- jest otwierane na czas pojedynczego requestu i natychmiast zamykane.

Zdarzenia zachowują kolejność `event_id`, otrzymują stabilne ID `journal:<alias>:<event_id>` i są opakowane w `bdb-event-v1`.

Bieżąca operacja jest projekcją stanów nieterminalnych komend. Nie jest nową blokadą ani mechanizmem koordynacji.

Logi pozostają osobnym bounded snapshotem. Nie są dopisywane do Journalu i nie są mieszane z audytowym event streamem.

## Konsekwencje

Pozytywne:

- brak drugiego źródła prawdy;
- brak zapisu z GUI/Operator API do Journalu;
- stabilny kursor i tożsamość eventów;
- możliwość odtworzenia widoku po restarcie GUI;
- brak trwałego procesu monitorującego.

Koszty:

- projekcja zależy od istniejącego schematu Journalu;
- starszy lub uszkodzony Journal daje jawny błąd odczytu;
- live update w GUI będzie wymagał bounded pollingu w późniejszym etapie;
- eventy nie obejmują automatycznie logów rozszerzenia i przeglądarki.

## Niezmienniki

- Journal pozostaje źródłem prawdy;
- projekcja nie uruchamia migracji;
- projekcja nie wykonuje zapisów;
- brak eventu nie zastępuje weryfikacji trwałego stanu lub receipt;
- payloady i logi są bounded;
- pełne patch content i `command_json` nie trafiają do snapshotu bieżącej operacji;
- zmiana na inny storage wymaga nowego ADR i planu kompatybilności.

## Odrzucone alternatywy

### Nowa tabela operatorskich eventów

Odrzucone, ponieważ duplikowałaby istniejący Journal i wymagała synchronizacji transakcyjnej.

### `Journal.open()` w GUI

Odrzucone, ponieważ może wykonywać migracje i ma semantykę rdzenia, nie projekcji.

### Parsowanie wyłącznie tekstowych logów

Odrzucone jako niestabilne, niepełne i pozbawione trwałej tożsamości.

### Stały watcher bazy i logów

Odrzucone w P04. Pierwsza wersja jest request/response i nie działa w tle.
