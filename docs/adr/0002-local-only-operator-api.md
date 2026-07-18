# ADR-0002: Operator API jest lokalne i transportowo wymienne

- Status: **Accepted**
- Data: **2026-07-18**
- Etap: **P02**

## Kontekst

Control Center potrzebuje stabilnego interfejsu do odczytu stanu i jawnego wywoływania operacji operatorskich. Jednocześnie BDB nie potrzebuje serwera internetowego ani zdalnego sterowania. Wczesne związanie kontraktu domenowego z HTTP, portem TCP lub frameworkiem GUI utrudniłoby późniejszą integrację z Bartosz OS i zwiększyłoby powierzchnię bezpieczeństwa.

## Decyzja

Operator API będzie interfejsem lokalnym, działającym jako zwykły proces użytkownika Windows albo biblioteka hostowana przez lokalny proces operatorski.

Kontrakt domenowy ma być niezależny od transportu. P03 wybierze minimalny transport lokalny po porównaniu co najmniej:

- wywołań in-process;
- lokalnego IPC systemowego;
- stdin/stdout z ramkowaniem wiadomości.

Publiczne HTTP, WebSocket, nasłuchiwanie na interfejsach sieciowych i cloud relay są poza zakresem P03 oraz MVP.

Domyślna polityka:

- brak uprawnień administratora;
- brak otwartego portu sieciowego;
- brak automatycznego uruchamiania przy starcie systemu;
- brak zdalnego uwierzytelniania, ponieważ nie ma zdalnego transportu;
- jawny katalog dozwolonych operacji;
- wersjonowane request/response DTO.

## Konsekwencje

Pozytywne:

- mała powierzchnia ataku;
- brak zależności od firewalla i konfiguracji sieci;
- łatwiejsze testy kontraktowe;
- możliwość późniejszej wymiany transportu bez zmiany GUI i domeny;
- prostsza instalacja użytkownika.

Koszty:

- zdalne sterowanie nie będzie dostępne w MVP;
- trzeba jawnie rozdzielić kontrakt domenowy od adaptera transportowego;
- integracja mobilna lub chmurowa wymaga przyszłego ADR.

## Niezmienniki

- request nie może zawierać arbitralnej komendy shell;
- transport nie może rozszerzać uprawnień BDB Core;
- wszystkie mutacje wymagają jawnego działania użytkownika;
- identyfikatory operacji i wyniki muszą być idempotentne lub bezpieczne przy ponowieniu;
- odczyt statusu nie może uruchamiać Bridge'a ani uzbrajać Native Hosta.

## Odrzucone alternatywy

### Lokalny serwer HTTP na `localhost`

Nie jest zakazany na zawsze, ale został odrzucony dla MVP. Nadal tworzy port, politykę origin, uwierzytelnianie lokalne i dodatkową powierzchnię błędów.

### Bezpośrednie wywoływanie skryptów PowerShell przez GUI

Odrzucone jako trwała architektura. PowerShell może pozostać adapterem zgodności, ale GUI musi korzystać ze stabilnego Operator API i kodów błędów.

### Połączenie GUI bezpośrednio z Native Hostem

Odrzucone. Native Host jest kanałem przeglądarkowym, nie ogólnym backendem operatorskim.
