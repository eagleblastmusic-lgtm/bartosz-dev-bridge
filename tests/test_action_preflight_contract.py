from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "browser_extension"


def test_full_background_entry_loads_action_preflight_last() -> None:
    entry = (EXTENSION / "background_full_entry.js").read_text(encoding="utf-8")

    assert '"background_project_launcher.js"' in entry
    assert '"background_action_preflight.js"' in entry
    assert entry.index('"background_project_launcher.js"') < entry.index(
        '"background_action_preflight.js"'
    )


def test_action_preflight_checks_hashes_and_local_allowed_paths() -> None:
    preflight = (EXTENSION / "background_action_preflight.js").read_text(
        encoding="utf-8"
    )

    assert "submitActionBeforePreflight" in preflight
    assert "submitActionWithPreflight" in preflight
    assert 'await nativeContext(repoAlias)' in preflight
    assert "context.allowed_paths" in preflight
    assert "bdbCanonicalBase64Bytes" in preflight
    assert 'crypto.subtle.digest("SHA-256", bytes)' in preflight
    assert "content_sha256 mismatch" in preflight
    assert "Path is not allowed by local policy" in preflight
    assert "await bdbPreflightAction(action)" in preflight
