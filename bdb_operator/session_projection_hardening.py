from __future__ import annotations

from typing import Any

from . import session_projection as _projection


_INSTALLED = False


def install_session_projection_diagnostics() -> None:
    """Expose bounded terminal detail through the existing attempt projection."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    original = _projection._result_summary

    def result_summary_with_terminal_detail(document: dict[str, Any] | None, row: Any) -> dict[str, Any]:
        summary = original(document, row)
        if not isinstance(document, dict):
            return summary
        data = document.get("data")
        if not isinstance(data, dict):
            return summary
        detail = data.get("terminal_detail")
        diagnostic_code = data.get("terminal_error_code")
        if not isinstance(detail, str) or not detail.strip():
            return summary
        bounded_detail = " ".join(detail.split())[:500]
        code = diagnostic_code if isinstance(diagnostic_code, str) and diagnostic_code else summary.get("error_code")
        summary["error_code"] = f"{code} — {bounded_detail}" if code else bounded_detail
        summary["terminal_detail"] = bounded_detail
        summary["summary"] = (
            " ".join(str(document.get("summary", "")).split())[:500]
            or bounded_detail
        )
        return summary

    _projection._result_summary = result_summary_with_terminal_detail
