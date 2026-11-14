from datetime import UTC, datetime, timedelta

from millensys_guardian.sql_backup import (
    assess_backup_history,
    backup_baselines,
    compliance_calendar,
    recovery_readiness,
    sanitize_destination,
)
from millensys_guardian.store import Store

NOW = datetime(2026, 11, 14, 12, tzinfo=UTC)


def backup(kind: str, hours: float, backup_id: int, first=100, last=200):
    return {
        "backup_set_id": backup_id,
        "backup_type": kind,
        "finish_time": (NOW - timedelta(hours=hours)).isoformat(),
        "first_lsn": first,
        "last_lsn": last,
        "destination": r"D:\SQLBackups\ERP.bak",
        "has_checksums": True,
    }


def test_full_and_log_sla_are_compliant_with_continuous_chain():
    result = assess_backup_history(
        [backup("D", 12, 1), backup("L", 0.5, 2, 200, 300)], now=NOW,
        recovery_model="FULL", policy={"log_max_minutes": 60},
    )
    assert result["status"] == "Healthy"
    assert result["chain_status"] == "Continuous"
    assert result["rpo_exposure_hours"] == 0.5


def test_missing_and_late_backups_need_attention():
    result = assess_backup_history([backup("D", 30, 1)], now=NOW, recovery_model="FULL")
    statuses = {item["backup_type"]: item["status"] for item in result["checks"]}
    assert statuses == {"D": "Late", "I": "Not required", "L": "Missing"}
    assert result["status"] == "Attention"


def test_simple_recovery_does_not_require_logs_by_default():
    result = assess_backup_history([backup("D", 2, 1)], now=NOW, recovery_model="SIMPLE")
    assert next(item for item in result["checks"] if item["backup_type"] == "L")["status"] == "Not required"
    assert result["status"] == "Healthy"


def test_log_chain_gap_and_signed_url_sanitization():
    result = assess_backup_history(
        [backup("D", 2, 1), backup("L", 2, 2, 100, 200), backup("L", 1, 3, 250, 300)],
        now=NOW, recovery_model="FULL", policy={"log_max_minutes": 120},
    )
    assert result["chain_status"] == "Gap detected"
    assert result["status"] == "Attention"
    safe = sanitize_destination("https://user:secret@store.example/db.bak?sig=private")
    assert safe == "https://store.example/db.bak"
    assert "secret" not in safe and "sig" not in safe


def test_duration_and_size_baselines_flag_latest_outlier():
    rows = []
    for index, duration in enumerate((60, 62, 58, 61, 59, 180), 1):
        row = backup("D", 7 - index, index)
        finish = datetime.fromisoformat(row["finish_time"])
        row.update({"start_time": (finish - timedelta(seconds=duration)).isoformat(), "backup_size_mb": 100 if index < 6 else 30})
        rows.append(row)
    baseline = backup_baselines(rows)["D"]
    assert baseline["confidence"] == "Established"
    assert baseline["median_duration_seconds"] == 60.5
    assert len(baseline["anomalies"]) == 1
    result = assess_backup_history(rows, now=NOW, recovery_model="SIMPLE")
    assert result["anomaly_count"] == 1
    assert "Duration" in result["checks"][0]["baseline_anomaly"]


def test_31_day_calendar_distinguishes_compliant_attention_and_no_evidence():
    full = backup("D", 1, 1)
    log_only = backup("L", 25, 2)
    calendar = compliance_calendar([full, log_only], now=NOW, days=3)
    assert [day["status"] for day in calendar] == ["No evidence", "Attention", "Compliant"]


def test_recovery_test_evidence_is_auditable_and_scoped(tmp_path):
    store = Store(tmp_path / "evidence.db")
    store.add_sql_recovery_evidence("primary", "ERP", {
        "evidence_type": "Isolated restore test", "performed_at": "2026-11-14T00:00:00",
        "result": "Succeeded", "owner": "DBA team", "notes": "CHG-1042; restored to ERP_DR",
    })
    store.add_sql_recovery_evidence("other", "CRM", {
        "evidence_type": "RESTORE VERIFYONLY", "performed_at": "2026-11-13T00:00:00",
        "result": "Failed", "owner": "Support", "notes": "INC-88",
    })
    rows = store.sql_recovery_evidence("primary")
    assert len(rows) == 1
    assert rows[0]["database_name"] == "ERP"
    assert rows[0]["owner"] == "DBA team"


def test_recovery_readiness_requires_healthy_backups_and_restore_proof():
    assurance = {"status": "Healthy", "chain_status": "Continuous"}
    evidence = [{
        "evidence_type": "Isolated restore test", "performed_at": "2026-11-10T00:00:00+00:00",
        "result": "Succeeded", "owner": "DBA", "destination": "ERP_DR",
    }]
    ready = recovery_readiness(assurance, evidence, now=NOW)
    assert ready["status"] == "Ready"
    attention = recovery_readiness({"status": "Attention", "chain_status": "Gap detected"}, [], now=NOW)
    assert attention["status"] == "Attention"
    assert len(attention["reasons"]) >= 3
