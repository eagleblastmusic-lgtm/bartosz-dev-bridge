from __future__ import annotations

from typing import Any


_INSTALLED = False


def install_session_history_diagnostics(widget_type: type) -> None:
    """Render exact bounded terminal diagnostics in the visible result column."""

    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    original_render_rows = widget_type._render_rows

    def render_rows_with_diagnostics(self: Any) -> None:
        original_render_rows(self)
        for row, session in enumerate(self._sessions):
            latest = session.latest_attempt
            if latest is None or not latest.error_code:
                continue
            if latest.result_status in {None, "success", "pending", "accepted", "running"}:
                continue
            item = self.table.item(row, 4)
            if item is None:
                continue
            diagnostic = " ".join(latest.error_code.split())[:220]
            item.setText(f"{latest.result_status} · {diagnostic}")
            item.setToolTip(diagnostic)

    widget_type._render_rows = render_rows_with_diagnostics
