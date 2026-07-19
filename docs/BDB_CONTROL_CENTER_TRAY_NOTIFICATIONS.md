# BDB Control Center — P12 tray i powiadomienia

Status: IMPLEMENTED ON BRANCH

## Zachowanie okna

- zwykłe zamknięcie okna ukrywa Control Center w zasobniku, gdy system tray jest dostępny;
- brak tray’a zachowuje zwykłe zamknięcie aplikacji;
- ponowne pokazanie nie uruchamia Bridge’a, promotera ani Native Host;
- headless smoke nigdy nie tworzy ikony tray i zachowuje deterministyczne zamknięcie.

## Jawne zakończenie

Akcja `Zakończ Control Center…` ma trzy wyniki:

1. pozostaw lokalne BDB uruchomione i zamknij tylko panel;
2. zatrzymaj jawnie wybrany projekt, zaczekaj na wynik istniejącego `ControlWorker`, a następnie zamknij panel wyłącznie po sukcesie;
3. anuluj.

Zakończenie jest blokowane, gdy trwa inna operacja. Stop przed wyjściem nie używa nowej ścieżki backendowej — korzysta z tego samego serializowanego serwisu P07.

## Powiadomienia

Powiadomienia są lokalne i powstają tylko po istniejących sygnałach zakończenia:

- Start, Stop i re-arm;
- Prepare projektu;
- eksport diagnostyczny.

Nie ma timera, pollingu, watchera, telemetrii, wysyłania do sieci ani odczytu Journalu przez tray.

## Bezpieczeństwo

- ukrycie okna nie zmienia stanu BDB;
- tray nie wykonuje automatycznego Start, re-arm ani Prepare;
- pierwsze ukrycie wyświetla informację, że aplikacja działa w zasobniku;
- nieudany Stop przed wyjściem pozostawia panel dostępny;
- zakończenie panelu nie jest przedstawiane jako zatrzymanie BDB.
