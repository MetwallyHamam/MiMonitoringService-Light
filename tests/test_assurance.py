import os
from datetime import UTC, datetime, timedelta

from millensys_guardian.assurance import (
    AssuranceMonitor,
    diff_snapshots,
    evaluate_backup,
    friendly_property_name,
    tracked_file_snapshot,
)
from millensys_guardian.notifier import Notifier
from millensys_guardian.store import Store


def test_backup_age_uses_calendar_days(tmp_path):
    backup = tmp_path / "ERP_FULL_001.bak"
    backup.write_bytes(b"backup")
    now = datetime(2026, 11, 13, 6, 0, tzinfo=UTC)
    modified = now - timedelta(days=1, hours=5)
    os.utime(backup, (modified.timestamp(), modified.timestamp()))

    result = evaluate_backup(
        {
            "path": str(tmp_path),
            "pattern": "*.bak",
            "keyword": "ERP",
            "max_age_days": 1,
        },
        now,
    )

    assert result["ok"]
    assert result["age_days"] == 1


def test_missing_backup_is_reported(tmp_path):
    result = evaluate_backup(
        {"path": str(tmp_path), "pattern": "*.bak", "max_age_days": 0},
        datetime.now(UTC),
    )

    assert not result["ok"]
    assert "No backup matches" in result["reason"]


def test_tracked_file_detects_rename_and_extension_change(tmp_path):
    original = tmp_path / "important.config"
    original.write_text("protected", encoding="utf-8")
    baseline = tracked_file_snapshot(original)
    renamed = tmp_path / "important.txt"
    original.rename(renamed)

    current = tracked_file_snapshot(original, baseline)

    assert current["renamed"]
    assert current["name"] == "important.txt"
    assert current["extension"] == ".txt"
    assert current["sha256"] == baseline["sha256"]


def test_snapshot_diff_lists_old_and_new_values():
    assert diff_snapshots(
        {"runtime": "v4.0", "physical_path": "D:/App/v1"},
        {"runtime": "v4.0", "physical_path": "D:/App/v2"},
    ) == [("physical_path", "D:/App/v1", "D:/App/v2")]


def test_iis_property_paths_have_clear_names():
    assert (
        friendly_property_name("pool/appcmd/APPPOOL/add/@enable32BitAppOnWin64")
        == "Enable 32-Bit Applications"
    )
    assert (
        friendly_property_name(
            "application/Default Web Site/API/virtualDirectories/appcmd/VDIR/@physicalPath"
        )
        == "Default Web Site/API · Application Physical Path"
    )
    assert (
        friendly_property_name("pool/appcmd/APPPOOL/add/processModel/@idleTimeout")
        == "Process Model · Idle Time-out"
    )


def test_backup_failure_opens_two_percent_incident(tmp_path):
    store = Store(tmp_path / "monitor.db")
    config = store.get("configuration")
    config["backups"] = [
        {
            "id": "erp",
            "name": "ERP backup",
            "path": str(tmp_path / "missing"),
            "pattern": "*.bak",
            "check_time": "00:00",
            "max_age_days": 0,
            "notify_missing": False,
            "enabled": True,
        }
    ]
    store.set("configuration", config)

    AssuranceMonitor(store, Notifier(store))._process_backups(config)

    incident = store.open_score_incidents()[0]
    assert incident["incident_key"] == "backup:erp"
    assert incident["deduction"] == 2


def test_manual_backup_check_uses_compliance_incident_workflow(tmp_path):
    store = Store(tmp_path / "manual-backup.db")
    rule = {"id": "erp", "name": "ERP backup", "path": str(tmp_path / "missing"), "pattern": "*.bak", "max_age_days": 0, "notify_missing": False}

    result = AssuranceMonitor(store, Notifier(store)).check_backup_rule(rule, manual=True)

    assert result["ok"] is False
    assert store.open_score_incidents()[0]["incident_key"] == "backup:erp"
    assert store.get("backup_runtime")["erp"]["result"]["ok"] is False
    assert store.backup_samples("2026-01-01T00:00:00+00:00")


def test_component_change_is_persisted_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "monitor.db")
    store.upsert_target("service", "Demo", "Demo Service", {})
    monitor = AssuranceMonitor(store, Notifier(store))
    config = store.get("configuration")
    snapshots = iter(
        (
            {"exists": True, "version": "1.0"},
            {"exists": True, "version": "2.0"},
            {"exists": True, "version": "2.0"},
        )
    )
    monkeypatch.setattr(
        "millensys_guardian.assurance.service_snapshot", lambda _name: next(snapshots)
    )
    monkeypatch.setattr("millensys_guardian.assurance.iis_pool_snapshot", lambda _name: {})

    monitor._process_snapshots(config)
    monitor._process_snapshots(config)
    monitor._process_snapshots(config)

    changes = store.component_changes()
    assert len(changes) == 1
    assert changes[0]["property_name"] == "version"
    assert changes[0]["old_value"] == "1.0"
    assert changes[0]["new_value"] == "2.0"
