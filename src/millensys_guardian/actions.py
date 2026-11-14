from __future__ import annotations

import shutil
import subprocess
import time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(command: list[str], timeout: int = 45) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    message = (result.stdout or result.stderr).strip()
    return result.returncode == 0, message


def service_action(name: str, action: str) -> tuple[bool, str]:
    if action not in {"start", "stop"}:
        return False, "Unsupported service action"
    return _run(["sc.exe", action, name])


def service_state(name: str) -> str:
    ok, output = _run(["sc.exe", "query", name], timeout=15)
    if not ok:
        return "unknown"
    for state in ("RUNNING", "STOPPED", "START_PENDING", "STOP_PENDING", "PAUSED"):
        if state in output:
            return state.lower()
    return "unknown"


def wait_for_service(name: str, expected: str, timeout: int = 45) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if service_state(name) == expected:
            return True
        time.sleep(0.5)
    return False


def restart_service(name: str, related_services: list[str] | None = None) -> tuple[bool, str]:
    """Restart a service while safely quiescing and restoring configured related services."""
    related = list(dict.fromkeys(item for item in (related_services or []) if item != name))
    running_related = [item for item in related if service_state(item) == "running"]
    stopped_related: list[str] = []
    messages: list[str] = []
    try:
        for item in reversed(running_related):
            ok, detail = service_action(item, "stop")
            if not ok or not wait_for_service(item, "stopped"):
                return False, f"Could not stop related service {item}: {detail}"
            stopped_related.append(item)
            messages.append(f"stopped related {item}")

        main_state = service_state(name)
        if main_state in {"running", "paused", "start_pending"}:
            ok, detail = service_action(name, "stop")
            if not ok or not wait_for_service(name, "stopped"):
                return False, f"Could not stop main service {name}: {detail}"
            messages.append(f"stopped main {name}")
        elif main_state == "unknown":
            return False, f"Could not determine the current state of main service {name}"
        elif main_state != "stopped":
            return False, f"Main service {name} is in unsupported state {main_state}"

        ok, detail = service_action(name, "start")
        if not ok or not wait_for_service(name, "running"):
            return False, f"Could not start main service {name}: {detail}"
        messages.append(f"started main {name}")
    finally:
        for item in reversed(stopped_related):
            ok, detail = service_action(item, "start")
            if ok and wait_for_service(item, "running"):
                messages.append(f"restored related {item}")
            else:
                messages.append(f"FAILED to restore related {item}: {detail}")
    failed_restore = next((message for message in messages if message.startswith("FAILED")), None)
    return (False, "; ".join(messages)) if failed_restore else (True, "; ".join(messages))


def pool_action(name: str, action: str) -> tuple[bool, str]:
    if action not in {"start", "stop", "recycle"}:
        return False, "Unsupported IIS action"
    appcmd = shutil.which("appcmd.exe") or r"C:\Windows\System32\inetsrv\appcmd.exe"
    verb = "recycle" if action == "recycle" else action
    return _run([appcmd, verb, "apppool", f"/apppool.name:{name}"])
