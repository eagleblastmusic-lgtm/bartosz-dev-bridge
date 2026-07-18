# BDB Control Center — zamrożone granice architektury

Status: **P02 / accepted**  
Data: **2026-07-18**

## Cel

BDB Control Center ma być cienkim, lokalnym panelem operatorskim dla istniejącego Bartosz Dev Bridge. Nie zastępuje Bridge'a, Native Hosta, rozszerzenia Chrome ani trwałego Journalu. Nie jest również pierwszą wersją Bartosz OS.

Docelowy przepływ:

```text
ChatGPT + rozszerzenie Chrome + Native Host
                    |
                    v
                 BDB Core
                    |
                    v
            BDB Operator API
                    |
                    v
          BDB Control Center GUI
                    |
                    v
     przyszły adapter modułu Bartosz OS
```

## Warstwy i odpowiedzialności

### 1. BDB Core

Istniejący kod wykonawczy pozostaje jedynym właścicielem:

- Journalu i trwałych stanów komend;
- kolejkowania oraz pojedynczego aktywnego workera;
- izolowanych worktree;
- walidacji patchy i hashy;
- uruchamiania allowlistowanych profili;
- checkpointów, rollbacku i recovery;
- publikacji wyników;
- promocji `git merge --ff-only` i receipts;
- lifecycle Bridge'a oraz locka systemowego.

GUI i Operator API nie mogą implementować drugiej wersji żadnej z tych reguł.

### 2. BDB Operator API

Operator API jest lokalną fasadą aplikacyjną nad BDB Core. Odpowiada za:

- spójne odczyty statusu Bridge'a, Native Hosta, promotera i projektów;
- wywołanie istniejących operacji `Prepare`, `Start`, `Status`, `Stop`;
- udostępnienie zagregowanego dziennika i bieżącej operacji;
- eksport diagnostyczny;
- publikowanie zdarzeń w kontrakcie `bdb-event-v1`;
- mapowanie błędów technicznych na stabilne kody operatorskie.

Operator API nie może:

- wykonywać arbitralnego shella;
- edytować repozytorium poza BDB Core;
- omijać allowlist, profilu, checkpointu, rollbacku lub promocji;
- usuwać Journalu, worktree, receipts ani logów;
- wykonywać merge, push lub deploy poza istniejącym, jawnie dozwolonym kontraktem BDB;
- samodzielnie uzbrajać Native Hosta przy samym odczycie statusu.

### 3. BDB Control Center GUI

GUI jest klientem Operator API. Odpowiada za prezentację i świadome działania użytkownika.

Pierwszy MVP obejmuje:

- dashboard stanu;
- jawne `Start`, `Stop`, `Status` i ponowne uzbrojenie;
- listę skonfigurowanych projektów;
- bieżącą operację;
- historię i Journal w trybie odczytu;
- diagnostykę oraz eksport;
- informacje o wersjach.

GUI:

- uruchamia się w trybie tylko do odczytu;
- nie wykonuje ukrytego `Start` ani re-arm;
- nie naprawia automatycznie brudnego repozytorium;
- nie zabija procesów;
- nie oferuje arbitralnego terminala;
- nie dodaje automatycznego merge ani deployu;
- nie modyfikuje konfiguracji projektu bez osobnego, jawnego przepływu.

Zamknięcie okna może schować aplikację do traya. Pełne wyjście musi zapytać, czy pozostawić BDB uruchomiony, czy wykonać bezpieczny `Stop`.

### 4. Adapter Bartosz OS

Adapter jest przyszłą, wymienną warstwą integracyjną. Nie może być zależnością BDB Core.

Kierunek zależności:

```text
bdb_bartosz_os -> bdb_operator -> bdb_bridge
```

Niedozwolone:

```text
bdb_bridge -> bdb_operator
bdb_bridge -> bdb_gui
bdb_bridge -> bdb_bartosz_os
```

Przyszły manifest modułu będzie miał identyfikator schematu:

```text
bartosz-os-module-manifest-v1
```

P02 nie definiuje jeszcze pełnego manifestu ani runtime Bartosz OS.

## Zamrożone granice procesu i transportu

- Wszystkie komponenty MVP działają lokalnie jako zwykłe procesy użytkownika Windows.
- P03 nie może wprowadzić publicznego HTTP, WebSocket, chmury ani zdalnego sterowania.
- Transport Operator API ma być lokalny i wymienny za interfejsem aplikacyjnym.
- Dobór konkretnego transportu nastąpi w P03 po spike'u, bez zmiany kontraktu domenowego.
- Brak uprawnień administratora jest założeniem podstawowym.

## Źródła prawdy

| Informacja | Źródło prawdy |
|---|---|
| stan komendy i recovery | Journal BDB |
| stan procesu Bridge | lifecycle/lock/heartbeat BDB Core |
| stan Native Hosta | istniejący kontrakt Native Host |
| wynik testu i patcha | trwały result/checkpoint |
| stan promocji | promotion receipt |
| status repozytorium | odczyt Git wykonywany przez istniejącą warstwę operatorską |
| stan widoku GUI | cache odtwarzalny z Operator API, nigdy źródło prawdy |

## Reguła bezpieczeństwa wywołań

Każde działanie zmieniające stan musi spełniać wszystkie warunki:

1. użytkownik wykonał jawne działanie w GUI albo zatwierdzonym kliencie;
2. Operator API zweryfikowało stan wejściowy;
3. operacja jest dostępna w zamkniętym katalogu komend;
4. BDB Core pozostaje właścicielem walidacji i skutków;
5. wynik jest zwracany z trwałą tożsamością i kodem błędu;
6. ponowienie nie może powodować podwójnego skutku.

## Poza zakresem MVP

- pełny Bartosz OS;
- integracja GicleeApp;
- sterowanie z telefonu lub przez Internet;
- konta użytkowników i serwer wieloużytkownikowy;
- cloud sync;
- automatyczny updater bez osobnej bramki bezpieczeństwa;
- arbitrary shell;
- edytor kodu;
- wieloprojektowe mutacje równoległe;
- `Start All`;
- automatyczny merge, push lub deploy;
- automatyczne czyszczenie lub naprawa repozytoriów.

## Struktura docelowa repozytorium

P02 rezerwuje następujące granice pakietów, ale ich jeszcze nie implementuje:

```text
bdb_bridge/       # istniejący rdzeń
bdb_operator/     # P03+
bdb_gui/          # P05+
bdb_bartosz_os/   # P14+
schemas/          # kontrakty wersjonowane
docs/adr/         # decyzje architektoniczne
```

## Bramka wyjścia z P02

P02 jest zakończone, gdy:

- ADR-y są zapisane i spójne z tym dokumentem;
- test kontraktowy pilnuje zakazanych zależności oraz kluczowych identyfikatorów;
- nie dodano runtime'u Operator API ani GUI;
- CI jest zielone;
- kolejny etap P03 może rozpocząć się bez ponownego negocjowania granic odpowiedzialności.
