from __future__ import annotations

from typing import Any

from .edit_operation_models import StructuralEditSpec
from .multi_file_patch_models import FileReplacementSpec
from .multi_file_patch_parser import parse_multi_file_patch
from .protocol import BridgeError, path_matches, require_string, validate_repo_relative_path


_INSTALLED = False


def _require_allowed(path: str, patterns: tuple[str, ...] | list[str]) -> None:
    if not path_matches(path, patterns):
        raise BridgeError(
            "policy_denied",
            f"Path is not allowed by local policy: {path}",
        )


def _preflight_multi_file_patch(action: dict[str, Any], patterns: tuple[str, ...] | list[str]) -> None:
    payload = action.get("payload")
    if not isinstance(payload, dict):
        raise BridgeError("invalid_payload", "Action payload must be an object")
    patch_document = payload.get("patch")
    if not isinstance(patch_document, dict):
        raise BridgeError("invalid_payload", "multi_file_patch payload.patch must be an object")
    patch = parse_multi_file_patch(patch_document)
    for operation in patch.operations:
        if isinstance(operation, FileReplacementSpec):
            _require_allowed(operation.path, patterns)
            continue
        if not isinstance(operation, StructuralEditSpec):
            raise BridgeError("invalid_payload", "Unsupported multi-file patch operation")
        if operation.source_path is not None:
            _require_allowed(operation.source_path, patterns)
        if operation.destination_path is not None:
            _require_allowed(operation.destination_path, patterns)


def _preflight_replace_exact(action: dict[str, Any], patterns: tuple[str, ...] | list[str]) -> None:
    payload = action.get("payload")
    if not isinstance(payload, dict):
        raise BridgeError("invalid_payload", "Action payload must be an object")
    path = validate_repo_relative_path(require_string(payload, "path"))
    _require_allowed(path, patterns)


def install_native_action_preflight(composer_type: type[Any]) -> None:
    """Validate mutating payload integrity and scope before binding a native session."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    original_compose = composer_type.compose

    def compose_with_preflight(self: Any, action: dict[str, Any]):
        if not isinstance(action, dict):
            return original_compose(self, action)
        operation = action.get("operation")
        if operation not in {"multi_file_patch", "replace_exact_and_test"}:
            return original_compose(self, action)
        repo_alias = require_string(action, "repo_alias")
        repository = self._repository(repo_alias)
        patterns = repository.bridge_config.allowed_paths
        if operation == "multi_file_patch":
            _preflight_multi_file_patch(action, patterns)
        else:
            _preflight_replace_exact(action, patterns)
        return original_compose(self, action)

    composer_type.compose = compose_with_preflight
