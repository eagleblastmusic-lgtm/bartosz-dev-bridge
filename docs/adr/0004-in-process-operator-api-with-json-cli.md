# ADR-0004: In-process Operator API z lokalnym adapterem JSON CLI

- Status: **Accepted**
- Data: **2026-07-18**
- Etap: **P03**

## Kontekst

ADR-0002 wymaga lokalnego i transportowo wymiennego Operator API, bez publicznego HTTP, WebSocket ani zdalnego sterowania. P03 potrzebuje pierwszej działającej implementacji, która będzie testowalna bez GUI i nie wprowadzi osobnego demona.

## Decyzja

Operator API v1 jest zwykłym pakietem Python `bdb_operator` używanym in-process przez przyszłych klientów lokalnych.

Dodatkowo udostępniamy lokalny adapter CLI:

```text
bdb-operator <closed-operation> [validated arguments]
```

CLI:

- nie jest serwerem;
- nie nasłuchuje na porcie;
- drukuje dokładnie jeden obiekt `bdb-operator-response-v1`;
- zwraca kod 0 dla sukcesu i 1 dla błędu;
- korzysta z tego samego `OperatorApi`, a nie z osobnej logiki.

Warstwa wykonawcza używa istniejących, bezpiecznych punktów wejścia BDB:

- `prepare_workspace_loop.py` dla `prepare`;
- `Invoke-BDBWorkspaceLoop.ps1` dla `start`, `status` i `stop`;
- CLI Native Hosta dla jawnego `rearm`.

Wszystkie procesy są uruchamiane z listą argumentów i `shell=False`.

## Konsekwencje

Pozytywne:

- brak nowego procesu stale działającego;
- brak powierzchni sieciowej;
- łatwe testy z fałszywym runnerem;
- GUI może użyć API in-process albo przyszłego adaptera IPC;
- logika operatorska pozostaje jedna.

Koszty:

- klient działający w innym procesie musi chwilowo użyć CLI lub przyszłego IPC;
- timeout i stderr/stdout muszą być mapowane na stabilne błędy;
- pakiet jest obecnie ograniczony do Windows dla operacji mutujących.

## Niezmienniki

- brak metody arbitralnego shella;
- zamknięty katalog operacji;
- `status` i `list_projects` są tylko do odczytu;
- `rearm` jest oddzielną, jawną mutacją;
- BDB Core i istniejący operator pozostają właścicielami skutków;
- zmiana na named pipe, stdio daemon lub inny IPC nie może zmienić DTO domenowych.

## Odrzucone alternatywy

### Lokalny HTTP od P03

Odrzucone z powodów opisanych w ADR-0002.

### GUI wywołujące PowerShell bezpośrednio

Odrzucone, ponieważ ominęłoby stabilne DTO, katalog operacji i mapowanie błędów.

### Nowy Python daemon w P03

Odrzucone jako przedwczesne. P03 dostarcza fasadę aplikacyjną; stały host może zostać dodany dopiero po realnej potrzebie GUI.
