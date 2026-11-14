from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

SERVICE_NAME = "MiMonitoringService"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(args: list[str], timeout: int = 45) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
        return result.returncode == 0, (result.stdout or result.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def status() -> str:
    ok, output = _run(["sc.exe", "query", SERVICE_NAME])
    if not ok:
        return "Not installed"
    for state in ("RUNNING", "STOPPED", "START_PENDING", "STOP_PENDING", "PAUSED"):
        if state in output:
            return state.replace("_", " ").title()
    return "Unknown"


def _packaged_service() -> Path | None:
    candidate = Path(sys.executable).with_name("MiMonitoringService.Service.exe")
    return candidate if getattr(sys, "frozen", False) and candidate.exists() else None


def _run_elevated(executable: Path, arguments: list[str], timeout: int = 60) -> tuple[bool, str]:
    """Run the signed service helper with UAC and wait without opening a console."""
    try:
        import win32api
        import win32con
        import win32event
        import win32process
        from win32com.shell import shell, shellcon

        result = shell.ShellExecuteEx(
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS,
            lpVerb="runas",
            lpFile=str(executable),
            lpParameters=subprocess.list2cmdline(arguments),
            nShow=win32con.SW_HIDE,
        )
        process = result["hProcess"]
        wait = win32event.WaitForSingleObject(process, timeout * 1000)
        if wait == win32event.WAIT_TIMEOUT:
            return False, f"Elevated service action exceeded {timeout} seconds."
        exit_code = win32process.GetExitCodeProcess(process)
        win32api.CloseHandle(process)
        return (
            exit_code == 0,
            "Service action completed."
            if exit_code == 0
            else f"Service helper exited with code {exit_code}.",
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"Administrator approval was cancelled or failed: {exc}"


def manage(action: str) -> tuple[bool, str]:
    packaged = _packaged_service()
    if action == "start":
        if packaged:
            return _run_elevated(packaged, ["start"])
        return _run(["sc.exe", "start", SERVICE_NAME])
    if action == "stop":
        if packaged:
            return _run_elevated(packaged, ["stop"])
        return _run(["sc.exe", "stop", SERVICE_NAME])
    if action == "remove":
        if status() == "Running":
            stop_ok, stop_message = (
                _run_elevated(packaged, ["stop"])
                if packaged
                else _run(["sc.exe", "stop", SERVICE_NAME])
            )
            if not stop_ok:
                return False, f"Service removal cancelled because stop failed: {stop_message}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and status() not in {"Stopped", "Not installed"}:
                time.sleep(0.5)
        if status() not in {"Stopped", "Not installed"}:
            return False, "Service did not stop within 30 seconds; removal was cancelled."
        if packaged:
            return _run_elevated(packaged, ["remove"])
        return _run(["sc.exe", "delete", SERVICE_NAME])
    if action == "install":
        if not packaged:
            return (
                False,
                "Install the packaged service executable beside the GUI first. Development installation: python -m millensys_guardian.service_host install",
            )
        return _run_elevated(packaged, ["--startup", "auto", "install"])
    return False, "Unsupported service management action"
