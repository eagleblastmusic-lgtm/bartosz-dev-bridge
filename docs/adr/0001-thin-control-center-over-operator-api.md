# ADR-0001: Cienki Control Center nad lokalnym Operator API

- Status: **Accepted**
- Data: **2026-07-18**
- Etap: **P02**

## Kontekst

BDB ma działający rdzeń wykonawczy, Native Hosta, rozszerzenie Chrome, operator Windows i trwałe mechanizmy recovery. Kolejnym celem jest aplikacja Windows upraszczająca codzienną obsługę bez przepisywania sprawdzonej logiki.

Bez jawnej granicy GUI mogłoby zacząć samodzielnie interpretować Journal, uruchamiać procesy, wykonywać Git albo odtwarzać reguły recovery. Powstałyby dwa źródła prawdy i dwa różne modele bezpieczeństwa.

## Decyzja

Wprowadzamy architekturę:

```text
BDB Control Center GUI -> BDB Operator API -> BDB Core
```

BDB Core pozostaje jedynym właścicielem wykonania, trwałego stanu i polityk bezpieczeństwa. Operator API jest lokalną fasadą aplikacyjną. GUI jest cienkim klientem prezentacyjnym.

Kierunek zależności jest jednostronny:

```text
bdb_gui -> bdb_operator -> bdb_bridge
```

`bdb_bridge` nie może importować `bdb_operator` ani `bdb_gui`.

## Konsekwencje

Pozytywne:

- jedna implementacja reguł bezpieczeństwa;
- GUI można wymienić bez zmiany rdzenia;
- Operator API można testować bez interfejsu graficznego;
- przyszły adapter Bartosz OS użyje tej samej fasady;
- awaria GUI nie zmienia trwałego stanu Bridge'a.

Koszty:

- potrzebny jest stabilny model DTO i kodów błędów;
- część informacji musi być agregowana z kilku istniejących źródeł;
- GUI nie może korzystać ze skrótów bezpośrednio do plików stanu.

## Niezmienniki

- Journal, receipts i lifecycle pozostają własnością BDB Core.
- GUI nie wykonuje Git ani patchy bezpośrednio.
- Operator API nie replikuje executorów, recovery ani promocji.
- Każda mutacja przechodzi przez istniejący kontrakt BDB Core.

## Odrzucone alternatywy

### GUI bezpośrednio czyta i modyfikuje pliki stanu

Odrzucone z powodu ryzyka race condition, rozjazdu schematów i obejścia recovery.

### Przeniesienie logiki BDB do aplikacji desktopowej

Odrzucone, ponieważ unieważniłoby sprawdzony rdzeń i zwiększyło zakres regresji.

### Osobny backend sieciowy od pierwszego MVP

Odrzucone. MVP jest lokalne i nie wymaga powierzchni sieciowej.
