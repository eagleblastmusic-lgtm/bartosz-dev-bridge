# BDB Operator API v1

Status: **P03 / proposed implementation**  
Transport: **local in-process API with optional JSON CLI adapter**

## Cel

`bdb_operator.OperatorApi` jest stabilną fasadą aplikacyjną nad istniejącym operatorem BDB. Nie odczytuje ani nie modyfikuje Journalu bezpośrednio i nie implementuje ponownie lifecycle, recovery, promotera, Native Hosta ani operacji Git.

Kierunek zależności:

```text
future bdb_gui -> bdb_operator -> existing scripts and bdb_bridge CLI
```

`bdb_bridge` nie importuje `bdb_operator`.

## Katalog operacji v1

Odczyty:

- `capabilities()`;
- `list_projects(workspaces_root)`;
- `status(workspace_root)`.

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
- Operator API nie interpretuje Journalu i nie wykonuje Git bezpośrednio.

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

## Bezpieczeństwo

- API działa lokalnie;
- nie otwiera portu HTTP ani WebSocket;
- `status` i `list_projects` nie wykonują `Start` ani re-arm;
- mutacje mają osobne, jawne metody;
- lista dozwolonych operacji jest zamknięta;
- argumenty procesu są przekazywane jako tablica;
- stdout musi być jednym obiektem JSON;
- stderr/stdout w błędach są ograniczone do końcowych 4000 znaków;
- GUI nie będzie źródłem prawdy — odpowiedź jedynie opakowuje wynik istniejącego operatora.

## CLI lokalne

Po instalacji editable:

```powershell
bdb-operator capabilities
bdb-operator list-projects --workspaces-root "$env:LOCALAPPDATA\BartoszDevBridge\workspaces"
bdb-operator status --root "$env:LOCALAPPDATA\BartoszDevBridge\workspaces\calculator2"
bdb-operator start --root <workspace-root> --arm-minutes 30
bdb-operator rearm --root <workspace-root> --arm-minutes 30
bdb-operator stop --root <workspace-root>
```

CLI zawsze drukuje odpowiedź JSON v1 i zwraca kod `0` przy `ok=true`, a `1` przy `ok=false`.

## Poza zakresem P03

- GUI;
- event stream `bdb-event-v1` — P04;
- tray i powiadomienia;
- HTTP, WebSocket lub cloud relay;
- automatyczny start przy uruchomieniu klienta;
- automatyczny merge, deploy lub naprawa repozytorium;
- pełny kreator projektu — P11 rozbuduje `prepare` o UX i walidację prezentacyjną.

## Bramka wyjścia P03

- publiczny pakiet `bdb_operator`;
- wersjonowany response schema;
- zamknięty katalog operacji;
- testy potwierdzające brak ukrytych mutacji i brak `shell=True`;
- testy błędów, listy projektów, statusu, start/stop/re-arm i prepare;
- zielone CI;
- brak pakietu `bdb_gui` oraz brak listenera sieciowego.
