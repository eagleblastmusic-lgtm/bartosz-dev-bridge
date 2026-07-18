# ADR-0012: Two-gate project Prepare wizard

Status: Accepted  
Data: 2026-07-18

## Kontekst

Operator API udostępnia mutujące `prepare`, które korzysta z istniejącego, transakcyjnego preparera workspace. Control Center potrzebuje wygodnego formularza, ale nie może ukrywać zakresu operacji, duplikować Git preflightu ani automatycznie przygotowywać projektu po wpisaniu ścieżki.

## Decyzja

Kreator stosuje dwie niezależne bramki:

1. read-only `PreparePlan` tworzony po jawnym kliknięciu;
2. mutujące `OperatorApi.prepare()` dopiero po zaznaczeniu acknowledgement i osobnym potwierdzeniu planu.

Zmiana dowolnego pola unieważnia plan. Prepare przyjmuje wyłącznie instancję wcześniej zwalidowanego `PreparePlan`.

GUI waliduje format i podstawowe relacje ścieżek. Istniejący preparer pozostaje jedynym właścicielem Git preflightu, tworzenia worktree/control repo, Native Host config i rollbacku.

## Konsekwencje

### Pozytywne

- użytkownik widzi dokładny plan przed zapisem;
- anulowanie zatrzymuje przepływ przed Operator API;
- brak drugiej implementacji Git workflow w GUI;
- ograniczony, wersjonowany kontrakt planu;
- po sukcesie katalog projektów jest odświeżany bez automatycznego Start.

### Ograniczenia

- kreator nie klonuje repozytoriów;
- nie naprawia brudnego checkoutu;
- nie zmienia brancha;
- nie edytuje istniejącego projektu;
- ostateczna walidacja może odrzucić plan mimo poprawnego podglądu GUI.

## Alternatywy odrzucone

### Prepare bez planu

Odrzucone, ponieważ ukrywałoby docelową ścieżkę, allowed paths i limity.

### Git preflight w GUI

Odrzucony jako duplikacja istniejącego preparera i źródło rozjazdu zasad bezpieczeństwa.

### Automatyczny Start po Prepare

Odrzucony, ponieważ Prepare i uruchomienie procesów są odrębnymi mutacjami wymagającymi osobnych intencji.

## Bramka zmiany

Klonowanie, edycja istniejącego projektu, automatyczny Start, naprawa source checkoutu albo drugi preparer wymagają osobnego ADR i nowych testów rollbacku.
