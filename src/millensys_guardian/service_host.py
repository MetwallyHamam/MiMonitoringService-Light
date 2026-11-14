from __future__ import annotations

import sys
import threading

import servicemanager
import win32event
import win32service
import win32serviceutil

from .engine import MonitorEngine
from .licensing import current_license
from .logging_setup import configure_logging
from .store import Store


class GuardianService(win32serviceutil.ServiceFramework):
    _svc_name_ = "MiMonitoringService"
    _svc_display_name_ = "MiMonitoringService"
    _svc_description_ = (
        "Monitors selected Windows Services, IIS pools, disks, and server performance."
    )

    def __init__(self, args: list[str]) -> None:
        super().__init__(args)
        self.stop_handle = win32event.CreateEvent(None, 0, 0, None)
        self.stop_event = threading.Event()

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_event.set()
        win32event.SetEvent(self.stop_handle)

    def SvcDoRun(self) -> None:
        store = Store()
        configure_logging(store.get("configuration")["agent"].get("debug", False))
        license_status = current_license()
        if not license_status.valid:
            store.add_event("critical", "license", "activation", license_status.message)
            servicemanager.LogErrorMsg(
                f"MiMonitoringService cannot start monitoring: {license_status.message}"
            )
            return
        servicemanager.LogInfoMsg("MiMonitoringService started")
        MonitorEngine(store, self.stop_event).run_forever()


def main() -> None:
    # pywin32's handler stops and waits before removal when invoked with `remove`.
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(GuardianService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(GuardianService)


if __name__ == "__main__":
    main()
