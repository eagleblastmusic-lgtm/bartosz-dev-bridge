# ADR 0018: Explicit durable repair correlation

- Status: Accepted
- Date: 2026-07-19
- Extends: ADR 0017

## Context

ADR 0017 zabrania łączenia oddzielnych sesji naprawczych na podstawie czasu, aliasu projektu, nazw plików albo kolejności. Taka projekcja nie byłaby wiarygodnym dowodem wykonania.

Dwa niezależne piloty BDB wykonują jednak rzeczywisty przebieg w dwóch sesjach: pierwsza próba kończy się błędem i rollbackiem, a druga wprowadza poprawkę, przechodzi testy i jest promowana. Control Center potrzebuje trwałego, jawnego dowodu, że druga sesja jest naprawą pierwszej.

## Decision

1. Wprowadzamy wersjonowany obiekt `bdb-repair-correlation-v1`.
2. Obiekt zawiera:
   - `correlation_id`;
   - rolę `initial` albo `repair`;
   - `predecessor_session_id`.
3. Rola `initial` wymaga `predecessor_session_id=null`.
4. Rola `repair` wymaga istniejącego, różnego od bieżącej sesji `predecessor_session_id`.
5. Correlation jest opcjonalne dla kompatybilności wstecznej, ale po związaniu z sesją staje się niemutowalne.
6. Native Action Composer zapisuje correlation w Native Session Store i w każdym manifeście tej sesji.
7. Ingestion ponownie waliduje i normalizuje obiekt przed trwałym zapisem manifestu w Journalu.
8. Nie wykonujemy migracji tabel SQLite. Źródłem correlation dla projekcji jest wersjonowany manifest zapisany w istniejącym `session_ingestion`.
9. Operator API buduje bounded grupy wyłącznie z jawnych correlation zapisanych w manifestach.
10. Grupa jest `verified=true` tylko wtedy, gdy:
    - ma dokładnie jedną sesję `initial`;
    - zawiera co najmniej jedną sesję `repair`;
    - każdy predecessor jest obecny w bounded odpowiedzi;
    - każdy łańcuch repair dochodzi do initial;
    - nie występuje cykl.
11. Operator i GUI zawsze publikują `relationship_inferred=false`.
12. GUI pokazuje role `START`, `NAPRAWA` albo `NIEZWERYF.` i odrzuca niespójne ID grup, krawędzie poza bounded odpowiedzią oraz relacje oznaczone jako inferowane.
13. Historyczne sesje bez correlation pozostają niepowiązane. System nie uzupełnia ich heurystycznie.

## Consequences

### Positive

- Control Center może pokazać audytowalny przebieg `failure → rollback → repair → promotion` między oddzielnymi sesjami.
- Relacja przechodzi przez ten sam trwały łańcuch prawdy co manifest sesji.
- Nie jest potrzebna migracja bazy ani retroaktywne przepisywanie historii.
- Stare klienty i stare sesje pozostają zgodne, ponieważ correlation jest opcjonalne.
- Oba niezależne piloty mogą udowodnić spójność identyfikatorów, ról i predecessorów.

### Negative

- Bounded odpowiedź może nie zawierać całego łańcucha; wtedy grupa pozostaje widoczna jako niezweryfikowana.
- Correlation musi zostać przekazane przy utworzeniu pierwszej komendy sesji i nie może być później zmienione.
- Historyczne dane bez correlation nie otrzymają połączonej osi czasu.

## Security properties

- brak dopasowania czasowego, nazwowego i plikowego;
- brak zapisu przez Operator API lub GUI;
- brak dowolnych identyfikatorów poza istniejącym bezpiecznym formatem session ID;
- odrzucenie self-reference, nieznanych ról, nieznanych kluczy i zmiany correlation w trakcie sesji;
- projekcja pozostaje SQLite `mode=ro` i `query_only=ON`;
- GUI nie tworzy ani nie koryguje relacji.

## Rejected alternatives

- migracja SQLite z osobną tabelą relacji — zbędna dla pierwszej wersji, ponieważ manifest jest już trwale journalowany;
- inferowanie z czasu lub podobnych zmian — nie jest audytowalne;
- przechowywanie relacji tylko w raporcie pilota — raport nie jest źródłem prawdy wykonania;
- correlation ustawiane dopiero po zakończeniu sesji — umożliwiałoby retroaktywne przepisywanie historii.
