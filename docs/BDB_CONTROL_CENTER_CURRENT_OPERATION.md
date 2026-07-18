# BDB Control Center — P08 bieżąca operacja

Status: IMPLEMENTED ON BRANCH

## Cel

P08 zastępuje placeholder `Current operation` rzeczywistym widokiem bieżącej operacji BDB. Widok korzysta wyłącznie z publicznego:

```text
OperatorApi.current_operation(workspace_root)
```

Nie odczytuje SQLite bezpośrednio i nie odtwarza logiki Journalu w warstwie GUI.

## Źródło danych

Operator API wykorzystuje wdrożoną w P04 projekcję `bdb-current-operation-v1`. Journal jest otwierany przez SQLite `mode=ro` z `PRAGMA query_only=ON`.

GUI mapuje wynik do:

- `bdb-gui-current-operation-v1`;
- `bdb-gui-operation-details-v1`.

Każdy snapshot zawiera:

- `read_only=true`;
- `mutation_operations_invoked=0`;
- identyfikator operacji Operator API;
- jawny status błędu zamiast domniemania wyniku.

## Zakres widoku

Dla aktywnej komendy pokazywane są:

- command ID;
- session ID;
- numer sekwencji;
- stan komendy;
- rodzaj operacji;
- ścieżka docelowa;
- profil testowy;
- repository ID;
- stan sesji;
- rewizja i state hash workspace;
- wynik i kod błędu;
- czasy utworzenia, aktualizacji i wygenerowania projekcji.

Brak aktywnej komendy jest pełnoprawnym stanem `BRAK AKTYWNEJ OPERACJI`, nie błędem.

## Odświeżanie

Widok nie używa timera ani pollingu. Odczyt następuje:

1. po pierwszym statusie wybranego projektu w normalnym uruchomieniu;
2. po końcowym statusie jawnej operacji Start/Stop/re-arm;
3. po kliknięciu `Odśwież operację`.

Wszystkie odczyty korzystają z tego samego globalnego mechanizmu `one active worker`, więc nie nakładają się na bootstrap, status ani sterowanie procesem.

## Zachowanie smoke

Ogólny headless smoke nie posiada rzeczywistego projektu ani Journalu. Potwierdza jedynie:

- obecność widoku;
- obecność jawnego przycisku odświeżania;
- deklarację read-only;
- `mutation_operations_invoked=0`.

Odczyty aktywnego i pustego Journalu są testowane osobno z wstrzykniętym Operator API.

## Poza zakresem

- lista i filtrowanie zdarzeń — P09;
- logi i eksport diagnostyczny — P10;
- automatyczny polling — celowo niewprowadzony;
- sterowanie komendą, anulowanie lub retry — poza kontraktem P08;
- edycja Journalu — zabroniona.
