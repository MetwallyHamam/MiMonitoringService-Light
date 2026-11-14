from datetime import UTC, datetime

from docx import Document
from pypdf import PdfReader

from millensys_guardian.analytics_report import (
    ReportPeriod,
    build_analytics,
    export_docx,
    export_pdf,
)
from millensys_guardian.store import Store


def seeded_store(tmp_path):
    store = Store(tmp_path / "analytics.db")
    config = store.get("configuration")
    config["client"].update({"name": "Example Hospital", "site": "Cairo"})
    config["sql_monitors"] = [{"id": "prod", "name": "Production SQL", "server": "db01"}]
    store.set("configuration", config)
    store.upsert_target("service", "MSSQLSERVER", "SQL Server")
    store.upsert_target("pool", "ClientPortal", "Client Portal")
    with store.connect() as db:
        for day, cpu, memory, disk in ((1, 30, 45, 42), (10, 72, 68, 31), (20, 55, 61, 28), (30, 40, 52, 34)):
            db.execute(
                "INSERT INTO health_samples(created_at,cpu,memory,minimum_disk_free_percent) VALUES(?,?,?,?)",
                (f"2026-11-{day:02d}T09:00:00+00:00", cpu, memory, disk),
            )
        for when, total in (("2026-11-01T09:00:00+00:00", 1000), ("2026-11-30T09:00:00+00:00", 1250)):
            db.execute(
                "INSERT INTO sql_samples(created_at,profile_id,profile_name,database_name,state,data_mb,log_mb,total_mb) VALUES(?,?,?,?,?,?,?,?)",
                (when, "prod", "Production SQL", "ERP", "ONLINE", total - 100, 100, total),
            )
        for day, cpu, sessions in ((1, 20, 3), (10, 55, 7), (20, 85, 12), (30, 45, 5)):
            db.execute(
                "INSERT INTO sql_performance_samples(collected_at,profile_id,user_connections,active_sessions,sql_cpu_percent,memory_grants_pending,blocked_sessions,longest_blocked_seconds,tempdb_used_mb,tempdb_free_mb,details) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"2026-11-{day:02d}T09:00:00+00:00", "prod", sessions * 2, sessions,
                 cpu, 0, int(day == 20), 75 if day == 20 else 0, 200, 500, "{}"),
            )
        db.execute(
            "INSERT INTO sql_query_evidence(collected_at,profile_id,evidence_type,query_signature,database_name,duration_ms,cpu_ms,logical_reads,writes,executions,rank_score) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-11-20T09:00:00+00:00", "prod", "long_running", "0xAABB", "ERP",
             120000, 90000, 120000, 400, 1, 120000),
        )
        db.execute(
            "INSERT INTO events(created_at,severity,category,target,message) VALUES(?,?,?,?,?)",
            ("2026-11-12T10:00:00+00:00", "critical", "service", "MSSQLSERVER", "Service stopped"),
        )
        db.execute(
            "INSERT INTO events(created_at,severity,category,target,message) VALUES(?,?,?,?,?)",
            ("2026-11-12T10:10:00+00:00", "recovery", "service", "MSSQLSERVER", "Service recovered"),
        )
        for day in range(1, 31):
            db.execute(
                "INSERT INTO backup_samples(checked_at,rule_id,rule_name,ok,age_days,message) VALUES(?,?,?,?,?,?)",
                (f"2026-11-{day:02d}T06:00:00+00:00", "erp", "ERP backup", int(day != 15), 0, "Validated"),
            )
    return store


def test_analytics_contains_all_operational_dimensions(tmp_path):
    store = seeded_store(tmp_path)
    period = ReportPeriod(datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 30, 23, 59, tzinfo=UTC))

    report = build_analytics(store, period)

    assert report["databases"][0]["growth_mb"] == 250
    assert report["backup_rate"] < 100
    assert report["services"][0]["availability"] < 100
    assert len(report["backup_days"]) == 30
    assert report["business_impact"]
    assert report["next_actions"]
    assert report["performance_summary"][0]["cpu"]["p95"] > 70
    assert report["top_queries"][0]["query_signature"] == "0xAABB"


def test_analytics_includes_sql_native_and_restore_evidence(tmp_path):
    store = seeded_store(tmp_path)
    store.upsert_sql_backup_history("prod", "ERP", [{
        "backup_set_id": 7, "backup_type": "D", "start_time": "2026-11-20T01:00:00+00:00",
        "finish_time": "2026-11-20T01:10:00+00:00", "backup_size_mb": 900,
    }])
    store.add_sql_recovery_evidence("prod", "ERP", {
        "evidence_type": "Isolated restore test", "performed_at": "2026-11-21T00:00:00+00:00",
        "result": "Succeeded", "owner": "DBA", "destination": "ERP_DR",
        "duration_minutes": 18, "notes": "CHG-42",
    })
    store.upsert_sql_job_execution("prod", "Nightly ETL", {"execution_id": 1, "status": "Succeeded", "last_run": "2026-11-20T02:00:00+00:00", "duration_seconds": 600, "message": "", "next_run": "2026-11-21T02:00:00+00:00"})
    store.upsert_sql_job_execution("prod", "Nightly ETL", {"execution_id": 2, "status": "Failed", "last_run": "2026-11-21T02:00:00+00:00", "duration_seconds": 900, "message": "Step failed", "next_run": "2026-11-22T02:00:00+00:00"})
    period = ReportPeriod(datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 30, 23, 59, tzinfo=UTC))
    report = build_analytics(store, period)
    assert len(report["sql_backups"]) == 1
    assert report["recovery_evidence"][0]["destination"] == "ERP_DR"
    assert report["job_summary"][0]["success_percent"] == 50
    assert report["agent_success_percent"] == 50
    assert any("restore test" in item for item in report["business_impact"])


def test_pdf_and_word_exports_include_eight_sections(tmp_path):
    store = seeded_store(tmp_path)
    period = ReportPeriod(datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 30, 23, 59, tzinfo=UTC))
    pdf = export_pdf(store, period, tmp_path / "client-report.pdf")
    word = export_docx(store, period, tmp_path / "client-report.docx")

    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf).pages)
    word_document = Document(word)
    word_text = "\n".join(paragraph.text for paragraph in word_document.paragraphs)
    word_table_text = "\n".join(cell.text for table in word_document.tables for row in table.rows for cell in row.cells)
    for heading in (
        "1 Duration overview summary",
        "2 Detailed operational evidence",
        "3 Database health and backup validation",
        "4 Incidents and warnings",
        "5 Services stability",
        "6 Pools stability",
        "7 Business impact for the client",
        "8 Next actions",
    ):
        assert heading in pdf_text
        assert heading in word_text
    assert "CPU P99" in pdf_text
    assert "0xAABB" in pdf_text
    assert "Production SQL" in pdf_text
    assert "Likely root cause" in pdf_text
    assert "Query 1" in pdf_text
    assert "CPU P99" in word_table_text
    assert "0xAABB" in word_table_text
