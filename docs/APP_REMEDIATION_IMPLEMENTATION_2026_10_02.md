# Wykonanie planu poprawek aplikacji

Zakres odpowiada T01–T07 z planu z 2 października 2026. Audyt i plan pozostają materiałami wejściowymi; ten dokument opisuje zachowanie implementacji. Kwalifikacja źródeł, integralność przygotowanego pakietu, ACTIVE i witness z używanego profilu Chrome wymagają osobnych dowodów.

| Obszar | Zachowanie po zmianie | Właściwa weryfikacja |
| --- | --- | --- |
| T01 Memory | Reader i writer używają wspólnej walidacji typów, wymaganych pól oraz limitów bytes. Odrzucony zapis zachowuje wcześniejszy stan. Odczyt nie naprawia historii. | `test_project_memory_persistence_contract.py`, Memory, acceptance/replay, GUI degradacji |
| T02 DOM | Pakiet zawiera istniejącą poprawkę ownership zagnieżdżonego selection node. Native, Browser i client plan muszą wskazywać ten sam czysty source subject. | Build/manifest/client-plan verification; osobno realny profil Chrome i delivery witness po autoryzowanej aktywacji |
| T03 GUI | Workflow tworzenia/otwierania, promptów, importu planu i AUTO wykonują się przez Qt worker. Busy blokuje duplikat, STOP może zapisać fence w osobnym workerze. Timer czyta projekcje i nie uruchamia reconciliation/send/resume. | `test_project_workflow_responsiveness.py`, NX-030 i write-capability regression |
| T04 Browser | Retencja usuwa tylko ACKED z dokładnie potwierdzonym canonical receipt. Replay usuniętego key odwołuje się do Native lookup. SENT/UNKNOWN zachowują recovery. Digest admission jest zgodny z M3a. | Worker harness; framed Native + trwały store, lost-ACK/restart/replay |
| T05 Wzrost v1 | `memory.json` pozostaje authority. Istniejący retention controller/CAS przechowuje immutable historię, a pointer wiąże ją digestem. Active/unresolved pozostają w bieżącej projekcji. | Rzeczywisty workflow ponad 2048 events i 512 bindingów/prób/receipts; crash, parity, export/restore, STOP przy pełnej projekcji; NX-018/066 |
| T06 Local Execution | Efekt pochodzi z executable/argv/environment; Git read wyłącza pager/fsmonitor/external diff/textconv. Dowolne skrypty wymagają approval. Raw stdout/stderr są strumieniowane do istniejącego storage z preview do 64 KiB. Windows proces pozostaje suspended do poprawnego przypisania Job Object. | NX-042/043/044/049, integration, adversarial argv/env, 4 MiB stdout + stderr, tamper i Job Object fault injection |
| T07 Guidance/CI | README wskazuje repo-local runtime, a historyczny snapshot podaje swój zakres czasowy. CI uruchamia regresje zmienionych producentów. | `test_remediation_guidance.py`, aktualny czysty source subject i wykonane gate |

## Authority, capacity i recovery

Limity bieżącej projekcji wynoszą 2048 events, 512 wpisów w ograniczonych kolekcjach, 512 KiB execution oraz 4 MiB Memory. Historia logiczna może je przekraczać dzięki retencji zakończonych rekordów. Nierozstrzygnięte dane nie są usuwane dla odzyskania miejsca; GUI pokazuje `CAPACITY_WARNING` przy wykorzystaniu co najmniej 80% odpowiedniego limitu.

CAS zostaje utrwalony i zweryfikowany przed atomową publikacją pointera. Awaria przed replace pozostawia poprzednią authority; opublikowany pointer wymaga zgodności content digest i logical digest przy ponownym odczycie. Brak lub modyfikacja archiwum daje typed `memory_retention_invalid`.

Pełną logiczną historię eksportuje `ProjectMemoryStore.export_archive()`. `restore_archive()` sprawdza digest i wymaga pustego, izolowanego celu. Backup projektu objętego retencją musi obejmować `memory.json`, `retention.db` i niezmienne plany albo zweryfikowany pełny export. Sam ogon `memory.json` jest odrzucany przez shadow importer. Nie wykonano migracji authority istniejących projektów.

STOP fence należy do istniejącego store v2. GUI zapisuje fence przed projekcją STOP w v1. Błąd tej projekcji pozostawia fence i pokazuje konieczność ponowienia odczytu/projekcji. Wznowienie wymaga istniejącej jawnej komendy AUTO.

## Local Execution

Klasyfikacja jest zachowawcza: testy i skrypty mogą wykonać dowolne efekty, więc nie stają się READ_ONLY na podstawie adapter ID. Dostęp sieciowy rozpoznawanych narzędzi jest odrzucany przez policy. Klasyfikacja nie stanowi systemowej izolacji sieci dowolnego zatwierdzonego skryptu; obowiązuje request-bound approval i boundary wykonania.

Raw artefakt jest utrwalany również dla małego outputu; presentation/redaction pozostają osobnym widokiem. Wynik nie uzyskuje kompletnego evidence, jeżeli finalny artefakt nie przejdzie odczytu i sprawdzenia digestu/rozmiaru. Cancellation i timeout zachowują oddzielny mechaniczny status.

## Release evidence

Aktualne wyniki i source identities są zapisywane w `artifacts/remediation-20261002/`. Źródłowy harness nie potwierdza załadowanej identity Chrome. Przygotowanie maintenance nie jest aktywacją. Readback ACTIVE i realny Browser witness należy dopisać dopiero po konkretnym zatwierdzonym transition i reload używanego profilu.

Historyczny gate NX-070 kwalifikuje zmianę wyłącznie dokumentacyjną względem swojego source subject i sierpniowej obserwacji Bootstrap. Zachowuje te warunki; nie jest release gate tego zadania implementacyjnego. Aktualne guidance ma osobny test runtime resolvera, zakresu czasowego snapshotu oraz aktywnego CI.

Przed publikacją nowego Bootstrap procedura maintenance odtwarza stare routes/client bytes w razie awarii. Po publikacji obowiązuje istniejący `ROLL_FORWARD_ONLY`. Nowy pointer retencji v1 wymaga readera z tej generacji; PREVIOUS nie służy do odczytu nowej historii przez stary reader.
