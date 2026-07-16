# GHB2-B — Multi-file patch i scope validation

## Cel

GHB2-B buduje deterministyczny plan wielu zmian bez fizycznego modyfikowania workspace. Plan łączy:

- exact whole-file replacement;
- `create_file`;
- `delete_file`;
- `rename_file`;
- `move_file`.

Operacje są symulowane kolejno na wirtualnym stanie plików. Dzięki temu późniejsza operacja może bezpiecznie odwołać się do wyniku wcześniejszej, np. utworzyć plik, zastąpić jego treść, a następnie przenieść go.

GHB2-B nie dodaje batch `apply()`. Fizyczne wykonanie wielu plików bez trwałego checkpointu mogłoby pozostawić częściowo zmieniony workspace. Journal, rollback i recovery zostaną dodane razem w GHB2-C.

## File replacement v1

```json
{
  "schema": "bdb-file-replacement-v1",
  "kind": "replace_file",
  "path": "pkg/module.py",
  "expected_sha256": "sha256:<64 lowercase hex>",
  "content_base64": "...",
  "content_sha256": "sha256:<64 lowercase hex>"
}
```

Replacement jest binary-safe i zastępuje cały plik. Nie używa niejednoznacznego search/replace. Wymaga dokładnego hash source oraz hash decoded destination bytes.

## Multi-file patch v1

```json
{
  "schema": "bdb-multi-file-patch-v1",
  "operations": [
    {
      "schema": "bdb-file-replacement-v1",
      "kind": "replace_file",
      "path": "pkg/module.py",
      "expected_sha256": "sha256:<64 lowercase hex>",
      "content_base64": "...",
      "content_sha256": "sha256:<64 lowercase hex>"
    },
    {
      "schema": "bdb-edit-operation-v1",
      "kind": "move_file",
      "source_path": "pkg/old.py",
      "destination_path": "archive/old.py",
      "expected_source_sha256": "sha256:<64 lowercase hex>"
    }
  ]
}
```

Każda pozycja zachowuje własny kanoniczny schema contract. Batch parser nie toleruje dodatkowych kluczy, nieznanego schema ani niekanonicznego base64.

## Limity

```text
operations: 1..100
unique paths: <=200
supplied after content: <=8 MiB
single existing source: <=1 MiB
combined before + final after snapshot: <=16 MiB
```

Limity są polityką bezpieczeństwa v1, a nie benchmarkiem wydajności.

## Kolejność i wirtualny stan

Planner przetwarza operacje w kolejności listy:

1. przy pierwszym dotknięciu ścieżki pobiera exact physical before state;
2. sprawdza source/destination scope i typ pliku;
3. stosuje precondition do bieżącego wirtualnego stanu;
4. aktualizuje wirtualne bytes lub istnienie ścieżki;
5. po ostatniej operacji tworzy finalny stan każdej ścieżki.

Przykładowe sekwencje:

```text
create A → replace A → move A do B
replace A → rename A do B
delete A → create A z nową treścią
```

Expected SHA dla późniejszej operacji odnosi się do wirtualnego wyniku wcześniejszej operacji, nie zawsze do początkowego working tree.

Batch, którego finalny stan jest identyczny z początkowym, jest odrzucany jako net no-op.

## Scope validation

Każda source, destination i replacement path przechodzi przez `WorkspaceManager.resolve_allowed_path()`, co wymaga jednocześnie:

- dopasowania do lokalnego `config.allowed_paths`;
- dopasowania do `manifest.allowed_paths`;
- repo-relative POSIX path;
- containment w exact session worktree;
- braku symlink/junction/reparse escape.

Nie wystarczy, że jedna z warstw scope pozwala na ścieżkę.

Destination parent musi istnieć. Operacje nie tworzą katalogów.

Committed-sensitive paths i zarezerwowane `.bdb_*` pozostają zablokowane przez kontrakt GHB2-A.

## MultiFilePatchPlan

Plan zawiera dla każdej dotkniętej ścieżki:

- exact before existence i bytes;
- before SHA-256;
- final after existence i bytes;
- after SHA-256;
- role ścieżki;
- indeksy operacji, które jej dotknęły.

Dodatkowo zawiera:

- kanoniczny `patch_sha256`;
- posortowane `touched_paths`;
- posortowane `changed_paths`;
- łączny rozmiar before i after;
- deterministyczny `plan_sha256`.

Planner nie zapisuje planu do SQLite i nie modyfikuje plików.

## Revalidation

`MultiFilePatchPlanner.revalidate()` przed przyszłym apply:

- ponownie przeprowadza local + manifest scope validation;
- sprawdza integralność plan hash;
- potwierdza, że każdy physical before state nadal ma exact bytes lub nadal nie istnieje.

Jakakolwiek zmiana workspace po planowaniu daje controlled `state_mismatch`.

## Granica GHB2-C

GHB2-C użyje kompletnego planu do:

- trwałego checkpointu before/after;
- atomowego oznaczenia fazy wykonania;
- kontrolowanego apply ścieżka po ścieżce;
- rollbacku po błędzie;
- restart recovery po awarii w dowolnym miejscu batchu;
- jednoznacznego finalnego ACK.

Dopiero wtedy multi-file operations mogą zostać włączone do remote runtime.

## Poza zakresem

GHB2-B nie dodaje:

- częściowego batch apply;
- Journal v9;
- rollbacku;
- automatycznego recovery;
- tworzenia/usuwania katalogów;
- wildcard/glob operations;
- symlink operations;
- chmod;
- Git commit/push;
- test profile;
- zdalnego arbitrary shell.
