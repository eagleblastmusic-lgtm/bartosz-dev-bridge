# BDB Control Center 0.2.1 — stabilizacja po odbiorze 0.2.0

Data: 2026-07-19

Status: IMPLEMENTED ON BRANCH; CI, release artifact and local packaged smoke pending

## Punkt wyjścia

Przenośny pakiet Control Center 0.2.0 został zbudowany z commitu:

```text
6d6da1d5d7b480ddc6bd5868e8b58e9496a1a959
```

Ręczny workflow wydaniowy zakończył się powodzeniem:

- workflow run: `29692305194`;
- artifact ID: `8443960321`;
- artifact: `bdb-control-center-windows-0.2.0`;
- produkt: `BDB-Control-Center-windows-x86_64-0.2.0.zip`;
- SHA-256 produktu: `45905667f427414853e9f0b320b42cd92bbc169fa7656e27e9815951d82cb4e4`.

Pakiet pozostał niepodpisany, bez instalatora i bez publikacji jako GitHub Release.

## Potwierdzony odbiór 0.2.0

Na Windows potwierdzono:

- headless smoke;
- normalne uruchomienie GUI;
- Start;
- Stop;
- Re-arm;
- wygaśnięcie uzbrojenia;
- Diagnostics;
- sanitizowany eksport ZIP;
- hide-to-tray i ponowne otwarcie;
- Exit + Stop;
- czysty source checkout po odbiorze.

Końcowy runtime po Exit + Stop: aplikacja zakończona, Bridge i Promoter zatrzymane.

## Zakres 0.2.1

0.2.1 jest minimalnym wydaniem stabilizacyjnym. Nie dodaje nowego mechanizmu wykonawczego.

Zmiany:

1. wynik ostatniej operacji `start`, `stop` albo `rearm` pozostaje widoczny po kontrolnym odczycie statusu;
2. historyczne `armed_until` nie jest przedstawiane jako aktywny termin, gdy `armed=false`;
3. wersja pakietu i manifestu modułu zostaje podniesiona do `0.2.1`;
4. oba problemy UX otrzymują testy regresyjne.

## Niezmienione granice

0.2.1 nie wykonuje i nie dodaje:

- automatycznego merge;
- deployu;
- publikacji GitHub Release;
- instalatora MSI/MSIX;
- podpisu Authenticode;
- automatycznej instalacji lub aktualizacji;
- arbitralnego shella;
- globalnych instalacji;
- dostępu do sekretów;
- zmian w repozytorium `gicleeart`.

## Wymagane bramki przed uznaniem wydania za zakończone

- testy regresyjne dashboardu PASS;
- pełny pytest PASS;
- wszystkie zwykłe workflow CI PASS;
- ręczny workflow `Control Center Release Artifact` PASS na dokładnym commicie 0.2.1;
- skrócony lokalny odbiór pobranego pakietu Windows PASS;
- czysty source checkout;
- zapis końcowego commitu, run ID, artifact ID i SHA-256 produktu.

Do czasu przejścia wszystkich bramek dokument opisuje implementację kandydata 0.2.1, a nie opublikowane lub wdrożone wydanie.
