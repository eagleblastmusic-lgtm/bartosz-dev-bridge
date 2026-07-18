# ADR-0006: PySide6 + Qt Widgets dla MVP BDB Control Center

- Status: **Accepted**
- Data: **2026-07-18**
- Etap: **P05**

## Kontekst

BDB Core i Operator API są napisane w Pythonie. Control Center ma być cienkim klientem lokalnym, który po uruchomieniu wykonuje tylko odczyty, a jawne mutacje deleguje do `bdb_operator`.

Rozważono trzy główne technologie:

- PySide6 + Qt Widgets;
- WPF + .NET;
- WinUI 3 + Windows App SDK.

WPF i WinUI 3 są silnymi frameworkami Windows, ale ich użycie wprowadziłoby drugi język i runtime. Aby zachować `bdb_operator` jako jedyną fasadę aplikacyjną, potrzebny byłby dodatkowy proces/IPC albo binding C#↔Python. Taki koszt nie jest uzasadniony w pierwszym MVP.

PySide6 jest oficjalnym bindingiem Qt dla Pythona. Pozwala użyć Operator API in-process i oferuje Qt Widgets, high-DPI, system tray oraz narzędzia wdrożeniowe.

## Decyzja

MVP BDB Control Center będzie budowany w:

```text
Python 3.11+ + PySide6 + Qt Widgets
```

Qt Quick/QML nie jest częścią pierwszego MVP. QML może zostać oceniony później dla wybranych widoków, ale nie może zmienić granicy `bdb_gui -> bdb_operator`.

PySide6 jest zależnością opcjonalną. Bazowa instalacja BDB Core i Operator API nie instaluje Qt.

## Uzasadnienie

- bezpośrednie, typowane wywołania istniejącego `OperatorApi`;
- jeden model błędów i DTO;
- brak lokalnego serwera oraz IPC w MVP;
- krótsza ścieżka do działającego dashboardu;
- Qt Widgets wystarcza dla panelu operatorskiego, historii, tabel, diagnostyki i traya;
- łatwiejsze testy headless przez platformę `offscreen`;
- możliwość późniejszego wydzielenia transportu bez przepisywania domeny.

## Konsekwencje

Pozytywne:

- najmniejszy zakres implementacyjny P06–P10;
- brak duplikowania Operator API w C#;
- jedna instalacja diagnostyczna Python;
- szybkie testy widoków i view-modeli;
- dostępne API system tray i high-DPI.

Koszty:

- artefakt dystrybucyjny będzie zawierał biblioteki Qt;
- wygląd wymaga świadomego stylowania, aby pasował do Windows 11;
- trzeba utrzymać responsywny UI przy długich operacjach przez worker threads/signals;
- obowiązują wymagania licencyjne Qt/PySide6;
- część zaawansowanych integracji Windows może wymagać adapterów Win32.

## Niezmienniki

- `bdb_gui` nie importuje prywatnych modułów `bdb_bridge`;
- GUI korzysta wyłącznie z publicznego `bdb_operator`;
- brak ukrytego Start, re-arm lub naprawy stanu przy otwarciu;
- długie wywołania nie blokują głównego wątku Qt;
- żadna mutacja nie jest wykonywana przez konstruktor okna ani timer odświeżania;
- PySide6 nie jest bazową zależnością `bartosz-dev-bridge`;
- instalator i dystrybucja wymagają osobnej kontroli licencyjnej przed P13.

## Techniczna linia bazowa

- pierwsze widoki: Qt Widgets;
- komunikacja: in-process `OperatorApi`;
- odświeżanie: bounded polling uruchamiany przez GUI, tylko dla operacji read-only;
- mutacje: jawne akcje użytkownika wykonywane poza głównym wątkiem;
- tray: `QSystemTrayIcon` dopiero w P12;
- pakowanie: decyzja odroczona do P13 po pomiarze `pyside6-deploy` i alternatyw.

## Plan rezygnacji

WPF staje się preferowanym planem zastępczym, gdy Qt nie przejdzie bramki dostępności, licencji, dystrybucji lub stabilności. Migracja musi zachować Operator API i może dodać wyłącznie lokalny, wersjonowany adapter IPC.

WinUI 3 może zostać ponownie oceniony dla przyszłej natywnej powłoki Bartosz OS.

## Odrzucone alternatywy

### WPF jako pierwsze GUI

Odrzucone dla MVP z powodu konieczności utrzymywania C# i Python oraz nowej granicy procesowej. Nie odrzucono go jako planu awaryjnego.

### WinUI 3 jako pierwsze GUI

Odrzucone dla MVP ze względu na największy koszt toolchainu, Windows App SDK i wdrożenia przy braku bezpośredniej integracji z Pythonem.

### Qt Quick/QML od początku

Odrzucone jako niepotrzebne zwiększenie złożoności dla panelu operatorskiego. Qt Widgets wystarcza do walidacji produktu i architektury.

### GUI webowe na localhost

Odrzucone zgodnie z ADR-0002: tworzyłoby listener, politykę origin i dodatkową powierzchnię bezpieczeństwa.
