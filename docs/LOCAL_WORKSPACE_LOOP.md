# Local Workspace Loop

Local Workspace Loop zamyka ręczny pilot Bartosz Dev Bridge w jeden kontrolowany przebieg:

```text
polecenie użytkownika
→ bounded workspace context
→ odczyt potrzebnych plików
→ multi_file_patch w detached worktree
→ profil testowy
→ rollback i kolejna próba przy recoverable failure
→ commit w worktree
→ zweryfikowany fast-forward lokalnego checkoutu
→ receipt promocji
→ końcowa odpowiedź ChatGPT
```

Użytkownik nie kopiuje `BDB_RESULT` i nie uruchamia osobnej promocji. JSON pozostaje audytowalnym protokołem wewnętrznym, ale przy `presentation.mode = "compact"` rozszerzenie pokazuje małą kartę stanu i chowa źródło akcji.

## Zakres wdrożenia

- bounded, read-only snapshot lokalnego repo;
- filtrowanie wszystkich treści i nazw plików przez `allowed_paths`;
- maksymalnie 2000 śledzonych ścieżek, 80 treści plików, 256 KiB treści, 500 symboli;
- dalszy dokładny odczyt przez istniejącą operację `open_read`;
- opt-in AUTO z limitem iteracji i czasu;
- automatyczna kontynuacja po błędzie profilu tylko po potwierdzonym pełnym rollbacku;
- automatyczna promocja wyłącznie zielonego `multi_file_patch` z nowej sesji `sequence = 1`;
- commit z dokładnym zestawem ścieżek i wyłącznie `git merge --ff-only` do czystego checkoutu;
- trwały receipt z commitem, plikami i hashami;
- jednoznaczny operator Windows: `Prepare`, `Start`, `Status`, `Stop`.

## Jednorazowe podłączenie projektu

W przykładzie projekt znajduje się w `C:\Projekty\Kalkulator test`, a runtime poza checkoutem:

```powershell
Set-Location "C:\Projekty\DevMaster\bartosz-dev-bridge"

.\scripts\Invoke-BDBWorkspaceLoop.ps1 `
  -Action Prepare `
  -Root "$env:LOCALAPPDATA\BartoszDevBridge\workspaces\calculator" `
  -Repo "C:\Projekty\Kalkulator test" `
  -Alias "calculator" `
  -AllowedPath @(
    "*.py",
    "tests/*.py",
    "README.md",
    ".gitignore",
    ".github/workflows/*.yml"
  )
```

`Prepare` wymaga:

- istniejącego, czystego repo Git;
- checkoutu przypiętego do lokalnej gałęzi;
- nieistniejącego katalogu runtime;
- istniejącej konfiguracji Native Hosta;
- jawnej allowlisty co najmniej jednej ścieżki.

Operacja tworzy lokalny kanał `commands/results`, runtime, worktree root, konfigurację Bridge, kopię bezpieczeństwa konfiguracji Native Hosta i trwały alias. Nie modyfikuje kodu projektu.

## Codzienny start

```powershell
.\scripts\Invoke-BDBWorkspaceLoop.ps1 `
  -Action Start `
  -Root "$env:LOCALAPPDATA\BartoszDevBridge\workspaces\calculator" `
  -ArmMinutes 30
```

Kolejność jest stała:

1. promoter startuje i oznacza wszystkie stare wyniki jako zastane;
2. Bridge przechodzi do `RUNNING`;
3. Native Host zostaje uzbrojony na ograniczony TTL.

Dopiero wtedy status może być `READY`.

```powershell
.\scripts\Invoke-BDBWorkspaceLoop.ps1 `
  -Action Status `
  -Root "$env:LOCALAPPDATA\BartoszDevBridge\workspaces\calculator"
```

`READY` wymaga równocześnie:

- Bridge `RUNNING`;
- Native Host `armed = true`;
- żywego procesu promotera;
- czystego lokalnego checkoutu.

## Bezpieczny stop

```powershell
.\scripts\Invoke-BDBWorkspaceLoop.ps1 `
  -Action Stop `
  -Root "$env:LOCALAPPDATA\BartoszDevBridge\workspaces\calculator"
```

Stop:

1. rozbraja Native Hosta;
2. zatrzymuje Bridge i czeka na `OFFLINE`;
3. wysyła kooperacyjne żądanie stop do promotera;
4. zachowuje worktree, Journal, wyniki, receipts i logi.

Nie wykonuje `git reset`, `git clean`, `worktree prune`, `Remove-Item -Recurse` ani usuwania repozytorium.

## AUTO w rozszerzeniu

AUTO pozostaje domyślnie wyłączone. Użytkownik włącza je lokalnie w popupie rozszerzenia. Limity domyślne:

```text
4 iteracje
10 minut
```

Dwie bramki muszą być spełnione jednocześnie:

- popup: `autoEnabled = true`;
- akcja: `automation.mode = "auto"`.

ASSISTED nadal działa bez AUTO i wymaga ręcznego przycisku.

## Pierwsza akcja: kontekst

Wewnętrzna akcja rozpoczynająca pętlę:

```json
{
  "schema": "bdb-action-v1",
  "repo_alias": "calculator",
  "operation": "workspace_context",
  "payload": {},
  "automation": {
    "mode": "auto",
    "loop_id": "calculator-history-20260717",
    "iteration": 1
  },
  "presentation": {
    "mode": "compact"
  }
}
```

Rozszerzenie kieruje ją do zaufanego `context` Native Hosta. Wynik zawiera:

- exact `base_sha`;
- czystość checkoutu;
- dozwolone ścieżki;
- śledzone pliki;
- bounded treści UTF-8 i SHA-256;
- bounded indeks symboli;
- licznik zmian poza zakresem bez ujawnienia ich nazw;
- najnowszy zweryfikowany receipt promocji.

Snapshot nie ujawnia absolutnej ścieżki repozytorium.

## Dokładny odczyt

Gdy potrzebny plik nie znalazł się w bounded snapshot, kolejna iteracja używa istniejącej operacji:

```json
{
  "schema": "bdb-action-v1",
  "repo_alias": "calculator",
  "operation": "open_read",
  "payload": {
    "path": "calculator.py"
  },
  "automation": {
    "mode": "auto",
    "loop_id": "calculator-history-20260717",
    "iteration": 2
  },
  "presentation": {
    "mode": "compact"
  }
}
```

Odczyt nadal podlega aliasowi, allowliście, exact base SHA i limitom Native Messaging.

## Mutacja, test i promocja

Każda automatycznie promowana mutacja musi być świeżą sesją Bridge z `sequence = 1`. `automation.iteration` jest licznikiem pętli przeglądarkowej i nie jest sekwencją Bridge.

Szkielet akcji:

```json
{
  "schema": "bdb-action-v1",
  "repo_alias": "calculator",
  "operation": "multi_file_patch",
  "expected_revision": 0,
  "payload": {
    "profile_id": "poc_pytest",
    "patch": {
      "schema": "bdb-multi-file-patch-v1",
      "operations": []
    }
  },
  "promotion": {
    "mode": "required"
  },
  "automation": {
    "mode": "auto",
    "loop_id": "calculator-history-20260717",
    "iteration": 3,
    "continue_on_failure": true
  },
  "presentation": {
    "mode": "compact"
  }
}
```

Pełna akcja zawiera kanoniczne replacement/create/delete operations z exact hashami i treścią Base64.

Po sukcesie rozszerzenie nie kontynuuje tylko na podstawie zielonego profilu. Czeka, aż kontekst zwróci receipt z tym samym `command_id` i czysty checkout. Brak receiptu kończy AUTO jako `needs_user`.

## Pętla naprawcza

`continue_on_failure = true` nie ignoruje dowolnych błędów. Kontynuacja następuje wyłącznie, gdy:

- Native Host zwrócił zakończony wynik;
- status profilu to `failed` lub `timeout`;
- operacją był `multi_file_patch`;
- `rollback_performed = true`;
- `checkpoint_state = "rolled_back"`.

Wtedy wynik testów i logi trafiają automatycznie do kolejnej tury, która może przygotować nową, świeżą sesję i poprawkę. Poniższe stany zawsze zatrzymują pętlę:

- `policy_denied`;
- `manual_reconciliation_required`;
- kolizja lub niezgodny stan;
- brak pełnego rollbacku;
- przekroczenie czasu lub liczby iteracji;
- brak potwierdzonej promocji.

## Reguły promotera

Promoter akceptuje tylko trwały wynik spełniający wszystkie warunki:

- `status = success`;
- `exit_code = 0`;
- `sequence = 1`;
- `operation = multi_file_patch`;
- checkpoint `committed`;
- brak rollbacku;
- wszystkie zmienione ścieżki należą do allowlisty.

Następnie:

1. sprawdza kanoniczną ścieżkę wyniku;
2. sprawdza dokładnie jeden zarejestrowany detached worktree;
3. porównuje zestaw zmian z durable result;
4. wykonuje `git diff --check`;
5. stage'uje wyłącznie zatwierdzone ścieżki;
6. tworzy pojedynczy commit z jednym rodzicem;
7. wymaga czystego, przypiętego source checkoutu na tym rodzicu;
8. wykonuje wyłącznie `git merge --ff-only <commit>`;
9. ponownie odczytuje HEAD, status i hashe plików;
10. zapisuje atomowy receipt.

Watcher jest idempotentny. Stare wyniki są oznaczane jako `ignored_existing`, a każdy nowy wynik jest podejmowany najwyżej raz. Zablokowany wynik pozostaje do inspekcji zamiast być automatycznie ponawiany.

## Codzienne doświadczenie użytkownika

Po jednorazowym `Prepare`, uruchomieniu operatora i lokalnym włączeniu AUTO użytkownik pisze tylko:

> W projekcie calculator dodaj historię ostatnich 20 działań i przetestuj całość.

Widoczny przebieg ma postać kart:

```text
Odczytywanie lokalnego kontekstu…
Analizowanie plików…
Uruchamianie testów…
Wycofano nieudaną próbę, analizowanie błędu…
Uruchamianie poprawionej wersji…
Testy zakończone powodzeniem…
Lokalny checkout zaktualizowany przez fast-forward.
```

## Granice

- ChatGPT nie dostaje nieograniczonego dostępu do dysku;
- kontekst i zapis są ograniczone aliasem oraz `allowed_paths`;
- brak dowolnego shell/PowerShell z rozmowy;
- profil wykonawczy pozostaje allowlistowany po stronie Bridge;
- główny checkout nigdy nie jest edytowany przed zielonym profilem;
- automatyczna promocja nie obsługuje wielosekwencyjnej sesji;
- brudny checkout, rozbieżny HEAD lub niezgodny zestaw plików wymaga człowieka;
- rozszerzenie działa wyłącznie na `https://chatgpt.com/*` i przez konkretny Native Host.
