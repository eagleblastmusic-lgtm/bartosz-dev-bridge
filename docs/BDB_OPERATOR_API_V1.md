# BDB Operator API v1

Status: **P03 accepted / P04 observability extension**  
Transport: **local in-process API with optional JSON CLI adapter**

## Cel

`bdb_operator.OperatorApi` jest stabilną fasadą aplikacyjną nad istniejącym operatorem BDB. Nie implementuje ponownie lifecycle, recovery, promotera, Native Hosta, operacji Git ani mutacji Journalu.

Od P04 publiczna fasada zawiera również osobną, wymuszoną jako read-only projekcję Journalu dla eventów i bieżącej operacji. Projekcja nie używa `Journal.open()`, nie wykonuje migracji i nie zapisuje do bazy.

Kierunek zależności:

```text
future bdb_gui -> bdb_operator -> existing scripts and bdb_bridge CLI / read-only Journal projection
```

`bdb_bridge` nie importuje `bdb_operator`.

## Katalog operacji v1

Odczyty:

- `capabilities()`;
- `list_projects(workspaces_root)`;
- `status(workspace_root)`;
- `events(workspace_root, ...)`;
- `current_operation(workspace_root)`;
- `logs(workspace_root, ...)`.

Jawne mutacje:

- `prepare(...)`;
- `start(workspace_root, arm_minutes=30)`;
- `stop(workspace_root)`;
- `rearm(workspace_root, arm_minutes=30)`.

Nie istnieje metoda przyjmująca dowolną nazwę programu, argumenty shell ani surowe polecenie użytkownika.

## Adaptery wykonawcze

- `Prepare` wywołuje istniejący `scripts/prepare_workspace_loop.py`;
- `Start`, `Status` i `Stop` wywołują istniejący `scripts/Invoke-BDBWorkspaceLoop.ps1`;
- `rearm` wywołuje istniejące `python -m bdb_bridge bridge native-host arm`;
- każde wywołanie procesu używa `shell=False`;
- eventy i bieżąca operacja korzystają z SQLite `mode=ro` oraz `PRAGMA query_only=ON`;
- logi pochodzą tylko ze ścieżek zapisanych w stanie przygotowanego workspace;
- Operator API nie wykonuje Git bezpośrednio.

Szczegóły P04: [BDB Operator Observability v1](BDB_OPERATOR_OBSERVABILITY_V1.md).

## Odpowiedź

Każda metoda zwraca `bdb-operator-response-v1`:

```json
{
  "schema": "bdb-operator-response-v1",
  "operation_id": "6c221f9d-1d80-46f8-bb4c-f77223a110ae",
  "operation": "status",
  "ok": true,
  "generated_at": "2026-07-18T19:00:00Z",
  "project_alias": "calculator2",
  "data": {},
  "error": null
}
```

Przy błędzie `ok=false`, `data` pozostaje pustym obiektem, a `error` zawiera stabilny kod, komunikat oraz bounded szczegóły diagnostyczne.

Schemat: `schemas/bdb-operator-response-v1.schema.json`.

## Kody błędów v1

Podstawowe:

- `invalid_argument`;
- `unsupported_platform`;
- `workspace_state_missing`;
- `workspace_state_invalid`;
- `operator_script_missing`;
- `executable_missing`;
- `command_failed`;
- `command_timeout`;
- `invalid_response`;
- `internal_error`.

Obserwowalność:

- `observability_config_missing`;
- `observability_config_invalid`;
- `journal_missing`;
- `journal_unavailable`;
- `log_read_failed`.

## Bezpieczeństwo

- API działa lokalnie;
- nie otwiera portu HTTP ani WebSocket;
- `status`, `list_projects`, `events`, `current_operation` i `logs` nie wykonują `Start` ani re-arm;
- mutacje mają osobne, jawne metody;
- lista dozwolonych operacji jest zamknięta;
- argumenty procesu są przekazywane jako tablica;
- stdout musi być jednym obiektem JSON;
- stderr/stdout w błędach są ograniczone do końcowych 4000 znaków;
- event payloady i logi mają twarde limity;
- GUI nie będzie źródłem prawdy — odpowiedzi są projekcjami istniejącego operatora i Journalu.

## CLI lokalne

Po instalacji editable:

```powershell
bdb-operator capabilities
bdb-operator list-projects --workspaces-root "$env:LOCALAPPDATA\BartoszDevBridge\workspaces"
bdb-operator status --root "$env:LOCALAPPDATA\BartoszDevBridge\workspaces\calculator2"
bdb-operator events --root <workspace-root> --after-event-id 0 --limit 100
bdb-operator current-operation --root <workspace-root>
bdb-operator logs --root <workspace-root> --max-lines 200
bdb-operator start --root <workspace-root> --arm-minutes 30
bdb-operator rearm --root <workspace-root> --arm-minutes 30
bdb-operator stop --root <workspace-root>
```

CLI zawsze drukuje odpowiedź JSON v1 i zwraca kod `0` przy `ok=true`, a `1` przy `ok=false`.

## Nadal poza zakresem

- GUI;
- tray i powiadomienia;
- HTTP, WebSocket lub cloud relay;
- automatyczny start przy uruchomieniu klienta;
- automatyczny merge, deploy lub naprawa repozytorium;
- live streaming i watcher działający w tle;
- pełny kreator projektu — P11 rozbuduje `prepare` o UX i walidację prezentacyjną.

## Bramka P03/P04

- publiczny pakiet `bdb_operator`;
- wersjonowane response i projection schemas;
- zamknięty katalog operacji;
- testy potwierdzające brak ukrytych mutacji i brak `shell=True`;
- read-only Journal z `mode=ro` i `query_only`;
- bounded event payloady oraz logi;
- testy błędów, listy projektów, statusu, eventów, bieżącej operacji, logów, start/stop/re-arm i prepare;
- zielone CI;
- brak pakietu `bdb_gui` oraz brak listenera sieciowego.
