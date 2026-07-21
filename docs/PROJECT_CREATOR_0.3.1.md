# BDB Project Creator — Control Center 0.3.1 / extension 0.3.0

## Cel

Przycisk **Kreator projektu** w Control Center uruchamia jeden potwierdzony przebieg dla nowego albo istniejącego projektu:

```text
wybór trybu → repozytorium Git → GitHub → Prepare → Start → Re-arm → aktywna rozmowa ChatGPT
```

Po uruchomieniu prompt prowadzi tę samą rozmowę do normalnej pętli BDB: kontekst, analiza, dozwolone edycje, test, poprawka, promocja i końcowe potwierdzenie czystego źródła.

## Nowy projekt

Kreator:

1. tworzy pusty katalog projektu;
2. zapisuje `README.md` i `.gitignore`;
3. inicjalizuje branch `main` i lokalny commit;
4. sprawdza lokalne `gh auth status`;
5. tworzy prywatne albo publiczne repo przez `gh repo create`;
6. dodaje `origin` i wysyła wyłącznie commit inicjalizacyjny;
7. przygotowuje workspace BDB przez istniejący `ProjectPrepareService`;
8. uruchamia Bridge i uzbraja Native Host;
9. kolejkuje prompt dla aktualnie otwartej rozmowy ChatGPT — bez otwierania nowej karty lub okna.

GitHub CLI działa bez powłoki (`shell=False`). Kreator nie wykonuje merge, deployu ani późniejszych pushy kodu wygenerowanego przez BDB bez osobnego zakresu operacji.

## Istniejący projekt

Źródłem może być:

- czysty lokalny checkout Git przypięty do brancha;
- URL `https://github.com/owner/repo[.git]`;
- adres SSH `git@github.com:owner/repo[.git]`.

Dla URL repozytorium jest klonowane do wskazanego katalogu projektów. Dla lokalnego checkoutu Kreator nie kopiuje ani nie przebudowuje repo.

## Przekazanie prompta do bieżącej rozmowy

Control Center zapisuje jeden ograniczony prompt do lokalnej kolejki obok konfiguracji Native Host. Rozszerzenie 0.3.0:

1. nie otwiera nowej karty ani nowego okna ChatGPT;
2. odczytuje oczekujący prompt przez Native Messaging;
3. dopuszcza claim wyłącznie w widocznej, aktywnej i posiadającej fokus rozmowie `/c/...`;
4. nie dotyka zajętego edytora;
5. wstawia prompt tylko do pustego edytora;
6. opcjonalnie wysyła go z tym samym potwierdzonym mechanizmem co AUTO;
7. zapisuje lokalne powiązanie `conversation_id ↔ repo_alias ↔ launch_id`, a po uruchomieniu operacji również `session_id` i `command_id`;
8. usuwa kolejkę dopiero po potwierdzonej obsłudze.

45-sekundowa dzierżawa zapobiega jednoczesnemu wstawieniu tego samego prompta do kilku kart. Po awarii wygasa, ale launch nadal może przejąć wyłącznie aktualnie aktywna rozmowa.

## Jeden kontrakt ścieżek

Plan Kreatora zapisuje dokładną efektywną allowlistę. Ta sama lista trafia do Native Host Context, manifestu Bridge, Workspace Managera i Promotera. Prompt startowy zawiera tę listę i zabrania generowania operacji poza nią.

Domyślna allowlista pozostaje ograniczona do typowych plików projektu oraz bezpiecznych skryptów uruchomieniowych w katalogu głównym:

```text
README.md
.gitignore
src/**
tests/**
app/**
public/**
package.json
package-lock.json
pyproject.toml
requirements*.txt
*.sln
*.csproj
*.cmd
*.bat
*.ps1
```

Jawnie przekazana allowlista użytkownika nie jest automatycznie rozszerzana.

## Preflight przed Native Hostem

Każda mutująca akcja jest sprawdzana przed utworzeniem komendy:

- poprawność bezpiecznych ścieżek POSIX;
- zgodność wszystkich ścieżek z lokalnym `allowed_paths`;
- kanoniczny Base64;
- rzeczywisty SHA-256 każdej przekazanej treści;
- limity rozmiaru istniejącego protokołu.

Błąd zawiera konkretną operację i ścieżkę, np.:

```text
policy_denied: Path is not allowed by local policy: START-MP4-PLAYER.cmd
```

## Naprawa i ponowne uruchomienie

Pierwsza mutacja otrzymuje jawne `bdb-repair-correlation-v1` z rolą `initial`. Rozszerzenie przechowuje ograniczony stan ostatnich akcji.

Przycisk **Napraw i uruchom ponownie**:

- dla rozbieżności `content_sha256` przelicza wyłącznie metadane integralności i ponownie uruchamia preflight;
- gdy błąd został wykryty przed Native Hostem, używa nadal niezwiązanej sesji początkowej;
- po terminalnym wyniku tworzy nowy `session_id` z rolą `repair` i dokładnym `predecessor_session_id`;
- dla błędów wymagających zmiany kodu lub zakresu wysyła pełną diagnozę do tej samej rozmowy, a następną poprawioną akcję automatycznie wiąże z poprzednikiem;
- nigdy nie używa ponownie terminalnie zakończonej sesji.

## Dokładne błędy

Terminalny błąd przed mutacją jest zapisywany atomowo w Journalu jako wersjonowane zdarzenie. Result, projekcja Session History i widoczna kolumna wyniku zachowują jego dokładny kod i bounded detail zamiast ogólnego `policy_denied`.

## Bezpieczeństwo

- wymagane jest jedno jawne potwierdzenie całego planu w oknie Kreatora;
- alias, nazwa repo, URL, prompt, limity i allowlista są walidowane przed mutacją;
- istniejący workspace lub katalog nowego projektu nie jest nadpisywany;
- brudny albo detached checkout jest odrzucany przez istniejący preflight;
- polecenia są ograniczone do katalogu `git`/`gh`; nie ma dowolnej powłoki;
- przy błędzie artefakty pozostają do inspekcji; nie ma automatycznego cleanupu;
- start prompt nie udziela zgody na push, merge ani deploy kodu zadania;
- rozszerzenie nie rozszerza po cichu allowlisty istniejącego workspace.

## Wymagania operatorskie

- Git w `PATH`;
- dla nowego repo: GitHub CLI `gh` w `PATH` i aktywne logowanie;
- aktualny Control Center 0.3.1;
- rozszerzenie 0.3.0;
- windowless Native Host z bieżącego źródła;
- otwarta, widoczna i aktywna rozmowa ChatGPT z pustym edytorem.
