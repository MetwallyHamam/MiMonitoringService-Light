from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, time
from html import escape
from pathlib import Path
from typing import Any

import psutil

from .notifier import Notifier, branded_html
from .scheduler import configured_timezone
from .store import Store


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def file_snapshot(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {"exists": False, "path": str(path)}
        stat = path.stat()
    except OSError as exc:
        return {"exists": False, "path": str(path), "access_error": str(exc)}
    result: dict[str, Any] = {
        "exists": True,
        "path": str(path.resolve()),
        "name": path.name,
        "extension": path.suffix.lower(),
        "size": stat.st_size,
        "created_utc": datetime.fromtimestamp(stat.st_ctime, UTC).isoformat(timespec="seconds"),
        "modified_utc": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
    }
    if path.is_file():
        try:
            result["sha256"] = _sha256(path)
        except OSError as exc:
            result["access_error"] = str(exc)
    return result


def tracked_file_snapshot(path: Path, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    snapshot = file_snapshot(path)
    if snapshot["exists"] or not baseline or not baseline.get("sha256"):
        return snapshot
    parent = path.parent
    try:
        candidates = [
            item
            for item in parent.iterdir()
            if item.is_file() and item.stat().st_size == baseline.get("size")
        ][:500]
    except OSError:
        return snapshot
    for candidate in candidates:
        try:
            if _sha256(candidate) == baseline["sha256"]:
                renamed = file_snapshot(candidate)
                renamed["original_path"] = str(path)
                renamed["renamed"] = True
                return renamed
        except OSError:
            continue
    return snapshot


def _executable_path(command: str) -> Path | None:
    if not command:
        return None
    try:
        first = shlex.split(command, posix=False)[0].strip('"')
    except ValueError:
        first = command.split()[0].strip('"')
    expanded = os.path.expandvars(first)
    return Path(expanded) if expanded else None


def _file_version(path: Path) -> dict[str, str]:
    try:
        import win32api

        info = win32api.GetFileVersionInfo(str(path), "\\VarFileInfo\\Translation")
        language, codepage = info[0]
        prefix = f"\\StringFileInfo\\{language:04X}{codepage:04X}\\"
        return {
            key.lower(): str(win32api.GetFileVersionInfo(str(path), prefix + key) or "")
            for key in ("CompanyName", "FileDescription", "FileVersion", "ProductVersion")
        }
    except Exception:  # noqa: BLE001
        return {}


def service_snapshot(name: str) -> dict[str, Any]:
    try:
        info = psutil.win_service_get(name).as_dict()
    except (AttributeError, OSError, psutil.Error):
        return {"exists": False, "service_name": name}
    command = str(info.get("binpath") or "")
    executable = _executable_path(command)
    result: dict[str, Any] = {
        "exists": True,
        "service_name": name,
        "display_name": info.get("display_name") or "",
        "binary_path": command,
        "start_type": info.get("start_type") or "",
        "username": info.get("username") or "",
        "description": info.get("description") or "",
    }
    if executable and executable.exists():
        file_info = file_snapshot(executable)
        result.update({f"executable_{key}": value for key, value in file_info.items()})
        result.update({f"version_{key}": value for key, value in _file_version(executable).items()})
    return result


def _appcmd(*arguments: str) -> str:
    executable = shutil.which("appcmd.exe") or r"C:\Windows\System32\inetsrv\appcmd.exe"
    try:
        process = subprocess.run(
            [executable, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return process.stdout.lstrip("\ufeff") if process.returncode == 0 else ""


def _flatten_xml(xml: str, prefix: str) -> dict[str, str]:
    if not xml.strip():
        return {"exists": "false"}
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return {"exists": "false"}
    result = {"exists": "true"}

    def sensitive(value: str) -> bool:
        lowered = value.lower()
        return any(token in lowered for token in ("password", "secret", "token", "credential"))

    def walk(element: ET.Element, path: str) -> None:
        current = f"{path}/{element.tag.split('}')[-1]}"
        for key, value in sorted(element.attrib.items()):
            if key.lower() not in {"state", "wp.name"} and not sensitive(key):
                result[f"{current}/@{key}"] = str(value)
        text_value = (element.text or "").strip()
        if text_value and not sensitive(current):
            result[current] = text_value
        for child in element:
            walk(child, current)

    walk(root, prefix)
    return result


def iis_pool_snapshot(name: str) -> dict[str, Any]:
    result: dict[str, Any] = _flatten_xml(
        _appcmd("list", "apppool", name, "/config:*", "/xml"), "pool"
    )
    applications = _appcmd("list", "app", "/xml")
    if applications.strip():
        try:
            root = ET.fromstring(applications)
            for app in root.iter():
                pool = app.attrib.get("APPPOOL.NAME") or app.attrib.get("applicationPool")
                app_name = app.attrib.get("APP.NAME") or app.attrib.get("path")
                if pool != name or not app_name:
                    continue
                for key, value in sorted(app.attrib.items()):
                    result[f"application/{app_name}/@{key}"] = str(value)
                vdirs = _appcmd("list", "vdir", f"/app.name:{app_name}", "/xml")
                result.update(_flatten_xml(vdirs, f"application/{app_name}/virtualDirectories"))
        except ET.ParseError:
            result["applications/error"] = "Cannot parse IIS application inventory"
    return result


def evaluate_backup(rule: dict[str, Any], now: datetime) -> dict[str, Any]:
    source = Path(os.path.expandvars(str(rule.get("path", ""))))
    pattern = str(rule.get("pattern") or "*.bak")
    keyword = str(rule.get("keyword") or "").lower()
    try:
        candidates = [source] if source.is_file() else list(source.glob(pattern))
        candidates = [
            item
            for item in candidates
            if item.is_file() and (not keyword or keyword in item.name.lower())
        ]
    except OSError as exc:
        return {"ok": False, "reason": f"Cannot access backup path: {exc}"}
    if not candidates:
        return {"ok": False, "reason": f"No backup matches {pattern} in {source}"}
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    modified = datetime.fromtimestamp(latest.stat().st_mtime, now.tzinfo)
    age_days = (now.date() - modified.date()).days
    allowed = max(0, int(rule.get("max_age_days", 0)))
    ok = 0 <= age_days <= allowed
    return {
        "ok": ok,
        "reason": (
            f"Latest backup {latest.name} is {age_days} day(s) old"
            if ok
            else f"Latest backup {latest.name} is {age_days} day(s) old; maximum is {allowed}"
        ),
        "file": str(latest),
        "age_days": age_days,
        "modified": modified.isoformat(timespec="seconds"),
        "size": latest.stat().st_size,
    }


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[str, str, str]]:
    def display(value: Any) -> str:
        if value is None:
            return "Not present"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value)

    changes = []
    for key in sorted(set(old) | set(new)):
        before = display(old.get(key))
        after = display(new.get(key))
        if before != after:
            changes.append((key, before, after))
    return changes


def friendly_property_name(value: str) -> str:
    leaf = value.rsplit("/", 1)[-1].removeprefix("@")
    names = {
        "enable32BitAppOnWin64": "Enable 32-Bit Applications",
        "managedRuntimeVersion": ".NET CLR Version",
        "RuntimeVersion": ".NET CLR Version",
        "managedPipelineMode": "Managed Pipeline Mode",
        "PipelineMode": "Managed Pipeline Mode",
        "queueLength": "Queue Length",
        "startMode": "Start Mode",
        "physicalPath": "Application Physical Path",
        "applicationPool": "Assigned Application Pool",
        "identityType": "Process Identity",
        "idleTimeout": "Idle Time-out",
        "idleTimeoutAction": "Idle Time-out Action",
        "rapidFailProtection": "Rapid-Fail Protection",
        "sha256": "Executable SHA-256",
        "extension": "File Extension",
        "modified_utc": "Modified Time",
        "created_utc": "Created Time",
        "autoStart": "Auto Start",
        "enableConfigurationOverride": "Enable Configuration Override",
        "enableEmulationOnWinArm64": "Enable ARM64 Emulation",
        "managedRuntimeLoader": "Managed Runtime Loader",
        "CLRConfigFile": "CLR Configuration File",
        "APP.NAME": "Application Name",
        "APPPOOL.NAME": "Assigned Application Pool",
        "SITE.NAME": "Site Name",
        "VDIR.NAME": "Virtual Directory Name",
        "exists": "Component Exists",
    }
    friendly = names.get(leaf)
    if not friendly:
        friendly = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", leaf).replace("_", " ").title()
    if value.startswith("application/"):
        application = value.split("/virtualDirectories", 1)[0].removeprefix("application/")
        if leaf == "path":
            friendly = (
                "Virtual Directory Path" if "/virtualDirectories/" in value else "Application Path"
            )
        return f"{application} · {friendly}"
    for marker, section in (
        ("/cpu/", "CPU"),
        ("/failure/", "Failure Protection"),
        ("/processModel/", "Process Model"),
        ("/recycling/", "Recycling"),
    ):
        if marker in value:
            return f"{section} · {friendly}"
    return friendly


class AssuranceMonitor:
    def __init__(self, store: Store, notifier: Notifier) -> None:
        self.store = store
        self.notifier = notifier

    def process(self, config: dict[str, Any]) -> None:
        # Lightweight file/folder backup assurance is independent of SQL credentials.
        self._process_backups(config)
        interval = max(60, int(config.get("assurance", {}).get("snapshot_interval_seconds", 300)))
        runtime = self.store.get("assurance_runtime", {})
        if datetime.now(UTC).timestamp() - float(runtime.get("last_snapshot", 0)) < interval:
            return
        self._process_snapshots(config)
        runtime["last_snapshot"] = datetime.now(UTC).timestamp()
        self.store.set("assurance_runtime", runtime)

    def _process_backups(self, config: dict[str, Any]) -> None:
        timezone = configured_timezone(config.get("agent", {}).get("timezone"))
        now = datetime.now(timezone)
        runtime = self.store.get("backup_runtime", {})
        for rule in config.get("backups", []):
            if not rule.get("enabled", True):
                continue
            rule_id = str(rule.get("id") or rule.get("name") or rule.get("path"))
            try:
                check_time = time.fromisoformat(str(rule.get("check_time", "06:00")))
            except ValueError:
                check_time = time(6, 0)
            if now.time() < check_time or runtime.get(rule_id, {}).get("date") == now.date().isoformat():
                continue
            self.check_backup_rule(rule, now=now, runtime=runtime)
        self.store.set("backup_runtime", runtime)

    def check_backup_rule(
        self,
        rule: dict[str, Any],
        *,
        now: datetime | None = None,
        runtime: dict[str, Any] | None = None,
        manual: bool = False,
    ) -> dict[str, Any]:
        """Evaluate and persist one backup rule through the same compliance workflow."""
        if now is None:
            config = self.store.get("configuration", {})
            now = datetime.now(configured_timezone(config.get("agent", {}).get("timezone")))
        rule_id = str(rule.get("id") or rule.get("name") or rule.get("path"))
        name = str(rule.get("name") or rule.get("path"))
        result = evaluate_backup(rule, now)
        runtime_state = runtime if runtime is not None else self.store.get("backup_runtime", {})
        runtime_state[rule_id] = {
            "date": now.date().isoformat(),
            "checked_at": now.isoformat(timespec="seconds"),
            "result": result,
        }
        self.store.set("backup_runtime", runtime_state)
        self.store.add_backup_sample(rule_id, name, result)
        incident_key = f"backup:{rule_id}"
        if result["ok"]:
            recovered = self.store.resolve_score_incident_by_key(incident_key, "Valid backup found")
            if rule.get("notify_found", False) or (recovered and rule.get("notify_recovery", True)):
                self._email(f"[BACKUP OK] {name}", name, result["reason"], "recovery")
        else:
            self.store.open_score_incident(incident_key, "backup", "backup", name, "backup_missing", result["reason"], 2)
            if rule.get("notify_missing", True):
                self._email(f"[BACKUP MISSING] {name}", name, result["reason"], "critical")
        self.store.add_event(
            "info" if result["ok"] else "error",
            "backup",
            name,
            f"{'Manual' if manual else 'Scheduled'} backup-file check: {result['reason']}",
            path=rule.get("path"),
        )
        return result

    def _process_snapshots(self, config: dict[str, Any]) -> None:
        baselines = self.store.get("component_baselines", {})
        current = self.store.get("component_current_snapshots", {})
        items: list[tuple[str, str, str, dict[str, Any]]] = []
        for rule in config.get("file_monitors", []):
            if rule.get("enabled", True):
                key = f"file:{rule.get('id') or rule.get('path')}"
                items.append(
                    (
                        key,
                        "File",
                        str(rule.get("name") or rule.get("path")),
                        tracked_file_snapshot(
                            Path(str(rule.get("path", ""))), baselines.get(key)
                        ),
                    )
                )
        for row in self.store.targets("service"):
            if row["enabled"]:
                key = f"service:{row['target_key']}"
                items.append((key, "Windows Service", str(row["display_name"]), service_snapshot(str(row["target_key"]))))
        for row in self.store.targets("pool"):
            if row["enabled"]:
                key = f"pool:{row['target_key']}"
                items.append((key, "IIS Pool", str(row["display_name"]), iis_pool_snapshot(str(row["target_key"]))))
        for key, component_type, name, snapshot in items:
            current[key] = snapshot
            if key not in baselines:
                baselines[key] = snapshot
                continue
            changes = diff_snapshots(baselines[key], snapshot)
            inserted = []
            for property_name, old_value, new_value in changes:
                change_hash = hashlib.sha256(
                    f"{key}|{property_name}|{old_value}|{new_value}".encode()
                ).hexdigest()
                if self.store.record_component_change(
                    change_hash,
                    component_type,
                    key,
                    name,
                    property_name,
                    old_value,
                    new_value,
                ):
                    inserted.append((friendly_property_name(property_name), old_value, new_value))
            if inserted and config.get("assurance", {}).get("email_changes", True):
                lines = "".join(
                    "<tr>"
                    f"<td style='padding:14px 12px;border-bottom:1px solid #e5e7eb;font-weight:600;color:#27364b'>{escape(prop)}</td>"
                    f"<td style='padding:14px 12px;border-bottom:1px solid #e5e7eb;background:#fff1f2;color:#b42318;word-break:break-word'><span style='display:inline-block;padding:3px 8px;border-radius:12px;background:#fee4e2;font-size:11px;font-weight:700'>OLD</span><br><span style='display:inline-block;margin-top:7px'>{escape(old or 'Empty')}</span></td>"
                    f"<td style='padding:14px 12px;border-bottom:1px solid #e5e7eb;background:#ecfdf3;color:#067647;word-break:break-word'><span style='display:inline-block;padding:3px 8px;border-radius:12px;background:#d1fadf;font-size:11px;font-weight:700'>NEW</span><br><span style='display:inline-block;margin-top:7px;font-weight:600'>{escape(new or 'Empty')}</span></td>"
                    "</tr>"
                    for prop, old, new in inserted[:30]
                )
                extra = len(inserted) - 30
                body = (
                    "<table width='100%' role='presentation' style='margin:0 0 20px;border-collapse:separate;border-spacing:0;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px'>"
                    f"<tr><td style='padding:15px'><div style='font-size:11px;letter-spacing:1px;color:#64748b'>CHANGED COMPONENT</div><div style='font-size:18px;font-weight:700;margin-top:4px'>{escape(name)}</div><div style='color:#64748b;margin-top:3px'>{escape(component_type)} · {len(inserted)} changed setting(s)</div></td></tr></table>"
                    "<table width='100%' role='presentation' style='border-collapse:collapse;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden'>"
                    "<tr style='background:#172033;color:#fff'><th align='left' style='padding:11px 12px'>Setting</th><th align='left' style='padding:11px 12px'>Previous value</th><th align='left' style='padding:11px 12px'>Current value</th></tr>"
                    f"{lines}</table>"
                    + (f"<p style='color:#64748b'>And {extra} additional change(s). Open Component Changes to review all values.</p>" if extra > 0 else "")
                    + "<p style='margin:20px 0 0;color:#475569'>Review this change in <b>Assurance → Component Changes</b>. If it is approved, accept the current snapshot as the new baseline.</p>"
                )
                self.notifier.enqueue(
                    f"[CHANGE] {component_type} · {name}",
                    branded_html(
                        f"{component_type} configuration changed",
                        body,
                        "warning",
                    ),
                )
        self.store.set("component_baselines", baselines)
        self.store.set("component_current_snapshots", current)

    def _email(self, subject: str, name: str, message: str, severity: str) -> None:
        body = f"<p><b>{escape(name)}</b></p><p>{escape(message)}</p>"
        self.notifier.enqueue(subject, branded_html(subject, body, severity))
