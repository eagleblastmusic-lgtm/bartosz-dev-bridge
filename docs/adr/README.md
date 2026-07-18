# Architecture Decision Records

ADR-y opisują decyzje architektoniczne, których kolejne etapy nie powinny zmieniać bez nowego ADR zastępującego poprzedni.

| ADR | Status | Decyzja |
|---|---|---|
| [0001](0001-thin-control-center-over-operator-api.md) | Accepted | Control Center jest cienkim GUI nad Operator API, a BDB Core pozostaje źródłem wykonania i trwałego stanu. |
| [0002](0002-local-only-operator-api.md) | Accepted | Operator API jest lokalne, bez publicznego transportu sieciowego w MVP i niezależne od konkretnego IPC. |
| [0003](0003-versioned-events-and-explicit-mutations.md) | Accepted | Zdarzenia używają `bdb-event-v1`, GUI otwiera się tylko do odczytu, a mutacje są jawne. |

Dokument nadrzędny P02: [BDB Control Center — zamrożone granice](../BDB_CONTROL_CENTER_BOUNDARIES.md).

## Zasada zmiany

Zmiana decyzji `Accepted` wymaga:

1. nowego ADR ze statusem `Proposed`;
2. wskazania zastępowanego ADR;
3. analizy wpływu na bezpieczeństwo, kompatybilność i migrację;
4. zielonych testów kontraktowych;
5. jawnej akceptacji przed implementacją.
