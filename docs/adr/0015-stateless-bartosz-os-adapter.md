# ADR-0015: Stateless Bartosz OS adapter over Operator API

Status: Accepted  
Data: 2026-07-19

## Kontekst

Docelowy Bartosz OS potrzebuje sposobu odkrywania i wywoływania Dev Bridge. Integracja nie może jednak przenieść odpowiedzialności z DevMastera, utworzyć drugiego backendu wykonawczego ani przedstawiać planowanego Core jako wdrożonego źródła stanu.

## Decyzja

Wprowadzamy manifest `bartosz-os-module-manifest-v1` oraz bezstanowy adapter in-process:

- adapter deleguje wyłącznie do publicznego Operator API;
- katalog operacji jest zamknięty;
- adapter domyślnie blokuje wszystkie mutacje;
- mutacja wymaga zarówno włączenia instancji adaptera, jak i autoryzacji konkretnego żądania;
- adapter nie nasłuchuje w sieci i nie zapisuje stanu;
- odpowiedź zachowuje pełną, wersjonowaną odpowiedź Operator API;
- manifest jawnie wskazuje DevMaster jako właściciela modułu i GitHub jako źródło kodu;
- manifest jawnie stwierdza, że Bartosz OS Core nie jest źródłem stanu operacyjnego.

## Konsekwencje

### Pozytywne

- przyszły Core otrzymuje stabilny kontrakt bez przejęcia wykonania;
- każda mutacja ma dwie bramki i nie może stać się domyślna;
- nie powstaje drugi model statusu, Journalu ani procesów;
- adapter można testować bez uruchamiania listenera lub usług sieciowych.

### Ograniczenia

- P14 nie wdraża Bartosz OS Core ani Event Bus;
- brak IPC i zdalnego transportu;
- brak trwałego rejestru modułów;
- włączenie mutacji jest decyzją procesu nadrzędnego i nadal nie zastępuje zgody użytkownika;
- adapter obsługuje tylko aktualny katalog Operator API.

## Alternatywy odrzucone

### Bezpośredni dostęp Core do BDB Core lub Journalu

Odrzucony jako naruszenie granicy Operator API i duplikacja odpowiedzialności.

### Listener HTTP w Dev Bridge

Odrzucony jako nowa powierzchnia sieciowa i przedwczesny wybór transportu.

### Mutacje włączone domyślnie

Odrzucone jako niezgodne z zasadą minimalnych uprawnień i jawnymi bramkami.

## Bramka kolejnego etapu

Transport IPC, Event Bus, rejestr modułów, zdalne wywołania lub zmiana właściciela odpowiedzialności wymagają osobnego ADR, modelu uwierzytelniania i bezpośredniej weryfikacji wdrożenia.
