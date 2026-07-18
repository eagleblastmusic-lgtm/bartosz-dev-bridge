# ADR-0007: Asynchroniczny bootstrap GUI tylko do odczytu

- Status: **Accepted**
- Data: **2026-07-18**
- Etap: **P06**

## Kontekst

Pierwsze produkcyjne okno Control Center musi pokazać dostępne projekty i podstawowe informacje o Operator API. Jednocześnie otwarcie aplikacji nie może zmieniać stanu BDB ani blokować głównego wątku Qt operacjami dyskowymi.

Bez jawnego kontraktu konstruktor okna mógłby zacząć wywoływać Operator API, wykonywać recovery albo uzbrajać Native Hosta. Powstałaby ukryta mutacja przy samym uruchomieniu programu oraz trudne do testowania zawieszenia interfejsu.

## Decyzja

Konstruktor `ControlCenterWindow` buduje wyłącznie widgety i model prezentacyjny. Nie wykonuje I/O, nie uruchamia workerów i nie wywołuje Operator API.

Po pokazaniu okna warstwa aplikacji jawnie wywołuje:

```text
window.start_bootstrap()
```

Bootstrap działa w `QThreadPool` i wykonuje dokładnie dwa odczyty publicznego Operator API:

1. `capabilities()`;
2. `list_projects(workspaces_root)`.

Wynik jest zamieniany na niemutowalny `BootstrapSnapshot` i przekazywany do głównego wątku Qt jednym sygnałem.

W P06 nie ma timera okresowego, stałego watchera ani automatycznego ponawiania. Przycisk `Odśwież odczyt` uruchamia ten sam ograniczony bootstrap na jawne żądanie użytkownika.

## Konsekwencje

Pozytywne:

- otwarcie okna pozostaje bezpieczne i przewidywalne;
- główny wątek Qt nie jest blokowany odczytem dysku;
- czysta usługa bootstrapu może być testowana bez PySide6;
- błędy Operator API są prezentowane bez uruchamiania recovery;
- P07 może dodać osobne workery dla jawnych mutacji bez zmiany modelu startowego.

Koszty:

- pierwszy stan okna jest przejściowo oznaczony jako ładowanie;
- worker i sygnał wymagają jawnego zarządzania cyklem życia;
- odświeżenie nie jest jeszcze automatyczne;
- szczegółowe statusy projektów nie są pobierane w P06.

## Niezmienniki

- konstruktor okna nie wywołuje `start_bootstrap()`;
- `BootstrapService` korzysta wyłącznie z `capabilities()` i `list_projects()`;
- bootstrap nie wywołuje `status`, `events`, `current_operation`, `logs`, `prepare`, `start`, `stop` ani `rearm`;
- brak katalogu workspace nie powoduje jego utworzenia;
- wyjątek workera staje się snapshotem błędu;
- aktualizacja widgetów odbywa się wyłącznie w głównym wątku Qt;
- `mutation_operations_invoked` pozostaje równe zero;
- długie operacje P07+ również muszą działać poza głównym wątkiem.

## Odrzucone alternatywy

### Synchroniczny bootstrap w konstruktorze

Odrzucone z powodu ryzyka zawieszenia UI i ukrytych skutków podczas tworzenia okna.

### Automatyczny polling od pierwszej wersji

Odrzucone jako przedwczesne. P06 ma zweryfikować shell, nawigację i pojedynczy bounded bootstrap.

### Bezpośredni odczyt plików workspace przez GUI

Odrzucone. GUI korzysta z publicznego `bdb_operator`, który pozostaje jedyną fasadą aplikacyjną.

### Automatyczny Start lub recovery po wykryciu błędu

Odrzucone. Mutacje będą osobnymi, jawnymi akcjami użytkownika w P07.
