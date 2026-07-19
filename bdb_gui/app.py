from __future__ import annotations

import argparse
import json
import os
import platform
from pathlib import Path
from typing import Any, Sequence


SMOKE_SCHEMA = "bdb-control-center-smoke-v1"


def _default_workspaces_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "BartoszDevBridge" / "workspaces"
    return Path.home() / ".local" / "share" / "BartoszDevBridge" / "workspaces"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BDB Control Center")
    parser.add_argument(
        "--workspaces-root",
        default=str(_default_workspaces_root()),
        help="Directory containing prepared BDB workspace folders",
    )
    parser.add_argument(
        "--headless-smoke",
        action="store_true",
        help="Run the production GUI shell through an offscreen bootstrap and exit",
    )
    parser.add_argument("--json-out", help="Optional path for the headless smoke report")
    parser.add_argument("--smoke-timeout-ms", type=int, default=15_000)
    return parser


def _render_report(report: dict[str, Any], output_path: str | None) -> None:
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    print(rendered)
    if output_path:
        path = Path(output_path).expanduser().resolve(strict=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1_000 <= args.smoke_timeout_ms <= 120_000:
        report = {
            "schema": SMOKE_SCHEMA,
            "status": "failed",
            "error_code": "invalid_smoke_timeout",
            "error": "smoke-timeout-ms must be between 1000 and 120000",
        }
        _render_report(report, args.json_out)
        return 2

    if args.headless_smoke:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    try:
        import PySide6
        from PySide6.QtCore import QTimer, qVersion
        from PySide6.QtWidgets import QApplication

        from .bootstrap import BootstrapService
        from .operations import ProjectOperationsService
        from .project_window import ProjectControlCenterWindow
        from .tray import TrayController
        from .tray_window import TrayProjectControlCenterWindow
    except ImportError as error:
        report = {
            "schema": SMOKE_SCHEMA,
            "status": "failed",
            "error_code": "pyside6_missing",
            "error": str(error),
            "install_hint": 'python -m pip install -e ".[gui]"',
        }
        _render_report(report, args.json_out)
        return 2

    workspaces_root = str(Path(args.workspaces_root).expanduser().resolve(strict=False))
    application = QApplication.instance() or QApplication(["bdb-control-center"])
    application.setApplicationName("BDB Control Center")
    application.setOrganizationName("Bartosz Dev Bridge")
    application.setQuitOnLastWindowClosed(args.headless_smoke)

    tray_controller: TrayController | None = None
    if args.headless_smoke:
        # Smoke never creates a tray icon and never changes close semantics.
        window = ProjectControlCenterWindow(
            bootstrap_service=BootstrapService(),
            operations_service=ProjectOperationsService(),
            workspaces_root=workspaces_root,
            auto_load_status=not args.headless_smoke,
        )
    else:
        window = TrayProjectControlCenterWindow(
            bootstrap_service=BootstrapService(),
            operations_service=ProjectOperationsService(),
            workspaces_root=workspaces_root,
            auto_load_status=not args.headless_smoke,
        )
        tray_controller = TrayController(application, window)
        window.install_tray_controller(tray_controller)
        tray_controller.start()

    report: dict[str, Any] = {}
    timed_out = False

    def finish_smoke() -> None:
        if not args.headless_smoke or report:
            return
        report.update(window.smoke_report())
        report.update(
            {
                "schema": SMOKE_SCHEMA,
                "status": "success" if report["bootstrap_ok"] else "failed",
                "workspaces_root": workspaces_root,
                "qt_version": qVersion(),
                "pyside_version": PySide6.__version__,
                "python_version": platform.python_version(),
                "qt_platform": os.environ.get("QT_QPA_PLATFORM") or "native",
                "tray_created": False,
            }
        )
        window.close()
        QTimer.singleShot(0, application.quit)

    def fail_timeout() -> None:
        nonlocal timed_out
        if not args.headless_smoke or report:
            return
        timed_out = True
        report.update(
            {
                "schema": SMOKE_SCHEMA,
                "status": "failed",
                "error_code": "bootstrap_timeout",
                "workspaces_root": workspaces_root,
                "bootstrap_completed": False,
                "mutation_operations_invoked": 0,
                "tray_created": False,
            }
        )
        window.close()
        application.quit()

    if args.headless_smoke:
        window.dashboard_ready.connect(finish_smoke)
        QTimer.singleShot(args.smoke_timeout_ms, fail_timeout)

    window.show()
    window.start_bootstrap()
    exit_code = int(application.exec())

    if args.headless_smoke:
        report.setdefault("schema", SMOKE_SCHEMA)
        report.setdefault("status", "failed")
        report["event_loop_exit_code"] = exit_code
        report["timed_out"] = timed_out
        _render_report(report, args.json_out)
        return 0 if report.get("status") == "success" and exit_code == 0 else 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
