# ADR-0010: Bounded manual Journal history

Status: Accepted  
Data: 2026-07-18

## Kontekst

P04 udostępnił wersjonowane eventy `bdb-event-v1` oraz bounded metodę `OperatorApi.events()`. P08 pokazuje wyłącznie jedną bieżącą operację. Operator potrzebuje również przeglądu historii, ale pełne ładowanie Journalu, bezpośredni SQL albo polling zwiększałyby ryzyko i koszt GUI.

## Decyzja

Historia Control Center:

- korzysta wyłącznie z `OperatorApi.events()`;
- jest read-only;
- używa kursora `after_event_id`;
- ogranicza każdą stronę do 1–500 eventów;
- wspiera tylko exact filtry `session_id` i `command_id`;
- odświeża się wyłącznie po jawnym kliknięciu;
- uczestniczy w globalnej serializacji workerów;
- nie zachowuje własnej bazy i nie staje się drugim źródłem prawdy.

## Integralność stron

GUI sprawdza:

- zgodność kursora odpowiedzi z żądaniem;
- ścisły wzrost sekwencji;
- zgodność `next_after_event_id` z ostatnim zdarzeniem;
- zgodność filtrów;
- wersję schematu każdego eventu.

Odpowiedź naruszająca kontrakt jest prezentowana jako `invalid_operator_response` i nie jest częściowo dopisywana.

## Konsekwencje

### Pozytywne

- pamięć i czas renderowania pozostają ograniczone;
- definicja eventów i odczyt SQLite pozostają w Operator API;
- GUI może bezpiecznie pokazać duże Journale strona po stronie;
- testy nie wymagają rzeczywistego procesu BDB.

### Ograniczenia

- brak pełnotekstowego wyszukiwania payloadów;
- filtry są exact, nie rozmyte;
- użytkownik ręcznie pobiera kolejne strony;
- historia nie jest widokiem czasu rzeczywistego.

## Alternatywy odrzucone

### `SELECT *` lub bezpośredni SQLite w GUI

Odrzucone z powodu duplikowania P04 i zależności od wewnętrznego schematu Journalu.

### Wczytanie całej historii

Odrzucone z powodu nieograniczonego kosztu pamięci i interfejsu.

### Polling albo push sieciowy

Odrzucone, ponieważ nie są potrzebne do MVP i naruszałyby local in-process boundary.

## Bramka zmiany

Nieograniczone strony, polling, nowe rodzaje filtrów, trwały cache albo bezpośredni dostęp do Journalu wymagają osobnego ADR i nowych testów zasobów oraz bezpieczeństwa.
