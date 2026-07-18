# ADR-0003: Wersjonowane zdarzenia i jawne mutacje operatorskie

- Status: **Accepted**
- Data: **2026-07-18**
- Etap: **P02**

## Kontekst

Control Center musi przedstawiać bieżący stan procesu, projektów, operacji, testów i promocji. Polling wyłącznie surowych plików lub logów prowadziłby do zależności od wewnętrznych formatów. Z kolei automatyczne wykonywanie działań przy otwarciu GUI mogłoby zmienić stan bez świadomej decyzji użytkownika.

## Decyzja

Operator API publikuje wersjonowane zdarzenia o identyfikatorze schematu:

```text
bdb-event-v1
```

Minimalna koperta zdarzenia:

```json
{
  "schema": "bdb-event-v1",
  "event_id": "<stable-id>",
  "event_type": "<closed-catalog-value>",
  "occurred_at": "<UTC ISO-8601>",
  "source": "bridge|native_host|promoter|operator|project",
  "severity": "info|warning|error",
  "correlation_id": "<optional operation id>",
  "payload": {}
}
```

P03 zdefiniuje zamknięty katalog `event_type`, limity payloadu oraz mapowanie istniejących źródeł na zdarzenia. Zdarzenie nie zastępuje Journalu i nie jest źródłem prawdy. Jest projekcją odtwarzalną z trwałych danych i aktualnego stanu runtime.

GUI po uruchomieniu:

- wykonuje tylko odczyty;
- nie uruchamia Bridge'a;
- nie uzbraja Native Hosta;
- nie naprawia stanu `STALE`;
- nie zatrzymuje promotera;
- nie modyfikuje konfiguracji.

Operacje zmieniające stan wymagają jawnego działania użytkownika i osobnego requestu. Każdy request mutujący musi otrzymać stabilny `operation_id`, wynik i kod błędu.

## Konsekwencje

Pozytywne:

- GUI nie zależy od formatów logów;
- zdarzenia mogą zasilać historię, tray i przyszłe powiadomienia;
- możliwe jest testowanie kolejności oraz korelacji;
- otwarcie aplikacji jest bezpieczne i przewidywalne;
- późniejszy adapter Bartosz OS może konsumować ten sam strumień.

Koszty:

- potrzebny jest mapper i deduplikacja;
- należy rozróżnić snapshot stanu od zdarzeń historycznych;
- event stream nie może udawać pełnego audytu, jeśli źródłem prawdy pozostaje Journal.

## Niezmienniki

- `bdb-event-v1` jest wersjonowane i nie zmienia znaczenia istniejących pól;
- eventy nie uruchamiają mutacji jako efekt uboczny odczytu;
- GUI cache jest odtwarzalny;
- mutacja bez jawnego requestu jest błędem kontraktu;
- brak zdarzenia nie może być interpretowany jako dowód braku trwałego skutku — potwierdzenie pochodzi ze źródła prawdy i receipt.

## Odrzucone alternatywy

### Parsowanie tekstowych logów bez kontraktu

Odrzucone jako kruche, trudne do wersjonowania i podatne na błędną interpretację.

### GUI automatycznie uruchamia wszystko przy starcie

Odrzucone. Ukryta mutacja przeczy zasadzie jawnego sterowania i utrudnia diagnostykę.

### Event sourcing jako nowy główny model BDB

Odrzucone dla P02. BDB ma już trwały Journal i lifecycle; zdarzenia są projekcją operatorską, a nie nowym rdzeniem.
