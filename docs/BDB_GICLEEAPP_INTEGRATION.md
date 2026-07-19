# Bartosz Dev Bridge — P15 integracja GicleeApp

Status: IMPLEMENTED ON BRANCH

## Zweryfikowany kontekst

Kanoniczne repozytorium aplikacji to `eagleblastmusic-lgtm/gicleeart`. W czasie przygotowania P15 repozytorium miało default branch `master`, a plik `layout/theme.liquid` potwierdzał strukturę motywu Shopify.

Default branch w descriptorze jest wskazówką, nie zastępuje sprawdzenia bieżącego lokalnego checkoutu przed Prepare.

## Zakres P15

P15 dodaje wyłącznie po stronie Dev Bridge:

- wersjonowany descriptor `bdb-gicleeapp-integration-v1`;
- read-only builder `bdb-gicleeapp-prepare-plan-v1`;
- bezpieczny domyślny allowlist zakresu motywu;
- wykrycie attached local branch bez uruchamiania Git;
- dokładny zestaw parametrów zgodny z publicznym kontraktem Prepare.

P15 nie zmienia żadnego pliku w repozytorium `gicleeart`, nie tworzy workspace, nie uruchamia Prepare, Start, merge ani deploy.

## Domyślny zakres

Allowlist obejmuje typowe powierzchnie motywu:

- assets;
- blocks;
- layout;
- locales;
- sections;
- snippets;
- templates;
- tests i scripts;
- README;
- wyłącznie `config/settings_schema.json` z katalogu config.

Celowo nie używa `config/**`. `config/settings_data.json`, `.env`, klucze, certyfikaty i wzorce sekretów pozostają poza domyślnym zakresem.

## Builder planu

Builder przyjmuje istniejący lokalny checkout, katalog workspace'ów, interpreter Pythona i timeout testów. Następnie:

1. potwierdza obecność `.git` jako katalogu albo pliku worktree;
2. odczytuje `HEAD` i wymaga attached local branch;
3. wylicza workspace `<workspaces_root>/gicleeart`;
4. odrzuca istniejący target i niebezpieczne zagnieżdżenie;
5. waliduje interpreter i timeout;
6. zwraca wyłącznie niemutujący plan.

Plan ma `repository_identity_verification=external_required`, ponieważ odczyt lokalnego `.git/HEAD` nie dowodzi jeszcze, że origin wskazuje kanoniczne repozytorium. Właściwy preflight i ewentualny Prepare pozostają osobnym, potwierdzonym krokiem Operator API.

## Granice odpowiedzialności

- DevMaster jest właścicielem descriptoru integracyjnego;
- GicleeApp pozostaje właścicielem repozytorium aplikacji;
- GitHub pozostaje źródłem prawdy dla kodu;
- descriptor nie jest drugim źródłem architektury ani stanu GicleeApp;
- zmiany w `gicleeart` wymagają osobnego zadania, brancha, testów i zgody na merge/deploy.
