from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from .notifier import Notifier, branded_html
from .security import unprotect
from .sql_backup import assess_backup_history, sanitize_destination
from .sql_capacity import tempdb_balance
from .sql_jobs import duration_seconds, job_intelligence, schedule_assessment
from .sql_performance import (
    io_delta,
    period_baseline_comparison,
    sustained_performance_assessment,
    wait_deltas,
)
from .store import Store

STATUS_NAMES = {0: "Failed", 1: "Succeeded", 2: "Retry", 3: "Canceled", 4: "Running"}
SQL_PROCESS_CPU_QUERY = (
    "SELECT TOP (1) CAST(record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]','int') AS int) "
    "FROM (SELECT CONVERT(xml,record) record,[timestamp] FROM sys.dm_os_ring_buffers "
    "WHERE ring_buffer_type='RING_BUFFER_SCHEDULER_MONITOR' AND record LIKE '%<SystemHealth>%') rb ORDER BY [timestamp] DESC"
)


def performance_root_cause(metric: str, sample: dict[str, Any], queries: list[dict[str, Any]]) -> str:
    """Return a concise evidence-based likely cause without exposing SQL text."""
    if metric == "sql_cpu_percent":
        ranked = sorted(queries, key=lambda item: float(item.get("cpu_ms") or 0), reverse=True)
        if ranked and float(ranked[0].get("cpu_ms") or 0) >= 1000:
            item = ranked[0]
            database = item.get("database_name") or "unidentified database"
            signature = str(item.get("query_signature") or "unknown")[-12:]
            return f"Likely workload contributor: query evidence {signature} on {database}."
        active = int(sample.get("active_sessions") or 0)
        if active:
            return f"No dominant query was captured; {active} active SQL session(s) were present."
        return "No active query contributor was captured in this sample; validate host CPU and the next sample."
    if metric == "memory_grants_pending":
        return f"{int(sample.get('memory_grants_pending') or 0)} query memory grant(s) were waiting."
    if metric == "longest_blocked_seconds":
        blocker = int(sample.get("head_blocker_session_id") or 0)
        return f"Blocking was led by session {blocker}." if blocker else "Blocking was present but the head blocker was not identified."
    if metric in {"io_read_latency_ms", "io_write_latency_ms"}:
        key = "read_latency_ms" if metric == "io_read_latency_ms" else "write_latency_ms"
        candidates = [item for item in sample.get("io", []) if item.get(key) is not None]
        if candidates:
            item = max(candidates, key=lambda value: float(value[key]))
            return f"Highest latency was on {item.get('database') or 'unknown database'} / {item.get('file') or 'unknown file'}."
        return "No individual database file contributor was captured."
    return "No likely cause was identified from the captured evidence."


def sanitize_query_text(value: Any, limit: int = 2000) -> str:
    """Keep diagnostic SQL structure while removing literal values and comments."""
    text = str(value or "")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"--[^\r\n]*", " ", text)
    text = re.sub(r"N?'(?:''|[^'])*'", "'?'", text, flags=re.IGNORECASE)
    text = re.sub(r"\b0x[0-9a-f]+\b", "0x?", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<![\w@#])[-+]?\d+(?:\.\d+)?(?![\w])", "?", text)
    return " ".join(text.split())[:limit]


def capacity_risk(file_info: dict[str, Any]) -> str:
    risks = []
    utilization = file_info.get("utilization_percent")
    volume_free = file_info.get("volume_free_mb")
    if utilization is not None and float(utilization) >= 90:
        risks.append("File utilization is at least 90%")
    if volume_free is not None and float(volume_free) < 5120:
        risks.append("Hosting volume has less than 5 GB free")
    if file_info.get("growth_is_percent"):
        risks.append("Percentage autogrowth can create unpredictable increments")
    elif float(file_info.get("growth_value", 0)) == 0:
        risks.append("Autogrowth is disabled")
    elif float(file_info.get("growth_value", 0)) < 64:
        risks.append("Autogrowth increment is smaller than 64 MB")
    maximum = file_info.get("max_size_mb")
    if maximum is not None and float(maximum) > 0:
        allocated = float(file_info.get("allocated_mb", 0))
        if allocated / float(maximum) >= 0.9:
            risks.append("File is within 10% of its configured maximum")
    return "; ".join(risks)


def _pyodbc():
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError("SQL monitoring requires the bundled pyodbc component") from exc
    return pyodbc


def connection_string(profile: dict[str, Any], password: str = "") -> str:
    driver = str(profile.get("driver") or "ODBC Driver 18 for SQL Server").replace("}", "")
    server = str(profile.get("server", "")).strip()
    database = str(profile.get("database") or "master").strip()
    parts = [f"DRIVER={{{driver}}}", f"SERVER={server}", f"DATABASE={database}", "APP=MiMonitoringService"]
    if profile.get("auth_mode", "sql") == "windows":
        parts.append("Trusted_Connection=yes")
    else:
        parts.extend([f"UID={profile.get('username', '')}", f"PWD={password}"])
    parts.extend(
        [
            f"Encrypt={'yes' if profile.get('encrypt', True) else 'no'}",
            f"TrustServerCertificate={'yes' if profile.get('trust_certificate', True) else 'no'}",
            "Connection Timeout=10",
        ]
    )
    return ";".join(parts) + ";"


def _dt(run_date: Any, run_time: Any) -> str:
    if not run_date:
        return "—"
    try:
        return datetime.strptime(f"{int(run_date):08d}{int(run_time or 0):06d}", "%Y%m%d%H%M%S").isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return "—"


def duration_text(value: Any) -> str:
    try:
        raw = f"{int(value or 0):06d}"
        hours, minutes, seconds = int(raw[:-4]), int(raw[-4:-2]), int(raw[-2:])
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    except (TypeError, ValueError):
        return "00:00:00"


def test_connection(profile: dict[str, Any], password: str = "") -> dict[str, Any]:
    pyodbc = _pyodbc()
    with pyodbc.connect(connection_string(profile, password), timeout=10) as db:
        cursor = db.cursor()
        cursor.execute("SELECT CAST(SERVERPROPERTY('ServerName') AS nvarchar(256)), CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128))")
        server_name, version = cursor.fetchone()
        cursor.execute("SELECT name FROM msdb.dbo.sysjobs ORDER BY name")
        jobs = [str(row[0]) for row in cursor.fetchall()]
    return {"server": str(server_name), "version": str(version), "jobs": jobs}


def start_agent_job(profile: dict[str, Any], job_name: str, password: str = "") -> None:
    if not profile.get("allow_manual_job_run", False):
        raise PermissionError("Manual SQL Agent execution is disabled for this monitoring profile")
    if job_name not in profile.get("jobs", []):
        raise ValueError("Only explicitly monitored SQL Agent jobs may be started")
    if profile.get("auth_mode", "sql") == "sql" and not password:
        password = unprotect(str(profile.get("password_protected", "")))
    pyodbc = _pyodbc()
    with pyodbc.connect(connection_string(profile, password), timeout=10) as db:
        db.cursor().execute("EXEC msdb.dbo.sp_start_job @job_name=?", job_name)
        db.commit()


def kill_sessions(
    profile: dict[str, Any],
    session_ids: list[int],
    password: str = "",
    expected_sessions: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not profile.get("allow_session_termination", False):
        raise PermissionError("SQL session termination is disabled for this monitoring profile")
    requested = sorted({int(value) for value in session_ids if 0 < int(value) <= 32767})
    if not requested:
        raise ValueError("Select at least one valid SQL session")
    if profile.get("auth_mode", "sql") == "sql" and not password:
        password = unprotect(str(profile.get("password_protected", "")))
    pyodbc = _pyodbc()
    with pyodbc.connect(connection_string(profile, password), timeout=10) as db:
        cursor = db.cursor()
        cursor.timeout = 15
        placeholders = ",".join("?" for _ in requested)
        cursor.execute(
            "SELECT @@SPID,s.session_id,s.is_user_process,s.status,s.login_name,s.program_name,"
            "s.host_name,CONVERT(nvarchar(33),s.login_time,126) "
            "FROM sys.dm_exec_sessions s WHERE s.session_id IN (" + placeholders + ")",
            *requested,
        )
        rows = list(cursor.fetchall())
        own_session = int(rows[0][0]) if rows else -1
        found = {int(row[1]): row for row in rows}
        results = []
        for session_id in requested:
            row = found.get(session_id)
            if row is None:
                results.append({"session_id": session_id, "ok": False, "message": "Session no longer exists"})
                continue
            if session_id == own_session or not bool(row[2]) or session_id <= 50 or str(row[5] or "").startswith("MiMonitoringService"):
                results.append({"session_id": session_id, "ok": False, "message": "Protected system or monitoring session"})
                continue
            expected = (expected_sessions or {}).get(session_id)
            if expected and any(
                str(actual or "") != str(expected.get(field) or "")
                for field, actual in (
                    ("login_name", row[4]),
                    ("application_name", row[5]),
                    ("host_name", row[6]),
                    ("login_time", row[7]),
                )
            ):
                results.append({"session_id": session_id, "ok": False, "message": "Session identity changed or the SPID was reused"})
                continue
            try:
                cursor.execute(f"KILL {session_id}")
            except Exception as exc:  # noqa: BLE001 - return an audited result per session
                results.append({"session_id": session_id, "ok": False, "message": str(exc)[:500]})
            else:
                results.append(
                    {
                        "session_id": session_id,
                        "ok": True,
                        "message": f"Terminated {row[3]} session for {row[4] or 'unknown login'} / {row[5] or 'unknown application'}",
                    }
                )
        db.commit()
        return results


class SqlServerMonitor:
    def __init__(self, store: Store, notifier: Notifier) -> None:
        self.store = store
        self.notifier = notifier

    def process(self, config: dict[str, Any]) -> None:
        runtime = self.store.get("sql_runtime", {})
        for profile in config.get("sql_monitors", []):
            if not profile.get("enabled", True):
                continue
            profile_id = str(profile.get("id") or profile.get("server"))
            state = runtime.setdefault(profile_id, {})
            try:
                self._check(profile, state)
                state["status"] = "Connected"
                state["error"] = ""
                state["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            except Exception as exc:  # noqa: BLE001 - one inaccessible SQL profile must not stop others
                state["status"] = "Connection failed"
                state["error"] = str(exc)[:500]
                state["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                self.store.add_event_deduplicated("error", "sql", str(profile.get("name") or profile.get("server")), f"SQL monitoring failed: {exc}", window_seconds=1800)
        self.store.set("sql_runtime", runtime)

    def _check(self, profile: dict[str, Any], state: dict[str, Any]) -> None:
        password = unprotect(str(profile.get("password_protected", ""))) if profile.get("auth_mode", "sql") == "sql" else ""
        pyodbc = _pyodbc()
        with pyodbc.connect(connection_string(profile, password), timeout=10) as db:
            cursor = db.cursor()
            database = str(profile.get("database", ""))
            cursor.execute(
                "SELECT d.state_desc, COALESCE(SUM(CASE WHEN mf.type=0 THEN mf.size END),0)*8.0/1024, COALESCE(SUM(CASE WHEN mf.type=1 THEN mf.size END),0)*8.0/1024 "
                "FROM sys.databases d LEFT JOIN sys.master_files mf ON mf.database_id=d.database_id WHERE d.name=? GROUP BY d.state_desc",
                database,
            )
            db_row = cursor.fetchone()
            if db_row is None:
                raise RuntimeError(f"Database not found or not visible: {database}")
            data_mb, log_mb = float(db_row[1]), float(db_row[2])
            total_mb = data_mb + log_mb
            previous = state.get("database", {})
            growth_mb = total_mb - float(previous.get("total_mb", total_mb))
            state["database"] = {"name": database, "state": str(db_row[0]), "data_mb": round(data_mb, 2), "log_mb": round(log_mb, 2), "total_mb": round(total_mb, 2), "growth_mb": round(growth_mb, 2)}
            self.store.add_sql_sample(
                str(profile.get("id") or profile.get("server")),
                str(profile.get("name") or profile.get("server")),
                state["database"],
            )
            threshold = float(profile.get("growth_notify_mb", 0) or 0)
            if profile.get("notify_growth", False) and threshold > 0 and growth_mb >= threshold:
                bucket = f"{round(total_mb / threshold)}"
                if state.get("growth_bucket") != bucket:
                    state["growth_bucket"] = bucket
                    self._database_email(profile, state["database"])
            self._collect_capacity_if_due(profile, state, cursor)
            self._collect_backup_assurance_if_due(profile, state, cursor)
            self._collect_performance_if_due(profile, state, cursor)
            self._collect_sessions_if_due(profile, state, cursor)
            self._collect_configuration_if_due(profile, state, cursor)
            self._collect_deadlocks_if_due(profile, state, cursor)
            jobs = [name.strip() for name in profile.get("jobs", []) if name.strip()]
            if jobs:
                placeholders = ",".join("?" for _ in jobs)
                metadata: dict[str, dict[str, Any]] = {}
                try:
                    cursor.execute(
                        "SELECT j.name,j.enabled,COUNT(js.schedule_id),"
                        "COALESCE(SUM(CASE WHEN s.enabled=1 THEN 1 ELSE 0 END),0) "
                        "FROM msdb.dbo.sysjobs j LEFT JOIN msdb.dbo.sysjobschedules js ON js.job_id=j.job_id "
                        "LEFT JOIN msdb.dbo.sysschedules s ON s.schedule_id=js.schedule_id "
                        "WHERE j.name IN (" + placeholders + ") GROUP BY j.name,j.enabled",
                        *jobs,
                    )
                    metadata = {str(row[0]): {"job_enabled": bool(row[1]), "schedule_count": int(row[2] or 0), "enabled_schedule_count": int(row[3] or 0)} for row in cursor.fetchall()}
                except Exception:  # noqa: BLE001 - outcome history can remain available
                    metadata = {}
                running: dict[str, Any] = {}
                try:
                    cursor.execute(
                        "SELECT j.name,ja.start_execution_date FROM msdb.dbo.sysjobactivity ja "
                        "JOIN msdb.dbo.sysjobs j ON j.job_id=ja.job_id "
                        "WHERE ja.session_id=(SELECT MAX(session_id) FROM msdb.dbo.sysjobactivity) "
                        "AND ja.start_execution_date IS NOT NULL AND ja.stop_execution_date IS NULL "
                        "AND j.name IN (" + placeholders + ")",
                        *jobs,
                    )
                    running = {str(row[0]): row[1] for row in cursor.fetchall()}
                except Exception:  # noqa: BLE001 - completed history remains useful without activity access
                    running = {}
                cursor.execute(
                    "SELECT j.name,h.instance_id,h.run_status,h.run_date,h.run_time,h.run_duration,h.message,js.next_run_date,js.next_run_time "
                    "FROM msdb.dbo.sysjobs j OUTER APPLY (SELECT TOP 1 instance_id,run_status,run_date,run_time,run_duration,message FROM msdb.dbo.sysjobhistory WHERE job_id=j.job_id AND step_id=0 ORDER BY instance_id DESC) h "
                    "LEFT JOIN msdb.dbo.sysjobschedules js ON js.job_id=j.job_id WHERE j.name IN (" + placeholders + ")",
                    *jobs,
                )
                found = set()
                job_states = state.setdefault("jobs", {})
                for row in cursor.fetchall():
                    name = str(row[0]); found.add(name)
                    execution_id = int(row[1] or 0); status_code = int(row[2] if row[2] is not None else 4)
                    job = {"status": STATUS_NAMES.get(status_code, f"Unknown ({status_code})"), "last_run": _dt(row[3], row[4]), "duration": duration_text(row[5]), "duration_seconds": duration_seconds(row[5]), "message": str(row[6] or ""), "next_run": _dt(row[7], row[8]), "execution_id": execution_id}
                    previous_job = job_states.get(name, {})
                    old_id = int(previous_job.get("execution_id", 0))
                    old_status = str(previous_job.get("status", ""))
                    job_states[name] = job
                    job.update(metadata.get(name, {}))
                    self.store.upsert_sql_job_execution(str(profile.get("id") or profile.get("server")), name, job)
                    history = [dict(item) for item in self.store.sql_job_history(str(profile.get("id") or profile.get("server")), name)]
                    job["intelligence"] = job_intelligence(history, float(profile.get("job_overdue_hours", 24)))
                    streak_threshold = max(2, int(profile.get("job_failure_streak_threshold", 3)))
                    if job["intelligence"]["failure_streak"] >= streak_threshold:
                        self.store.add_event_deduplicated(
                            "error", "sql-job", name,
                            f"SQL Agent job has failed {job['intelligence']['failure_streak']} consecutive times",
                            window_seconds=3600,
                        )
                    if execution_id and execution_id != old_id:
                        if status_code == 1 and profile.get("notify_success", False):
                            self._job_email(profile, name, job, True)
                        elif status_code != 1 and profile.get("notify_failure", True):
                            self._job_email(profile, name, job, False)
                        severity = "recovery" if status_code == 1 and old_status not in ("", "Succeeded") else "info" if status_code == 1 else "error"
                        self.store.add_event(severity, "sql-job", name, f"SQL Agent job {job['status'].lower()} in {job['duration']}")
                for missing in set(jobs) - found:
                    job_states[missing] = {"status": "Job not found", "last_run": "—", "duration": "—", "next_run": "—", "execution_id": 0}
                for name, started in running.items():
                    start = started if isinstance(started, datetime) else datetime.fromisoformat(str(started))
                    if start.tzinfo is None:
                        start = start.astimezone()
                    seconds = max(0, int((datetime.now().astimezone() - start).total_seconds()))
                    current = job_states.setdefault(name, {})
                    limit_seconds = max(1, int(profile.get("job_long_running_minutes", 120))) * 60
                    baseline_limit = float(current.get("intelligence", {}).get("p95_seconds") or 0) * 1.5
                    current.update({"status": "Running", "started_at": start.isoformat(timespec="seconds"), "duration": f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}", "duration_seconds": seconds,
                                    "long_running": seconds >= max(limit_seconds, baseline_limit)})
                for name, current in job_states.items():
                    if name in jobs:
                        current["schedule"] = schedule_assessment(current, grace_minutes=int(profile.get("job_schedule_grace_minutes", 15)))
                try:
                    cursor.execute(
                        "SELECT TOP (200) j.name,h.instance_id,h.step_id,h.step_name,h.run_status,"
                        "h.run_date,h.run_time,h.run_duration,h.message FROM msdb.dbo.sysjobs j "
                        "JOIN msdb.dbo.sysjobhistory h ON h.job_id=j.job_id "
                        "WHERE h.step_id>0 AND j.name IN (" + placeholders + ") "
                        "ORDER BY h.instance_id DESC",
                        *jobs,
                    )
                    steps = []
                    for row in cursor.fetchall():
                        steps.append({"job_name": str(row[0]), "instance_id": int(row[1]),
                                      "step_id": int(row[2]), "step_name": str(row[3] or f"Step {row[2]}"),
                                      "status": STATUS_NAMES.get(int(row[4]), f"Unknown ({row[4]})"),
                                      "run_at": _dt(row[5], row[6]), "duration_seconds": duration_seconds(row[7]),
                                      "message": str(row[8] or "")})
                    self.store.upsert_sql_job_steps(str(profile.get("id") or profile.get("server")), steps)
                    for step in steps:
                        if step["status"] != "Succeeded" and not job_states.get(step["job_name"], {}).get("latest_failed_step"):
                            job_states.setdefault(step["job_name"], {})["latest_failed_step"] = step
                except Exception as exc:  # noqa: BLE001 - step detail is optional evidence
                    state["job_step_evidence"] = {"status": "Unavailable", "error": str(exc)[:500]}
                else:
                    state["job_step_evidence"] = {"status": "Available", "error": ""}

    def _collect_capacity_if_due(
        self, profile: dict[str, Any], state: dict[str, Any], cursor: Any
    ) -> None:
        capacity = state.setdefault("capacity", {})
        interval = max(5, int(profile.get("capacity_interval_minutes", 15)))
        last_text = str(capacity.get("checked_at", ""))
        if last_text:
            try:
                if datetime.now(UTC) - datetime.fromisoformat(last_text).astimezone(UTC) < timedelta(
                    minutes=interval
                ):
                    return
            except ValueError:
                pass
        try:
            files = self._capacity_files(cursor)
        except Exception as exc:  # noqa: BLE001 - permission/capability state is reported separately
            message = str(exc)[:500]
            lowered = message.lower()
            capacity.update(
                {
                    "status": "No permission"
                    if any(word in lowered for word in ("permission", "denied", "not authorized"))
                    else "Unavailable",
                    "error": message,
                    "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )
            return
        database_name = str(profile.get("database") or "master")
        tempdb = tempdb_balance(files, database_name)
        capacity.update(
            {
                "status": "Attention"
                if any(item["risk"] for item in files)
                or (tempdb and tempdb["status"] == "Attention")
                else "Healthy",
                "error": "",
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "files": files,
                "tempdb": tempdb,
            }
        )
        growth_events = self.store.add_sql_file_samples(
            str(profile.get("id") or profile.get("server")),
            str(profile.get("name") or profile.get("server")),
            database_name,
            files,
        )
        for event in growth_events:
            self.store.add_event(
                "info",
                "sql-capacity",
                f"{database_name} / {event['logical_name']}",
                f"Database file grew by {event['growth_mb']:,.2f} MB "
                f"from {event['previous_mb']:,.2f} MB to {event['current_mb']:,.2f} MB",
            )

    @staticmethod
    def _capacity_files(cursor: Any) -> list[dict[str, Any]]:
        cursor.execute(
            "SELECT df.name,df.type_desc,df.physical_name,df.size*8.0/1024.0,"
            "CASE WHEN df.type=0 THEN FILEPROPERTY(df.name,'SpaceUsed')*8.0/1024.0 END,"
            "CASE WHEN df.max_size=-1 THEN NULL ELSE df.max_size*8.0/1024.0 END,"
            "CASE WHEN df.is_percent_growth=1 THEN df.growth ELSE df.growth*8.0/1024.0 END,"
            "df.is_percent_growth,vs.volume_mount_point,vs.total_bytes/1048576.0,"
            "vs.available_bytes/1048576.0 "
            "FROM sys.database_files df OUTER APPLY sys.dm_os_volume_stats(DB_ID(),df.file_id) vs "
            "ORDER BY df.type,df.file_id"
        )
        result = []
        for row in cursor.fetchall():
            allocated = round(float(row[3] or 0), 2)
            used = round(float(row[4]), 2) if row[4] is not None else None
            free = round(max(0.0, allocated - used), 2) if used is not None else None
            item = {
                "logical_name": str(row[0]),
                "file_type": str(row[1]),
                "physical_path": str(row[2]),
                "allocated_mb": allocated,
                "used_mb": used,
                "free_mb": free,
                "utilization_percent": round(100.0 * used / allocated, 2)
                if used is not None and allocated > 0
                else None,
                "max_size_mb": round(float(row[5]), 2) if row[5] is not None else None,
                "growth_value": round(float(row[6] or 0), 2),
                "growth_is_percent": bool(row[7]),
                "volume_mount": str(row[8] or ""),
                "volume_total_mb": round(float(row[9]), 2) if row[9] is not None else None,
                "volume_free_mb": round(float(row[10]), 2) if row[10] is not None else None,
            }
            item["risk"] = capacity_risk(item)
            result.append(item)
        return result

    def _collect_backup_assurance_if_due(
        self, profile: dict[str, Any], state: dict[str, Any], cursor: Any
    ) -> None:
        assurance = state.setdefault("backup_assurance", {})
        interval = max(15, int(profile.get("backup_interval_minutes", 60)))
        try:
            last = datetime.fromisoformat(str(assurance.get("checked_at", ""))).astimezone(UTC)
            if datetime.now(UTC) - last < timedelta(minutes=interval):
                return
        except ValueError:
            pass
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        database = str(profile.get("database") or "master")
        try:
            cursor.execute("SELECT recovery_model_desc FROM sys.databases WHERE name=?", database)
            recovery_row = cursor.fetchone()
            recovery_model = str(recovery_row[0]) if recovery_row else "UNKNOWN"
            cursor.execute(
                "SELECT TOP (5000) bs.backup_set_id,bs.type,bs.backup_start_date,"
                "bs.backup_finish_date,bs.backup_size/1048576.0,"
                "bs.compressed_backup_size/1048576.0,bs.is_copy_only,bs.has_backup_checksums,"
                "bs.first_lsn,bs.last_lsn,bs.database_backup_lsn,bmf.physical_device_name "
                "FROM msdb.dbo.backupset bs LEFT JOIN msdb.dbo.backupmediafamily bmf "
                "ON bmf.media_set_id=bs.media_set_id WHERE bs.database_name=? "
                "AND bs.type IN ('D','I','L') ORDER BY bs.backup_finish_date DESC",
                database,
            )
            rows = []
            for row in cursor.fetchall():
                rows.append({
                    "backup_set_id": int(row[0]), "backup_type": str(row[1]),
                    "start_time": row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2]),
                    "finish_time": row[3].isoformat() if hasattr(row[3], "isoformat") else str(row[3]),
                    "backup_size_mb": round(float(row[4] or 0), 2),
                    "compressed_size_mb": round(float(row[5] or 0), 2),
                    "is_copy_only": bool(row[6]), "has_checksums": bool(row[7]),
                    "first_lsn": str(row[8] or ""), "last_lsn": str(row[9] or ""),
                    "database_backup_lsn": str(row[10] or ""),
                    "destination": sanitize_destination(row[11]),
                })
            policy = {
                "require_full": profile.get("backup_require_full", True),
                "full_max_hours": profile.get("backup_full_max_hours", 24),
                "require_differential": profile.get("backup_require_differential", False),
                "differential_max_hours": profile.get("backup_differential_max_hours", 24),
                "require_log": profile.get("backup_require_log"),
                "log_max_minutes": profile.get("backup_log_max_minutes", 60),
            }
            result = assess_backup_history(rows, policy=policy, recovery_model=recovery_model)
            result.update({"checked_at": checked_at, "error": ""})
            assurance.update(result)
            self.store.upsert_sql_backup_history(
                str(profile.get("id") or profile.get("server")), database, rows
            )
        except Exception as exc:  # noqa: BLE001 - backup permissions do not affect connectivity
            message = str(exc)[:500]
            assurance.update({
                "status": "No permission" if any(value in message.lower() for value in ("permission", "denied", "not authorized")) else "Unavailable",
                "error": message, "checked_at": checked_at, "checks": [],
                "chain_status": "Unavailable", "history_count": 0,
            })

    def _collect_performance_if_due(
        self, profile: dict[str, Any], state: dict[str, Any], cursor: Any
    ) -> None:
        performance = state.setdefault("performance", {})
        interval = max(1, int(profile.get("performance_interval_minutes", 5)))
        try:
            last = datetime.fromisoformat(str(performance.get("checked_at", ""))).astimezone(UTC)
            if datetime.now(UTC) - last < timedelta(minutes=interval):
                return
        except ValueError:
            pass
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        previous = dict(performance)
        try:
            cursor.execute(
                "SELECT COUNT(*),SUM(CASE WHEN is_user_process=1 AND status<>'sleeping' THEN 1 ELSE 0 END) "
                "FROM sys.dm_exec_sessions WHERE is_user_process=1"
            )
            session_row = cursor.fetchone() or (0, 0)
            cursor.execute(
                "SELECT TOP (20) program_name,host_name,login_name,DB_NAME(database_id),COUNT(*) "
                "FROM sys.dm_exec_sessions WHERE is_user_process=1 GROUP BY program_name,host_name,login_name,database_id ORDER BY COUNT(*) DESC"
            )
            session_context = [{"application": str(row[0] or "Unknown"), "host": str(row[1] or "Unknown"),
                                "login": str(row[2] or "Unknown"), "database": str(row[3] or "No database"),
                                "sessions": int(row[4])} for row in cursor.fetchall()]
            cursor.execute(SQL_PROCESS_CPU_QUERY)
            cpu_row = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*),COALESCE(MAX(wait_time_ms),0) FROM sys.dm_exec_query_memory_grants WHERE grant_time IS NULL"
            )
            grant_row = cursor.fetchone() or (0, 0)
            cursor.execute(
                "SELECT COUNT(*),COALESCE(MAX(wait_time),0),COALESCE(MAX(blocking_session_id),0) "
                "FROM sys.dm_exec_requests WHERE blocking_session_id>0"
            )
            blocking_row = cursor.fetchone() or (0, 0, 0)
            cursor.execute(
                "SELECT SUM(user_object_reserved_page_count)*8.0/1024,SUM(internal_object_reserved_page_count)*8.0/1024,"
                "SUM(version_store_reserved_page_count)*8.0/1024,SUM(unallocated_extent_page_count)*8.0/1024 "
                "FROM tempdb.sys.dm_db_file_space_usage"
            )
            tempdb_row = cursor.fetchone() or (0, 0, 0, 0)
            cursor.execute(
                "SELECT DB_NAME(vfs.database_id),mf.name,mf.type_desc,vfs.database_id,vfs.file_id,vfs.num_of_reads,"
                "vfs.io_stall_read_ms,vfs.num_of_bytes_read,vfs.num_of_writes,vfs.io_stall_write_ms,vfs.num_of_bytes_written "
                "FROM sys.dm_io_virtual_file_stats(NULL,NULL) vfs JOIN sys.master_files mf ON mf.database_id=vfs.database_id AND mf.file_id=vfs.file_id"
            )
            io_counters = [{"database": str(row[0]), "file": str(row[1]), "file_type": str(row[2]),
                            "key": f"{row[3]}:{row[4]}", "reads": int(row[5]), "read_stall_ms": int(row[6]),
                            "bytes_read": int(row[7]), "writes": int(row[8]), "write_stall_ms": int(row[9]),
                            "bytes_written": int(row[10])} for row in cursor.fetchall()]
            cursor.execute("SELECT wait_type,waiting_tasks_count,wait_time_ms FROM sys.dm_os_wait_stats")
            wait_counters = [{"wait_type": str(row[0]), "tasks": int(row[1]), "wait_ms": int(row[2])}
                             for row in cursor.fetchall()]
            query_error = ""
            try:
                long_queries, expensive_queries = self._query_evidence(cursor, profile)
            except Exception as exc:  # noqa: BLE001 - optional query evidence is independently isolated
                long_queries, expensive_queries = [], []
                query_error = str(exc)[:500]
            sample = {"status": "Healthy", "error": "", "checked_at": checked_at,
                      "collected_at": checked_at, "user_connections": int(session_row[0] or 0),
                      "active_sessions": int(session_row[1] or 0),
                      "sql_cpu_percent": float(cpu_row[0]) if cpu_row and cpu_row[0] is not None else None,
                      "memory_grants_pending": int(grant_row[0] or 0), "longest_grant_wait_ms": int(grant_row[1] or 0),
                      "blocked_sessions": int(blocking_row[0] or 0), "longest_blocked_seconds": int((blocking_row[1] or 0) / 1000),
                      "head_blocker_session_id": int(blocking_row[2] or 0), "session_context": session_context,
                      "tempdb_user_mb": round(float(tempdb_row[0] or 0), 2), "tempdb_internal_mb": round(float(tempdb_row[1] or 0), 2),
                      "tempdb_version_store_mb": round(float(tempdb_row[2] or 0), 2), "tempdb_free_mb": round(float(tempdb_row[3] or 0), 2),
                      "tempdb_used_mb": round(sum(float(value or 0) for value in tempdb_row[:3]), 2),
                      "io": io_delta(io_counters, previous), "waits": wait_deltas(wait_counters, previous),
                      "long_queries": long_queries, "expensive_queries": expensive_queries,
                      "query_evidence_error": query_error,
                      "io_counters": io_counters, "wait_counters": wait_counters}
            if sample["memory_grants_pending"] or sample["blocked_sessions"]:
                sample["status"] = "Attention"
            performance.clear()
            performance.update(sample)
            profile_id = str(profile.get("id") or profile.get("server"))
            self.store.add_sql_performance_sample(profile_id, sample)
            since = (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")
            history = self.store.sql_performance_samples(since, profile_id)
            assessment = sustained_performance_assessment(history, sample, profile)
            assessment["period_comparison"] = period_baseline_comparison(
                history, sample,
                self.store.get("configuration").get("agent", {}).get("timezone", "UTC"),
            )
            performance["assessment"] = assessment
            self.store.add_sql_query_evidence(profile_id, long_queries + expensive_queries)
            sustained = {item["metric"]: item for item in assessment["sustained"]}
            labels = {
                "sql_cpu_percent": "SQL process CPU",
                "memory_grants_pending": "pending memory grants",
                "longest_blocked_seconds": "blocking duration",
                "io_read_latency_ms": "read latency",
                "io_write_latency_ms": "write latency",
            }
            target = str(profile.get("name") or profile.get("server") or profile_id)
            for metric, label in labels.items():
                condition = f"sustained_{metric}"
                breach = sustained.get(metric)
                if breach:
                    cause = performance_root_cause(metric, sample, long_queries)
                    self.notifier.alert(
                        "sql-performance", target, condition,
                        f"{label} remained abnormal for {breach['duration_minutes']:.0f} minutes: "
                        f"{breach['value']:,.2f} (threshold {breach['threshold']:,.2f}). {cause}",
                        "warning",
                    )
                else:
                    self.notifier.recover(
                        "sql-performance", target, condition,
                        f"{label.capitalize()} returned below the sustained threshold.",
                    )
        except Exception as exc:  # noqa: BLE001 - performance permissions do not affect connectivity
            message = str(exc)[:500]
            performance.update({"status": "No permission" if any(value in message.lower() for value in ("permission", "denied", "not authorized")) else "Unavailable",
                                "error": message, "checked_at": checked_at})

    def _collect_sessions_if_due(
        self, profile: dict[str, Any], state: dict[str, Any], cursor: Any
    ) -> None:
        evidence = state.setdefault("sessions", {})
        interval_seconds = max(15, int(profile.get("session_interval_seconds", 60)))
        try:
            last = datetime.fromisoformat(str(evidence.get("checked_at", ""))).astimezone(UTC)
            if datetime.now(UTC) - last < timedelta(seconds=interval_seconds):
                return
        except ValueError:
            pass
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        profile_id = str(profile.get("id") or profile.get("server"))
        try:
            cursor.execute(
                "SELECT s.session_id,LOWER(COALESCE(r.status,s.status)),"
                "COALESCE(r.blocking_session_id,0),COALESCE(r.wait_type,''),"
                "COALESCE(r.wait_time,0),DB_NAME(COALESCE(r.database_id,s.database_id)),"
                "s.program_name,s.host_name,s.login_name,r.command,st.text,st.objectid,"
                "CASE WHEN ISNULL(st.objectid,0)=0 THEN CASE WHEN st.text IS NULL THEN 'Session / connection' ELSE 'Ad hoc or prepared' END "
                "WHEN st.dbid=DB_ID() AND OBJECTPROPERTYEX(st.objectid,'IsTrigger')=1 THEN 'Trigger' "
                "WHEN st.dbid=DB_ID() AND OBJECTPROPERTYEX(st.objectid,'IsProcedure')=1 THEN 'Stored procedure' "
                "WHEN st.dbid=DB_ID() AND (OBJECTPROPERTYEX(st.objectid,'IsScalarFunction')=1 OR OBJECTPROPERTYEX(st.objectid,'IsTableFunction')=1) THEN 'Function' "
                "ELSE 'Database object' END,"
                "CASE WHEN ISNULL(st.objectid,0)>0 THEN COALESCE(QUOTENAME(DB_NAME(COALESCE(r.database_id,s.database_id)))+'.'+"
                "QUOTENAME(OBJECT_SCHEMA_NAME(st.objectid,COALESCE(r.database_id,s.database_id)))+'.'+"
                "QUOTENAME(OBJECT_NAME(st.objectid,COALESCE(r.database_id,s.database_id))),CONVERT(nvarchar(30),st.objectid)) "
                "ELSE COALESCE(r.command,s.program_name,'Unknown') END,"
                "s.open_transaction_count,COALESCE(DATEDIFF(second,tx.transaction_begin_time,SYSDATETIME()),0),"
                "CASE WHEN s.is_user_process=1 THEN 1 ELSE 0 END,CONVERT(nvarchar(33),s.login_time,126) "
                "FROM sys.dm_exec_sessions s LEFT JOIN sys.dm_exec_requests r "
                "ON r.session_id=s.session_id OUTER APPLY sys.dm_exec_sql_text(COALESCE(r.sql_handle,s.most_recent_sql_handle)) st "
                "OUTER APPLY (SELECT MIN(at.transaction_begin_time) transaction_begin_time "
                "FROM sys.dm_tran_session_transactions stx JOIN sys.dm_tran_active_transactions at "
                "ON at.transaction_id=stx.transaction_id WHERE stx.session_id=s.session_id) tx "
                "WHERE s.is_user_process=1 AND s.session_id<>@@SPID ORDER BY s.session_id"
            )
            sessions = []
            for row in cursor.fetchall():
                sessions.append(
                    {
                        "session_id": int(row[0]),
                        "status": str(row[1] or "unknown"),
                        "blocking_session_id": int(row[2] or 0),
                        "wait_type": str(row[3] or ""),
                        "wait_ms": int(row[4] or 0),
                        "database_name": str(row[5] or ""),
                        "application_name": str(row[6] or ""),
                        "host_name": str(row[7] or ""),
                        "login_name": str(row[8] or ""),
                        "command": str(row[9] or ""),
                        "query_text": sanitize_query_text(row[10]),
                        "source_type": str(row[12] or "Unknown"),
                        "source_name": str(row[13] or "Unknown"),
                        "open_transactions": int(row[14] or 0),
                        "transaction_age_seconds": int(row[15] or 0),
                        "is_user_process": bool(row[16]),
                        "login_time": str(row[17] or ""),
                        "query_evidence_kind": "Currently executing" if row[9] else "Most recently submitted",
                    }
                )
            blocker_ids = {
                int(item["blocking_session_id"])
                for item in sessions
                if int(item["blocking_session_id"]) > 0
            }
            for item in sessions:
                item["is_blocker"] = int(item["session_id"]) in blocker_ids
                item["session_type"] = (
                    "Blocker"
                    if item["is_blocker"]
                    else "Blocked"
                    if item["blocking_session_id"]
                    else item["status"].title()
                )
            blocking_rows = [
                item for item in sessions if item["blocking_session_id"] or item["is_blocker"]
            ]
            self.store.add_sql_blocking_evidence(profile_id, checked_at, blocking_rows)
            evidence.update(
                {
                    "status": "Blocking" if blocking_rows else "Healthy",
                    "checked_at": checked_at,
                    "interval_seconds": interval_seconds,
                    "sessions": sessions,
                    "blocking_count": sum(bool(item["blocking_session_id"]) for item in sessions),
                    "blocker_count": sum(bool(item["is_blocker"]) for item in sessions),
                    "error": "",
                }
            )
        except Exception as exc:  # noqa: BLE001 - session inventory is independently isolated
            message = str(exc)[:500]
            evidence.update(
                {
                    "status": "No permission"
                    if any(value in message.lower() for value in ("permission", "denied", "not authorized"))
                    else "Unavailable",
                    "checked_at": checked_at,
                    "error": message,
                }
            )

    def _collect_configuration_if_due(
        self, profile: dict[str, Any], state: dict[str, Any], cursor: Any
    ) -> None:
        evidence = state.setdefault("configuration_changes", {})
        interval = max(15, int(profile.get("configuration_interval_minutes", 60)))
        try:
            last = datetime.fromisoformat(str(evidence.get("checked_at", ""))).astimezone(UTC)
            if datetime.now(UTC) - last < timedelta(minutes=interval):
                return
        except ValueError:
            pass
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        profile_id = str(profile.get("id") or profile.get("server"))
        database = str(profile.get("database") or "master")
        try:
            cursor.execute(
                "SELECT name,CONVERT(nvarchar(256),value),CONVERT(nvarchar(256),value_in_use) "
                "FROM sys.configurations WHERE name IN ("
                "'min server memory (MB)','max server memory (MB)','max worker threads',"
                "'max degree of parallelism','cost threshold for parallelism','affinity mask',"
                "'affinity I/O mask','remote access','remote query timeout (s)',"
                "'user options','backup compression default','backup checksum default',"
                "'recovery interval (min)','fill factor (%)','optimize for ad hoc workloads',"
                "'cross db ownership chaining','clr enabled','xp_cmdshell') ORDER BY name"
            )
            snapshot = {
                f"server/{row[0]}": {"configured": str(row[1]), "running": str(row[2])}
                for row in cursor.fetchall()
            }
            cursor.execute(
                "SELECT recovery_model_desc,compatibility_level,is_auto_close_on,is_auto_shrink_on,"
                "page_verify_option_desc,is_read_committed_snapshot_on,snapshot_isolation_state_desc,"
                "is_trustworthy_on,is_db_chaining_on,is_broker_enabled,containment_desc,"
                "is_auto_create_stats_on,is_auto_update_stats_on,is_query_store_on,SUSER_SNAME(owner_sid) "
                "FROM sys.databases WHERE name=?",
                database,
            )
            row = cursor.fetchone()
            if row:
                names = (
                    "recovery_model", "compatibility_level", "auto_close", "auto_shrink",
                    "page_verify", "read_committed_snapshot", "snapshot_isolation", "trustworthy",
                    "db_chaining", "broker", "containment", "auto_create_statistics",
                    "auto_update_statistics", "query_store", "owner",
                )
                snapshot.update(
                    {f"database/{database}/{name}": str(value) for name, value in zip(names, row)}
                )
            baselines = self.store.get("sql_configuration_baselines", {})
            previous = baselines.get(profile_id)
            inserted = 0
            if isinstance(previous, dict):
                for setting in sorted(set(previous) | set(snapshot)):
                    old_value = json.dumps(previous.get(setting), sort_keys=True, ensure_ascii=False)
                    new_value = json.dumps(snapshot.get(setting), sort_keys=True, ensure_ascii=False)
                    if old_value == new_value:
                        continue
                    change_key = hashlib.sha256(
                        f"{profile_id}|{setting}|{old_value}|{new_value}".encode()
                    ).hexdigest()
                    if self.store.record_component_change(
                        change_key,
                        "SQL Server",
                        f"sql:{profile_id}",
                        str(profile.get("name") or profile.get("server") or profile_id),
                        setting,
                        old_value,
                        new_value,
                    ):
                        inserted += 1
            baselines[profile_id] = snapshot
            self.store.set("sql_configuration_baselines", baselines)
            evidence.update(
                {
                    "status": "Changed" if inserted else "Healthy",
                    "checked_at": checked_at,
                    "settings": len(snapshot),
                    "changes": inserted,
                    "error": "",
                }
            )
        except Exception as exc:  # noqa: BLE001 - configuration evidence is independently isolated
            message = str(exc)[:500]
            evidence.update(
                {
                    "status": "No permission"
                    if any(value in message.lower() for value in ("permission", "denied", "not authorized"))
                    else "Unavailable",
                    "checked_at": checked_at,
                    "error": message,
                }
            )

    def _collect_deadlocks_if_due(
        self, profile: dict[str, Any], state: dict[str, Any], cursor: Any
    ) -> None:
        evidence = state.setdefault("deadlocks", {})
        interval = max(1, int(profile.get("performance_interval_minutes", 5)))
        try:
            last = datetime.fromisoformat(str(evidence.get("checked_at", ""))).astimezone(UTC)
            if datetime.now(UTC) - last < timedelta(minutes=interval):
                return
        except ValueError:
            pass
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        profile_id = str(profile.get("id") or profile.get("server"))
        try:
            cursor.execute(
                "SELECT CONVERT(nvarchar(max),xt.target_data) "
                "FROM sys.dm_xe_session_targets xt "
                "JOIN sys.dm_xe_sessions xs ON xs.address=xt.event_session_address "
                "WHERE xs.name='system_health' AND xt.target_name='ring_buffer'"
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                evidence.update(
                    {"status": "Unavailable", "checked_at": checked_at, "count": 0,
                     "error": "system_health ring-buffer target is unavailable"}
                )
                return
            root = ET.fromstring(str(row[0]))
            seen_state = self.store.get("sql_deadlock_seen", {})
            seen = list(seen_state.get(profile_id, []))
            seen_set = set(seen)
            inserted = 0
            for event in root.findall(".//event[@name='xml_deadlock_report']"):
                deadlock = event.find(".//deadlock")
                if deadlock is None:
                    continue
                xml_text = ET.tostring(deadlock, encoding="unicode")
                fingerprint = hashlib.sha256(xml_text.encode()).hexdigest()
                if fingerprint in seen_set:
                    continue
                victim_node = deadlock.find("./victim-list/victimProcess")
                victim_id = victim_node.get("id", "") if victim_node is not None else ""
                processes = []
                for process in deadlock.findall("./process-list/process"):
                    input_buffer = process.findtext("inputbuf") or ""
                    processes.append(
                        {
                            "id": process.get("id", ""),
                            "spid": process.get("spid", ""),
                            "database_id": process.get("currentdb", ""),
                            "application": process.get("clientapp", ""),
                            "host": process.get("hostname", ""),
                            "login": process.get("loginname", ""),
                            "isolation": process.get("isolationlevel", ""),
                            "source": process.get("procname", "") or "Ad hoc batch",
                            "query": sanitize_query_text(input_buffer),
                            "victim": process.get("id", "") == victim_id,
                        }
                    )
                resource_types = [node.tag for node in deadlock.findall("./resource-list/*")]
                if "exchangeEvent" in resource_types:
                    cause = "Parallel-query workers formed a deadlock cycle"
                elif any(name in resource_types for name in ("keylock", "ridlock", "pagelock", "objectlock")):
                    cause = "Transactions acquired database locks in a conflicting order"
                else:
                    cause = "Concurrent transactions formed a circular wait"
                victim = next((item for item in processes if item["victim"]), {})
                occurred_at = str(event.get("timestamp") or checked_at)
                target = str(profile.get("name") or profile.get("server") or profile_id)
                self.store.add_event(
                    "error",
                    "sql-deadlock",
                    target,
                    f"Deadlock victim SPID {victim.get('spid') or 'unknown'} · {cause}",
                    profile_id=profile_id,
                    occurred_at=occurred_at,
                    victim=victim,
                    processes=processes,
                    resource_types=resource_types,
                    likely_cause=cause,
                    evidence_id=fingerprint[-12:],
                    deadlock_xml=xml_text[:100000],
                )
                seen.append(fingerprint)
                seen_set.add(fingerprint)
                inserted += 1
            seen_state[profile_id] = seen[-500:]
            self.store.set("sql_deadlock_seen", seen_state)
            evidence.update(
                {
                    "status": "Deadlock detected" if inserted else "Healthy",
                    "checked_at": checked_at,
                    "new_count": inserted,
                    "error": "",
                }
            )
        except Exception as exc:  # noqa: BLE001 - deadlock evidence is independently isolated
            message = str(exc)[:500]
            evidence.update(
                {
                    "status": "No permission"
                    if any(value in message.lower() for value in ("permission", "denied", "not authorized"))
                    else "Unavailable",
                    "checked_at": checked_at,
                    "error": message,
                }
            )
    @staticmethod
    def _query_evidence(cursor: Any, profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        long_query_seconds = max(5, int(profile.get("long_query_seconds", 60)))
        cursor.execute(
            "SELECT TOP (20) r.session_id,r.request_id,DB_NAME(r.database_id),s.program_name,"
            "s.host_name,s.login_name,r.command,r.total_elapsed_time,r.cpu_time,r.logical_reads,"
            "r.writes,CONVERT(varchar(34),r.query_hash,1),st.text,st.objectid,"
            "CASE WHEN s.is_user_process=0 THEN 'System / internal' "
            "WHEN ISNULL(st.objectid,0)=0 THEN 'Ad hoc or prepared' "
            "WHEN r.database_id=DB_ID() AND OBJECTPROPERTYEX(st.objectid,'IsTrigger')=1 THEN 'Trigger' "
            "WHEN r.database_id=DB_ID() AND OBJECTPROPERTYEX(st.objectid,'IsProcedure')=1 THEN 'Stored procedure' "
            "ELSE 'Database object' END,"
            "CASE WHEN ISNULL(st.objectid,0)>0 THEN COALESCE(QUOTENAME(DB_NAME(r.database_id))+'.'+"
            "QUOTENAME(OBJECT_SCHEMA_NAME(st.objectid,r.database_id))+'.'+QUOTENAME(OBJECT_NAME(st.objectid,r.database_id)),r.command) "
            "ELSE r.command END,s.is_user_process FROM sys.dm_exec_requests r "
            "JOIN sys.dm_exec_sessions s ON s.session_id=r.session_id CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) st "
            "WHERE r.session_id<>@@SPID AND r.total_elapsed_time>=? ORDER BY r.total_elapsed_time DESC",
            long_query_seconds * 1000,
        )
        long_queries = [{"evidence_type": "long_running", "query_signature": str(row[11] or f"session:{row[0]}:{row[1]}"),
                         "database_name": str(row[2] or ""), "application_name": str(row[3] or ""),
                         "host_name": str(row[4] or ""), "login_name": str(row[5] or ""),
                         "command": str(row[6] or ""), "duration_ms": int(row[7] or 0),
                         "cpu_ms": int(row[8] or 0), "logical_reads": int(row[9] or 0),
                         "writes": int(row[10] or 0), "executions": 1,
                         "query_text": sanitize_query_text(row[12]),
                         "source_type": str(row[14] or "Unknown"),
                         "source_name": str(row[15] or row[6] or "Unknown"),
                         "is_system": not bool(row[16]),
                         "rank_score": float(row[7] or 0)} for row in cursor.fetchall()]
        # Deliberately avoid scanning the complete plan cache. SQL evidence is captured only
        # for currently running requests that exceed the configured duration; blocking and
        # deadlock collectors provide event-time evidence without profiler-style overhead.
        return long_queries, []

    def _job_email(self, profile: dict[str, Any], name: str, job: dict[str, Any], success: bool) -> None:
        color = "#067647" if success else "#b42318"
        body = f"<p style='font-size:16px'><b>{escape(name)}</b></p><table width='100%' style='border-collapse:collapse'><tr><td style='padding:10px;background:#f8fafc'>Status</td><td style='padding:10px;color:{color};font-weight:700'>{escape(job['status'])}</td></tr><tr><td style='padding:10px;background:#f8fafc'>Last run</td><td style='padding:10px'>{escape(job['last_run'])}</td></tr><tr><td style='padding:10px;background:#f8fafc'>Duration</td><td style='padding:10px'>{escape(job['duration'])}</td></tr><tr><td style='padding:10px;background:#f8fafc'>Next scheduled run</td><td style='padding:10px'>{escape(job['next_run'])}</td></tr><tr><td style='padding:10px;background:#f8fafc'>SQL message</td><td style='padding:10px'>{escape(job.get('message') or '—')}</td></tr></table>"
        title = f"SQL job {'succeeded' if success else 'failed'}"
        self.notifier.enqueue(f"[SQL JOB {'SUCCESS' if success else 'FAILED'}] {name}", branded_html(title, body, "recovery" if success else "critical"))

    def _database_email(self, profile: dict[str, Any], database: dict[str, Any]) -> None:
        body = f"<p><b>{escape(database['name'])}</b> grew by <b>{database['growth_mb']:.2f} MB</b> since the previous check.</p><p>Current allocated size: {database['total_mb']:.2f} MB (data {database['data_mb']:.2f} MB · log {database['log_mb']:.2f} MB).</p>"
        self.notifier.enqueue(f"[SQL GROWTH] {database['name']}", branded_html("SQL database growth detected", body, "warning"))
