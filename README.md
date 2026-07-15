# Bartosz Dev Bridge

Minimalna implementacja **POC-0** lokalnego Bridge'a dla ChatGPT Plus i GitHuba, zgodna z dokumentacją projektową v1.1.

Aktualna faza:

```text
GHB0-1 — stabilne granice rdzenia
```

Zakres obejmuje:

- pakiet `bdb_bridge` ze stabilnymi granicami protokołu v1.1 (walidacja, serializacja, konfiguracja, modele);
- warstwę wykonawczą `bdb_poc` z zachowaną kompatybilnością importów POC-0;
- jednorazowy `poc_bridge.py`;
- syntetyczne repozytorium `bdb-poc-fixture`;
- polling branchu `commands`;
- publikację małych wyników na branch `results`;
- operacje `open_read` i `replace_exact_and_test`;
- stały lokalny profil `python -m pytest -q`;
- jedno worktree i jedną aktywną sesję;
- testy jednostkowe/integracyjne oraz GitHub Actions.

Poza zakresem pozostają GHB-0+, GUI, SQLite, LSP, Browser Lab, Hermes, wielosesyjność, prawdziwe repozytoria GicleeApp oraz operacje produkcyjne.

Instrukcja uruchomienia:

```text
POC_0_WINDOWS_START.md
```

Repozytorium nie może zawierać tokenów, sekretów, plików `.env` ani prywatnych danych użytkownika.
