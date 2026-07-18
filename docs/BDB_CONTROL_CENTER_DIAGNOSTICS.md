# BDB Control Center — P10 diagnostyka i eksport

Status: IMPLEMENTED ON BRANCH

## Cel

P10 zastępuje placeholder `Diagnostics` bounded snapshotem diagnostycznym oraz jawnym eksportem sanitizowanego ZIP-u.

## Zbieranie snapshotu

Kliknięcie `Zbierz diagnostykę` wykonuje dokładnie cztery odczyty Operator API:

1. `capabilities()`;
2. `status(workspace_root)`;
3. `current_operation(workspace_root)`;
4. `logs(workspace_root, max_bytes=262144, max_lines=200)`.

Snapshot:

- ma schemat `bdb-gui-diagnostics-v1`;
- pozostaje `read_only=true`;
- ma `mutation_operations_invoked=0`;
- zachowuje osobny wynik każdej sekcji;
- może być częściowy, gdy jeden odczyt zwróci błąd;
- zawiera wersje BDB, Pythona, platformy i PySide6.

## Sanitizacja

Przed pokazaniem oraz eksportem dane przechodzą redakcję `bdb-redaction-v1`.

Redagowane są:

- klucze i pola zawierające token, password, passwd, secret, cookie, authorization, API key, access key albo private key;
- przypisania tego typu w liniach tekstowych;
- wartości `Bearer ...`.

Snapshot nie zawiera dowolnych plików repozytorium ani pełnego Journalu.

## Eksport

Eksport jest osobnym działaniem po zebraniu snapshotu:

1. użytkownik klika `Eksportuj ZIP`;
2. wskazuje docelowy plik `.zip`;
3. istniejący plik wymaga osobnego potwierdzenia nadpisania;
4. eksporter zapisuje plik tymczasowy;
5. po poprawnym zamknięciu archiwum używa atomowego `os.replace`;
6. zwraca ścieżkę, rozmiar, SHA-256 i listę wpisów.

Archiwum zawiera:

- `diagnostics.json`;
- osobny JSON każdej sekcji;
- `manifest.json` z rozmiarami i hashami.

Manifest jawnie deklaruje:

- `contains_journal_database=false`;
- `contains_repository_files=false`.

## Brak ukrytego zapisu

Otwarcie aplikacji, bootstrap, wybór projektu oraz zebranie snapshotu nie zapisują pliku. Eksport rozpoczyna się dopiero po jawnym wyborze ścieżki.

## Serializacja

Collect i Export korzystają ze wspólnego mechanizmu `one active task`. Podczas ich działania wszystkie pozostałe odczyty i mutacje GUI są zablokowane.

## Poza zakresem

- automatyczne wysyłanie pakietu;
- upload do GitHub, Drive lub chmury;
- dołączanie Journalu SQLite;
- dołączanie kodu repozytorium;
- logi inne niż bounded tails udostępnione przez Operator API;
- automatyczny eksport przy błędzie;
- telemetryka zdalna.
