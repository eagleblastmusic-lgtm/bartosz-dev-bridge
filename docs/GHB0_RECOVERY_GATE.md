# GHB-0 Recovery Gate

GHB0-7 zamyka etap proof-of-architecture przez procesową bramkę recovery, trwały lifecycle worktree oraz jawne procedury operatorskie.

## Macierz A–G

Każdy przypadek tworzy nowe syntetyczne fixture repo, bare/control repo, bridge clone, Journal, worktree, UUID sesji i proces foreground. Po kontrolowanym fault exit uruchamiany jest nowy proces z nowymi obiektami runtime. Trzeci restart sprawdza no-op po sukcesie.

| Case | Durable boundary | Stan przed restartem |
|---|---|---|
| A | `AFTER_DISCOVERED_BEFORE_VALIDATION` | `DISCOVERED` |
| B | `AFTER_EXECUTE_CLAIM` | `CLAIMED` |
| C | `AFTER_TEMP_WRITE_BEFORE_REPLACE` | `EXECUTING` |
| D | `AFTER_FILE_REPLACE_BEFORE_EFFECT_COMMIT` | `EXECUTING` |
| E | `AFTER_EFFECT_COMMIT_BEFORE_PROFILE` | `EFFECT_RECORDED` |
| F | `AFTER_STAGE_COMMIT_BEFORE_PUBLISH` | `RESULT_STAGED` |
| G | `AFTER_REMOTE_PUSH_BEFORE_LOCAL_ACK` | `RESULT_STAGED`, remote push trwały |

Każda sesja musi zakończyć się jako `RESULT_PUBLISHED`, z revision `1`, pojedynczym plan/effect/result/outbox, pojedynczym claimem, pojedynczym patchem i pojedynczym remote publish. Remote bytes, path, SHA-256 i historia fast-forward są sprawdzane dokładnie.

Canonical report:

```json
{"cases":[],"failed":0,"gate":"GHB0","passed":7,"schema_version":"1.0","sessions":7}
```

Raport zawiera metadane audytowe, ale nie zawiera treści plików, środowiska, tokenów ani sekretów.

## Scenariusze bezpieczeństwa

Bramka obejmuje również:

- persisted transport retry, restart i późniejszą publikację;
- command collision z immutable pierwszym commandem i hash-only diagnostics;
- result collision bez nadpisania remote i bez force push;
- divergent workspace przechodzący do manual reconciliation i `preserve`;
- drugi realny proces odrzucony przez wspólny OS lock;
- ponowny restart po sukcesie bez duplikowania patcha, revision, eventu i publish.

## Journal v6

Migracja `journal_v6_workspace_lifecycle` dodaje trwałą tabelę `workspace_lifecycle`. Wiersz pozostaje po fizycznym usunięciu worktree i nie posiada delete API.

Dyspozycje:

```text
preserve | cleanup
```

Stany:

```text
preserved | cleanup_requested | removing | removed | blocked
```

Immutable identity obejmuje session ID, exact absolute path, base SHA, expected revision i expected state hash. Zmiany stanu są optimistic, transakcyjne i emitują eventy:

```text
workspace.preserved
workspace.cleanup_requested
workspace.cleanup_started
workspace.cleanup_completed
workspace.cleanup_blocked
```

## Finalizacja

`bdb bridge session finalize` jest osobną operacją operatorską. Wymaga service `OFFLINE`, wspólnego locka i kompletnej, ponownie sprawdzonej eligibility. Nie dodaje protocol ACK i nie uruchamia automatycznego cleanupu.

## Cleanup contract

Cleanup jest jawny, potwierdzony exact session ID i dostępny wyłącznie dla `COMPLETED`. Przed fizyczną operacją sprawdzane są m.in. active commands, outbox, ingestion issues, lifecycle identity, exact root/path, reparse points, source cleanliness, detached exact-base registration, unauthorized paths, temp artifacts i physical state hash.

Crash recovery obejmuje:

```text
AFTER_CLEANUP_REQUEST_BEFORE_START
AFTER_CLEANUP_STARTED_BEFORE_REMOVE
AFTER_WORKTREE_REMOVE_BEFORE_JOURNAL_ACK
```

Jeżeli path i registration są niespójne, cleanup kończy się `blocked` i nie używa prune ani ręcznego delete.

## Uruchomienie

Windows:

```powershell
.\scripts\Invoke-GHB0RecoveryGate.ps1
```

Bezpośredni runner:

```powershell
python scripts\run_ghb0_recovery_gate.py --output artifacts\ghb0-gate\recovery-gate.json
```

Pełna bramka obejmuje compile, realną macierz A–G, targeted lifecycle, POC-0A/POC-0B i pełny pytest na wspieranych platformach.
