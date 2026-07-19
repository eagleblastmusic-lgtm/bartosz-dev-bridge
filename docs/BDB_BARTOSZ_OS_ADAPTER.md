# Bartosz Dev Bridge — P14 adapter Bartosz OS

Status: IMPLEMENTED ON BRANCH

## Cel

P14 definiuje stabilny kontrakt, przez który przyszły Bartosz OS może odkryć możliwości Dev Bridge i wywołać publiczne Operator API. Adapter nie przejmuje odpowiedzialności DevMastera i nie staje się nowym źródłem stanu.

## Manifest modułu

`bartosz-os-module-manifest-v1` deklaruje:

- identyfikator `devmaster.bartosz-dev-bridge`;
- właściciela odpowiedzialności `DevMaster`;
- kanoniczne repozytorium kodu;
- lokalny transport `in_process` bez listenera sieciowego;
- zamknięty katalog operacji odczytu i mutacji;
- brak arbitralnego shella, auto-merge i auto-deploy;
- domyślnie wyłączone mutacje;
- GitHub jako źródło prawdy dla kodu;
- brak roli Bartosz OS Core jako źródła stanu operacyjnego.

Statyczny descriptor znajduje się w `manifests/bartosz-dev-bridge.module.json`; funkcja `module_manifest()` generuje ten sam kontrakt z bieżącą wersją pakietu.

## Żądanie i odpowiedź

Adapter używa:

- `bdb-bartosz-os-request-v1`;
- `bdb-bartosz-os-response-v1`;
- istniejącego `bdb-operator-response-v1` jako zagnieżdżonego dowodu wykonania.

Każde żądanie zawiera UUID, dokładną operację, obiekt parametrów oraz jawne pole `mutation_authorized`.

## Polityka mutacji

Mutacja przechodzi dopiero po dwóch niezależnych bramkach:

1. instancja adaptera została utworzona z `mutations_enabled=True`;
2. konkretne żądanie ma `mutation_authorized=True`.

Odczyt z ustawionym `mutation_authorized=True` również jest odrzucany, aby pole nie stało się bezwartościowym domyślnym przełącznikiem.

## Routing

Adapter przekazuje wyłącznie zamknięty katalog publicznego Operator API:

### Odczyt

- capabilities;
- list_projects;
- status;
- events z bounded cursorem;
- current_operation;
- logs z bounded limitami.

### Mutacje

- prepare;
- start;
- stop;
- rearm.

Dodatkowe i brakujące parametry są odrzucane przed wywołaniem Operator API.

## Granice

Adapter:

- nie otwiera SQLite ani plików Journalu;
- nie wykonuje Git, PowerShell ani subprocess;
- nie nasłuchuje w sieci;
- nie zapisuje stanu;
- nie generuje własnych definicji statusu;
- nie zmienia własności decyzji i źródeł prawdy;
- nie jest wdrożeniem Bartosz OS Core.

Operator API pozostaje jedyną granicą wykonania, a zagnieżdżona odpowiedź operatora zachowuje rzeczywisty sukces lub błąd operacji.
