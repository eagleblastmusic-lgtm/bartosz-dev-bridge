# ADR-0008: Jawne i serializowane sterowanie procesami z GUI

- Status: **Accepted**
- Data: **2026-07-18**
- Etap: **P07**

## Kontekst

Control Center ma zastąpić rutynowe użycie PowerShella dla `Status`, `Start`, `Stop` i ponownego uzbrojenia Native Hosta. Te operacje zmieniają lokalny stan procesów i muszą zachować istniejące reguły BDB Core oraz Operator API.

GUI jest środowiskiem zdarzeniowym. Bez dodatkowej blokady użytkownik może wykonać kilka kliknięć, zmienić projekt albo odświeżyć listę podczas trwającej operacji. Równoległe Start/Stop/re-arm mogłyby prowadzić do niejednoznacznego wyniku lub mylącego widoku.

## Decyzja

P07 udostępnia cztery oddzielne działania:

- `Odśwież status` — tylko odczyt;
- `Start` — jawna mutacja;
- `Stop` — jawna mutacja;
- `Re-arm` — jawna mutacja.

Mutacje są dozwolone wyłącznie po spełnieniu całej sekwencji:

1. użytkownik wybrał przygotowany projekt;
2. użytkownik kliknął konkretny przycisk;
3. GUI pokazało opis skutku i nazwę projektu;
4. użytkownik potwierdził operację;
5. wszystkie kontrolki projektu zostały zablokowane;
6. pojedynczy worker wywołał zamkniętą metodę `ProjectOperationsService`;
7. po zakończeniu GUI wykonało osobny read-only `status`;
8. dopiero status potwierdzający odblokował interfejs.

`ProjectOperationsService` ma zamknięty katalog:

```text
read_status
execute(start | stop | rearm)
```

Nie istnieje metoda przekazująca dowolną nazwę operacji, program, argumenty procesu ani shell.

## Status

Status może być pobrany automatycznie po wyborze projektu, ponieważ jest publiczną operacją tylko do odczytu. Odczyt:

- działa poza głównym wątkiem Qt;
- nie wykonuje recovery;
- nie uzbraja hosta;
- nie uruchamia i nie zatrzymuje procesów;
- zwraca `bdb-gui-project-status-v1` z `mutation_operations_invoked=0`.

## Mutacje

Każda potwierdzona mutacja:

- działa poza głównym wątkiem Qt;
- korzysta wyłącznie z `OperatorApi.start`, `OperatorApi.stop` albo `OperatorApi.rearm`;
- ma limit uzbrojenia `1..60` minut;
- zwraca `bdb-gui-control-result-v1` z `mutation_operations_invoked=1`;
- nie jest automatycznie ponawiana;
- po błędzie również kończy się osobnym odczytem statusu.

`Stop` nie dodaje żadnej logiki zabijania procesów. Zachowanie Journalu, wyników, receipts, worktree i logów pozostaje własnością istniejącego operatora BDB.

## Serializacja

W jednym oknie może działać najwyżej jeden z następujących workerów:

- bootstrap;
- status;
- control.

W trakcie aktywnego workera zablokowane są:

- wybór projektu;
- odświeżenie listy projektów;
- odświeżenie statusu;
- Start;
- Stop;
- Re-arm;
- zmiana czasu uzbrojenia.

GUI nie tworzy kolejki mutacji. Kolejne kliknięcie podczas aktywnej operacji jest ignorowane i nie trafia do Operator API.

## Konsekwencje

Pozytywne:

- brak równoległych Start/Stop/re-arm;
- użytkownik widzi skutki przed wykonaniem;
- status po operacji pochodzi z rzeczywistego Operator API, nie z optymistycznej aktualizacji UI;
- błędy mutacji nie blokują końcowej diagnostyki stanu;
- kontrolki pozostają cienką warstwą nad istniejącym operatorem.

Koszty:

- operacje wymagają dodatkowego potwierdzenia;
- interfejs jest chwilowo zablokowany podczas pracy;
- P07 nie ma kolejki ani anulowania operacji;
- pełna historia operacji pojawi się dopiero w P09.

## Niezmienniki

- konstruktor okna nie wykonuje statusu ani mutacji;
- zwykły bootstrap pozostaje tylko do odczytu;
- żadna mutacja nie jest uruchamiana przez timer, zmianę projektu ani odświeżenie;
- status nie może wywołać Start, Stop ani re-arm;
- potwierdzenie domyślnie wybiera `Nie`;
- jedna potwierdzona akcja wywołuje dokładnie jedną metodę mutującą;
- po wyniku mutacji wykonywany jest dokładnie jeden status potwierdzający;
- brak `taskkill`, arbitrary shell, Git i bezpośredniego dostępu do BDB Core w `bdb_gui`.

## Odrzucone alternatywy

### Jeden przycisk przełączający stan

Odrzucone, ponieważ etykieta może nie odpowiadać rzeczywistemu stanowi w chwili kliknięcia. Oddzielne Start i Stop są jednoznaczne.

### Automatyczny Start po uruchomieniu GUI

Odrzucone zgodnie z ADR-0007. Otwarcie okna pozostaje tylko do odczytu.

### Automatyczny re-arm przy wygaśnięciu

Odrzucone. Uzbrojenie jest czasową zgodą użytkownika i wymaga jawnej akcji.

### Równoległe operacje dla wielu projektów

Odrzucone dla MVP. Jedno okno serializuje wszystkie operacje procesowe.

### Optymistyczna zmiana statusu po kliknięciu

Odrzucone. Widok jest aktualizowany dopiero na podstawie osobnego, rzeczywistego odczytu statusu.
