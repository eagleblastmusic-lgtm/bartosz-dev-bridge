# Persistent operator pilot

## Cel

Ten pilot jest pierwszym trwałym przebiegiem operatorskim po syntetycznej bramce pytest. Uruchamia prawdziwy proces `bdb`, prawdziwy transport Git, finalny `multi_file_patch`, profil `poc_pytest`, publikację wyniku i graceful stop.

Nie używa GicléeApp, repozytoriów biznesowych ani `bartosz-dev-poc-control`.

## Uruchomienie

Z aktualnego `main`:

```powershell
cd C:\Projekty\DevMaster\bartosz-dev-bridge
git pull --ff-only origin main
.\scripts\Invoke-BDBPersistentPilot.ps1
```

Skrypt automatycznie wybiera `.venv\Scripts\python.exe`, jeżeli środowisko projektu istnieje.

Można podać własny, wcześniej nieistniejący katalog:

```powershell
.\scripts\Invoke-BDBPersistentPilot.ps1 `
  -Root "C:\Projekty\DevMaster\bdb-pilot-manual-01"
```

Pilot odmawia nadpisania istniejącego katalogu i wymaga lokalizacji poza checkoutem `bartosz-dev-bridge`.

## Co powstaje

W katalogu pilota pozostają:

```text
fixture/              osobne repozytorium źródłowe
control.git/          osobny bare remote Git
writer/               klient publikujący manifest i command
bridge-control/       clone używany przez Bridge
worktrees/<uuid>/     zachowany session worktree
runtime/journal.db    trwały Journal
config.json           dokładna konfiguracja przebiegu
bridge.stdout.log     stdout procesu Bridge
bridge.stderr.log     stderr procesu Bridge
pilot-report.json     kanoniczny raport końcowy
```

Żaden z tych artefaktów nie jest automatycznie usuwany.

## Przebieg

1. Skopiowanie małego `bdb-poc-fixture` do osobnego repozytorium.
2. Utworzenie lokalnego bare remote z gałęziami `main`, `commands` i `results`.
3. Start Bridge jako procesu foreground.
4. Oczekiwanie na publiczny stan `RUNNING`.
5. Publikacja nowej sesji i komendy `multi_file_patch` na gałęzi `commands`.
6. Atomowa zmiana `src/clamp.py` oraz utworzenie `PILOT_RESULT.md`.
7. Uruchomienie bounded profilu `poc_pytest`.
8. Trwały commit checkpointu, wynik i publikacja na gałęzi `results`.
9. Graceful stop i potwierdzenie wyjścia procesu kodem `0`.
10. Weryfikacja exact AFTER bytes oraz zdalnego JSON resultu.

## Oczekiwany wynik

```text
PERSISTENT PILOT: PASS
...
Artifacts were preserved. No cleanup was performed.
```

Raport `pilot-report.json` powinien zawierać:

```json
{
  "status": "pass",
  "edit_status": {
    "command_state": "result_published",
    "checkpoint_state": "committed",
    "profile_status": "success",
    "result_status": "success"
  }
}
```

## Bezpieczeństwo

Pilot:

- nie używa `shell=True`;
- nie używa `git reset`, `git clean`, `git stash`, `git rebase` ani force push;
- nie usuwa worktree, Journalu ani repozytoriów;
- nie dotyka checkoutu źródłowego poza utworzeniem Git worktree przez Bridge;
- pozwala wyłącznie na trzy ścieżki: `src/clamp.py`, `tests/test_clamp.py`, `PILOT_RESULT.md`;
- używa wyłącznie profilu `poc_pytest`;
- kończy proces przez publiczne `bdb bridge stop`.

W przypadku błędu pilot zachowuje katalog i zapisuje `status: failed` wraz z diagnostyką w `pilot-report.json`.

## Inspekcja po sukcesie

Ścieżki sesji i command ID znajdują się w raporcie. Bezpieczne komendy:

```powershell
$Report = Get-Content "C:\...\pilot-report.json" -Raw | ConvertFrom-Json
$Report.edit_status
Get-Content $Report.bridge_stdout_path
Get-Content $Report.bridge_stderr_path
git -C $Report.workspace_path status --porcelain=v1
git -C $Report.workspace_path diff --
```

Nie wykonuj automatycznego cleanupu. Katalog pilota pozostaje dowodem przebiegu do momentu osobnej decyzji operatorskiej.
