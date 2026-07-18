# BDB Control Center — P07 status i sterowanie procesami

Status: IMPLEMENTED ON BRANCH

## Cel

P07 podłącza produkcyjny dashboard Control Center do publicznego `bdb_operator.OperatorApi` bez dodawania alternatywnej ścieżki wykonawczej.

GUI może:

- odczytać status przygotowanego projektu;
- jawnie uruchomić BDB;
- jawnie zatrzymać BDB;
- jawnie ponownie uzbroić Native Hosta.

Nie może wykonywać arbitralnej komendy, operacji Git ani mutacji workspace poza zamkniętym katalogiem Operator API.

## Rozdział odczytu i mutacji

Odczyt statusu zwraca `bdb-gui-project-status-v1`:

- `read_only=true`;
- `mutation_operations_invoked=0`;
- Bridge, Native Host, promoter i stan source repo;
- błąd jako typowany kod, bez domniemania sukcesu.

Sterowanie zwraca `bdb-gui-control-result-v1`:

- akcja należy do `start | stop | rearm`;
- `mutation_operations_invoked=1`;
- zachowany jest identyfikator operacji Operator API;
- po wyniku GUI wykonuje osobny odczyt statusu.

## Bramka użytkownika

Każda mutacja wymaga:

1. wybranego przygotowanego projektu;
2. osobnego kliknięcia;
3. potwierdzenia w oknie modalnym;
4. braku aktywnej operacji;
5. zakończenia workera;
6. kontrolnego odczytu statusu.

Anulowanie okna potwierdzenia kończy przepływ przed wywołaniem Operator API.

## Serializacja

Control Center utrzymuje najwyżej jeden aktywny worker spośród:

- bootstrap;
- status;
- control.

Podczas aktywnego workera selektor projektu, odświeżanie i wszystkie przyciski sterujące są zablokowane. Drugie kliknięcie nie tworzy kolejnej operacji.

## Stop i stan STALE

GUI nie implementuje własnej procedury zatrzymania. Korzysta z istniejącego operatora Windows. Operator rozpoznaje bezpieczny `STALE` (`lock_held=false`, `pid_alive=false`), odzyskuje Bridge przez istniejącą ścieżkę start/recovery, a następnie wykonuje kooperacyjny Stop. Nie używa `taskkill`, `git clean`, `git reset` ani usuwania Journalu/worktree.

## Czas uzbrojenia

Start i re-arm przyjmują wyłącznie liczbę całkowitą od 1 do 60 minut. Wartość domyślna to 30 minut.

## Poza zakresem P07

- bieżąca operacja i jej postęp — P08;
- historia Journalu — P09;
- eksport diagnostyczny — P10;
- przygotowanie nowych projektów — P11;
- tray i powiadomienia — P12;
- installer i updater — P13;
- adapter Bartosz OS — P14;
- integracja GicleeApp — P15.

## Testy akceptacyjne

P07 wymaga:

- testów zamkniętego katalogu serwisu;
- testu odczytu bez mutacji;
- testu anulowania potwierdzenia;
- testu jednej potwierdzonej operacji i statusu końcowego;
- testu blokady podwójnego kliknięcia;
- zachowania headless smoke z `mutation_operations_invoked=0`;
- pełnego Bridge CI i Control Center GUI CI.
