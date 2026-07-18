from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Sequence


REPORT_SCHEMA = "bdb-gui-technology-spike-v1"
CANDIDATE_ID = "pyside6-qt-widgets"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PySide6 technology probe for BDB Control Center")
    parser.add_argument(
        "--headless-smoke",
        action="store_true",
        help="Use Qt's offscreen platform and close the event loop automatically",
    )
    parser.add_argument("--json-out", help="Optional path for the JSON report")
    return parser


def _load_qt() -> dict[str, Any]:
    try:
        import PySide6
        from PySide6.QtCore import QLibraryInfo, QTimer, Qt, qVersion
        from PySide6.QtWidgets import (
            QApplication,
            QFormLayout,
            QGroupBox,
            QLabel,
            QMainWindow,
            QPushButton,
            QSystemTrayIcon,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as error:
        raise RuntimeError(
            'PySide6 is not installed. Install the optional extra with: python -m pip install -e ".[gui-spike]"'
        ) from error
    return {
        "PySide6": PySide6,
        "QLibraryInfo": QLibraryInfo,
        "QTimer": QTimer,
        "Qt": Qt,
        "qVersion": qVersion,
        "QApplication": QApplication,
        "QFormLayout": QFormLayout,
        "QGroupBox": QGroupBox,
        "QLabel": QLabel,
        "QMainWindow": QMainWindow,
        "QPushButton": QPushButton,
        "QSystemTrayIcon": QSystemTrayIcon,
        "QVBoxLayout": QVBoxLayout,
        "QWidget": QWidget,
    }


def _operator_capabilities() -> dict[str, Any]:
    from bdb_operator import OperatorApi

    response = OperatorApi().capabilities()
    if not response.ok:
        raise RuntimeError("Operator API capabilities unexpectedly failed")
    return response.to_dict()


def _build_window(qt: dict[str, Any], capabilities: dict[str, Any]) -> Any:
    QMainWindow = qt["QMainWindow"]
    QWidget = qt["QWidget"]
    QVBoxLayout = qt["QVBoxLayout"]
    QGroupBox = qt["QGroupBox"]
    QFormLayout = qt["QFormLayout"]
    QLabel = qt["QLabel"]
    QPushButton = qt["QPushButton"]
    Qt = qt["Qt"]

    window = QMainWindow()
    window.setObjectName("BdbGuiTechnologyProbe")
    window.setWindowTitle("BDB Control Center — PySide6 Technology Probe")
    window.resize(720, 460)

    root = QWidget(window)
    layout = QVBoxLayout(root)

    heading = QLabel("BDB Control Center")
    heading.setObjectName("ProbeHeading")
    heading.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    heading.setStyleSheet("font-size: 24px; font-weight: 600;")
    layout.addWidget(heading)

    mode = QLabel("P05 TECHNOLOGY SPIKE · READ-ONLY")
    mode.setObjectName("ProbeMode")
    mode.setStyleSheet("color: #6b7280; font-weight: 600;")
    layout.addWidget(mode)

    group = QGroupBox("Architecture contract")
    form = QFormLayout(group)
    data = capabilities["data"]
    form.addRow("GUI candidate", QLabel(CANDIDATE_ID))
    form.addRow("Operator schema", QLabel(capabilities["schema"]))
    form.addRow("Transport", QLabel(str(data["transport"])))
    form.addRow("Network listener", QLabel(str(data["network_listener"]).lower()))
    form.addRow("Arbitrary shell", QLabel(str(data["arbitrary_shell"]).lower()))
    form.addRow("Journal access", QLabel(str(data.get("journal_access", "not-reported"))))
    layout.addWidget(group)

    note = QLabel(
        "Probe constructs the window and reads OperatorApi.capabilities(). "
        "It has no workspace path and cannot call Start, Stop, re-arm, or repository mutations."
    )
    note.setWordWrap(True)
    note.setObjectName("ProbeSafetyNote")
    layout.addWidget(note)

    close_button = QPushButton("Close probe")
    close_button.setObjectName("ProbeCloseButton")
    close_button.clicked.connect(window.close)
    layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)

    window.setCentralWidget(root)
    return window


def run_probe(*, headless_smoke: bool) -> dict[str, Any]:
    if headless_smoke:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    qt = _load_qt()
    QApplication = qt["QApplication"]
    QSystemTrayIcon = qt["QSystemTrayIcon"]
    QTimer = qt["QTimer"]
    QLibraryInfo = qt["QLibraryInfo"]

    application = QApplication.instance() or QApplication(["bdb-gui-technology-probe"])
    application.setApplicationName("BDB Control Center Technology Probe")
    application.setOrganizationName("Bartosz Dev Bridge")

    capabilities = _operator_capabilities()
    window = _build_window(qt, capabilities)
    if headless_smoke:
        window.show()
        application.processEvents()
        QTimer.singleShot(0, application.quit)
        exit_code = int(application.exec())
    else:
        window.show()
        exit_code = int(application.exec())

    screen = application.primaryScreen()
    report = {
        "schema": REPORT_SCHEMA,
        "candidate": CANDIDATE_ID,
        "headless_smoke": headless_smoke,
        "event_loop_exit_code": exit_code,
        "window_constructed": window.objectName() == "BdbGuiTechnologyProbe",
        "operator_response_schema": capabilities["schema"],
        "operator_network_listener": capabilities["data"]["network_listener"],
        "operator_arbitrary_shell": capabilities["data"]["arbitrary_shell"],
        "mutation_operations_invoked": 0,
        "pyside_version": qt["PySide6"].__version__,
        "qt_version": qt["qVersion"](),
        "qt_library_path": QLibraryInfo.path(QLibraryInfo.LibraryPath.LibrariesPath),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "qt_platform": os.environ.get("QT_QPA_PLATFORM") or "native",
        "primary_screen_available": screen is not None,
        "device_pixel_ratio": float(screen.devicePixelRatio()) if screen is not None else None,
        "system_tray_available": bool(QSystemTrayIcon.isSystemTrayAvailable()),
    }
    window.deleteLater()
    application.processEvents()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_probe(headless_smoke=args.headless_smoke)
    except Exception as error:
        report = {
            "schema": REPORT_SCHEMA,
            "candidate": CANDIDATE_ID,
            "status": "failed",
            "error_type": type(error).__name__,
            "error": str(error),
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 1

    report["status"] = "success"
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    print(rendered)
    if args.json_out:
        output = Path(args.json_out).expanduser().resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
