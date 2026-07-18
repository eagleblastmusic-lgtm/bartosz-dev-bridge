# GUI technology spikes

Ten katalog zawiera odwracalne proof-of-concept. Nie jest produkcyjnym pakietem `bdb_gui`.

## PySide6 + Qt Widgets

Instalacja opcjonalna:

```powershell
python -m pip install -e ".[dev,gui-spike]"
```

Interaktywny probe:

```powershell
python spikes/gui/pyside6_probe.py
```

Headless smoke:

```powershell
python spikes/gui/pyside6_probe.py --headless-smoke --json-out .artifacts/pyside6-probe.json
```

Probe:

- tworzy pojedyncze okno Qt Widgets;
- odczytuje tylko `OperatorApi.capabilities()`;
- nie przyjmuje ścieżki workspace;
- nie wykonuje `Start`, `Stop`, `rearm`, `prepare`, patcha ani Git;
- raportuje wersje, DPI, platform plugin i dostępność API system tray;
- kończy event loop automatycznie w trybie headless.

Właściwy pakiet `bdb_gui` powstanie dopiero w P06.
