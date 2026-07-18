# BDB Control Center — P11 kreator Prepare

Status: IMPLEMENTED ON BRANCH

## Cel

P11 zastępuje placeholder `Projects` kreatorem bezpiecznego przygotowania projektu BDB. Kreator nie implementuje własnego Git workflow. Korzysta wyłącznie z publicznego `OperatorApi.prepare()`, który deleguje do istniejącego `scripts/prepare_workspace_loop.py`.

## Dwie bramki

### 1. Zbuduj plan

Pierwszy krok:

- waliduje alias według tego samego zamkniętego wzorca co Operator API;
- sprawdza, że katalog workspace'ów istnieje;
- wylicza docelową ścieżkę `<workspaces_root>/<alias>`;
- sprawdza, że docelowy workspace jeszcze nie istnieje;
- sprawdza, że source repo jest istniejącym checkoutem Git;
- normalizuje i ogranicza allowed paths;
- waliduje interpreter Pythona oraz test timeout;
- tworzy `bdb-gui-prepare-plan-v1`.

Ten krok jest tylko do odczytu i ma `mutation_operations_invoked=0`.

### 2. Przygotuj projekt

Drugi krok jest dostępny dopiero, gdy:

- plan jest ważny;
- formularz nie zmienił się po walidacji;
- użytkownik zaznaczył świeże, jawne potwierdzenie;
- użytkownik zaakceptował finalne okno dialogowe.

Każdy nowy plan i każda zmiana formularza zerują wcześniejsze potwierdzenie. Dopiero wtedy worker wywołuje `OperatorApi.prepare()` dokładnie raz.

## Zamknięty kontrakt Operator API

Kreator przekazuje wyłącznie parametry obsługiwane przez publiczne `OperatorApi.prepare()`:

- `workspace_root`;
- `source_repo`;
- `alias`;
- `allowed_paths`;
- `test_timeout_seconds`;
- `python_executable`.

GUI nie przekazuje nieobsługiwanych limitów ani alternatywnej konfiguracji Native Host. Rozszerzenie kontraktu wymagałoby osobnego etapu backendowego, testów i ADR.

## Właściciel preflightu

GUI nie wykonuje ani nie duplikuje właściwego Git preflightu. Istniejący preparer pozostaje właścicielem kontroli:

- source checkout jest czysty;
- checkout nie jest detached;
- branch i base SHA są aktualne;
- alias i target paths nie kolidują;
- worktree zostaje utworzony w kontrolowany sposób;
- control repo oraz Native Host config powstają transakcyjnie;
- rollback usuwa wyłącznie nowo utworzone artefakty preparera.

Plan jawnie zapisuje `preflight_owner=existing_prepare_workspace_loop`.

## Pola kreatora

- alias;
- source repo;
- allowed paths — jeden wzorzec na linię;
- Python executable;
- test timeout.

## Walidacja ścieżek

Allowed paths:

- są normalizowane do `/`;
- nie mogą być absolutne;
- nie mogą zawierać przejścia `..`;
- nie mogą zawierać dysku `C:` ani innego `:`;
- są deduplikowane;
- muszą zawierać od 1 do 100 wpisów.

## Po sukcesie

Po sukcesie Prepare:

1. GUI pokazuje receipt Operator API;
2. zwiększa licznik jawnych mutacji o 1;
3. odświeża katalog przygotowanych projektów;
4. nie uruchamia Bridge’a ani nie wykonuje re-arm.

## Po błędzie

Błąd preparera jest pokazany bez zgadywania. GUI nie próbuje samodzielnego cleanupu ani ponownego Prepare.

## Poza zakresem

- klonowanie repozytorium;
- zmiana brancha source repo;
- automatyczne czyszczenie brudnego checkoutu;
- arbitralne komendy Git lub shell;
- Prepare wielu projektów jednocześnie;
- Start po Prepare;
- edycja istniejącej konfiguracji projektu;
- rozszerzanie parametrów publicznego Operator API.
