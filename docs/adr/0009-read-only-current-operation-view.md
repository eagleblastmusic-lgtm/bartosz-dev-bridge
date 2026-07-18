# ADR-0009: Read-only current operation view

Status: Accepted  
Data: 2026-07-18

## Kontekst

P04 udostępnił `OperatorApi.current_operation()` jako read-only projekcję istniejącego Journalu. P06 utworzył szkielet GUI, a P07 dodał status i jawne sterowanie procesami. Strona `Current operation` nadal była placeholderem.

Control Center potrzebuje pokazywać operatorowi aktywną komendę bez:

- bezpośredniego dostępu GUI do SQLite;
- duplikowania zapytań i definicji aktywnych stanów;
- dodawania pollingu;
- mieszania obserwowalności z mutacjami.

## Decyzja

Widok bieżącej operacji jest cienkim adapterem nad:

```text
bdb_gui -> bdb_operator.OperatorApi.current_operation() -> ObservabilityReader -> Journal mode=ro
```

GUI:

- nie importuje `bdb_bridge`;
- nie otwiera Journalu bezpośrednio;
- nie wykonuje Start, Stop, re-arm, Prepare ani operacji Git;
- nie posiada timera odświeżającego;
- mapuje wersjonowany kontrakt P04 na `bdb-gui-current-operation-v1`;
- traktuje brak aktywnej operacji jako poprawny stan pusty;
- wykonuje odczyt w workerze objętym wspólną serializacją z innymi zadaniami GUI.

## Automatyczny odczyt

Automatyczny odczyt jest dozwolony wyłącznie jako skończony krok po:

1. poprawnym odczycie statusu wybranego projektu;
2. kontrolnym statusie po jawnej mutacji.

Nie jest to polling. Każdy kolejny odczyt wymaga nowego zdarzenia użytkownika albo zakończenia jawnie rozpoczętego przepływu.

## Konsekwencje

### Pozytywne

- jedna definicja aktywnej operacji pozostaje w Operator API;
- Journal zachowuje gwarancję read-only;
- widok jest testowalny z fake Operator API;
- brak osobnego procesu i transportu;
- brak dodatkowego ryzyka równoległości.

### Ograniczenia

- widok nie jest transmisją czasu rzeczywistego;
- zmiana stanu poza GUI jest widoczna po kolejnym odświeżeniu;
- brak Journalu jest pokazany jako błąd odczytu;
- P08 nie udostępnia retry, cancel ani ingerencji w aktywną komendę.

## Alternatywy odrzucone

### Bezpośredni odczyt SQLite w GUI

Odrzucony, ponieważ duplikowałby P04 i wiązał prezentację ze schematem trwałego magazynu.

### Polling co kilka sekund

Odrzucony w MVP z powodu zbędnej pracy, niejawnych odczytów i komplikacji serializacji.

### WebSocket lub lokalny HTTP push

Odrzucony, ponieważ naruszałby decyzję local in-process Operator API i dodawał listener sieciowy.

## Bramka zmiany

Dodanie pollingu, bezpośredniego SQLite, mutacji aktywnej komendy albo nowego transportu wymaga osobnego ADR i nowych testów bezpieczeństwa.
