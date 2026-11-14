import json

from millensys_guardian.notifier import Notifier
from millensys_guardian.sql_monitor import SqlServerMonitor, sanitize_query_text
from millensys_guardian.sql_performance import (
    counter_delta,
    io_delta,
    performance_baselines,
    period_baseline_comparison,
    sustained_performance_assessment,
    wait_category,
    wait_deltas,
)
from millensys_guardian.store import Store


class PerformanceCursor:
    def __init__(self):
        self.rows = []

    def execute(self, sql, *_parameters):
        if "GROUP BY program_name" in sql:
            self.rows = [("ERP", "APP01", "service", "ERP", 7)]
        elif "COUNT(*)" in sql and "dm_exec_sessions" in sql:
            self.rows = [(12, 4)]
        elif "dm_os_ring_buffers" in sql:
            self.rows = [(27,)]
        elif "dm_exec_query_memory_grants" in sql:
            self.rows = [(2, 9000)]
        elif "dm_exec_requests r" in sql:
            self.rows = [(51, 1, "ERP", "Web", "APP01", "service", "SELECT", 90000,
                          20000, 4000, 20, "0xAABB", "SELECT * FROM Orders WHERE PatientId=42", 7,
                          "Stored procedure", "[ERP].[dbo].[GetOrders]", 1)]
        elif "dm_exec_requests" in sql:
            self.rows = [(3, 18000, 51)]
        elif "dm_db_file_space_usage" in sql:
            self.rows = [(100.0, 50.0, 25.0, 500.0)]
        elif "dm_io_virtual_file_stats" in sql:
            self.rows = [("ERP", "ERP_Data", "ROWS", 5, 1, 110, 1300, 20 * 1048576, 55, 700, 8 * 1048576)]
        elif "dm_os_wait_stats" in sql:
            self.rows = [("PAGEIOLATCH_SH", 12, 1200), ("SLEEP_TASK", 500, 90000)]
        elif "dm_exec_query_stats" in sql:
            self.rows = [("0xCCDD", "ERP", 50, 90000, 100000, 200, 120000, "2026-11-14",
                          "EXEC dbo.RefreshQueue @SiteId=12", 9, "Stored procedure",
                          "[ERP].[dbo].[RefreshQueue]", 0, "ERP")]
        else:
            raise AssertionError(f"Unexpected query: {sql}")
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class PerformanceCursorWithoutQueryEvidence(PerformanceCursor):
    def execute(self, sql, *_parameters):
        if "dm_exec_requests r" in sql:
            raise PermissionError("VIEW SERVER STATE denied for query evidence")
        return super().execute(sql, *_parameters)


class ConfigurationCursor:
    def __init__(self, max_memory: int):
        self.max_memory = max_memory
        self.rows = []

    def execute(self, sql, *_parameters):
        if "FROM sys.configurations" in sql:
            self.rows = [("max server memory (MB)", self.max_memory, self.max_memory)]
        elif "FROM sys.databases WHERE name=?" in sql:
            self.rows = [("FULL", 150, 0, 0, "CHECKSUM", 0, "OFF", 0, 0, 1,
                          "NONE", 1, 1, 1, "sa")]
        else:
            raise AssertionError(f"Unexpected query: {sql}")
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class DeadlockCursor:
    XML = """<RingBufferTarget><event name="xml_deadlock_report" timestamp="2026-11-14T10:00:00Z"><data name="xml_report"><value><deadlock><victim-list><victimProcess id="p1" /></victim-list><process-list><process id="p1" spid="52" currentdb="7" clientapp="ERP" hostname="APP01" loginname="svc" isolationlevel="read committed" procname="dbo.SaveOrder"><inputbuf>UPDATE Orders SET Status='Done' WHERE Id=42</inputbuf></process><process id="p2" spid="53" currentdb="7" clientapp="ERP" procname="dbo.SaveLine"><inputbuf>UPDATE OrderLines SET Qty=2 WHERE Id=5</inputbuf></process></process-list><resource-list><keylock /></resource-list></deadlock></value></data></event></RingBufferTarget>"""

    def execute(self, sql, *_parameters):
        assert "system_health" in sql
        return self

    def fetchone(self):
        return (self.XML,)


def test_counter_deltas_handle_first_sample_and_resets():
    assert counter_delta(20, None) == 0
    assert counter_delta(20, 12) == 8
    assert counter_delta(3, 12) == 0


def test_io_and_wait_deltas_are_actionable_and_ignore_idle_waits():
    previous = {
        "io_counters": [{"key": "5:1", "reads": 100, "read_stall_ms": 1000,
                         "bytes_read": 10 * 1048576, "writes": 50,
                         "write_stall_ms": 600, "bytes_written": 5 * 1048576}],
        "wait_counters": [{"wait_type": "PAGEIOLATCH_SH", "tasks": 10, "wait_ms": 1000},
                          {"wait_type": "SLEEP_TASK", "tasks": 1, "wait_ms": 1}],
    }
    current_io = [{"key": "5:1", "database": "ERP", "file": "ERP_Data", "reads": 110,
                   "read_stall_ms": 1300, "bytes_read": 20 * 1048576, "writes": 55,
                   "write_stall_ms": 700, "bytes_written": 8 * 1048576}]
    current_waits = [{"wait_type": "PAGEIOLATCH_SH", "tasks": 12, "wait_ms": 1200},
                     {"wait_type": "SLEEP_TASK", "tasks": 500, "wait_ms": 90000}]

    io = io_delta(current_io, previous)[0]
    waits = wait_deltas(current_waits, previous)
    assert io["read_latency_ms"] == 30.0
    assert io["write_latency_ms"] == 20.0
    assert io["read_mb_delta"] == 10.0
    assert waits == [{"category": "Storage I/O", "wait_ms": 200.0, "tasks": 2,
                      "top_wait": "PAGEIOLATCH_SH", "top_wait_ms": 200.0}]
    assert wait_category("LCK_M_X") == "Blocking"


def test_performance_collection_persists_summary_and_evidence(tmp_path):
    store = Store(tmp_path / "performance.db")
    monitor = SqlServerMonitor(store, Notifier(store))
    state = {}
    monitor._collect_performance_if_due(
        {"id": "primary", "server": "db01", "performance_interval_minutes": 5},
        state,
        PerformanceCursor(),
    )

    performance = state["performance"]
    assert performance["status"] == "Attention"
    assert performance["user_connections"] == 12
    assert performance["active_sessions"] == 4
    assert performance["sql_cpu_percent"] == 27.0
    assert performance["memory_grants_pending"] == 2
    assert performance["blocked_sessions"] == 3
    assert performance["tempdb_used_mb"] == 175.0
    assert performance["session_context"][0]["application"] == "ERP"
    assert performance["long_queries"][0]["query_signature"] == "0xAABB"
    assert performance["long_queries"][0]["query_text"].endswith("PatientId=?")
    assert performance["long_queries"][0]["source_type"] == "Stored procedure"
    assert performance["expensive_queries"] == []
    rows = store.sql_performance_samples("2026-01-01T00:00:00+00:00", "primary")
    assert len(rows) == 1
    assert rows[0]["longest_blocked_seconds"] == 18
    evidence = store.sql_query_evidence("2026-01-01T00:00:00+00:00", "primary")
    assert {row["evidence_type"] for row in evidence} == {"long_running"}
    assert all("SELECT *" not in row["query_signature"] for row in evidence)
    assert {row["source_type"] for row in evidence} == {"Stored procedure"}


def test_query_text_sanitizer_removes_literals_and_comments():
    value = sanitize_query_text("SELECT * FROM T WHERE Patient='Alice' AND Id=42 -- private")
    assert value == "SELECT * FROM T WHERE Patient='?' AND Id=?"


def test_baselines_calculate_median_p95_p99_and_io_percentiles():
    rows = []
    for value in range(1, 11):
        rows.append({"user_connections": value * 2, "active_sessions": value,
                     "sql_cpu_percent": value * 5, "memory_grants_pending": 0,
                     "blocked_sessions": 0, "longest_blocked_seconds": 0,
                     "details": json.dumps({"io": [{"read_latency_ms": value,
                                                     "write_latency_ms": value * 2}]})})
    baselines = performance_baselines(rows)
    assert baselines["active_sessions"]["median"] == 5.5
    assert baselines["sql_cpu_percent"]["p95"] == 47.75
    assert baselines["sql_cpu_percent"]["p99"] == 49.55
    assert baselines["io_write_latency_ms"]["peak"] == 20.0


def test_sustained_assessment_uses_persisted_consecutive_samples():
    rows = []
    for minute in (0, 5, 10, 15):
        rows.append({"collected_at": f"2026-11-14T10:{minute:02d}:00+00:00",
                     "user_connections": 20, "active_sessions": 5, "sql_cpu_percent": 90,
                     "memory_grants_pending": 0, "blocked_sessions": 0,
                     "longest_blocked_seconds": 0, "details": "{}"})
    current = {**rows[-1], "io": []}
    result = sustained_performance_assessment(
        rows, current, {"performance_cpu_percent": 80, "performance_sustain_minutes": 10}
    )
    cpu = next(item for item in result["sustained"] if item["metric"] == "sql_cpu_percent")
    assert cpu["duration_minutes"] == 15
    assert cpu["consecutive_samples"] == 4


def test_single_spike_is_not_sustained_and_baseline_can_raise_threshold():
    rows = []
    for minute, cpu in enumerate((70, 72, 71, 73, 72, 90)):
        rows.append({"collected_at": f"2026-11-14T10:{minute * 5:02d}:00+00:00",
                     "user_connections": 10, "active_sessions": 2, "sql_cpu_percent": cpu,
                     "memory_grants_pending": 0, "blocked_sessions": 0,
                     "longest_blocked_seconds": 0, "details": "{}"})
    result = sustained_performance_assessment(
        rows, {**rows[-1], "io": []},
        {"performance_cpu_percent": 60, "performance_sustain_minutes": 10},
    )
    cpu = next(item for item in result["breaches"] if item["metric"] == "sql_cpu_percent")
    assert cpu["threshold"] > 60
    assert cpu["sustained"] is False


def test_period_baseline_compares_same_local_hour_and_weekday():
    rows = [
        {"collected_at": "2026-11-07T08:00:00+00:00", "sql_cpu_percent": 20,
         "user_connections": 1, "active_sessions": 1, "memory_grants_pending": 0,
         "blocked_sessions": 0, "longest_blocked_seconds": 0, "details": "{}"},
        {"collected_at": "2026-11-14T08:00:00+00:00", "sql_cpu_percent": 40,
         "user_connections": 1, "active_sessions": 1, "memory_grants_pending": 0,
         "blocked_sessions": 0, "longest_blocked_seconds": 0, "details": "{}"},
    ]
    result = period_baseline_comparison(rows, rows[-1], "Africa/Cairo")
    assert result["local_hour"] == 10
    assert result["weekday"] == "Saturday"
    assert result["same_hour"]["samples"] == 2
    assert result["same_weekday"]["median"] == 30.0


def test_optional_query_evidence_failure_does_not_suppress_performance_sample(tmp_path):
    store = Store(tmp_path / "isolated-query-evidence.db")
    state = {}
    SqlServerMonitor(store, Notifier(store))._collect_performance_if_due(
        {"id": "primary", "server": "db01"}, state, PerformanceCursorWithoutQueryEvidence()
    )
    performance = state["performance"]
    assert performance["status"] == "Attention"
    assert "denied" in performance["query_evidence_error"]
    assert len(store.sql_performance_samples("2026-01-01T00:00:00+00:00")) == 1
    assert store.sql_query_evidence("2026-01-01T00:00:00+00:00") == []


def test_sql_configuration_snapshot_records_changed_values(tmp_path):
    store = Store(tmp_path / "configuration.db")
    monitor = SqlServerMonitor(store, Notifier(store))
    profile = {"id": "primary", "name": "Production", "database": "ERP"}
    state = {}

    monitor._collect_configuration_if_due(profile, state, ConfigurationCursor(4096))
    state["configuration_changes"].pop("checked_at", None)
    monitor._collect_configuration_if_due(profile, state, ConfigurationCursor(8192))

    changes = store.component_changes(component_type="SQL Server")
    assert len(changes) == 1
    assert changes[0]["property_name"] == "server/max server memory (MB)"
    assert "4096" in changes[0]["old_value"]
    assert "8192" in changes[0]["new_value"]


def test_system_health_deadlock_is_redacted_and_deduplicated(tmp_path):
    store = Store(tmp_path / "deadlocks.db")
    monitor = SqlServerMonitor(store, Notifier(store))
    profile = {"id": "primary", "name": "Production"}
    state = {}

    monitor._collect_deadlocks_if_due(profile, state, DeadlockCursor())
    state["deadlocks"].pop("checked_at", None)
    monitor._collect_deadlocks_if_due(profile, state, DeadlockCursor())

    events = [row for row in store.recent_events() if row["category"] == "sql-deadlock"]
    assert len(events) == 1
    details = json.loads(events[0]["details"])
    assert details["victim"]["spid"] == "52"
    assert details["victim"]["query"].endswith("Id=?")
    assert "conflicting order" in details["likely_cause"]
