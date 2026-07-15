# POC-0 — uruchomienie na Windows

## Cel

Ten pakiet implementuje wyłącznie minimalny POC-0 opisany w dokumentacji v1.1. Sprawdza transport:

```text
ChatGPT -> branch commands -> lokalny poc_bridge.py -> branch results -> ChatGPT
```

Nie jest to GHB-0 ani produkt do codziennego użycia. Nie zawiera GUI, SQLite, LSP, Browser Lab, Hermesa, obsługi wielu sesji, commitowania kodu projektu, pushowania branchy projektu ani otwierania PR-ów przez lokalny Executor.

## Wymagania

- Windows 10 lub 11;
- Git dostępny jako `git`;
- Python 3.11 lub nowszy dostępny jako `python`;
- dostęp Git do prywatnego repozytorium `eagleblastmusic-lgtm/bartosz-dev-poc-control`;
- Git Credential Manager albo inne zewnętrzne uwierzytelnienie — tokenów nie zapisujemy w repozytorium ani konfiguracji POC.

## 1. Pobierz branch implementacyjny

Pracuj z repozytorium `eagleblastmusic-lgtm/bartosz-dev-bridge` i branchem:

```text
gpt/poc-0-bootstrap
```

Nie uruchamiaj POC z przypadkowej lub niezweryfikowanej kopii skryptu.

## 2. Bootstrap

W PowerShell, z katalogu repozytorium Bridge:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap_windows.ps1
```

Domyślny katalog lokalny:

```text
C:\Projekt\DevMaster\POC0
```

Parametr `Root` pozostaje konfigurowalny. Inny katalog można wskazać jawnie, na przykład:

```powershell
.\scripts\bootstrap_windows.ps1 -Root "D:\BartoszDev\POC0"
.\scripts\run_poc_bridge.ps1 -Root "D:\BartoszDev\POC0"
```

Bootstrap:

1. tworzy dedykowany virtual environment;
2. instaluje Bridge i pytest;
3. klonuje control repo bez zapisywania tokena;
4. potwierdza branche `commands` i `results`;
5. kopiuje syntetyczne fixture do osobnego lokalnego repozytorium;
6. tworzy czysty commit bazowy fixture;
7. zapisuje lokalny `poc_config.json` poza repozytorium kodu;
8. tworzy katalog worktree.

Zapisz wyświetlony exact `Base SHA`. Manifest sesji na branchu `commands` musi używać tego SHA.

## 3. Kontrakt sesji

Bridge obsługuje dokładnie jedną aktywną sesję, maksymalnie trzy komendy i schemat `1.1`.

Dozwolone operacje:

```text
open_read
replace_exact_and_test
```

Dozwolony profil wykonawczy:

```text
poc_pytest
```

Profil zawsze uruchamia lokalnie ustaloną komendę:

```text
<python z venv> -m pytest -q
```

Komenda z GitHuba nie może zmienić executable, argumentów, working directory ani środowiska procesu.

Oczekiwana struktura na branchu `commands`:

```text
sessions/<session_id>/manifest.json
sessions/<session_id>/commands/000001.json
sessions/<session_id>/commands/000002.json
sessions/<session_id>/commands/000003.json
```

Wyniki są publikowane na branchu `results`:

```text
sessions/<session_id>/results/000001.json
sessions/<session_id>/results/000002.json
sessions/<session_id>/results/000003.json
```

## 4. Uruchom Bridge

```powershell
.\scripts\run_poc_bridge.ps1
```

Bridge przez maksymalnie pięć minut:

- polluje exact ref `origin/commands`;
- waliduje schema version, `session_id`, `sequence`, `command_id`, `repository_id`, exact `base_sha` i `expected_revision`;
- tworzy worktree sesji z exact SHA;
- wykonuje tylko dozwolone operacje;
- publikuje mały wynik, maksymalnie 16 KiB, z end markerem i hashami outputu;
- kończy się po wyniku sekwencji 3 albo timeoutie.

## 5. Oczekiwany przebieg POC-0A

```text
001 open_read                         -> success
002 min(value, 100) + poc_pytest      -> failed
003 max(0, min(value, 100)) + pytest  -> success
```

Komenda 003 musi mieć `expected_revision: 1`. Finalny worktree pozostaje zachowany do inspekcji.

## Bezpieczeństwo i ograniczenia

- Bridge nie wykonuje tekstu shellowego otrzymanego z GitHuba.
- Repozytorium jest wybierane lokalnie, nie przez pełną ścieżkę z komendy.
- Manifest i lokalna allowlista muszą jednocześnie zezwalać na ścieżkę.
- Ścieżki absolutne, `..`, backslashe w protokole i symlinki uciekające poza worktree są odrzucane.
- Źródłowy checkout fixture musi pozostać czysty.
- Failure nie usuwa worktree.
- Lokalny PASS nie zastępuje GitHub Actions ani późniejszego niezależnego CI.
- POC-0 nie zapewnia jeszcze produkcyjnej izolacji procesu ani recovery po restarcie. Te elementy należą do późniejszych faz i nie są implementowane w tym branchu.
