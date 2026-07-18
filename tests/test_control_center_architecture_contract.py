from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = ROOT / "docs" / "BDB_CONTROL_CENTER_BOUNDARIES.md"
ADR_DIR = ROOT / "docs" / "adr"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_p02_architecture_documents_exist() -> None:
    expected = (
        BOUNDARIES,
        ADR_DIR / "0001-thin-control-center-over-operator-api.md",
        ADR_DIR / "0002-local-only-operator-api.md",
        ADR_DIR / "0003-versioned-events-and-explicit-mutations.md",
    )
    for path in expected:
        assert path.is_file(), f"Missing P02 architecture document: {path.relative_to(ROOT)}"
        assert path.stat().st_size > 0


def test_dependency_direction_is_frozen() -> None:
    content = read(BOUNDARIES)
    assert "bdb_gui/" in content
    assert "bdb_operator/" in content
    assert "bdb_bartosz_os/" in content
    assert "bdb_gui -> bdb_operator -> bdb_bridge" in read(
        ADR_DIR / "0001-thin-control-center-over-operator-api.md"
    )
    for forbidden in (
        "bdb_bridge -> bdb_operator",
        "bdb_bridge -> bdb_gui",
        "bdb_bridge -> bdb_bartosz_os",
    ):
        assert forbidden in content


def test_operator_api_stays_local_and_non_networked_in_mvp() -> None:
    content = read(ADR_DIR / "0002-local-only-operator-api.md")
    for marker in (
        "brak otwartego portu sieciowego",
        "Publiczne HTTP",
        "WebSocket",
        "brak uprawnień administratora",
        "request nie może zawierać arbitralnej komendy shell",
    ):
        assert marker in content


def test_event_and_future_module_schema_ids_are_reserved() -> None:
    boundaries = read(BOUNDARIES)
    events = read(ADR_DIR / "0003-versioned-events-and-explicit-mutations.md")
    assert "bdb-event-v1" in events
    assert "bartosz-os-module-manifest-v1" in boundaries


def test_gui_is_read_only_on_open_and_mutations_are_explicit() -> None:
    boundaries = read(BOUNDARIES)
    events = read(ADR_DIR / "0003-versioned-events-and-explicit-mutations.md")
    assert "uruchamia się w trybie tylko do odczytu" in boundaries
    assert "nie wykonuje ukrytego `Start` ani re-arm" in boundaries
    assert "Operacje zmieniające stan wymagają jawnego działania użytkownika" in events


def test_p02_does_not_implement_runtime_packages() -> None:
    # P02 freezes contracts only. Runtime implementation belongs to P03+.
    for path in (
        ROOT / "bdb_operator",
        ROOT / "bdb_gui",
        ROOT / "bdb_bartosz_os",
    ):
        assert not path.exists(), f"P02 unexpectedly introduced runtime package: {path.name}"
