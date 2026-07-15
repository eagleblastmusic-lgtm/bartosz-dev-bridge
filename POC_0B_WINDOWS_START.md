# POC-0B — ślepa diagnoza na Windows

## Cel

POC-0B sprawdza, czy ChatGPT potrafi w jednej aktywnej turze:

1. odczytać kod z nowego syntetycznego fixture;
2. wdrożyć logiczną pierwszą poprawkę;
3. odebrać rzeczywisty failure z lokalnego pytest;
4. rozpoznać niewskazany wcześniej przypadek brzegowy;
5. przygotować korektę wynikającą z tracebacku;
6. odebrać końcowy PASS.

Wybrany przypadek brzegowy nie jest przekazywany ChatGPT przed wynikiem komendy 002.

## Izolacja od POC-0A

POC-0B używa osobnego katalogu:

```text
C:\Projekt\DevMaster\POC0B
```

Nie modyfikuje ani nie usuwa dowodów z:

```text
C:\Projekt\DevMaster\POC0
```

## 1. Zaktualizuj branch implementacyjny

W PowerShell, w repozytorium Bridge:

```powershell
cd C:\Projekt\DevMaster\bartosz-dev-bridge
git switch gpt/poc-0-bootstrap
git pull --ff-only
git status --short
git rev-parse HEAD
```

Repozytorium powinno być czyste.

## 2. Utwórz ślepe fixture

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap_poc0b_windows.ps1
```

Bootstrap:

- tworzy osobny virtual environment i konfigurację POC-0B;
- klonuje osobną kopię control repo;
- lokalnie wybiera kryptograficznie losowy przypadek brzegowy;
- nie wyświetla wybranego przypadku;
- tworzy lokalne repo `bdb-poc0b-fixture`;
- zapisuje test brzegowy w exact bazowym commicie;
- wyświetla wyłącznie exact `Base SHA` potrzebny do manifestu sesji.

Jeżeli katalog fixture już istnieje, skrypt odmawia nadpisania dowodów. Dla kolejnego świeżego testu trzeba użyć innego parametru `-Root`.

## 3. Uruchom Bridge

```powershell
.\scripts\run_poc_bridge.ps1 -Root "C:\Projekt\DevMaster\POC0B"
```

Po uruchomieniu terminal pozostaje zajęty do zakończenia trzech komend albo pięciominutowego timeoutu.

## 4. Zakres sesji

Manifest POC-0B musi zawierać:

```text
repository_id: bdb-poc0b-fixture
allowed_paths:
  - src/normalize.py
base_sha: exact SHA wyświetlony przez bootstrap
```

Oczekiwany przebieg:

```text
001 open_read src/normalize.py                 -> success
002 prosta normalizacja + poc_pytest           -> failed
003 korekta wynikająca z rzeczywistego failure -> success
```

Komenda 003 musi powstać dopiero po pobraniu result 002 i używać aktualnych `workspace_revision` oraz `state_hash`.

## 5. Kryterium PASS

POC-0B przechodzi, gdy:

- użytkownik wysyła jedną wiadomość po uruchomieniu Bridge;
- ChatGPT wykonuje wszystkie trzy kroki bez kolejnej ingerencji użytkownika;
- result 002 ma `exit_code != 0` i ujawnia wybrany lokalnie przypadek;
- command 003 zostaje opublikowana dopiero po result 002;
- result 003 ma `exit_code: 0`;
- finalny diff obejmuje tylko `src/normalize.py`;
- źródłowe fixture pozostaje czyste;
- każdy wynik ma poprawny end marker;
- finalna odpowiedź następuje dopiero po PASS.

## Bezpieczeństwo

- Test używa wyłącznie syntetycznego repozytorium.
- GitHub nie przekazuje executable ani argumentów shellowych.
- Lokalna konfiguracja zezwala wyłącznie na `src/normalize.py`.
- Ukryty test jest częścią lokalnego bazowego commita i nie jest modyfikowany przez komendy.
- Żadne tokeny ani sekrety nie są zapisywane w repozytorium lub konfiguracji.
