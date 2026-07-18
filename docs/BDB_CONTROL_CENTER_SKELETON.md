# BDB Control Center — produkcyjny szkielet P06

Status: **P06 / proposed implementation**

## Cel

P06 tworzy pierwszy produkcyjny pakiet `bdb_gui` i uruchamialne okno Control Center. Nie dodaje jeszcze sterowania procesami.

```text
bdb-control-center
        |
        v
bdb_gui -> bdb_operator -> bdb_bridge
```

## Zakres

- aplikacja PySide6 + Qt Widgets;
- główne okno i nawigacja;
- strony: Dashboard, Projects, Current operation, History, Diagnostics;
- asynchroniczny bootstrap tylko do odczytu;
- odczyt `OperatorApi.capabilities()`;
- odczyt `OperatorApi.list_projects()`;
- wybór projektu z przygotowanych workspace'ów;
- komunikaty pustego i błędnego stanu;
- produkcyjny entrypoint `bdb-control-center`;
- headless smoke na Windows CI.

Strony poza Dashboardem są świadomymi placeholderami kolejnych etapów. P06 nie udaje, że funkcje P07–P10 są już wdrożone.

## Kontrakt uruchomienia

Konstruktor `ControlCenterWindow`:

- buduje wyłącznie widgety;
- nie wykonuje odczytów dysku;
- nie wywołuje Operator API;
- nie uruchamia wątku;
- nie zmienia stanu BDB.

`start_bootstrap()` jest uruchamiane jawnie przez warstwę aplikacji po pokazaniu okna. Bootstrap trafia do `QThreadPool` i wykonuje dokładnie dwa odczyty:

1. `capabilities()`;
2. `list_projects(workspaces_root)`.

Niedozwolone w P06:

- `status()` dla każdego projektu;
- `events()`, `current_operation()` lub `logs()` w timerze;
- `prepare()`, `start()`, `stop()` albo `rearm()`;
- automatyczne tworzenie katalogu workspace;
- bezpośredni odczyt Journalu;
- import prywatnych modułów `bdb_bridge`;
- subprocess, PowerShell, Git lub listener sieciowy w `bdb_gui`.

## Model stanu

`BootstrapSnapshot` jest niemutowalnym modelem czystego Pythona. Zawiera:

- root workspace'ów;
- listę `GuiProject`;
- informacje o publicznym Operator API;
- nieprawidłowe wpisy konfiguracji;
- stabilny błąd bootstrapu;
- niezmiennik `read_only=true`;
- licznik `mutation_operations_invoked=0`.

Qt otrzymuje gotowy snapshot i wyłącznie renderuje jego stan.

## Wątki

- główny wątek Qt tworzy i aktualizuje widgety;
- `BootstrapWorker` działa w `QThreadPool`;
- worker emituje jeden `BootstrapSnapshot`;
- wyjątek w workerze jest zamieniany na bezpieczny snapshot błędu;
- P06 nie ma stałego watchera ani okresowego pollingu.

Ten model jest podstawą dla P07, gdzie jawne mutacje również muszą działać poza wątkiem GUI.

## Interfejs

Pierwszy shell zawiera:

- ciemny sidebar;
- stałą informację `READ-ONLY STARTUP`;
- selektor projektu;
- przycisk `Odśwież odczyt` wykonujący ponownie tylko bootstrap;
- status Operator API;
- liczbę przygotowanych projektów;
- placeholdery etapów P07–P10.

Nie ma przycisków Start, Stop ani re-arm.

## Uruchomienie

Instalacja opcjonalnego GUI:

```powershell
python -m pip install -e ".[dev,gui]"
```

Aplikacja:

```powershell
bdb-control-center
```

Niestandardowy katalog workspace:

```powershell
bdb-control-center --workspaces-root "$env:LOCALAPPDATA\BartoszDevBridge\workspaces"
```

Headless smoke:

```powershell
bdb-control-center `
  --workspaces-root <existing-empty-or-prepared-root> `
  --headless-smoke `
  --json-out .artifacts/control-center-smoke.json
```

## Zachowanie przy braku konfiguracji

- istniejący, pusty katalog workspace daje poprawny stan `0 projektów`;
- brak katalogu daje widoczny błąd `invalid_argument` z Operator API;
- GUI nie tworzy katalogu automatycznie;
- błąd nie uruchamia recovery ani mutacji.

## Poza zakresem P06

- Start, Stop, Status i re-arm — P07;
- bieżąca operacja — P08;
- pełny Journal i historia — P09;
- diagnostyka i eksport — P10;
- kreator Prepare — P11;
- tray i powiadomienia — P12;
- instalator — P13.

## Bramka wyjścia

- `bdb_gui` jest oddzielnym pakietem;
- PySide6 pozostaje opcjonalne;
- bootstrap jest testowany bez Qt;
- rzeczywiste okno przechodzi Windows offscreen smoke;
- raport potwierdza `read_only_startup=true` i zero mutacji;
- brak importów `bdb_bridge` oraz brak procesów i sieci w GUI;
- pełna macierz Bridge CI pozostaje zielona.
