from __future__ import annotations

import sys

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from .licensing import current_license
from .logging_setup import configure_logging
from .paths import data_dir
from .registration import validate_operator
from .resources import resource_path
from .store import Store
from .ui import STYLE, ActivationDialog, MainWindow, SetupWizard


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MiMonitoringService")
    app.setOrganizationName("Millensys")
    instance_lock = QLockFile(str(data_dir() / "MiMonitoringService.gui.lock"))
    instance_lock.setStaleLockTime(30_000)
    if not instance_lock.tryLock(0) and (
        not instance_lock.removeStaleLockFile() or not instance_lock.tryLock(0)
    ):
        QMessageBox.information(
            None,
            "MiMonitoringService is already open",
            "Only one GUI instance can run at a time. Open the existing window from the system tray.",
        )
        return 0
    store = Store()
    config = store.get("configuration")
    configure_logging(config["agent"].get("debug", False))
    icon = resource_path("assets/mimonitoringservice-icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    app.setStyleSheet(STYLE)
    if not current_license().valid:
        if ActivationDialog(store).exec() != ActivationDialog.DialogCode.Accepted:
            return 0
        if not current_license().valid:
            return 0
        config = store.get("configuration")
    operator = validate_operator(
        config.get("client", {}).get("operator_email", ""),
        config.get("registration", {}),
    )
    if not config.get("setup_complete") or not operator.valid:
        SetupWizard(store).exec()
        completed = store.get("configuration")
        operator = validate_operator(
            completed.get("client", {}).get("operator_email", ""),
            completed.get("registration", {}),
        )
        if not completed.get("setup_complete") or not operator.valid:
            return 0
    window = MainWindow(store)
    if config["agent"].get("start_minimized", False) and window.tray_available:
        window.hide()
        window.tray_icon.showMessage(
            "MiMonitoringService",
            "The monitoring console is running in the system tray.",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
