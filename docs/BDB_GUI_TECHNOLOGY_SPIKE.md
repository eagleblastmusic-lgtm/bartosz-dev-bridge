# BDB Control Center — spike technologii GUI

Status: **P05 / decision complete**  
Data oceny: **2026-07-18**

## Cel

Wybrać technologię dla pierwszego lokalnego GUI BDB bez zmiany zamrożonych granic:

```text
bdb_gui -> bdb_operator -> bdb_bridge
```

P05 nie buduje jeszcze produkcyjnego GUI. Dostarcza porównanie, decyzję, opcjonalny proof-of-concept i bramkę CI dla wybranego stosu.

## Kandydaci

### PySide6 + Qt Widgets

- oficjalne bindingi Qt 6 dla Pythona;
- może korzystać z `bdb_operator.OperatorApi` in-process;
- Qt Widgets zapewnia klasyczny model aplikacji desktopowej;
- Qt 6 ma automatyczną obsługę high-DPI;
- `QSystemTrayIcon` zapewnia system tray;
- dostępne jest oficjalne narzędzie `pyside6-deploy`;
- dystrybucja wymaga świadomego spełnienia LGPLv3/GPLv3 albo użycia licencji komercyjnej.

### WPF + .NET

- dojrzały, Windows-only framework XAML;
- rozbudowane bindingi, style, dostępność i narzędzia .NET;
- wspiera nowoczesny Fluent theme;
- wymagałby osobnej warstwy C# oraz granicy procesowej/IPC do Pythonowego Operator API albo przepisywania fasady;
- publikowanie .NET jest dojrzałe, ale wprowadza drugi runtime i drugi toolchain.

### WinUI 3 + Windows App SDK

- rekomendowany przez Microsoft stos dla nowych natywnych aplikacji Windows;
- nowoczesny Fluent UI, XAML, wysokie DPI i integracja z Windows App SDK;
- oficjalnie wspiera C# i C++;
- Windows App SDK ma osobny cykl wersji i kilka modeli wdrożenia;
- wymagałby granicy C#↔Python oraz dodatkowego bootstrapu/pakowania runtime'u;
- największa zgodność wizualna z Windows 11, ale najwyższy koszt pierwszego MVP BDB.

## Kryteria i wagi

Skala ocen: `1` — słabo, `5` — bardzo dobrze.

| Kryterium | Waga | PySide6 | WPF | WinUI 3 |
|---|---:|---:|---:|---:|
| bezpośrednia integracja z Pythonowym BDB | 30 | 5 | 2 | 2 |
| utrzymanie jednej granicy bezpieczeństwa | 20 | 5 | 3 | 3 |
| natywność i jakość UX Windows | 15 | 3 | 4 | 5 |
| wdrożenie i diagnostyka MVP | 15 | 4 | 5 | 3 |
| zgodność z aktualnym toolchainem projektu | 10 | 5 | 2 | 2 |
| możliwość późniejszej wymiany powłoki | 10 | 4 | 4 | 4 |
| **wynik ważony / 5** | **100** | **4,45** | **3,15** | **3,00** |

## Decyzja

Dla MVP wybieramy:

```text
PySide6 + Qt Widgets
```

Powody rozstrzygające:

1. brak drugiego backendu i brak IPC w pierwszej wersji;
2. bezpośrednie użycie istniejących, testowanych DTO i metod `bdb_operator`;
3. możliwość uruchamiania operacji asynchronicznie bez blokowania UI;
4. dostępność high-DPI i system tray wymaganych przez plan Control Center;
5. najmniejszy zakres zmian przed P06–P10.

## Ograniczenia decyzji

- PySide6 nie trafia do bazowych `dependencies` BDB;
- P05 dodaje wyłącznie opcjonalny extra `gui-spike`;
- P06 utworzy właściwy pakiet `bdb_gui` dopiero po akceptacji tego spike'u;
- MVP używa Qt Widgets, nie QML;
- GUI nie może importować prywatnych modułów `bdb_bridge` ani czytać Journalu bezpośrednio;
- otwarcie okna pozostaje tylko-do-odczytu;
- operacje `Start`, `Stop` i `rearm` będą jawne i delegowane do `bdb_operator`;
- przed P13 należy wykonać oddzielną kontrolę licencji, notices, bundlingu bibliotek Qt i mechanizmu aktualizacji.

## Plan awaryjny

WPF pozostaje kandydatem rezerwowym, gdy wystąpi co najmniej jeden warunek:

- wymagania dostępności Windows nie mogą zostać spełnione przez Qt;
- polityka licencyjna lub dystrybucyjna wykluczy Qt;
- integracja z Windows Shell będzie wymagała nieproporcjonalnej liczby adapterów natywnych;
- pomiary P06–P10 pokażą nieakceptowalny startup, zużycie pamięci albo stabilność.

Przejście na WPF wymaga nowego ADR oraz zachowania `bdb_operator` jako jedynej fasady aplikacyjnej. WinUI 3 pozostaje opcją dla przyszłej, bardziej natywnej powłoki, nie dla pierwszego MVP.

## Proof-of-concept

`spikes/gui/pyside6_probe.py` sprawdza:

- utworzenie aplikacji i okna Qt Widgets;
- bezpośredni odczyt `OperatorApi.capabilities()`;
- brak listenera sieciowego i brak wywołania mutacji;
- dostępność API system tray;
- raport wersji Qt/PySide/Python i współczynnika DPI;
- zamknięcie event loop w trybie headless.

Probe nie zna ścieżki workspace, nie uruchamia Bridge'a i nie uzbraja Native Hosta.

## Źródła oficjalne przejrzane w P05

- Qt for Python — dokumentacja projektu, pakietów, deploymentu, high-DPI i `QSystemTrayIcon`;
- Microsoft Learn — WPF overview i aktualne zmiany WPF;
- Microsoft Learn — WinUI 3, Windows App SDK i modele wdrożenia.

## Bramka wyjścia P05

- ADR wyboru technologii jest zaakceptowany;
- PySide6 pozostaje zależnością opcjonalną;
- probe przechodzi na Windows CI w trybie offscreen;
- bazowa macierz BDB pozostaje zielona bez instalowania Qt;
- probe nie zawiera `Start`, `Stop`, `rearm`, shella ani listenera;
- nie utworzono jeszcze produkcyjnego pakietu `bdb_gui`.
