from contextlib import nullcontext

from millensys_guardian.notifier import Notifier
from millensys_guardian.sql_monitor import (
    SQL_PROCESS_CPU_QUERY,
    SqlServerMonitor,
    capacity_risk,
    connection_string,
    duration_text,
    kill_sessions,
    performance_root_cause,
    start_agent_job,
)
from millensys_guardian.store import Store


class FakeCursor:
    def __init__(self, database_row, job_rows, capacity_rows=None, backup_rows=None, running_rows=None, metadata_rows=None, step_rows=None):
        self.database_row = database_row
        self.job_rows = job_rows
        self.capacity_rows = capacity_rows or []
        self.backup_rows = backup_rows
        self.running_rows = running_rows or []
        self.metadata_rows = metadata_rows or []
        self.step_rows = step_rows or []
        self.rows = []

    def execute(self, sql, *parameters):
        if "msdb.dbo.backupset" in sql:
            if self.backup_rows is None:
                raise PermissionError("SELECT permission denied on backupset")
            self.rows = self.backup_rows
        elif "recovery_model_desc" in sql:
            self.rows = [("FULL",)]
        elif "sysjobactivity" in sql:
            self.rows = self.running_rows
        elif "sysschedules" in sql:
            self.rows = self.metadata_rows
        elif "h.step_id>0" in sql:
            self.rows = self.step_rows
        elif "sysjobs" in sql:
            self.rows = self.job_rows
        elif "sys.database_files" in sql:
            self.rows = self.capacity_rows
        else:
            self.rows = [self.database_row]
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def commit(self):
        return None


def test_connection_string_supports_windows_and_sql_authentication():
    windows = connection_string(
        {"server": "db01", "database": "ERP", "auth_mode": "windows"}
    )
    assert "SERVER=db01" in windows
    assert "Trusted_Connection=yes" in windows
    assert "UID=" not in windows

    sql = connection_string(
        {"server": "db01", "database": "ERP", "username": "monitor"}, "secret"
    )
    assert "UID=monitor" in sql
    assert "PWD=secret" in sql
    assert "APP=MiMonitoringService" in sql
    assert duration_text(10203) == "01:02:03"


def test_sql_process_cpu_uses_process_utilization_without_inversion():
    assert "ProcessUtilization" in SQL_PROCESS_CPU_QUERY
    assert "100-" not in SQL_PROCESS_CPU_QUERY.replace(" ", "")


def test_performance_root_cause_names_the_largest_cpu_evidence():
    cause = performance_root_cause("sql_cpu_percent", {"active_sessions": 4}, [
        {"query_signature": "0x00000000AABBCCDD", "database_name": "ERP", "cpu_ms": 9000},
        {"query_signature": "0x0000000011223344", "database_name": "ERP", "cpu_ms": 500},
    ])
    assert "AABBCCDD" in cause
    assert "ERP" in cause


def test_job_results_are_persisted_and_notified_only_once(tmp_path, monkeypatch):
    cursor = FakeCursor(
        ("ONLINE", 100.0, 20.0),
        [
            ("Nightly backup", 41, 0, 20261113, 13000, 125, "Disk full", 20261114, 13000),
            ("Index maintenance", 52, 1, 20261113, 20000, 304, "OK", 20261114, 20000),
        ],
    )
    monkeypatch.setattr(
        "millensys_guardian.sql_monitor._pyodbc",
        lambda: type("Odbc", (), {"connect": lambda *_args, **_kwargs: FakeConnection(cursor)}),
    )
    store = Store(tmp_path / "sql.db")
    configuration = store.get("configuration")
    configuration["email"]["recipients"] = ["support@millensys.com"]
    store.set("configuration", configuration)
    notifier = Notifier(store)
    monitor = SqlServerMonitor(store, notifier)
    config = {
        "sql_monitors": [
            {
                "id": "primary",
                "name": "Production SQL",
                "server": "db01",
                "database": "ERP",
                "auth_mode": "windows",
                "jobs": ["Nightly backup", "Index maintenance", "Missing job"],
                "notify_failure": True,
                "notify_success": True,
            }
        ]
    }

    monitor.process(config)
    monitor.process(config)

    runtime = store.get("sql_runtime")["primary"]
    assert runtime["status"] == "Connected"
    assert runtime["database"]["state"] == "ONLINE"
    assert runtime["jobs"]["Nightly backup"]["status"] == "Failed"
    assert runtime["jobs"]["Index maintenance"]["duration"] == "00:03:04"
    assert runtime["jobs"]["Nightly backup"]["intelligence"]["failure_streak"] == 1
    assert runtime["jobs"]["Missing job"]["status"] == "Job not found"
    with store.connect() as db:
        queued = db.execute("SELECT subject FROM mail_queue ORDER BY id").fetchall()
    assert len(queued) == 2
    assert queued[0][0].endswith("[SQL JOB FAILED] Nightly backup")
    assert queued[1][0].endswith("[SQL JOB SUCCESS] Index maintenance")
    assert len(store.sql_job_history("primary")) == 2


def test_database_growth_notification_uses_previous_sample(tmp_path, monkeypatch):
    cursor = FakeCursor(("ONLINE", 100.0, 20.0), [])
    monkeypatch.setattr(
        "millensys_guardian.sql_monitor._pyodbc",
        lambda: type("Odbc", (), {"connect": lambda *_args, **_kwargs: FakeConnection(cursor)}),
    )
    store = Store(tmp_path / "growth.db")
    configuration = store.get("configuration")
    configuration["email"]["recipients"] = ["support@millensys.com"]
    store.set("configuration", configuration)
    monitor = SqlServerMonitor(store, Notifier(store))
    config = {
        "sql_monitors": [
            {
                "id": "primary",
                "server": "db01",
                "database": "ERP",
                "auth_mode": "windows",
                "notify_growth": True,
                "growth_notify_mb": 10,
            }
        ]
    }
    monitor.process(config)
    cursor.database_row = ("ONLINE", 112.0, 20.0)
    monitor.process(config)

    with store.connect() as db:
        subject = db.execute("SELECT subject FROM mail_queue").fetchone()[0]
    assert subject.endswith("[SQL GROWTH] ERP")
    assert store.get("sql_runtime")["primary"]["database"]["growth_mb"] == 12.0


def test_connection_failure_isolated_per_profile(tmp_path, monkeypatch):
    def connect(connection, **_kwargs):
        if "SERVER=bad" in connection:
            raise OSError("login failed")
        return nullcontext(FakeConnection(FakeCursor(("ONLINE", 1, 1), [])))

    monkeypatch.setattr(
        "millensys_guardian.sql_monitor._pyodbc",
        lambda: type("Odbc", (), {"connect": connect}),
    )
    store = Store(tmp_path / "failure.db")
    monitor = SqlServerMonitor(store, Notifier(store))
    monitor.process(
        {
            "sql_monitors": [
                {"id": "bad", "server": "bad", "database": "master", "auth_mode": "windows"},
                {"id": "good", "server": "good", "database": "master", "auth_mode": "windows"},
            ]
        }
    )
    runtime = store.get("sql_runtime")
    assert runtime["bad"]["status"] == "Connection failed"
    assert runtime["good"]["status"] == "Connected"


def test_capacity_collection_persists_file_utilization_and_risks(tmp_path, monkeypatch):
    cursor = FakeCursor(
        ("ONLINE", 900.0, 100.0),
        [],
        [
            (
                "ERP_Data",
                "ROWS",
                r"D:\SQLData\ERP.mdf",
                1000.0,
                950.0,
                1200.0,
                10.0,
                1,
                "D:\\",
                100000.0,
                4000.0,
            )
        ],
    )
    monkeypatch.setattr(
        "millensys_guardian.sql_monitor._pyodbc",
        lambda: type("Odbc", (), {"connect": lambda *_args, **_kwargs: FakeConnection(cursor)}),
    )
    store = Store(tmp_path / "capacity.db")
    monitor = SqlServerMonitor(store, Notifier(store))
    monitor.process(
        {
            "sql_monitors": [
                {
                    "id": "primary",
                    "name": "Production SQL",
                    "server": "db01",
                    "database": "ERP",
                    "auth_mode": "windows",
                    "capacity_interval_minutes": 15,
                }
            ]
        }
    )

    capacity = store.get("sql_runtime")["primary"]["capacity"]
    assert capacity["status"] == "Attention"
    assert capacity["files"][0]["utilization_percent"] == 95.0
    assert "Percentage autogrowth" in capacity["files"][0]["risk"]
    assert "less than 5 GB" in capacity["files"][0]["risk"]
    samples = store.sql_file_samples("2026-01-01T00:00:00+00:00")
    assert len(samples) == 1
    assert samples[0]["logical_name"] == "ERP_Data"
    assert samples[0]["used_mb"] == 950.0


def test_capacity_risk_explains_safe_autogrowth_and_limits():
    risk = capacity_risk(
        {
            "allocated_mb": 950,
            "utilization_percent": 92,
            "volume_free_mb": 3000,
            "growth_value": 32,
            "growth_is_percent": False,
            "max_size_mb": 1000,
        }
    )
    assert "utilization" in risk
    assert "5 GB" in risk
    assert "smaller than 64 MB" in risk
    assert "configured maximum" in risk


def test_sql_native_backup_assurance_is_collected_and_persisted(tmp_path, monkeypatch):
    now = __import__("datetime").datetime.now()
    cursor = FakeCursor(
        ("ONLINE", 100.0, 20.0), [], backup_rows=[
            (1, "D", now, now, 100.0, 50.0, 0, 1, 100, 200, 100, r"D:\Backup\ERP.bak"),
            (2, "L", now, now, 5.0, 2.0, 0, 1, 200, 300, 100, r"D:\Backup\ERP.trn"),
        ],
    )
    monkeypatch.setattr(
        "millensys_guardian.sql_monitor._pyodbc",
        lambda: type("Odbc", (), {"connect": lambda *_args, **_kwargs: FakeConnection(cursor)}),
    )
    store = Store(tmp_path / "backup-assurance.db")
    SqlServerMonitor(store, Notifier(store)).process({"sql_monitors": [{
        "id": "primary", "name": "Production SQL", "server": "db01",
        "database": "ERP", "auth_mode": "windows",
    }]})

    assurance = store.get("sql_runtime")["primary"]["backup_assurance"]
    assert assurance["status"] == "Healthy"
    assert assurance["chain_status"] == "Continuous"
    assert len(store.sql_backup_history("2026-01-01T00:00:00+00:00")) == 2


def test_currently_running_job_comes_from_agent_activity(tmp_path, monkeypatch):
    now = __import__("datetime").datetime.now()
    cursor = FakeCursor(
        ("ONLINE", 1, 1),
        [("ETL", 5, 1, 20261114, 10000, 200, "OK", 20261115, 10000)],
        running_rows=[("ETL", now - __import__("datetime").timedelta(minutes=7))],
    )
    monkeypatch.setattr("millensys_guardian.sql_monitor._pyodbc", lambda: type("Odbc", (), {"connect": lambda *_args, **_kwargs: FakeConnection(cursor)}))
    store = Store(tmp_path / "running.db")
    SqlServerMonitor(store, Notifier(store)).process({"sql_monitors": [{"id": "p", "server": "db", "database": "ERP", "auth_mode": "windows", "jobs": ["ETL"], "job_long_running_minutes": 5}]})
    job = store.get("sql_runtime")["p"]["jobs"]["ETL"]
    assert job["status"] == "Running"
    assert job["duration_seconds"] >= 420
    assert job["long_running"] is True


def test_job_schedule_and_failed_step_evidence_are_retained(tmp_path, monkeypatch):
    cursor = FakeCursor(
        ("ONLINE", 1, 1),
        [("ETL", 9, 0, 20261114, 10000, 305, "Step failed", 20261115, 10000)],
        metadata_rows=[("ETL", 1, 1, 0)],
        step_rows=[("ETL", 8, 2, "Load warehouse", 0, 20261114, 10000, 305, "Conversion failed")],
    )
    monkeypatch.setattr("millensys_guardian.sql_monitor._pyodbc", lambda: type("Odbc", (), {"connect": lambda *_args, **_kwargs: FakeConnection(cursor)}))
    store = Store(tmp_path / "steps.db")
    SqlServerMonitor(store, Notifier(store)).process({"sql_monitors": [{"id": "p", "server": "db", "database": "ERP", "auth_mode": "windows", "jobs": ["ETL"]}]})
    job = store.get("sql_runtime")["p"]["jobs"]["ETL"]
    assert job["job_enabled"] is True
    assert job["enabled_schedule_count"] == 0
    assert job["latest_failed_step"]["step_name"] == "Load warehouse"
    assert store.sql_job_steps("p", "ETL")[0]["message"] == "Conversion failed"


def test_manual_job_start_requires_policy_and_monitored_job(monkeypatch):
    profile = {"server": "db", "database": "ERP", "auth_mode": "windows", "jobs": ["ETL"]}
    try:
        start_agent_job(profile, "ETL")
        raise AssertionError("disabled action should fail")
    except PermissionError:
        pass
    cursor = FakeCursor(("ONLINE", 1, 1), [])
    monkeypatch.setattr("millensys_guardian.sql_monitor._pyodbc", lambda: type("Odbc", (), {"connect": lambda *_args, **_kwargs: FakeConnection(cursor)}))
    profile["allow_manual_job_run"] = True
    start_agent_job(profile, "ETL")
    try:
        start_agent_job(profile, "Unmonitored")
        raise AssertionError("unmonitored action should fail")
    except ValueError:
        pass


def test_session_inventory_persists_only_blocking_chain(tmp_path):
    class SessionCursor:
        def execute(self, *_args):
            return self

        def fetchall(self):
            return [
                (60, "sleeping", 0, "", 0, "ERP", "ERP API", "APP01", "erp", None, "BEGIN TRAN UPDATE dbo.Invoice SET Status=?", None, "Ad hoc or prepared", "ERP API", 1, 120, 1, "2026-11-14T10:00:00"),
                (61, "suspended", 60, "LCK_M_X", 5000, "ERP", "ERP API", "APP02", "erp", "UPDATE", "UPDATE dbo.Invoice SET Status=?", 123, "Stored procedure", "[ERP].[dbo].[PostInvoice]", 1, 30, 1, "2026-11-14T10:01:00"),
                (62, "sleeping", 0, "", 0, "ERP", "SSMS", "DBA01", "dba", None, None, None, "Session / connection", "SSMS", 0, 0, 1, "2026-11-14T10:02:00"),
            ]

    store = Store(tmp_path / "sessions.db")
    state = {}
    SqlServerMonitor(store, Notifier(store))._collect_sessions_if_due(
        {"id": "primary", "server": "db01", "session_interval_seconds": 15}, state, SessionCursor()
    )

    assert len(state["sessions"]["sessions"]) == 3
    assert state["sessions"]["blocking_count"] == 1
    assert state["sessions"]["blocker_count"] == 1
    assert state["sessions"]["sessions"][0]["query_evidence_kind"] == "Most recently submitted"
    persisted = store.sql_blocking_evidence("2026-01-01T00:00:00+00:00", "primary")
    assert {row["session_id"] for row in persisted} == {60, 61}


def test_kill_sessions_revalidates_and_protects_system_sessions(monkeypatch):
    class KillCursor:
        def __init__(self):
            self.rows = []
            self.killed = []
            self.timeout = 0

        def execute(self, sql, *parameters):
            if sql.startswith("SELECT @@SPID"):
                self.rows = [(70, 45, 1, "sleeping", "legacy", "app", "old", "2026-11-14T09:00:00"), (70, 70, 1, "running", "monitor", "MiMonitoringService", "db", "2026-11-14T09:01:00"), (70, 80, 1, "suspended", "erp", "ERP API", "app01", "2026-11-14T09:02:00")]
            elif sql.startswith("KILL "):
                self.killed.append(sql)
            return self

        def fetchall(self):
            return self.rows

    cursor = KillCursor()
    monkeypatch.setattr("millensys_guardian.sql_monitor._pyodbc", lambda: type("Odbc", (), {"connect": lambda *_args, **_kwargs: FakeConnection(cursor)}))
    results = kill_sessions({"server": "db01", "database": "ERP", "auth_mode": "windows", "allow_session_termination": True}, [45, 70, 80, 99])

    assert cursor.killed == ["KILL 80"]
    assert next(item for item in results if item["session_id"] == 80)["ok"] is True
    assert "Protected" in next(item for item in results if item["session_id"] == 70)["message"]
    assert "no longer exists" in next(item for item in results if item["session_id"] == 99)["message"]


def test_kill_sessions_requires_policy_and_rejects_reused_spid(monkeypatch):
    profile = {"server": "db01", "database": "ERP", "auth_mode": "windows"}
    try:
        kill_sessions(profile, [80])
        raise AssertionError("disabled session termination should fail")
    except PermissionError:
        pass

    class Cursor:
        def __init__(self):
            self.timeout = 0
            self.killed = []
            self.rows = []

        def execute(self, sql, *_parameters):
            if sql.startswith("SELECT @@SPID"):
                self.rows = [(90, 80, 1, "running", "other_user", "Other App", "app02", "2026-11-14T11:00:00")]
            elif sql.startswith("KILL "):
                self.killed.append(sql)
            return self

        def fetchall(self): return self.rows

    cursor = Cursor()
    monkeypatch.setattr("millensys_guardian.sql_monitor._pyodbc", lambda: type("Odbc", (), {"connect": lambda *_args, **_kwargs: FakeConnection(cursor)}))
    profile["allow_session_termination"] = True
    result = kill_sessions(profile, [80], expected_sessions={80: {"login_name": "erp", "application_name": "ERP API", "host_name": "app01", "login_time": "2026-11-14T09:02:00"}})
    assert result[0]["ok"] is False
    assert "reused" in result[0]["message"]
    assert cursor.killed == []
