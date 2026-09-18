"""Regression tests for canonical runtime promotion during post-ACTIVE maintenance.

Reproduces the production bug where ``candidate_client_runtime_root`` pointed
to a temporary external staging directory, causing HKCU Native Messaging to
resolve to that temp folder instead of the canonical repo-local runtime.
Projects in ``control/project-catalog.json`` and ``project-memory/`` became
invisible because ``runtime_authority`` resolved ``runtime_root`` to the
temporary directory.

These tests verify that when ``canonical_runtime_root`` is passed to
``prepare_post_active_maintenance``:

1. The route transition plan binds the **canonical** manifest path.
2. ``apply_post_active_maintenance`` promotes clients to canonical root
   before switching routes.
3. Final route observation and client identity verification target the
   canonical runtime, not the temporary staging location.
4. Pre-bootstrap rollback reverses client promotion.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import bdb_vnext.m11c_post_active_maintenance as maintenance


SHA = "sha256:" + "a" * 64
STAGED_PLAN_SHA = "sha256:" + "1" * 64
CANON_PLAN_SHA = "sha256:" + "2" * 64
CANON_MANIFEST_SHA = "sha256:" + "3" * 64
HEAD = "1" * 40
TREE = "2" * 40
OLD = "3" * 40
PREVIOUS = "4" * 40


def _active(tmp_path: Path) -> dict[str, object]:
    return {
        "state": {
            "schema": maintenance.SLOT_STATE_V2_SCHEMA,
            "runtime_id": maintenance.RUNTIME_ID,
            "generation_id": maintenance.GENERATION_ID,
            "activation_authority": maintenance.M11C_ACTIVATION_AUTHORITY,
            "authority_boundary": "external_bootstrap_root",
            "candidate_manifest_sha256": None,
            "candidate_may_write_active_pointer": False,
            "production_activation_performed": True,
            "rollback_mode": maintenance.M11C_ROLLBACK_MODE,
            "legacy_runtime_root": str(tmp_path / "legacy"),
            "required_control_schema": 1,
            "required_capabilities": ["canonical-admission-v1"],
            "state_sha256": SHA,
            "active_manifest_sha256": "sha256:" + "b" * 64,
            "previous_manifest_sha256": "sha256:" + "c" * 64,
        },
        "active": {
            "source_commit": OLD,
            "bundle_root": str(tmp_path / "old"),
            "bundle_sha256": "sha256:" + "d" * 64,
            "bundle_role": "candidate",
            "known_good": True,
        },
        "previous": {
            "source_commit": PREVIOUS,
            "bundle_root": str(tmp_path / "previous"),
            "bundle_sha256": "sha256:" + "e" * 64,
            "bundle_role": "recovery",
            "known_good": True,
        },
    }


def _patch_common(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    canonical_root: Path | None = None,
) -> dict[str, object]:
    """Wire monkeypatches.  canonical_root determines manifest verification target."""
    current = _active(tmp_path)
    authority = tmp_path / "authority"
    staged = tmp_path / "staged-client"
    canonical = canonical_root or staged
    for root in (authority, tmp_path / "bundle", staged, canonical, tmp_path / "legacy"):
        root.mkdir(exist_ok=True, parents=True)

    staged_manifest = str((staged / "clients" / "native-host" / "com.bartosz.dev_bridge.vnext.json").resolve())
    canonical_manifest = str((canonical / "clients" / "native-host" / "com.bartosz.dev_bridge.vnext.json").resolve())

    old32 = str((tmp_path / "old32.json").resolve())
    old64 = str((tmp_path / "old64.json").resolve())
    route_state = {"values": {"32": old32, "64": old64}}

    def route_observation(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "target": [{"root": "HKCU", "view": view, "value": route_state["values"][view]} for view in ("32", "64")],
            "legacy": [],
            "target_conflict": False,
            "target_registered": True,
            "legacy_route_present": False,
        }

    def route_write(*, runtime_root: Path, view: str, manifest_path: str) -> None:
        route_state["values"][view] = manifest_path

    distinct_canonical = canonical_root is not None and canonical != staged

    # _client_identity deliberately returns different plan identities for the
    # staged and canonical roots.  This is the production regression: client
    # plans are path-bound, so their SHA changes after promotion.
    def client_identity(runtime_root: Path, **_kw: object) -> dict[str, object]:
        if distinct_canonical and str(runtime_root) == str(canonical):
            return {
                "client_plan_sha256": CANON_PLAN_SHA,
                "browser_bundle_digest": SHA,
                "native_manifest_digest": CANON_MANIFEST_SHA,
                "native_manifest_path": canonical_manifest,
            }
        return {
            "client_plan_sha256": STAGED_PLAN_SHA,
            "browser_bundle_digest": SHA,
            "native_manifest_digest": SHA,
            "native_manifest_path": staged_manifest,
        }

    # prepare must precompute the production-path-bound document set before
    # apply.  Keep this focused test independent from filesystem packaging.
    monkeypatch.setattr(
        maintenance,
        "query_client_plan",
        lambda **_: {"plan": {"client_plan_sha256": STAGED_PLAN_SHA}},
    )
    monkeypatch.setattr(
        maintenance,
        "_production_documents",
        lambda **_: (
            {
                "client_plan_sha256": CANON_PLAN_SHA,
                "native_manifest_sha256": CANON_MANIFEST_SHA,
                "native_manifest_path": canonical_manifest,
            },
            {},
            {},
        ),
    )

    monkeypatch.setattr(maintenance, "_active_observation", lambda _authority: current)
    monkeypatch.setattr(maintenance, "_observe_candidate_bundle", lambda **_: {"health": {"status": "READY"}})
    monkeypatch.setattr(maintenance, "_client_identity", client_identity)
    monkeypatch.setattr(maintenance, "_route_observation", route_observation)
    monkeypatch.setattr(maintenance, "set_windows_target_native_route_view", route_write)
    monkeypatch.setattr(maintenance, "_verify_routes", lambda *_args: None)
    return current


def _prepare_canonical(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    maintenance_id: str = "m-canon-test",
) -> tuple[dict[str, object], Path, Path]:
    """Prepare with a canonical_runtime_root different from staged root."""
    staged = tmp_path / "staged-client"
    canonical = tmp_path / "canonical-runtime"
    _patch_common(monkeypatch, tmp_path, canonical_root=canonical)
    result = maintenance.prepare_post_active_maintenance(
        authority_root=tmp_path / "authority",
        candidate_bundle_root=tmp_path / "bundle",
        candidate_bundle_sha256=SHA,
        candidate_client_runtime_root=staged,
        source_head=HEAD,
        source_tree=TREE,
        native_artifact_manifest_sha256=SHA,
        maintenance_id=maintenance_id,
        canonical_runtime_root=canonical,
    )
    return result, staged, canonical


class TestCanonicalPrepare:
    """Verify prepare binds the canonical manifest path in immutable documents."""

    def test_route_plan_targets_canonical_manifest(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        result, staged, canonical = _prepare_canonical(monkeypatch, tmp_path)
        route_plan = result["route_transition_plan"]
        candidate_manifest = route_plan["candidate_native_manifest_path"]
        # Must point to canonical, NOT staged.
        assert str(canonical) in candidate_manifest
        assert str(staged) not in candidate_manifest

    def test_candidate_contains_canonical_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        result, staged, canonical = _prepare_canonical(monkeypatch, tmp_path)
        cand = result["candidate"]
        assert cand["canonical_runtime_root"] == str(canonical)
        assert cand["staged_client_plan_sha256"] == STAGED_PLAN_SHA
        assert cand["client_plan_sha256"] == CANON_PLAN_SHA
        assert cand["native_manifest_digest"] == CANON_MANIFEST_SHA
        assert cand["candidate_client_runtime_root"] == str(staged)
        # candidate_native_manifest_path must be canonical
        assert str(canonical) in cand["candidate_native_manifest_path"]

    def test_plan_contains_canonical_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        result, staged, canonical = _prepare_canonical(monkeypatch, tmp_path)
        plan = result["plan"]
        assert plan["canonical_runtime_root"] == str(canonical)
        assert plan["staged_client_plan_sha256"] == STAGED_PLAN_SHA
        assert plan["client_plan_sha256"] == CANON_PLAN_SHA
        assert plan["native_manifest_digest"] == CANON_MANIFEST_SHA
        assert result["route_transition_plan"]["candidate_client_plan_sha256"] == CANON_PLAN_SHA
        assert str(canonical) in plan["candidate_native_manifest_path"]

    def test_prepare_without_canonical_has_no_extra_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """When canonical_runtime_root is None (default), no extra fields added."""
        _patch_common(monkeypatch, tmp_path)
        result = maintenance.prepare_post_active_maintenance(
            authority_root=tmp_path / "authority",
            candidate_bundle_root=tmp_path / "bundle",
            candidate_bundle_sha256=SHA,
            candidate_client_runtime_root=tmp_path / "staged-client",
            source_head=HEAD,
            source_tree=TREE,
            native_artifact_manifest_sha256=SHA,
            maintenance_id="m-no-canon",
        )
        assert "canonical_runtime_root" not in result["candidate"]
        assert "staged_client_plan_sha256" not in result["candidate"]
        assert "canonical_runtime_root" not in result["plan"]

    def test_prepare_with_same_canonical_has_no_extra_fields(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """When canonical == staged, no extra fields added (no-op promotion)."""
        staged = tmp_path / "staged-client"
        _patch_common(monkeypatch, tmp_path)
        result = maintenance.prepare_post_active_maintenance(
            authority_root=tmp_path / "authority",
            candidate_bundle_root=tmp_path / "bundle",
            candidate_bundle_sha256=SHA,
            candidate_client_runtime_root=staged,
            source_head=HEAD,
            source_tree=TREE,
            native_artifact_manifest_sha256=SHA,
            maintenance_id="m-same-canon",
            canonical_runtime_root=staged,
        )
        assert "canonical_runtime_root" not in result["candidate"]


class TestCanonicalApply:
    """Verify apply promotes clients and routes to canonical root."""

    def test_apply_calls_promote_and_switches_to_canonical(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        current = _active(tmp_path)
        result, staged, canonical = _prepare_canonical(monkeypatch, tmp_path, "m-canon-apply")

        # Track promotion calls
        promotion_calls: list[dict[str, object]] = []
        original_promote = maintenance.promote_client_plan

        def mock_promote(**kwargs: object) -> dict[str, object]:
            promotion_calls.append(kwargs)
            return {"status": "COMMITTED", "client_plan_sha256": CANON_PLAN_SHA}

        monkeypatch.setattr(maintenance, "promote_client_plan", mock_promote)

        # Standard apply mocks
        candidate_doc = {
            "schema": "bdb-vnext-bootstrap-slot-manifest-v1", "slot": "ACTIVE",
            "bundle_root": str(tmp_path / "bundle"),
            "bundle_sha256": SHA, "bundle_role": "candidate",
            "source_commit": HEAD, "known_good": True,
            "bundle_id": "candidate", "manifest_sha256": SHA,
            "compatibility": {"supported_control_schema": {"min": 1, "max": 1}, "capabilities": ["canonical-admission-v1"]},
        }
        monkeypatch.setattr(maintenance, "_inspect", lambda *_args, **_kwargs: candidate_doc)
        monkeypatch.setattr(maintenance, "_publish", lambda *_args, **_kwargs: SHA)
        monkeypatch.setattr(maintenance, "_replace_state", lambda *_args, **_kwargs: None)

        after = _active(tmp_path)
        after["active"] = {**current["active"], "source_commit": HEAD, "bundle_sha256": SHA}
        observations = iter((current, after))
        monkeypatch.setattr(maintenance, "_active_observation", lambda _authority: next(observations))

        applied = maintenance.apply_post_active_maintenance(
            authority_root=tmp_path / "authority",
            maintenance_id="m-canon-apply",
            expected_plan_sha256=result["plan"]["plan_sha256"],
            operator_approved=True,
        )

        assert applied["status"] == "ACTIVE"
        assert applied["production_activation_performed"] is True
        # Verify promote_client_plan was called with correct args
        assert len(promotion_calls) == 1
        call = promotion_calls[0]
        assert str(call["staged_runtime_root"]) == str(staged)
        assert str(call["production_runtime_root"]) == str(canonical)
        assert call["verify_routes"] is False

    def test_apply_without_canonical_does_not_promote(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """When there's no canonical_runtime_root, promote_client_plan is NOT called."""
        current = _active(tmp_path)
        _patch_common(monkeypatch, tmp_path)
        prepared = maintenance.prepare_post_active_maintenance(
            authority_root=tmp_path / "authority",
            candidate_bundle_root=tmp_path / "bundle",
            candidate_bundle_sha256=SHA,
            candidate_client_runtime_root=tmp_path / "staged-client",
            source_head=HEAD, source_tree=TREE,
            native_artifact_manifest_sha256=SHA,
            maintenance_id="m-no-canon-apply",
        )

        promotion_calls: list[dict[str, object]] = []

        def mock_promote(**kwargs: object) -> dict[str, object]:
            promotion_calls.append(kwargs)
            return {"status": "COMMITTED", "client_plan_sha256": CANON_PLAN_SHA}

        monkeypatch.setattr(maintenance, "promote_client_plan", mock_promote)

        candidate_doc = {
            "schema": "bdb-vnext-bootstrap-slot-manifest-v1", "slot": "ACTIVE",
            "bundle_root": str(tmp_path / "bundle"),
            "bundle_sha256": SHA, "bundle_role": "candidate",
            "source_commit": HEAD, "known_good": True,
            "bundle_id": "candidate", "manifest_sha256": SHA,
            "compatibility": {"supported_control_schema": {"min": 1, "max": 1}, "capabilities": ["canonical-admission-v1"]},
        }
        monkeypatch.setattr(maintenance, "_inspect", lambda *_args, **_kwargs: candidate_doc)
        monkeypatch.setattr(maintenance, "_publish", lambda *_args, **_kwargs: SHA)
        monkeypatch.setattr(maintenance, "_replace_state", lambda *_args, **_kwargs: None)

        after = _active(tmp_path)
        after["active"] = {**current["active"], "source_commit": HEAD, "bundle_sha256": SHA}
        observations = iter((current, after))
        monkeypatch.setattr(maintenance, "_active_observation", lambda _authority: next(observations))

        applied = maintenance.apply_post_active_maintenance(
            authority_root=tmp_path / "authority",
            maintenance_id="m-no-canon-apply",
            expected_plan_sha256=prepared["plan"]["plan_sha256"],
            operator_approved=True,
        )

        assert applied["status"] == "ACTIVE"
        assert len(promotion_calls) == 0


class TestCanonicalRollback:
    """Verify pre-bootstrap failure rolls back client promotion."""

    def test_pre_bootstrap_fault_rolls_back_promotion(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        current = _active(tmp_path)
        result, staged, canonical = _prepare_canonical(monkeypatch, tmp_path, "m-canon-rollback")

        promotion_calls: list[str] = []
        rollback_calls: list[dict[str, object]] = []

        def mock_promote(**kwargs: object) -> dict[str, object]:
            promotion_calls.append("promote")
            return {"status": "COMMITTED", "client_plan_sha256": CANON_PLAN_SHA}

        def mock_rollback(**kwargs: object) -> dict[str, object] | None:
            rollback_calls.append(kwargs)
            return {"state": "ROLLED_BACK"}

        monkeypatch.setattr(maintenance, "promote_client_plan", mock_promote)
        monkeypatch.setattr(maintenance, "rollback_client_promotion", mock_rollback)

        candidate_doc = {
            "schema": "bdb-vnext-bootstrap-slot-manifest-v1", "slot": "ACTIVE",
            "bundle_root": str(tmp_path / "bundle"),
            "bundle_sha256": SHA, "bundle_role": "candidate",
            "source_commit": HEAD, "known_good": True,
            "bundle_id": "candidate", "manifest_sha256": SHA,
            "compatibility": {"supported_control_schema": {"min": 1, "max": 1}, "capabilities": ["canonical-admission-v1"]},
        }
        monkeypatch.setattr(maintenance, "_inspect", lambda *_args, **_kwargs: candidate_doc)
        monkeypatch.setattr(maintenance, "_publish", lambda *_args, **_kwargs: SHA)
        monkeypatch.setattr(maintenance, "_replace_state", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(maintenance, "_active_observation", lambda _authority: current)

        def fault(stage: str) -> None:
            if stage == "after_manifest_inspection":
                raise maintenance.MaintenanceFault(stage)

        with pytest.raises(maintenance.MaintenanceFault):
            maintenance.apply_post_active_maintenance(
                authority_root=tmp_path / "authority",
                maintenance_id="m-canon-rollback",
                expected_plan_sha256=result["plan"]["plan_sha256"],
                operator_approved=True,
                fault_hook=fault,
            )

        assert len(promotion_calls) == 1
        assert len(rollback_calls) == 1
        rb = rollback_calls[0]
        assert str(rb["production_runtime_root"]) == str(canonical)
        assert rb["verify_routes"] is False
        assert rb["staged_client_plan_sha256"] == STAGED_PLAN_SHA
