# BDB Control Center — P13 pakiet wydaniowy

Status: IMPLEMENTED ON BRANCH

## Cel

P13 tworzy powtarzalny artefakt Windows dla Control Center oraz zamknięty manifest integralności. Nie wdraża automatycznej dystrybucji ani self-update.

## Budowanie

Workflow `Control Center Release Artifact`:

- uruchamia się wyłącznie ręcznie przez `workflow_dispatch`;
- wymaga jawnego numeru wersji semantycznej;
- buduje aplikację jako katalog PyInstaller `onedir`;
- dołącza istniejące skrypty operatorskie potrzebne przez publiczne Operator API;
- uruchamia headless smoke gotowego pliku wykonywalnego;
- tworzy ZIP;
- generuje `bdb-release-manifest-v1`;
- ponownie weryfikuje nazwę, rozmiar i SHA-256 ZIP-u;
- zapisuje ZIP i manifest jako tymczasowy artefakt GitHub Actions na 14 dni.

Workflow nie tworzy GitHub Release, taga, instalatora systemowego ani publikacji produkcyjnej.

## Manifest

Manifest zawiera:

- produkt i wersję;
- platformę `windows-x86_64`;
- pełny source commit SHA;
- czas budowania;
- nazwę, rozmiar i SHA-256 artefaktu;
- jawne `auto_download=false`, `auto_install=false`, `published_release=false`;
- `signature=null`, ponieważ P13 nie udaje wdrożonego podpisu kodu.

Loader odrzuca dodatkowe pola, nieprawidłowy format, inny kanał dystrybucji i próby włączenia automatycznej instalacji.

## Weryfikacja

`scripts/build_release_manifest.py verify` sprawdza lokalny plik bez dostępu do sieci:

1. schemat i zamknięty katalog pól;
2. nazwę artefaktu;
3. rozmiar;
4. pełny SHA-256.

Zmiana choćby jednego bajtu powoduje błąd weryfikacji.

## Ograniczenia

P13 nie zapewnia jeszcze:

- podpisu Authenticode;
- zaufanego klucza wydawcy;
- instalatora MSI/MSIX;
- automatycznego sprawdzania wersji;
- pobierania lub instalowania aktualizacji;
- publikowania kanału produkcyjnego.

Takie rozszerzenia wymagają osobnej decyzji dotyczącej kluczy, dystrybucji, rollbacku i własności wydania.
