# ADR-0016: Plan-only GicleeApp integration

Status: Accepted  
Data: 2026-07-19

## Kontekst

Dev Bridge ma docelowo obsługiwać GicleeApp, którego kanonicznym repozytorium jest `eagleblastmusic-lgtm/gicleeart`. Integracja nie może jednak automatycznie modyfikować aplikacji, rozszerzać allowlistu na dane sklepu ani mylić descriptoru z wykonanym Prepare lub deployem.

## Decyzja

P15 wprowadza descriptor i read-only builder planu:

- repository full name jest jawny, a `master` pozostaje default branch hint;
- identyfikacja origin musi zostać ponownie potwierdzona przed Prepare;
- builder odczytuje wyłącznie lokalny `.git/HEAD` i wymaga attached branch;
- domyślny allowlist obejmuje kod i zasoby motywu, ale nie obejmuje `config/settings_data.json`, `.env` ani wzorców sekretów;
- wynik ma `mutation_operations_invoked=0` i wymaga osobnego potwierdzenia;
- builder nie importuje Operator API, nie uruchamia Git i nie zapisuje plików;
- P15 nie zmienia repozytorium GicleeApp.

## Konsekwencje

### Pozytywne

- GicleeApp otrzymuje powtarzalne, bezpieczne domyślne parametry Prepare;
- dane stanu sklepu nie trafiają do szerokiego scope przez `config/**`;
- lokalne worktree są obsługiwane bez subprocess;
- granica odpowiedzialności DevMaster/GicleeApp pozostaje jawna.

### Ograniczenia

- descriptor nie weryfikuje origin remote;
- nie wybiera ani nie zmienia brancha;
- nie uruchamia testów Shopify;
- nie wykonuje Prepare, Start, merge ani deploy;
- zmiany architektury lub kodu GicleeApp pozostają poza tym repozytorium.

## Alternatywy odrzucone

### Automatyczny Prepare po znalezieniu checkoutu

Odrzucony, ponieważ wykrycie katalogu nie jest zgodą na utworzenie workspace i zmianę Native Host config.

### Allowlist `config/**`

Odrzucony, ponieważ obejmowałby `settings_data.json`, czyli stan sklepu, którego nie trzeba domyślnie modyfikować.

### Bezpośrednie zmiany w repozytorium GicleeApp z P15

Odrzucone jako rozszerzenie zakresu na inny moduł i repozytorium bez osobnego planu oraz bramek.

## Bramka dalszej integracji

Rzeczywiste Prepare lokalnego checkoutu, zmiany w `gicleeart`, testy motywu, PR, merge i deploy wymagają osobnych etapów oraz aktualnego preflightu repozytorium.
