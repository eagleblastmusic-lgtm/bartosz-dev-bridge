# ADR-0011: Explicit sanitized diagnostics export

Status: Accepted  
Data: 2026-07-18

## Kontekst

Operator potrzebuje jednego pakietu do analizy problemów Control Center, Bridge’a i promotera. Eksport nie może jednak kopiować całego Journalu, kodu repozytorium, sekretów ani uruchamiać się automatycznie.

## Decyzja

Diagnostyka ma dwa rozdzielone etapy:

1. bounded, read-only collection przez publiczny Operator API;
2. jawny eksport sanitizowanego ZIP-u do ścieżki wybranej przez użytkownika.

Collection obejmuje wyłącznie capabilities, status, bieżącą operację oraz bounded log tails. Eksport:

- nie rozpoczyna się bez osobnego kliknięcia;
- wymaga pliku `.zip` w istniejącym katalogu;
- nie nadpisuje pliku bez jawnego potwierdzenia;
- zapisuje atomowo przez plik tymczasowy i `os.replace`;
- dołącza manifest z hashami;
- nie zawiera Journal DB ani plików repozytorium;
- stosuje wersjonowaną redakcję sekretów.

## Konsekwencje

### Pozytywne

- łatwy do przekazania dowód diagnostyczny;
- minimalizacja ryzyka wycieku sekretów i kodu;
- częściowy snapshot pozostaje użyteczny;
- jednoznaczny receipt eksportu: path, size, SHA-256 i entries;
- brak sieci, telemetryki i automatycznego uploadu.

### Ograniczenia

- heurystyczna redakcja nie zastępuje kontroli użytkownika przed udostępnieniem archiwum;
- pakiet nie wystarcza do pełnej rekonstrukcji Journalu;
- logi są ograniczone do tails promotera;
- eksport wymaga zapisywalnego katalogu lokalnego.

## Alternatywy odrzucone

### Pełna kopia workspace albo Journalu

Odrzucona jako nadmiarowa, potencjalnie zawierająca kod, dane robocze i informacje wrażliwe.

### Automatyczny upload

Odrzucony, ponieważ wymagałby sieci, poświadczeń, nowego źródła danych i osobnej zgody.

### Eksport przy każdym błędzie

Odrzucony jako ukryty zapis i niekontrolowane tworzenie artefaktów.

## Bramka zmiany

Dodanie nowych źródeł danych, automatycznego eksportu, uploadu, telemetryki, Journal DB lub plików repozytorium wymaga osobnego ADR, analizy prywatności i jawnej zgody użytkownika.
