from __future__ import annotations

import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Any

import psutil

_IIS_WORKER_CACHE: tuple[float, dict[int, str]] = (0.0, {})


def windows_services() -> list[dict[str, str]]:
    if not hasattr(psutil, "win_service_iter"):
        return []
    result = []
    for service in psutil.win_service_iter():
        try:
            info = service.as_dict()
            result.append(
                {
                    "key": info["name"],
                    "name": info.get("display_name") or info["name"],
                    "status": info["status"],
                }
            )
        except (OSError, psutil.Error):
            continue
    return sorted(result, key=lambda item: item["name"].lower())


def iis_pools() -> list[dict[str, str]]:
    appcmd = shutil.which("appcmd.exe") or r"C:\Windows\System32\inetsrv\appcmd.exe"
    try:
        process = subprocess.run(
            [appcmd, "list", "apppool", "/xml"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if process.returncode or not process.stdout.strip():
        return []
    try:
        root = ET.fromstring(process.stdout.lstrip("\ufeff"))
    except ET.ParseError:
        return []
    pools = []
    for element in root.iter():
        name = element.attrib.get("APPPOOL.NAME") or element.attrib.get("name")
        if name:
            pools.append(
                {
                    "key": name,
                    "name": name,
                    "status": element.attrib.get("state", "unknown"),
                }
            )
    return sorted(pools, key=lambda item: item["name"].lower())


def disks() -> list[dict[str, Any]]:
    result = []
    for partition in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(partition.mountpoint)
        except OSError:
            continue
        result.append(
            {
                "key": partition.device,
                "name": partition.mountpoint,
                "status": f"{100 - usage.percent:.1f}% free",
                "free_mb": round(usage.free / 1024 / 1024),
                "free_percent": round(100 - usage.percent, 1),
            }
        )
    return result


def performance() -> dict[str, float]:
    memory = psutil.virtual_memory()
    return {"cpu": psutil.cpu_percent(interval=0.2), "memory": memory.percent}


def _service_pid_names() -> dict[int, str]:
    result: dict[int, str] = {}
    if not hasattr(psutil, "win_service_iter"):
        return result
    for service in psutil.win_service_iter():
        try:
            info = service.as_dict()
            pid = int(info.get("pid") or 0)
            if pid:
                result[pid] = info.get("display_name") or info.get("name") or str(pid)
        except (OSError, ValueError, psutil.Error):
            continue
    return result


def _iis_worker_pools() -> dict[int, str]:
    global _IIS_WORKER_CACHE
    cached_at, cached = _IIS_WORKER_CACHE
    if time.monotonic() - cached_at < 30:
        return cached
    appcmd = shutil.which("appcmd.exe") or r"C:\Windows\System32\inetsrv\appcmd.exe"
    try:
        process = subprocess.run(
            [appcmd, "list", "wp", "/xml"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        _IIS_WORKER_CACHE = (time.monotonic(), {})
        return {}
    if process.returncode or not process.stdout.strip():
        _IIS_WORKER_CACHE = (time.monotonic(), {})
        return {}
    try:
        root = ET.fromstring(process.stdout.lstrip("\ufeff"))
    except ET.ParseError:
        _IIS_WORKER_CACHE = (time.monotonic(), {})
        return {}
    result = {}
    for element in root.iter():
        pid_text = element.attrib.get("WP.NAME") or element.attrib.get("pid")
        pool = element.attrib.get("APPPOOL.NAME") or element.attrib.get("apppool.name")
        try:
            if pid_text and pool:
                result[int(pid_text)] = pool
        except ValueError:
            continue
    _IIS_WORKER_CACHE = (time.monotonic(), result)
    return result


def performance_consumers(limit: int = 15) -> list[dict[str, Any]]:
    """Return current high-resource processes with service and IIS ownership when known."""
    processes = []
    cpu_count = max(1, psutil.cpu_count(logical=True) or 1)
    for process in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            if process.pid == 0:
                continue
            process.cpu_percent(None)
            processes.append(process)
        except (psutil.Error, OSError):
            continue
    time.sleep(0.12)
    service_names = _service_pid_names()
    pool_names = _iis_worker_pools()
    rows = []
    for process in processes:
        try:
            pid = process.pid
            cpu = min(100.0, process.cpu_percent(None) / cpu_count)
            memory_mb = process.memory_info().rss / 1024 / 1024
            if pid in pool_names:
                kind, name = "IIS Pool", pool_names[pid]
            elif pid in service_names:
                kind, name = "Windows Service", service_names[pid]
            else:
                kind, name = "Process", process.name()
            rows.append(
                {
                    "kind": kind,
                    "name": name,
                    "pid": pid,
                    "cpu": round(cpu, 1),
                    "memory_mb": round(memory_mb, 1),
                }
            )
        except (psutil.Error, OSError):
            continue
    rows.sort(key=lambda row: (row["cpu"], row["memory_mb"]), reverse=True)
    return rows[: max(1, limit)]
