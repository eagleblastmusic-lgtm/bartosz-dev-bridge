# ADR-0014: Manual verified release artifacts

Status: Accepted  
Data: 2026-07-19

## Kontekst

Control Center potrzebuje powtarzalnego artefaktu Windows, ale projekt nie ma jeszcze zatwierdzonego podpisu kodu, kanału produkcyjnego, instalatora systemowego ani bezpiecznego mechanizmu self-update. Sam fakt zbudowania pliku nie może być przedstawiany jako publikacja lub wdrożenie.

## Decyzja

P13 wprowadza ręczny workflow budowania oraz `bdb-release-manifest-v1`:

- workflow działa tylko przez `workflow_dispatch`;
- buduje PyInstaller `onedir`, wykonuje headless smoke i tworzy ZIP;
- manifest wiąże artefakt z wersją, source commit, rozmiarem i SHA-256;
- lokalny verifier odrzuca zmianę nazwy, rozmiaru lub zawartości;
- `auto_download`, `auto_install` i `published_release` pozostają `false`;
- `signature` pozostaje `null`, dopóki rzeczywisty mechanizm podpisu nie zostanie wdrożony i zweryfikowany;
- wynik jest wyłącznie krótkotrwałym artefaktem GitHub Actions.

## Konsekwencje

### Pozytywne

- można odtworzyć i zweryfikować pakiet z konkretnego commitu;
- gotowy plik wykonywalny przechodzi ten sam zero-mutation smoke;
- projekt nie udaje zaufanego instalatora ani kanału aktualizacji;
- manifest i verifier nie wymagają sieci ani dodatkowego procesu wykonawczego.

### Ograniczenia

- użytkownik nadal musi ręcznie pobrać i rozpakować artefakt;
- brak podpisu wydawcy i reputacji SmartScreen;
- brak automatycznego rollbacku wersji;
- artefakt Actions wygasa;
- P13 nie jest deployem na komputerze użytkownika.

## Alternatywy odrzucone

### Cichy self-update

Odrzucony bez podpisu, zaufanego kanału, atomowej instalacji i rollbacku.

### Automatyczne tworzenie GitHub Release przy każdym merge

Odrzucone jako niejawna publikacja i rozszerzenie uprawnień workflow do zapisu.

### Deklarowanie podpisu bez infrastruktury klucza

Odrzucone jako fałszywy dowód bezpieczeństwa.

## Bramka kolejnego etapu

MSI/MSIX, Authenticode, publikacja Release, kanał aktualizacji lub automatyczna instalacja wymagają osobnego ADR, decyzji o właścicielu klucza, procedury rotacji, rollbacku i bezpośredniej zgody użytkownika.
