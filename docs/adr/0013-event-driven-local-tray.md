# ADR-0013: Event-driven local tray and notifications

Status: Accepted  
Data: 2026-07-18

## Kontekst

Control Center powinien pozostać dostępny po zamknięciu głównego okna i informować o zakończeniu jawnych operacji. Tray nie może stać się drugim operatorem, watcherem Journalu ani ukrytą usługą uruchamiającą BDB.

## Decyzja

Tray jest cienką, lokalną warstwą Qt:

- zwykłe zamknięcie ukrywa okno wyłącznie, gdy system tray jest dostępny;
- powiadomienia reagują na istniejące sygnały zakończenia operacji;
- nie istnieje timer, polling ani dostęp do Operator API z kontrolera tray;
- jawne wyjście rozróżnia pozostawienie BDB uruchomionego, potwierdzony Stop wybranego projektu i anulowanie;
- Stop używa istniejącego serializowanego `ControlWorker`;
- headless smoke nie tworzy tray’a.

## Konsekwencje

### Pozytywne

- panel może działać w tle bez zmiany stanu BDB;
- użytkownik otrzymuje lokalne informacje o zakończeniu operacji;
- semantyka „zamknij panel” i „zatrzymaj BDB” pozostaje rozdzielona;
- tray nie wprowadza nowego transportu ani źródła prawdy.

### Ograniczenia

- dostępność tray’a zależy od środowiska systemowego;
- powiadomienia obejmują tylko operacje wykonane przez bieżącą instancję GUI;
- brak globalnego monitoringu procesów poza aplikacją;
- brak zdalnych i mobilnych powiadomień.

## Alternatywy odrzucone

### Cykliczne odpytywanie Journalu

Odrzucone jako ukryty polling, dodatkowy stan i ryzyko rozjazdu z P04.

### Automatyczny Stop przy zamknięciu okna

Odrzucony, ponieważ zamknięcie panelu nie jest zgodą na mutację procesów.

### Powiadomienia chmurowe

Odrzucone jako nowy transport, telemetria i rozszerzenie zakresu bezpieczeństwa.
