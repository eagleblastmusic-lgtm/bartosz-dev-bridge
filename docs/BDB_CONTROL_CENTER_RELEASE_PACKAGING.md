# BDB Control Center — zweryfikowany pakiet przenośny Windows

Status: IMPLEMENTED FOR 0.3.0 CANDIDATE

## Cel

Proces tworzy powtarzalny, niepodpisany artefakt Windows dla Control Center oraz zamknięty manifest integralności. Nie wdraża automatycznej dystrybucji, instalatora ani self-update.

## Wersja 0.3.0

Tożsamość wersji jest zapisana i testowana w czterech miejscach:

- wejście ręcznego workflow;
- `pyproject.toml`;
- `bdb_gui.version.APPLICATION_VERSION`;
- manifest modułu `bartosz-dev-bridge.module.json`.

Budowa jest zatrzymywana, gdy dowolna z tych wartości różni się od `0.3.0`. Gotowy EXE raportuje własną `application_version`, więc ZIP nie może jedynie nosić nazwy nowej wersji przy starszym kodzie wewnątrz.

## Budowanie

Workflow `Control Center Release Artifact`:

- uruchamia się wyłącznie ręcznie przez `workflow_dispatch`;
- wymaga jawnego numeru wersji semantycznej;
- sprawdza zgodność wersji źródłowej;
- waliduje składnię samodzielnego skryptu odbioru PowerShell;
- uruchamia kontrakty manifestu, pakowania, schematów GUI i source smoke;
- buduje aplikację jako katalog PyInstaller `onedir`;
- dołącza istniejące skrypty operatorskie potrzebne przez publiczne Operator API;
- uruchamia headless smoke gotowego pliku wykonywalnego;
- tworzy ZIP;
- generuje `bdb-release-manifest-v1`;
- ponownie weryfikuje nazwę, rozmiar i SHA-256 ZIP-u;
- uruchamia samodzielny odbiór gotowego ZIP-u;
- zapisuje ZIP, manifest, skrypt odbioru i receipt jako tymczasowy artefakt GitHub Actions na 14 dni.

Workflow nie tworzy GitHub Release, taga, instalatora systemowego ani publikacji produkcyjnej.

## Smoke gotowego EXE

Bramka pakietu potwierdza:

- `application_version=0.3.0`;
- start tylko do odczytu;
- `mutation_operations_invoked=0`;
- brak tray w trybie headless;
- obecność przebiegu bieżącej operacji;
- read-only ekran bieżącej operacji;
- dwie zakładki historii;
- obecność read-only historii sesji;
- jawne otwieranie wyniku, receipt i katalogu;
- `session_repair_relationships_inferred=false`.

## Manifest

Manifest zawiera:

- produkt i wersję;
- platformę `windows-x86_64`;
- pełny source commit SHA;
- czas budowania;
- nazwę, rozmiar i SHA-256 artefaktu;
- jawne `auto_download=false`, `auto_install=false`, `published_release=false`;
- `signature=null`, ponieważ proces nie udaje wdrożonego podpisu kodu.

Loader odrzuca dodatkowe pola, nieprawidłowy format, inny kanał dystrybucji i próby włączenia automatycznej instalacji.

## Samodzielny odbiór

`scripts/Invoke-BDBControlCenterArtifactAcceptance.ps1` nie wymaga repozytorium ani Pythona. Odczytuje manifest, sprawdza ZIP, rozpakowuje pakiet do katalogu tymczasowego, uruchamia gotowy EXE i zapisuje `bdb-control-center-acceptance-v1.json`.

Skrypt nie pobiera plików, nie instaluje pakietu, nie wymaga administratora i nie zmienia konfiguracji systemowej. Ta sama procedura jest wykonywana w workflow przed udostępnieniem artefaktu.

Szczegółowy odbiór: [Control Center 0.3.0 acceptance](CONTROL_CENTER_0.3.0_ACCEPTANCE.md).

## Weryfikacja

`scripts/build_release_manifest.py verify` sprawdza lokalny plik bez dostępu do sieci:

1. schemat i zamknięty katalog pól;
2. nazwę artefaktu;
3. rozmiar;
4. pełny SHA-256.

Zmiana choćby jednego bajtu powoduje błąd weryfikacji.

## Ograniczenia

Proces nie zapewnia jeszcze:

- podpisu Authenticode;
- zaufanego klucza wydawcy;
- instalatora MSI/MSIX;
- automatycznego sprawdzania wersji;
- pobierania lub instalowania aktualizacji;
- publikowania kanału produkcyjnego;
- automatycznego deployu.

Takie rozszerzenia wymagają osobnej decyzji dotyczącej kluczy, dystrybucji, rollbacku i własności wydania.
