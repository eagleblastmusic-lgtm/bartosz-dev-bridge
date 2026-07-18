from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .diagnostics import DiagnosticsExportResult


GUI_DIAGNOSTICS_EXPORT_OUTCOME_SCHEMA = "bdb-gui-diagnostics-export-outcome-v1"


@dataclass(frozen=True)
class DiagnosticsExportOutcome:
    ok: bool
    result: DiagnosticsExportResult | None = None
    error_code: str | None = None
    error_message: str | None = None
    schema: str = GUI_DIAGNOSTICS_EXPORT_OUTCOME_SCHEMA

    @classmethod
    def success(cls, result: DiagnosticsExportResult) -> "DiagnosticsExportOutcome":
        return cls(ok=True, result=result)

    @classmethod
    def failure(cls, code: str, message: str) -> "DiagnosticsExportOutcome":
        return cls(ok=False, error_code=code, error_message=message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "ok": self.ok,
            "result": self.result.to_dict() if self.result is not None else None,
            "error": (
                None
                if self.ok
                else {"code": self.error_code, "message": self.error_message}
            ),
        }
