# BDB — pierwszy pilot na rzeczywistym repozytorium `kalkulator`

Status: IMPLEMENTED ON BRANCH

## Cel

Potwierdzić podstawowy cel Bartosz Dev Bridge na zwyczajnym, istniejącym repozytorium Git, a nie na fixture utworzonym specjalnie wewnątrz BDB.

Repozytorium źródłowe:

- `eagleblastmusic-lgtm/kalkulator`;
- publiczne;
- Python + Tkinter;
- szybki profil `python -m pytest -q`;
- przypięty commit `4bd377f0fb33194da586a2aa58b67efcb86bc2e4`.

## Zadanie pilota

Dodać do `CalculatorEngine` operację podnoszenia bieżącej liczby do kwadratu oraz skrót klawiaturowy `s`. Zaktualizować test i README.

Zakres jest ograniczony do:

- `calculator.py`;
- `tests/test_calculator.py`;
- `README.md`.

## Przebieg

1. read-only `git ls-remote` potwierdza przypięty `main`;
2. repozytorium jest klonowane do katalogu tymczasowego;
3. tworzony jest wyłącznie lokalny branch `bdb-real-pilot`;
4. `origin` jest usuwany przed uruchomieniem BDB;
5. baseline pytest musi przejść;
6. pierwsza sesja celowo implementuje podwojenie zamiast kwadratu;
7. test `test_square_current_value` musi rzeczywiście nie przejść;
8. MultiFilePatch musi przywrócić dokładne pierwotne bajty trzech plików;
9. analiza błędu musi wskazać dokładny test;
10. druga, jawnie skorelowana sesja stosuje prawidłową implementację;
11. pytest musi przejść;
12. WorkspacePromoter tworzy lokalny commit i wykonuje `git merge --ff-only`;
13. receipt musi wskazywać przypięty commit jako rodzica;
14. końcowy checkout musi być czysty i nadal pozbawiony remote;
15. drugi `git ls-remote` musi potwierdzić, że zdalny `main` nie zmienił się.

## Granice bezpieczeństwa

Pilot nie:

- wykonuje `git push`;
- tworzy brancha w repozytorium `kalkulator` na GitHubie;
- otwiera PR w repozytorium `kalkulator`;
- modyfikuje jego `main`;
- używa tokenu do zapisu;
- uruchamia deploy;
- korzysta z arbitralnego shella przekazanego przez użytkownika;
- zmienia więcej niż trzy allowlistowane pliki;
- wykonuje więcej niż dwie próby.

Klon sieciowy służy wyłącznie do pobrania publicznego, przypiętego commita. Po klonowaniu repozytorium nie ma żadnego remote.

## Dowody

Workflow `Real Repository Kalkulator Pilot` uruchamia:

- pełny pytest BDB na Ubuntu Python 3.11 i 3.12;
- pełny pytest BDB na Windows Python 3.11, 3.12 i 3.14;
- osobny właściwy pilot na Windows Python 3.14.

Artefakt dowodowy zawiera wyłącznie bounded dane:

- raport pilota;
- Journal;
- Native Session Store;
- receipt promocji;
- logi Bridge;
- końcowe trzy pliki;
- lokalny patch;
- JSON z branch, HEAD, parent, remotes i `status --porcelain`.

Katalogi `.git` nie są przesyłane w artefakcie.

## Kryteria PASS

- przypięty commit jest dokładny;
- baseline testów przechodzi;
- pierwsza próba kończy się jednym rzeczywistym błędem testu;
- rollback jest potwierdzony, a `changed_files=[]`;
- druga próba przechodzi bez interwencji użytkownika;
- correlation ID i predecessor są jawne;
- lokalna promocja jest `ff-only`;
- parent receipt odpowiada przypiętemu SHA;
- końcowe testy przechodzą;
- checkout jest czysty;
- brak remote w lokalnym klonie;
- zdalny `main` jest niezmieniony;
- `remote_mutation_performed=false`.

## Poza zakresem

- merge PR #66;
- publikacja Control Center 0.3.0;
- modyfikacja `kalkulator` na GitHubie;
- repozytoria biznesowe `gicleeart` i `gicleeapp`;
- deploy, tag, release, instalator i podpisywanie.
