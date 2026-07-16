# Local end-to-end POC

Ta bramka wykonuje lokalny, powtarzalny POC Bartosz Dev Bridge na syntetycznych repozytoriach Git. Nie używa repozytorium biznesowego ani control repo użytkownika.

## Uruchomienie na Windows

Z katalogu głównego projektu:

```powershell
.\scripts\Invoke-BDBLocalE2E.ps1
```

Skrypt automatycznie wybiera `.venv\Scripts\python.exe`, jeżeli środowisko istnieje. Inny interpreter można wskazać jawnie:

```powershell
.\scripts\Invoke-BDBLocalE2E.ps1 -Python "C:\Python312\python.exe"
```

## Zakres bramki

1. Kompilacja wszystkich modułów `bdb_bridge`.
2. Pełny lokalny transport Git POC-0:
   - syntetyczne source repo;
   - syntetyczne bare control remote;
   - branches `commands` i `results`;
   - trzy kolejne komendy i publikacja wyników.
3. Finalna bramka GHB2-D:
   - strict `multi_file_patch` ingestion;
   - jednorazowy bounded profile `poc_pytest`;
   - commit przy sukcesie;
   - pełny rollback przy failure;
   - durable result staging;
   - recovery bez ponownego wykonania profilu lub batchu.
4. GHB2-C durable multi-file recovery:
   - checkpoint;
   - apply;
   - rollback;
   - restart i idempotencja.
5. Foreground service lifecycle:
   - OS instance lock;
   - Journal PID i heartbeat;
   - drugi proces odrzucony;
   - graceful stop;
   - recovery po kontrolowanej awarii.
6. `git diff --check` dla lokalnego checkoutu.

## Izolacja

Repozytoria, worktree, Journal SQLite, runtime directories i bare remotes powstają wyłącznie w katalogach tymczasowych pytest. Bramka:

- nie modyfikuje `bartosz-dev-poc-control`;
- nie wykonuje operacji na repozytorium biznesowym;
- nie używa `git reset`, `git clean`, `git stash`, `git rebase` ani force push;
- nie wykonuje arbitralnego shell command;
- nie usuwa danych użytkownika.

## Wynik

Sukces kończy się komunikatem:

```text
LOCAL E2E POC: PASS
```

Każdy krok jest fail-fast. Kod wyjścia różny od zera zatrzymuje bramkę i wskazuje nazwę niezaliczonego etapu.
