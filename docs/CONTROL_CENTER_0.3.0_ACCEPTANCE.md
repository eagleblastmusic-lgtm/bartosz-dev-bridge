# BDB Control Center 0.3.0 — lokalny odbiór Windows

Status: CANDIDATE ACCEPTANCE PROCEDURE

## Zakres

Odbiór dotyczy wyłącznie przenośnego, niepodpisanego artefaktu Windows utworzonego przez workflow `Control Center Release Artifact`.

Pakiet nie jest instalatorem. Nie wymaga uprawnień administratora, nie zapisuje autostartu, nie publikuje Release, nie pobiera aktualizacji i nie wykonuje deployu.

## Zawartość pobranego artefaktu GitHub Actions

Katalog powinien zawierać dokładnie potrzebne elementy odbioru:

- `BDB-Control-Center-windows-x86_64-0.3.0.zip`;
- `bdb-release-manifest-v1.json`;
- `Invoke-BDBControlCenterArtifactAcceptance.ps1`;
- po przejściu automatycznej bramki: `bdb-control-center-acceptance-v1.json`.

## Automatyczny odbiór

W PowerShell uruchomionym jako zwykły użytkownik:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\Invoke-BDBControlCenterArtifactAcceptance.ps1 `
  -ReleaseDirectory . `
  -ExpectedVersion 0.3.0
```

Skrypt:

1. sprawdza zamknięty manifest wydania;
2. potwierdza wersję, platformę i politykę bez automatycznej dystrybucji;
3. porównuje nazwę, rozmiar i SHA-256 ZIP-u;
4. rozpakowuje pakiet do losowego katalogu tymczasowego;
5. uruchamia gotowy `BDB-Control-Center.exe` w trybie headless smoke;
6. potwierdza start tylko do odczytu i zero mutacji;
7. potwierdza panel przebiegu operacji;
8. potwierdza historię sesji i jawne otwieranie wyników/receipts/katalogów;
9. potwierdza `session_repair_relationships_inferred=false`;
10. zapisuje `bdb-control-center-acceptance-v1.json`.

Domyślnie katalog tymczasowy jest usuwany. Przełącznik `-KeepExtracted` pozostawia go do ręcznego uruchomienia GUI i zwraca jego ścieżkę w receipt.

## Ręczny odbiór GUI

Po przejściu automatycznego testu można ponownie uruchomić skrypt z `-KeepExtracted`, a następnie otworzyć wskazany plik `BDB-Control-Center.exe`.

Należy potwierdzić:

- aplikacja otwiera się jako `BDB Control Center`;
- ekran `Current operation` zawiera przebieg etapów;
- ekran `History` zawiera zakładki `Zdarzenia Journalu` i `Sesje i receipts`;
- przy pustym katalogu projektów aplikacja nie tworzy konfiguracji ani procesów;
- odświeżenia są jawne, bez automatycznego pollingu;
- przyciski otwarcia wyniku, receipt i katalogu są nieaktywne bez zwalidowanego artefaktu;
- zamknięcie aplikacji nie instaluje usług, autostartu ani aktualizacji.

## Warunki PASS

Odbiór jest pozytywny tylko wtedy, gdy:

- receipt ma `schema=bdb-control-center-acceptance-v1` i `status=pass`;
- `version` oraz `application_version` wynoszą `0.3.0`;
- SHA-256 artefaktu jest zgodny z manifestem;
- `read_only_startup=true`;
- `mutation_operations_invoked=0`;
- `operation_flow_present=true`;
- `session_history_view_present=true`;
- `session_repair_relationships_inferred=false`.

## Warunki STOP

Nie należy uruchamiać pakietu ręcznie, gdy:

- manifest lub ZIP jest nieobecny;
- nazwa, rozmiar albo SHA-256 nie zgadza się;
- source commit nie odpowiada oczekiwanemu kandydatowi;
- skrypt odbioru zgłasza błąd;
- Windows oznacza plik jako pochodzący z innego źródła niż pobrany artefakt GitHub Actions;
- pakiet żąda administratora, instalacji lub dostępu sieciowego.

## Poza zakresem 0.3.0

- MSI/MSIX i instalacja systemowa;
- Authenticode;
- GitHub Release i tag;
- automatyczne aktualizacje;
- deploy;
- użycie repozytorium biznesowego;
- automatyczny merge wyników pracy BDB.
