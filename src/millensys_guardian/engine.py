from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict

import requests

from . import __version__
from .actions import pool_action, restart_service
from .assurance import AssuranceMonitor
from .discovery import disks, iis_pools, performance, performance_consumers, windows_services
from .health import SustainedThresholds
from .licensing import current_license
from .notifier import Notifier
from .reports import ReportScheduler
from .scheduler import SafeScheduler, configured_timezone
from .sql_monitor import SqlServerMonitor
from .store import Store


class MonitorEngine:
    def __init__(self, store: Store, stop_event: threading.Event | None = None) -> None:
        self.store = store
        self.stop_event = stop_event or threading.Event()
        self.notifier = Notifier(store)
        self.assurance = AssuranceMonitor(store, self.notifier)
        self.sql_monitor = SqlServerMonitor(store, self.notifier)
        self.failures: dict[str, int] = defaultdict(int)
        self.api_failures: dict[str, int] = defaultdict(int)
        self.last_action: dict[str, float] = {}
        self.thresholds = SustainedThresholds()
        self.log = logging.getLogger("guardian.engine")
        config = store.get("configuration")
        self.scheduler = SafeScheduler(
            store, config.get("agent", {}).get("timezone", "SERVER_LOCAL")
        )
        self.report_scheduler = ReportScheduler(store)
        self.last_retention = 0.0

    def _recovery_runtime(self) -> dict:
        return self.store.get("recovery_runtime", {})

    def _recovery_attempts(self, key: str) -> int:
        return int(self._recovery_runtime().get(key, {}).get("attempts", 0))

    def _clear_recovery_runtime(self, key: str) -> None:
        runtime = self._recovery_runtime()
        if runtime.pop(key, None) is not None:
            self.store.set("recovery_runtime", runtime)

    def _has_healthy_baseline(self, kind: str, key: str) -> bool:
        baselines = self.store.get("target_health_baselines", {})
        return bool(baselines.get(f"{kind}:{key}"))

    def _record_healthy_baseline(self, kind: str, key: str) -> None:
        baselines = self.store.get("target_health_baselines", {})
        baseline_key = f"{kind}:{key}"
        if baseline_key not in baselines:
            baselines[baseline_key] = {"observed_running_at": time.time()}
            self.store.set("target_health_baselines", baselines)

    def _open_score_incident(
        self, kind: str, target: str, condition: str, title: str, deduction: int = 2
    ) -> None:
        fingerprint = Notifier.fingerprint(kind, target, condition)
        self.store.open_score_incident(
            f"active:{fingerprint}",
            "active",
            kind,
            target,
            condition,
            title,
            deduction,
        )

    def _claim_recovery_attempt(self, key: str, limit: int, delay_seconds: int) -> bool:
        runtime = self._recovery_runtime()
        state = runtime.get(key, {})
        if int(state.get("attempts", 0)) >= limit or time.time() < float(
            state.get("next_attempt", 0)
        ):
            return False
        state["attempts"] = int(state.get("attempts", 0)) + 1
        state["next_attempt"] = time.time() + max(10, delay_seconds)
        runtime[key] = state
        self.store.set("recovery_runtime", runtime)
        return True

    def run_forever(self) -> None:
        self.store.add_event("info", "agent", "monitor", "Monitoring engine started")
        self._heartbeat("running")
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                license_status = current_license()
                if not license_status.valid:
                    self.store.add_event(
                        "critical", "license", "activation", license_status.message
                    )
                    self._heartbeat("license_invalid")
                    return
                self.run_cycle()
                self.notifier.flush()
            except Exception:
                self.log.exception("Monitoring cycle failed")
                self.store.add_event(
                    "error", "agent", "monitor", "Monitoring cycle failed; see logs"
                )
            interval = int(self.store.get("configuration")["agent"].get("poll_seconds", 30))
            self.stop_event.wait(max(1, interval - (time.monotonic() - started)))
        self.store.add_event("info", "agent", "monitor", "Monitoring engine stopped")
        self._heartbeat("stopped")

    def run_cycle(self) -> None:
        config = self.store.get("configuration")
        self._heartbeat("running")
        self.scheduler.timezone = configured_timezone(
            config["agent"].get("timezone", "SERVER_LOCAL")
        )
        selected_services = {
            row["target_key"]: row for row in self.store.targets("service") if row["enabled"]
        }
        if selected_services:
            current = {item["key"]: item for item in windows_services()}
            if not current:
                self.store.add_event_deduplicated(
                    "error",
                    "discovery",
                    "windows-services",
                    "Windows Service discovery failed; target actions were suppressed",
                )
                selected_services = {}
            for name, row in selected_services.items():
                status = current.get(name, {}).get("status", "missing")
                target_config = json.loads(row["config"])
                if self.scheduler.process(
                    "service",
                    name,
                    target_config,
                    allow_actions=config["agent"].get("automatic_actions", True),
                ):
                    continue
                if status != "running":
                    if not self._has_healthy_baseline("service", name):
                        self.failures[name] = 0
                        self._clear_recovery_runtime(name)
                        self.notifier.suppress("service", name, "not_running")
                        self.store.add_event_deduplicated(
                            "info",
                            "service",
                            name,
                            f"Service is {status}; waiting to observe it running before alerting",
                        )
                        self._api_check(
                            "service",
                            name,
                            target_config,
                            allow_actions=config["agent"].get("automatic_actions", True),
                        )
                        continue
                    self.failures[name] += 1
                    self.store.add_event_deduplicated(
                        "critical", "service", name, f"Service is {status}"
                    )
                    recovery_enabled = target_config.get(
                        "start_if_stopped", target_config.get("auto_restart", True)
                    ) and config["agent"].get("automatic_actions", True)
                    if recovery_enabled:
                        self._guarded_restart(name, int(target_config.get("retry_count", 3)))
                    attempts_exhausted = self._recovery_attempts(name) >= int(
                        target_config.get("retry_count", 3)
                    )
                    if not recovery_enabled or attempts_exhausted:
                        detail = (
                            f"Windows service {name} is {status} after retries."
                            if recovery_enabled
                            else f"Windows service {name} is {status}; monitoring-only mode."
                        )
                        self.notifier.alert(
                            "service",
                            name,
                            "not_running",
                            detail,
                            "critical",
                            repeat=recovery_enabled,
                        )
                        self._open_score_incident(
                            "service", name, "not_running", detail
                        )
                else:
                    self._record_healthy_baseline("service", name)
                    if self.failures[name]:
                        self.store.add_event("recovery", "service", name, "Service recovered")
                    self.failures[name] = 0
                    self._clear_recovery_runtime(name)
                    self.notifier.recover(
                        "service", name, "not_running", f"Windows service {name} is running again."
                    )
                self._api_check(
                    "service",
                    name,
                    target_config,
                    allow_actions=config["agent"].get("automatic_actions", True),
                )
        selected_pools = {
            row["target_key"]: row for row in self.store.targets("pool") if row["enabled"]
        }
        if selected_pools:
            current_pools = {item["key"]: item for item in iis_pools()}
            if not current_pools:
                self.store.add_event_deduplicated(
                    "error",
                    "discovery",
                    "iis-pools",
                    "IIS pool discovery failed; target actions were suppressed",
                )
                selected_pools = {}
            for name, row in selected_pools.items():
                target_config = json.loads(row["config"])
                if self.scheduler.process(
                    "pool",
                    name,
                    target_config,
                    allow_actions=config["agent"].get("automatic_actions", True),
                ):
                    continue
                status = current_pools.get(name, {}).get("status", "missing").lower()
                if "started" not in status:
                    if not self._has_healthy_baseline("pool", name):
                        self.failures[f"pool:{name}"] = 0
                        self._clear_recovery_runtime(f"pool:{name}")
                        self.notifier.suppress("pool", name, "not_started")
                        self.store.add_event_deduplicated(
                            "info",
                            "pool",
                            name,
                            f"IIS pool is {status}; waiting to observe it started before alerting",
                        )
                        self._api_check(
                            "pool",
                            name,
                            target_config,
                            allow_actions=config["agent"].get("automatic_actions", True),
                        )
                        continue
                    self.failures[f"pool:{name}"] += 1
                    self.store.add_event_deduplicated(
                        "critical", "pool", name, f"IIS pool is {status}"
                    )
                    recovery_enabled = target_config.get(
                        "start_if_stopped", target_config.get("auto_restart", True)
                    ) and config["agent"].get("automatic_actions", True)
                    if recovery_enabled:
                        self._guarded_pool_start(name, int(target_config.get("retry_count", 3)))
                    attempts_exhausted = self._recovery_attempts(f"pool:{name}") >= int(
                        target_config.get("retry_count", 3)
                    )
                    if not recovery_enabled or attempts_exhausted:
                        detail = (
                            f"IIS pool {name} is {status} after retries."
                            if recovery_enabled
                            else f"IIS pool {name} is {status}; monitoring-only mode."
                        )
                        self.notifier.alert(
                            "pool",
                            name,
                            "not_started",
                            detail,
                            "critical",
                            repeat=recovery_enabled,
                        )
                        self._open_score_incident("pool", name, "not_started", detail)
                else:
                    self._record_healthy_baseline("pool", name)
                    if self.failures[f"pool:{name}"]:
                        self.store.add_event("recovery", "pool", name, "IIS pool recovered")
                    self.failures[f"pool:{name}"] = 0
                    self._clear_recovery_runtime(f"pool:{name}")
                    self.notifier.recover(
                        "pool", name, "not_started", f"IIS pool {name} is started again."
                    )
                self._api_check(
                    "pool",
                    name,
                    target_config,
                    allow_actions=config["agent"].get("automatic_actions", True),
                )
        disk_config = config["disk"]
        current_disks = disks()
        if not disk_config.get("monitor_all", True):
            selected_disks = {
                row["target_key"] for row in self.store.targets("disk") if row["enabled"]
            }
            current_disks = [disk for disk in current_disks if disk["key"] in selected_disks]
        for disk in current_disks:
            bad = (
                disk["free_percent"] < disk_config["minimum_free_percent"]
                or disk["free_mb"] < disk_config["minimum_free_mb"]
            )
            message = f"{disk['free_mb']:,} MB ({disk['free_percent']}%) free"
            if bad:
                self.store.add_event_deduplicated("warning", "disk", disk["key"], message)
                self.notifier.alert("disk", disk["key"], "low_space", message, "warning")
            else:
                self.notifier.recover(
                    "disk", disk["key"], "low_space", f"Disk space recovered: {message}"
                )
        perf = performance()
        now = time.monotonic()
        sustain_seconds = float(config["performance"].get("sustain_seconds", 120))
        for metric, threshold_key in (("cpu", "cpu_percent"), ("memory", "memory_percent")):
            threshold = float(config["performance"][threshold_key])
            transition = self.thresholds.update(
                metric, perf[metric] >= threshold, now, sustain_seconds
            )
            if transition.sustained:
                detail = (
                    f"{metric.upper()} is {perf[metric]:.1f}% "
                    f"(threshold {threshold:.1f}%)."
                )
                self.notifier.alert(
                    "performance",
                    metric,
                    "high_usage",
                    detail,
                    "warning",
                )
                self._open_score_incident(
                    "performance", metric, "high_usage", detail
                )
            elif transition.recovered:
                self.notifier.recover(
                    "performance",
                    metric,
                    "high_usage",
                    f"{metric.upper()} returned to {perf[metric]:.1f}%.",
                )
        self.store.set(
            "latest_health", {"performance": perf, "disks": disks(), "checked_at": time.time()}
        )
        minimum_disk = min((disk["free_percent"] for disk in current_disks), default=None)
        consumers = performance_consumers(10)
        self.store.add_health_sample(perf["cpu"], perf["memory"], minimum_disk, consumers)
        if time.monotonic() - self.last_retention >= 86400:
            diagnostics = config.get("diagnostics", {})
            self.store.apply_retention(
                int(diagnostics.get("retention_days", 30)),
                int(diagnostics.get("max_events", 100000)),
            )
            self.last_retention = time.monotonic()
        self.report_scheduler.process()
        try:
            self.assurance.process(config)
        except Exception as exc:
            self.log.exception("Component assurance cycle failed")
            self.store.add_event_deduplicated(
                "error",
                "assurance",
                "monitor",
                "Backup or component assurance check failed",
                error=str(exc)[:500],
            )
        self.sql_monitor.process(config)

    def _api_check(self, kind: str, name: str, config: dict, *, allow_actions: bool = True) -> None:
        api = config.get("api_check", {})
        url = api.get("url")
        if not api.get("enabled") or not url:
            return
        expected = int(api.get("expected_status", 200))
        try:
            response = requests.get(url, timeout=float(api.get("timeout_seconds", 10)))
            body_expected = api.get("expected_text", "")
            healthy = response.status_code == expected and (
                not body_expected or body_expected in response.text
            )
            detail = f"HTTP {response.status_code}; expected {expected}"
        except requests.RequestException as exc:
            healthy, detail = False, str(exc)
        if healthy:
            self.api_failures[f"{kind}:{name}"] = 0
            self.notifier.recover(
                kind, name, "api_unhealthy", f"API check recovered for {name}: {detail}"
            )
        else:
            failure_key = f"{kind}:{name}"
            self.api_failures[failure_key] += 1
            self.store.add_event_deduplicated(
                "warning", "api", name, "Linked API check failed", detail=detail
            )
            failure_limit = max(1, int(api.get("action_after_failures", 3)))
            if self.api_failures[failure_key] >= failure_limit:
                self.notifier.alert(
                    kind,
                    name,
                    "api_unhealthy",
                    f"Linked API check failed for {name}: {detail}",
                    "warning",
                )
                action = str(api.get("failure_action", "notify")).lower()
                if allow_actions and action not in {"", "none", "notify"}:
                    self._guarded_api_action(kind, name, action)

    def _guarded_restart(self, name: str, retry_limit: int) -> None:
        row = self.store.target("service", name)
        target_config = json.loads(row["config"] or "{}") if row else {}
        if not self._claim_recovery_attempt(
            name, retry_limit, int(target_config.get("retry_delay_seconds", 60))
        ):
            return
        related = target_config.get("related_services", [])
        ok, message = restart_service(name, related) if related else restart_service(name)
        self.store.add_event(
            "info" if ok else "error", "action", name, "Automatic restart requested", result=message
        )

    def _guarded_pool_start(self, name: str, retry_limit: int) -> None:
        key = f"pool:{name}"
        row = self.store.target("pool", name)
        target_config = json.loads(row["config"] or "{}") if row else {}
        if not self._claim_recovery_attempt(
            key, retry_limit, int(target_config.get("retry_delay_seconds", 60))
        ):
            return
        ok, message = pool_action(name, "start")
        self.store.add_event(
            "info" if ok else "error", "action", name, "IIS pool start requested", result=message
        )

    def _guarded_api_action(self, kind: str, name: str, action: str) -> None:
        key = f"api:{kind}:{name}:{action}"
        now = time.monotonic()
        if now - self.last_action.get(key, 0) < 300:
            return
        self.last_action[key] = now
        if kind == "service" and action == "restart":
            row = self.store.target("service", name)
            target_config = json.loads(row["config"] or "{}") if row else {}
            related = target_config.get("related_services", [])
            ok, message = restart_service(name, related) if related else restart_service(name)
        elif kind == "pool" and action in {"restart", "recycle"}:
            ok, message = pool_action(name, "recycle")
        elif kind == "pool" and action == "start":
            ok, message = pool_action(name, "start")
        else:
            ok, message = False, f"Unsupported API failure action: {action}"
        self.store.add_event(
            "info" if ok else "error",
            "api_action",
            name,
            f"API failure action {action} requested",
            result=message,
        )

    def _heartbeat(self, state: str) -> None:
        self.store.set(
            "agent_heartbeat",
            {
                "state": state,
                "timestamp": time.time(),
                "pid": os.getpid(),
                "version": __version__,
            },
        )
