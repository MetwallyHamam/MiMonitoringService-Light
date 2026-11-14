from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .paths import database_path

DEFAULT_SETTINGS: dict[str, Any] = {
    "setup_complete": False,
    "client": {"name": "", "site": "", "server_label": "", "operator_email": ""},
    "registration": {
        "required_domain": "millensys.com",
        "mode": "domain",
        "registered_users": [],
    },
    "email": {
        "enabled": True,
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "millensysnotifications@gmail.com",
        "sender_name": "MiMonitoringService",
        "password_protected": "",
        "use_tls": True,
        "recipients": [],
        "cc": [],
        "cooldown_minutes": 30,
        "max_attempts": 5,
    },
    "reports": {
        "daily_enabled": True,
        "daily_time": "08:00",
        "monthly_enabled": True,
        "monthly_day": 1,
        "monthly_time": "08:15",
        "recipients": [],
        "include_summary": True,
        "include_incidents": True,
        "monthly_analytics_attachment": True,
    },
    "performance": {"cpu_percent": 90, "memory_percent": 90, "sustain_seconds": 120},
    "disk": {"minimum_free_percent": 15, "minimum_free_mb": 5120, "monitor_all": True},
    "agent": {
        "poll_seconds": 30,
        "debug": False,
        "automatic_actions": True,
        "timezone": "SERVER_LOCAL",
        "close_to_tray": True,
        "start_minimized": False,
    },
    "diagnostics": {"retention_days": 30, "max_events": 100000, "trend_hours": 24},
    "backups": [],
    "file_monitors": [],
    "sql_monitors": [],
    "assurance": {"snapshot_interval_seconds": 60, "email_changes": True},
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def merge_defaults(current: dict[str, Any], defaults: dict[str, Any]) -> bool:
    """Add new configuration keys without replacing operator choices."""
    changed = False
    for key, default in defaults.items():
        if key not in current:
            current[key] = deepcopy(default)
            changed = True
        elif isinstance(default, dict) and isinstance(current[key], dict):
            changed = merge_defaults(current[key], default) or changed
    return changed


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or database_path()
        self._lock = threading.RLock()
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = sqlite3.connect(self.path, timeout=15)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
                connection.commit()
            finally:
                connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL, target_key TEXT NOT NULL, display_name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, config TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(kind, target_key)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    severity TEXT NOT NULL, category TEXT NOT NULL, target TEXT NOT NULL,
                    message TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS alert_state (
                    fingerprint TEXT PRIMARY KEY, active INTEGER NOT NULL, last_sent TEXT,
                    last_seen TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1,
                    category TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '',
                    condition TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'warning', first_seen TEXT
                );
                CREATE TABLE IF NOT EXISTS mail_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    next_attempt TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued', subject TEXT NOT NULL,
                    html TEXT NOT NULL, recipients TEXT NOT NULL, last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS action_claims (
                    claim_key TEXT PRIMARY KEY, claimed_at TEXT NOT NULL,
                    kind TEXT NOT NULL, target TEXT NOT NULL, action TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'claimed',
                    result TEXT, attempts INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS health_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    cpu REAL NOT NULL, memory REAL NOT NULL,
                    minimum_disk_free_percent REAL,
                    consumers TEXT NOT NULL DEFAULT '[]', root_cause TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS score_incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, incident_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, category TEXT NOT NULL, target TEXT NOT NULL,
                    condition TEXT NOT NULL, title TEXT NOT NULL, deduction INTEGER NOT NULL,
                    persistent INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'open',
                    occurrences INTEGER NOT NULL DEFAULT 1, opened_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL, resolved_at TEXT, resolution TEXT
                );
                CREATE TABLE IF NOT EXISTS incident_cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL,
                    category TEXT NOT NULL, target TEXT NOT NULL, condition TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS component_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, change_key TEXT NOT NULL UNIQUE,
                    component_type TEXT NOT NULL, item_key TEXT NOT NULL,
                    item_name TEXT NOT NULL, property_name TEXT NOT NULL,
                    old_value TEXT, new_value TEXT, detected_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open', resolved_at TEXT, resolution TEXT
                );
                CREATE TABLE IF NOT EXISTS sql_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    profile_id TEXT NOT NULL, profile_name TEXT NOT NULL,
                    database_name TEXT NOT NULL, state TEXT NOT NULL,
                    data_mb REAL NOT NULL, log_mb REAL NOT NULL, total_mb REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backup_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT NOT NULL,
                    rule_id TEXT NOT NULL, rule_name TEXT NOT NULL,
                    ok INTEGER NOT NULL, age_days INTEGER, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sql_file_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, collected_at TEXT NOT NULL,
                    profile_id TEXT NOT NULL, profile_name TEXT NOT NULL,
                    database_name TEXT NOT NULL, logical_name TEXT NOT NULL,
                    file_type TEXT NOT NULL, physical_path TEXT NOT NULL,
                    allocated_mb REAL NOT NULL, used_mb REAL, free_mb REAL,
                    utilization_percent REAL, max_size_mb REAL, growth_value REAL NOT NULL,
                    growth_is_percent INTEGER NOT NULL, volume_mount TEXT,
                    volume_total_mb REAL, volume_free_mb REAL, risk TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sql_capacity_daily (
                    profile_id TEXT NOT NULL, database_name TEXT NOT NULL,
                    logical_name TEXT NOT NULL, day TEXT NOT NULL,
                    start_allocated_mb REAL NOT NULL, end_allocated_mb REAL NOT NULL,
                    min_used_mb REAL, max_used_mb REAL, max_size_mb REAL,
                    min_volume_free_mb REAL, samples INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(profile_id,database_name,logical_name,day)
                );
                CREATE TABLE IF NOT EXISTS sql_file_growth_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, detected_at TEXT NOT NULL,
                    profile_id TEXT NOT NULL, database_name TEXT NOT NULL,
                    logical_name TEXT NOT NULL, previous_mb REAL NOT NULL,
                    current_mb REAL NOT NULL, growth_mb REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sql_backup_history (
                    profile_id TEXT NOT NULL, database_name TEXT NOT NULL,
                    backup_set_id INTEGER NOT NULL, backup_type TEXT NOT NULL,
                    start_time TEXT, finish_time TEXT NOT NULL, backup_size_mb REAL,
                    compressed_size_mb REAL, is_copy_only INTEGER NOT NULL DEFAULT 0,
                    has_checksums INTEGER NOT NULL DEFAULT 0, first_lsn TEXT, last_lsn TEXT,
                    database_backup_lsn TEXT, destination TEXT, recorded_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id,database_name,backup_set_id)
                );
                CREATE TABLE IF NOT EXISTS sql_recovery_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL,
                    database_name TEXT NOT NULL, evidence_type TEXT NOT NULL,
                    performed_at TEXT NOT NULL, result TEXT NOT NULL, owner TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '', recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sql_job_history (
                    profile_id TEXT NOT NULL, job_name TEXT NOT NULL,
                    execution_id INTEGER NOT NULL, status TEXT NOT NULL, run_at TEXT,
                    duration_seconds INTEGER NOT NULL, message TEXT NOT NULL DEFAULT '',
                    next_run TEXT, recorded_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id,job_name,execution_id)
                );
                CREATE TABLE IF NOT EXISTS sql_job_steps (
                    profile_id TEXT NOT NULL, job_name TEXT NOT NULL,
                    instance_id INTEGER NOT NULL, step_id INTEGER NOT NULL,
                    step_name TEXT NOT NULL, status TEXT NOT NULL, run_at TEXT,
                    duration_seconds INTEGER NOT NULL, message TEXT NOT NULL DEFAULT '',
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id,job_name,instance_id)
                );
                CREATE TABLE IF NOT EXISTS sql_performance_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, collected_at TEXT NOT NULL,
                    profile_id TEXT NOT NULL, user_connections INTEGER NOT NULL,
                    active_sessions INTEGER NOT NULL, sql_cpu_percent REAL,
                    memory_grants_pending INTEGER NOT NULL, blocked_sessions INTEGER NOT NULL,
                    longest_blocked_seconds INTEGER NOT NULL, tempdb_used_mb REAL,
                    tempdb_free_mb REAL, details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS sql_query_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, collected_at TEXT NOT NULL,
                    profile_id TEXT NOT NULL, evidence_type TEXT NOT NULL,
                    query_signature TEXT NOT NULL, database_name TEXT,
                    application_name TEXT, host_name TEXT, login_name TEXT,
                    command TEXT, query_text TEXT NOT NULL DEFAULT '',
                    source_type TEXT NOT NULL DEFAULT '', source_name TEXT NOT NULL DEFAULT '',
                    is_system INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0,
                    cpu_ms INTEGER NOT NULL DEFAULT 0, logical_reads INTEGER NOT NULL DEFAULT 0,
                    writes INTEGER NOT NULL DEFAULT 0, executions INTEGER NOT NULL DEFAULT 0,
                    rank_score REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sql_blocking_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, collected_at TEXT NOT NULL,
                    profile_id TEXT NOT NULL, session_id INTEGER NOT NULL,
                    blocking_session_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT '', wait_type TEXT NOT NULL DEFAULT '',
                    wait_ms INTEGER NOT NULL DEFAULT 0, database_name TEXT NOT NULL DEFAULT '',
                    application_name TEXT NOT NULL DEFAULT '', host_name TEXT NOT NULL DEFAULT '',
                    login_name TEXT NOT NULL DEFAULT '', source_type TEXT NOT NULL DEFAULT '',
                    source_name TEXT NOT NULL DEFAULT '', query_text TEXT NOT NULL DEFAULT '',
                    transaction_age_seconds INTEGER NOT NULL DEFAULT 0,
                    open_transactions INTEGER NOT NULL DEFAULT 0,
                    is_blocker INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
                CREATE INDEX IF NOT EXISTS idx_events_target ON events(target);
                CREATE INDEX IF NOT EXISTS idx_health_samples_created_at ON health_samples(created_at);
                CREATE INDEX IF NOT EXISTS idx_score_incidents_status ON score_incidents(status);
                CREATE INDEX IF NOT EXISTS idx_incident_cycles_fingerprint ON incident_cycles(fingerprint,occurred_at);
                CREATE INDEX IF NOT EXISTS idx_component_changes_status ON component_changes(status,detected_at);
                CREATE INDEX IF NOT EXISTS idx_sql_samples_created ON sql_samples(created_at,profile_id);
                CREATE INDEX IF NOT EXISTS idx_backup_samples_checked ON backup_samples(checked_at,rule_id);
                CREATE INDEX IF NOT EXISTS idx_sql_file_samples_collected ON sql_file_samples(collected_at,profile_id,database_name);
                CREATE INDEX IF NOT EXISTS idx_sql_growth_events_detected ON sql_file_growth_events(detected_at,profile_id,database_name);
                CREATE INDEX IF NOT EXISTS idx_sql_backup_finish ON sql_backup_history(finish_time,profile_id,database_name);
                CREATE INDEX IF NOT EXISTS idx_sql_recovery_evidence ON sql_recovery_evidence(profile_id,database_name,performed_at);
                CREATE INDEX IF NOT EXISTS idx_sql_job_history ON sql_job_history(profile_id,job_name,run_at);
                CREATE INDEX IF NOT EXISTS idx_sql_job_steps ON sql_job_steps(profile_id,job_name,run_at);
                CREATE INDEX IF NOT EXISTS idx_sql_performance_samples ON sql_performance_samples(profile_id,collected_at);
                CREATE INDEX IF NOT EXISTS idx_sql_query_evidence ON sql_query_evidence(profile_id,collected_at,evidence_type);
                CREATE INDEX IF NOT EXISTS idx_sql_blocking_evidence ON sql_blocking_evidence(profile_id,collected_at,session_id);
                """
            )
            alert_columns = {row["name"] for row in db.execute("PRAGMA table_info(alert_state)")}
            for name, definition in {
                "category": "TEXT NOT NULL DEFAULT ''",
                "target": "TEXT NOT NULL DEFAULT ''",
                "condition": "TEXT NOT NULL DEFAULT ''",
                "message": "TEXT NOT NULL DEFAULT ''",
                "severity": "TEXT NOT NULL DEFAULT 'warning'",
                "first_seen": "TEXT",
            }.items():
                if name not in alert_columns:
                    db.execute(f"ALTER TABLE alert_state ADD COLUMN {name} {definition}")
            db.execute(
                "UPDATE alert_state SET first_seen=COALESCE(first_seen,last_seen) WHERE first_seen IS NULL"
            )
            action_columns = {row["name"] for row in db.execute("PRAGMA table_info(action_claims)")}
            if "attempts" not in action_columns:
                db.execute(
                    "ALTER TABLE action_claims ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1"
                )
            mail_columns = {row["name"] for row in db.execute("PRAGMA table_info(mail_queue)")}
            if "attachments" not in mail_columns:
                db.execute("ALTER TABLE mail_queue ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
            health_columns = {row["name"] for row in db.execute("PRAGMA table_info(health_samples)")}
            if "consumers" not in health_columns:
                db.execute(
                    "ALTER TABLE health_samples ADD COLUMN consumers TEXT NOT NULL DEFAULT '[]'"
                )
            if "root_cause" not in health_columns:
                db.execute(
                    "ALTER TABLE health_samples ADD COLUMN root_cause TEXT NOT NULL DEFAULT ''"
                )
            recovery_columns = {row["name"] for row in db.execute("PRAGMA table_info(sql_recovery_evidence)")}
            for name, definition in {
                "destination": "TEXT NOT NULL DEFAULT ''",
                "duration_minutes": "INTEGER",
            }.items():
                if name not in recovery_columns:
                    db.execute(f"ALTER TABLE sql_recovery_evidence ADD COLUMN {name} {definition}")
            query_columns = {row["name"] for row in db.execute("PRAGMA table_info(sql_query_evidence)")}
            for name, definition in {
                "query_text": "TEXT NOT NULL DEFAULT ''",
                "source_type": "TEXT NOT NULL DEFAULT ''",
                "source_name": "TEXT NOT NULL DEFAULT ''",
                "is_system": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in query_columns:
                    db.execute(f"ALTER TABLE sql_query_evidence ADD COLUMN {name} {definition}")
            invalid_pool_clause = (
                "kind='pool' AND (target_key LIKE 'ERROR (%' "
                "OR target_key='Not implemented' OR target_key=')')"
            )
            invalid_pool_keys = [
                row["target_key"]
                for row in db.execute(f"SELECT target_key FROM targets WHERE {invalid_pool_clause}")
            ]
            if invalid_pool_keys:
                db.execute(f"DELETE FROM targets WHERE {invalid_pool_clause}")
                db.executemany(
                    "UPDATE alert_state SET active=0,last_seen=? "
                    "WHERE category='pool' AND target=?",
                    [(utc_now(), key) for key in invalid_pool_keys],
                )
        if self.get_raw("configuration") is None:
            self.set("configuration", DEFAULT_SETTINGS)
        else:
            configuration = self.get("configuration")
            if merge_defaults(configuration, DEFAULT_SETTINGS):
                self.set("configuration", configuration)
        if self.get_raw("assurance_refresh_v2") is None:
            configuration = self.get("configuration")
            configuration.setdefault("assurance", {})["snapshot_interval_seconds"] = 60
            self.set("configuration", configuration)
            self.set("assurance_refresh_v2", True)
        if self.get_raw("server_local_timezone_v1") is None:
            configuration = self.get("configuration")
            configuration.setdefault("agent", {})["timezone"] = "SERVER_LOCAL"
            self.set("configuration", configuration)
            self.set("server_local_timezone_v1", True)
        if self.get_raw("sql_cpu_process_utilization_v3") is None:
            # Earlier builds stored 100 - ProcessUtilization, so their SQL CPU history is inverted.
            # Preserve every other performance metric while invalidating only the incorrect field.
            with self.connect() as db:
                db.execute("UPDATE sql_performance_samples SET sql_cpu_percent=NULL")
            runtime = self.get("sql_runtime", {})
            for profile in runtime.values():
                performance = profile.get("performance", {})
                performance["sql_cpu_percent"] = None
                performance.pop("assessment", None)
            if runtime:
                self.set("sql_runtime", runtime)
            self.set("sql_cpu_process_utilization_v3", True)

    def get_raw(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return None if row is None else str(row["value"])

    def get(self, key: str, default: Any = None) -> Any:
        raw = self.get_raw(key)
        return default if raw is None else json.loads(raw)

    def set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self.connect() as db:
            db.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, payload, utc_now()),
            )

    def add_event(
        self, severity: str, category: str, target: str, message: str, **details: Any
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(created_at,severity,category,target,message,details) VALUES(?,?,?,?,?,?)",
                (utc_now(), severity, category, target, message, json.dumps(details)),
            )

    def add_event_deduplicated(
        self,
        severity: str,
        category: str,
        target: str,
        message: str,
        window_seconds: int = 300,
        **details: Any,
    ) -> None:
        cutoff = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - max(1, window_seconds), UTC
        ).isoformat(timespec="seconds")
        with self.connect() as db:
            existing = db.execute(
                "SELECT 1 FROM events WHERE severity=? AND category=? AND target=? "
                "AND message=? AND created_at>=? LIMIT 1",
                (severity, category, target, message, cutoff),
            ).fetchone()
            if existing is None:
                db.execute(
                    "INSERT INTO events(created_at,severity,category,target,message,details) "
                    "VALUES(?,?,?,?,?,?)",
                    (utc_now(), severity, category, target, message, json.dumps(details)),
                )

    def deactivate_target_alerts(self, category: str, target: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE alert_state SET active=0,last_seen=? WHERE category=? AND target=?",
                (utc_now(), category, target),
            )

    def recent_events(self, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))

    def filtered_events(
        self,
        limit: int = 500,
        severity: str | None = None,
        query: str = "",
        since: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses, args = [], []
        if since:
            clauses.append("created_at>=?")
            args.append(since)
        if severity and severity != "All":
            clauses.append("severity=?")
            args.append(severity.lower())
        if query:
            clauses.append("(target LIKE ? OR message LIKE ? OR category LIKE ?)")
            pattern = f"%{query}%"
            args.extend([pattern, pattern, pattern])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(limit)
        with self.connect() as db:
            return list(db.execute(f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?", args))

    def target_events(self, target: str, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM events WHERE target=? ORDER BY id DESC LIMIT ?",
                    (target, limit),
                )
            )

    def active_alerts(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM alert_state WHERE active=1 "
                    "ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'error' THEN 1 "
                    "WHEN 'warning' THEN 2 ELSE 3 END, first_seen"
                )
            )

    def open_score_incident(
        self,
        incident_key: str,
        kind: str,
        category: str,
        target: str,
        condition: str,
        title: str,
        deduction: int,
        *,
        persistent: bool = False,
    ) -> None:
        now = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO score_incidents(incident_key,kind,category,target,condition,title,"
                "deduction,persistent,status,opened_at,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(incident_key) DO UPDATE SET "
                "kind=excluded.kind,category=excluded.category,target=excluded.target,"
                "condition=excluded.condition,title=excluded.title,deduction=excluded.deduction,"
                "persistent=excluded.persistent,status='open',last_seen=excluded.last_seen,"
                "opened_at=CASE WHEN score_incidents.status='resolved' THEN excluded.opened_at "
                "ELSE score_incidents.opened_at END,"
                "occurrences=CASE WHEN score_incidents.status='resolved' "
                "THEN score_incidents.occurrences+1 ELSE score_incidents.occurrences END,"
                "resolved_at=NULL,resolution=NULL",
                (
                    incident_key,
                    kind,
                    category,
                    target,
                    condition,
                    title,
                    max(0, int(deduction)),
                    int(persistent),
                    "open",
                    now,
                    now,
                ),
            )

    def resolve_score_incident(self, incident_id: int, resolution: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE score_incidents SET status='resolved',resolved_at=?,resolution=? "
                "WHERE id=? AND status='open'",
                (utc_now(), resolution, incident_id),
            )
            return cursor.rowcount > 0

    def resolve_score_incident_by_key(self, incident_key: str, resolution: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE score_incidents SET status='resolved',resolved_at=?,resolution=? "
                "WHERE incident_key=? AND status='open'",
                (utc_now(), resolution, incident_key),
            )
            return cursor.rowcount > 0

    def open_score_incidents(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM score_incidents WHERE status='open' "
                    "ORDER BY deduction DESC,opened_at"
                )
            )

    def score_incidents(
        self, *, since: str | None = None, until: str | None = None
    ) -> list[sqlite3.Row]:
        clauses, args = [], []
        if since:
            clauses.append("opened_at>=?")
            args.append(since)
        if until:
            clauses.append("opened_at<?")
            args.append(until)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as db:
            return list(
                db.execute(
                    f"SELECT * FROM score_incidents{where} ORDER BY opened_at DESC", args
                )
            )

    def record_incident_cycle(
        self,
        fingerprint: str,
        category: str,
        target: str,
        condition: str,
        *,
        threshold: int = 3,
        window_minutes: int = 15,
    ) -> int:
        now = datetime.now(UTC)
        cutoff = (now - timedelta(minutes=max(1, window_minutes))).isoformat(timespec="seconds")
        occurred_at = now.isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute(
                "INSERT INTO incident_cycles(fingerprint,category,target,condition,occurred_at) "
                "VALUES(?,?,?,?,?)",
                (fingerprint, category, target, condition, occurred_at),
            )
            count = int(
                db.execute(
                    "SELECT COUNT(*) FROM incident_cycles WHERE fingerprint=? AND occurred_at>=?",
                    (fingerprint, cutoff),
                ).fetchone()[0]
            )
        if count >= max(2, threshold):
            self.open_score_incident(
                f"repeat:{fingerprint}",
                "recurring",
                category,
                target,
                condition,
                f"Repeated issue {count} times within {window_minutes} minutes",
                3,
                persistent=True,
            )
        return count

    def record_component_change(
        self,
        change_key: str,
        component_type: str,
        item_key: str,
        item_name: str,
        property_name: str,
        old_value: str,
        new_value: str,
    ) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO component_changes(change_key,component_type,item_key,"
                "item_name,property_name,old_value,new_value,detected_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(change_key) DO UPDATE SET "
                "status='open',detected_at=excluded.detected_at,resolved_at=NULL,resolution=NULL "
                "WHERE component_changes.status='resolved'",
                (
                    change_key,
                    component_type,
                    item_key,
                    item_name,
                    property_name,
                    old_value,
                    new_value,
                    utc_now(),
                ),
            )
            return cursor.rowcount == 1

    def component_changes(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        component_type: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses, args = [], []
        if since:
            clauses.append("detected_at>=?")
            args.append(since)
        if until:
            clauses.append("detected_at<?")
            args.append(until)
        if component_type and component_type != "All":
            clauses.append("component_type=?")
            args.append(component_type)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as db:
            return list(
                db.execute(
                    f"SELECT * FROM component_changes{where} ORDER BY detected_at DESC", args
                )
            )

    def resolve_component_change(self, change_id: int, resolution: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE component_changes SET status='resolved',resolved_at=?,resolution=? "
                "WHERE id=? AND status='open'",
                (utc_now(), resolution, change_id),
            )
            return cursor.rowcount > 0

    def resolve_component_changes_for_item(self, item_key: str, resolution: str) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE component_changes SET status='resolved',resolved_at=?,resolution=? "
                "WHERE item_key=? AND status='open'",
                (utc_now(), resolution, item_key),
            )
            return cursor.rowcount

    def add_health_sample(
        self,
        cpu: float,
        memory: float,
        minimum_disk_free_percent: float | None,
        consumers: list[dict[str, Any]] | None = None,
    ) -> None:
        consumers = consumers or []
        lead = consumers[0] if consumers else None
        root_cause = (
            f"{lead['kind']}: {lead['name']} · CPU {lead['cpu']:.1f}% · "
            f"Memory {lead['memory_mb']:.1f} MB"
            if lead
            else "No process attribution available"
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO health_samples(created_at,cpu,memory,minimum_disk_free_percent,"
                "consumers,root_cause) VALUES(?,?,?,?,?,?)",
                (
                    utc_now(),
                    cpu,
                    memory,
                    minimum_disk_free_percent,
                    json.dumps(consumers),
                    root_cause,
                ),
            )

    def health_samples(self, since: str, limit: int = 2000) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM health_samples WHERE created_at>=? ORDER BY id LIMIT ?",
                    (since, limit),
                )
            )

    def add_sql_sample(self, profile_id: str, profile_name: str, database: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO sql_samples(created_at,profile_id,profile_name,database_name,state,"
                "data_mb,log_mb,total_mb) VALUES(?,?,?,?,?,?,?,?)",
                (
                    utc_now(), profile_id, profile_name, database.get("name", ""),
                    database.get("state", ""), float(database.get("data_mb", 0)),
                    float(database.get("log_mb", 0)), float(database.get("total_mb", 0)),
                ),
            )

    def sql_samples(self, since: str, until: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sql_samples WHERE created_at>=?"
        args: list[Any] = [since]
        if until:
            sql += " AND created_at<?"
            args.append(until)
        with self.connect() as db:
            return list(db.execute(sql + " ORDER BY created_at", args))

    def add_backup_sample(self, rule_id: str, rule_name: str, result: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO backup_samples(checked_at,rule_id,rule_name,ok,age_days,message) "
                "VALUES(?,?,?,?,?,?)",
                (
                    utc_now(), rule_id, rule_name, int(bool(result.get("ok"))),
                    result.get("age_days"), str(result.get("reason", "")),
                ),
            )

    def upsert_sql_backup_history(
        self, profile_id: str, database_name: str, rows: list[dict[str, Any]]
    ) -> None:
        with self.connect() as db:
            db.executemany(
                "INSERT INTO sql_backup_history(profile_id,database_name,backup_set_id,backup_type,"
                "start_time,finish_time,backup_size_mb,compressed_size_mb,is_copy_only,has_checksums,"
                "first_lsn,last_lsn,database_backup_lsn,destination,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id,database_name,backup_set_id) DO UPDATE SET destination=excluded.destination,recorded_at=excluded.recorded_at",
                [(
                    profile_id, database_name, int(row["backup_set_id"]), row["backup_type"],
                    row.get("start_time"), row["finish_time"], row.get("backup_size_mb"),
                    row.get("compressed_size_mb"), int(bool(row.get("is_copy_only"))),
                    int(bool(row.get("has_checksums"))), row.get("first_lsn"), row.get("last_lsn"),
                    row.get("database_backup_lsn"), row.get("destination"), utc_now(),
                ) for row in rows],
            )

    def sql_backup_history(self, since: str, profile_id: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM sql_backup_history WHERE finish_time>=?", [since]
        if profile_id:
            sql += " AND profile_id=?"
            args.append(profile_id)
        with self.connect() as db:
            return list(db.execute(sql + " ORDER BY finish_time DESC", args))

    def add_sql_recovery_evidence(
        self, profile_id: str, database_name: str, evidence: dict[str, Any]
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO sql_recovery_evidence(profile_id,database_name,evidence_type,"
                "performed_at,result,owner,notes,recorded_at,destination,duration_minutes) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (profile_id, database_name, evidence["evidence_type"], evidence["performed_at"],
                 evidence["result"], evidence["owner"], evidence.get("notes", ""), utc_now(),
                 evidence.get("destination", ""), evidence.get("duration_minutes")),
            )

    def sql_recovery_evidence(self, profile_id: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM sql_recovery_evidence", []
        if profile_id:
            sql += " WHERE profile_id=?"
            args.append(profile_id)
        with self.connect() as db:
            return list(db.execute(sql + " ORDER BY performed_at DESC,id DESC", args))

    def upsert_sql_job_execution(self, profile_id: str, job_name: str, job: dict[str, Any]) -> None:
        if not int(job.get("execution_id", 0)): return
        with self.connect() as db:
            db.execute(
                "INSERT INTO sql_job_history(profile_id,job_name,execution_id,status,run_at,"
                "duration_seconds,message,next_run,recorded_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id,job_name,execution_id) DO UPDATE SET status=excluded.status,"
                "duration_seconds=excluded.duration_seconds,message=excluded.message,next_run=excluded.next_run",
                (profile_id, job_name, int(job["execution_id"]), job["status"], job.get("last_run"),
                 int(job.get("duration_seconds", 0)), job.get("message", ""), job.get("next_run"), utc_now()),
            )

    def sql_job_history(self, profile_id: str | None = None, job_name: str | None = None) -> list[sqlite3.Row]:
        clauses, args = [], []
        if profile_id: clauses.append("profile_id=?"); args.append(profile_id)
        if job_name: clauses.append("job_name=?"); args.append(job_name)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            return list(db.execute("SELECT * FROM sql_job_history" + where + " ORDER BY run_at DESC", args))

    def upsert_sql_job_steps(self, profile_id: str, steps: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            db.executemany(
                "INSERT INTO sql_job_steps(profile_id,job_name,instance_id,step_id,step_name,status,"
                "run_at,duration_seconds,message,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(profile_id,job_name,instance_id) DO UPDATE SET status=excluded.status,"
                "duration_seconds=excluded.duration_seconds,message=excluded.message",
                [(profile_id, item["job_name"], int(item["instance_id"]), int(item["step_id"]),
                  item["step_name"], item["status"], item.get("run_at"),
                  int(item.get("duration_seconds", 0)), item.get("message", ""), utc_now()) for item in steps],
            )

    def sql_job_steps(self, profile_id: str | None = None, job_name: str | None = None) -> list[sqlite3.Row]:
        clauses, args = [], []
        if profile_id: clauses.append("profile_id=?"); args.append(profile_id)
        if job_name: clauses.append("job_name=?"); args.append(job_name)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            return list(db.execute("SELECT * FROM sql_job_steps" + where + " ORDER BY run_at DESC,instance_id DESC", args))

    def backup_samples(self, since: str, until: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM backup_samples WHERE checked_at>=?"
        args: list[Any] = [since]
        if until:
            sql += " AND checked_at<?"
            args.append(until)
        with self.connect() as db:
            return list(db.execute(sql + " ORDER BY checked_at", args))

    def add_sql_file_samples(
        self,
        profile_id: str,
        profile_name: str,
        database_name: str,
        files: list[dict[str, Any]],
        collected_at: str | None = None,
    ) -> list[dict[str, Any]]:
        collected_at = collected_at or utc_now()
        growth_events = []
        with self.connect() as db:
            for item in files:
                previous = db.execute(
                    "SELECT allocated_mb FROM sql_file_samples WHERE profile_id=? "
                    "AND database_name=? AND logical_name=? ORDER BY id DESC LIMIT 1",
                    (profile_id, database_name, item["logical_name"]),
                ).fetchone()
                if previous is not None and float(item["allocated_mb"]) > float(previous[0]):
                    growth = round(float(item["allocated_mb"]) - float(previous[0]), 2)
                    event = {
                        "logical_name": item["logical_name"],
                        "previous_mb": float(previous[0]),
                        "current_mb": float(item["allocated_mb"]),
                        "growth_mb": growth,
                    }
                    growth_events.append(event)
                    db.execute(
                        "INSERT INTO sql_file_growth_events(detected_at,profile_id,database_name,"
                        "logical_name,previous_mb,current_mb,growth_mb) VALUES(?,?,?,?,?,?,?)",
                        (
                            collected_at,
                            profile_id,
                            database_name,
                            item["logical_name"],
                            event["previous_mb"],
                            event["current_mb"],
                            growth,
                        ),
                    )
            db.executemany(
                "INSERT INTO sql_file_samples(collected_at,profile_id,profile_name,database_name,"
                "logical_name,file_type,physical_path,allocated_mb,used_mb,free_mb,"
                "utilization_percent,max_size_mb,growth_value,growth_is_percent,volume_mount,"
                "volume_total_mb,volume_free_mb,risk) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        collected_at,
                        profile_id,
                        profile_name,
                        database_name,
                        item["logical_name"],
                        item["file_type"],
                        item["physical_path"],
                        item["allocated_mb"],
                        item.get("used_mb"),
                        item.get("free_mb"),
                        item.get("utilization_percent"),
                        item.get("max_size_mb"),
                        item["growth_value"],
                        int(item["growth_is_percent"]),
                        item.get("volume_mount"),
                        item.get("volume_total_mb"),
                        item.get("volume_free_mb"),
                        item.get("risk", ""),
                    )
                    for item in files
                ],
            )
            day = collected_at[:10]
            for item in files:
                db.execute(
                    "INSERT INTO sql_capacity_daily(profile_id,database_name,logical_name,day,"
                    "start_allocated_mb,end_allocated_mb,min_used_mb,max_used_mb,max_size_mb,"
                    "min_volume_free_mb,samples) VALUES(?,?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(profile_id,database_name,logical_name,day) DO UPDATE SET "
                    "end_allocated_mb=excluded.end_allocated_mb,"
                    "min_used_mb=CASE WHEN sql_capacity_daily.min_used_mb IS NULL THEN excluded.min_used_mb "
                    "WHEN excluded.min_used_mb IS NULL THEN sql_capacity_daily.min_used_mb "
                    "ELSE MIN(sql_capacity_daily.min_used_mb,excluded.min_used_mb) END,"
                    "max_used_mb=CASE WHEN sql_capacity_daily.max_used_mb IS NULL THEN excluded.max_used_mb "
                    "WHEN excluded.max_used_mb IS NULL THEN sql_capacity_daily.max_used_mb "
                    "ELSE MAX(sql_capacity_daily.max_used_mb,excluded.max_used_mb) END,"
                    "max_size_mb=excluded.max_size_mb,"
                    "min_volume_free_mb=CASE WHEN sql_capacity_daily.min_volume_free_mb IS NULL "
                    "THEN excluded.min_volume_free_mb WHEN excluded.min_volume_free_mb IS NULL "
                    "THEN sql_capacity_daily.min_volume_free_mb ELSE MIN(sql_capacity_daily.min_volume_free_mb,"
                    "excluded.min_volume_free_mb) END,samples=sql_capacity_daily.samples+1",
                    (
                        profile_id,
                        database_name,
                        item["logical_name"],
                        day,
                        item["allocated_mb"],
                        item["allocated_mb"],
                        item.get("used_mb"),
                        item.get("used_mb"),
                        item.get("max_size_mb"),
                        item.get("volume_free_mb"),
                    ),
                )
        return growth_events

    def sql_file_samples(
        self,
        since: str,
        until: str | None = None,
        profile_id: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses, args = ["collected_at>=?"], [since]
        if until:
            clauses.append("collected_at<?")
            args.append(until)
        if profile_id:
            clauses.append("profile_id=?")
            args.append(profile_id)
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM sql_file_samples WHERE " + " AND ".join(clauses)
                    + " ORDER BY collected_at,profile_id,database_name,logical_name",
                    args,
                )
            )

    def sql_capacity_daily(
        self, since_day: str, profile_id: str | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sql_capacity_daily WHERE day>=?"
        args: list[Any] = [since_day]
        if profile_id:
            sql += " AND profile_id=?"
            args.append(profile_id)
        with self.connect() as db:
            return list(db.execute(sql + " ORDER BY day,database_name,logical_name", args))

    def sql_growth_events(self, since: str, profile_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sql_file_growth_events WHERE detected_at>=?"
        args: list[Any] = [since]
        if profile_id:
            sql += " AND profile_id=?"
            args.append(profile_id)
        with self.connect() as db:
            return list(db.execute(sql + " ORDER BY detected_at DESC", args))

    def performance_peaks(self, since: str, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as db:
            return list(
                db.execute(
                    "SELECT * FROM health_samples WHERE created_at>=? "
                    "ORDER BY MAX(cpu,memory) DESC, id DESC LIMIT ?",
                    (since, limit),
                )
            )

    def add_sql_performance_sample(self, profile_id: str, sample: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO sql_performance_samples(collected_at,profile_id,user_connections,"
                "active_sessions,sql_cpu_percent,memory_grants_pending,blocked_sessions,"
                "longest_blocked_seconds,tempdb_used_mb,tempdb_free_mb,details) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sample["collected_at"], profile_id, sample["user_connections"],
                 sample["active_sessions"], sample.get("sql_cpu_percent"),
                 sample["memory_grants_pending"], sample["blocked_sessions"],
                 sample["longest_blocked_seconds"], sample.get("tempdb_used_mb"),
                 sample.get("tempdb_free_mb"), json.dumps(sample, default=str)),
            )

    def sql_performance_samples(self, since: str, profile_id: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sql_performance_samples WHERE collected_at>=?"
        args: list[Any] = [since]
        if profile_id:
            sql += " AND profile_id=?"
            args.append(profile_id)
        with self.connect() as db:
            return list(db.execute(sql + " ORDER BY collected_at,profile_id", args))

    def add_sql_query_evidence(self, profile_id: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        collected_at = utc_now()
        with self.connect() as db:
            db.executemany(
                "INSERT INTO sql_query_evidence(collected_at,profile_id,evidence_type,"
                "query_signature,database_name,application_name,host_name,login_name,command,"
                "query_text,source_type,source_name,is_system,duration_ms,cpu_ms,logical_reads,writes,executions,rank_score) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(collected_at, profile_id, row["evidence_type"], row["query_signature"],
                  row.get("database_name"), row.get("application_name"), row.get("host_name"),
                  row.get("login_name"), row.get("command"), row.get("query_text", ""),
                  row.get("source_type", ""), row.get("source_name", ""),
                  int(bool(row.get("is_system", False))), int(row.get("duration_ms", 0)),
                  int(row.get("cpu_ms", 0)), int(row.get("logical_reads", 0)),
                  int(row.get("writes", 0)), int(row.get("executions", 0)),
                  float(row.get("rank_score", 0))) for row in rows],
            )
            db.execute(
                "DELETE FROM sql_query_evidence WHERE profile_id=? AND id NOT IN "
                "(SELECT id FROM sql_query_evidence WHERE profile_id=? ORDER BY id DESC LIMIT 5000)",
                (profile_id, profile_id),
            )

    def sql_query_evidence(
        self, since: str, profile_id: str | None = None, limit: int = 200
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sql_query_evidence WHERE collected_at>=?"
        args: list[Any] = [since]
        if profile_id:
            sql += " AND profile_id=?"
            args.append(profile_id)
        args.append(max(1, min(5000, limit)))
        with self.connect() as db:
            return list(db.execute(sql + " ORDER BY collected_at DESC,rank_score DESC LIMIT ?", args))

    def add_sql_blocking_evidence(
        self, profile_id: str, collected_at: str, rows: list[dict[str, Any]]
    ) -> None:
        if not rows:
            return
        with self.connect() as db:
            db.executemany(
                "INSERT INTO sql_blocking_evidence(collected_at,profile_id,session_id,"
                "blocking_session_id,status,wait_type,wait_ms,database_name,application_name,"
                "host_name,login_name,source_type,source_name,query_text,transaction_age_seconds,"
                "open_transactions,is_blocker) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        collected_at,
                        profile_id,
                        int(row.get("session_id", 0)),
                        int(row.get("blocking_session_id", 0)),
                        str(row.get("status", "")),
                        str(row.get("wait_type", "")),
                        int(row.get("wait_ms", 0)),
                        str(row.get("database_name", "")),
                        str(row.get("application_name", "")),
                        str(row.get("host_name", "")),
                        str(row.get("login_name", "")),
                        str(row.get("source_type", "")),
                        str(row.get("source_name", "")),
                        str(row.get("query_text", "")),
                        int(row.get("transaction_age_seconds", 0)),
                        int(row.get("open_transactions", 0)),
                        int(bool(row.get("is_blocker", False))),
                    )
                    for row in rows
                ],
            )

    def sql_blocking_evidence(
        self, since: str, profile_id: str | None = None, limit: int = 1000
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sql_blocking_evidence WHERE collected_at>=?"
        args: list[Any] = [since]
        if profile_id:
            sql += " AND profile_id=?"
            args.append(profile_id)
        args.append(max(1, min(10000, limit)))
        with self.connect() as db:
            return list(
                db.execute(sql + " ORDER BY collected_at DESC,id DESC LIMIT ?", args)
            )

    def apply_retention(self, retention_days: int, max_events: int) -> None:
        cutoff = datetime.now(UTC).timestamp() - max(1, retention_days) * 86400
        cutoff_text = datetime.fromtimestamp(cutoff, UTC).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute("DELETE FROM health_samples WHERE created_at<?", (cutoff_text,))
            db.execute("DELETE FROM events WHERE created_at<?", (cutoff_text,))
            db.execute("DELETE FROM sql_samples WHERE created_at<?", (cutoff_text,))
            db.execute("DELETE FROM backup_samples WHERE checked_at<?", (cutoff_text,))
            db.execute("DELETE FROM sql_file_samples WHERE collected_at<?", (cutoff_text,))
            db.execute("DELETE FROM sql_capacity_daily WHERE day<?", (cutoff_text[:10],))
            db.execute("DELETE FROM sql_file_growth_events WHERE detected_at<?", (cutoff_text,))
            db.execute("DELETE FROM sql_backup_history WHERE finish_time<?", (cutoff_text,))
            db.execute("DELETE FROM sql_job_history WHERE recorded_at<?", (cutoff_text,))
            db.execute("DELETE FROM sql_job_steps WHERE recorded_at<?", (cutoff_text,))
            db.execute("DELETE FROM sql_performance_samples WHERE collected_at<?", (cutoff_text,))
            db.execute("DELETE FROM sql_query_evidence WHERE collected_at<?", (cutoff_text,))
            db.execute("DELETE FROM sql_blocking_evidence WHERE collected_at<?", (cutoff_text,))
            db.execute(
                "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (max(1000, max_events),),
            )

    def targets(self, kind: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM targets", ()
        if kind:
            sql, args = sql + " WHERE kind=?", (kind,)
        with self.connect() as db:
            return list(db.execute(sql + " ORDER BY display_name", args))

    def upsert_target(
        self, kind: str, key: str, name: str, config: dict[str, Any] | None = None
    ) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO targets(kind,target_key,display_name,config) VALUES(?,?,?,?) "
                "ON CONFLICT(kind,target_key) DO UPDATE SET display_name=excluded.display_name",
                (kind, key, name, json.dumps(config or {})),
            )

    def set_target_enabled(self, kind: str, key: str, enabled: bool) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE targets SET enabled=? WHERE kind=? AND target_key=?",
                (int(enabled), kind, key),
            )
        if not enabled:
            self.deactivate_target_alerts(kind, key)
            runtime = self.get("recovery_runtime", {})
            runtime.pop(key if kind == "service" else f"{kind}:{key}", None)
            self.set("recovery_runtime", runtime)

    def set_target_config(self, kind: str, key: str, config: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE targets SET config=? WHERE kind=? AND target_key=?",
                (json.dumps(config), kind, key),
            )

    def target(self, kind: str, key: str) -> sqlite3.Row | None:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM targets WHERE kind=? AND target_key=?", (kind, key)
            ).fetchone()

    def claim_action(
        self,
        claim_key: str,
        kind: str,
        target: str,
        action: str,
        scheduled_for: str,
        *,
        retry_failed: bool = False,
        max_attempts: int = 3,
        retry_delay_seconds: int = 60,
    ) -> bool:
        """Atomically claim an action so concurrent/restarted agents cannot run it twice."""
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO action_claims"
                "(claim_key,claimed_at,kind,target,action,scheduled_for) VALUES(?,?,?,?,?,?)",
                (claim_key, utc_now(), kind, target, action, scheduled_for),
            )
            if cursor.rowcount == 1:
                return True
            if not retry_failed:
                return False
            cutoff = datetime.fromtimestamp(
                datetime.now(UTC).timestamp() - max(10, retry_delay_seconds), UTC
            ).isoformat(timespec="seconds")
            retry = db.execute(
                "UPDATE action_claims SET status='claimed',claimed_at=?,attempts=attempts+1 "
                "WHERE claim_key=? AND status='failed' AND attempts<? AND claimed_at<=?",
                (utc_now(), claim_key, max(1, max_attempts), cutoff),
            )
            return retry.rowcount == 1

    def finish_action(self, claim_key: str, ok: bool, result: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE action_claims SET status=?,result=? WHERE claim_key=?",
                ("completed" if ok else "failed", result[:1000], claim_key),
            )
