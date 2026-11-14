from datetime import UTC, datetime

from millensys_guardian.scheduler import (
    SafeScheduler,
    due_action,
    in_maintenance,
    latest_occurrence,
)
from millensys_guardian.store import Store


def test_daily_and_weekly_latest_occurrence():
    now = datetime(2026, 11, 13, 10, 30, tzinfo=UTC)  # Friday
    daily = latest_occurrence({"enabled": True, "frequency": "Daily", "time": "09:15"}, now)
    weekly = latest_occurrence(
        {"enabled": True, "frequency": "Weekly", "weekday": 2, "time": "08:00"}, now
    )

    assert daily == datetime(2026, 11, 13, 9, 15, tzinfo=UTC)
    assert weekly == datetime(2026, 11, 11, 8, 0, tzinfo=UTC)


def test_monthly_schedule_clamps_to_last_day_of_short_month():
    now = datetime(2026, 2, 28, 12, 0, tzinfo=UTC)
    occurrence = latest_occurrence(
        {"enabled": True, "frequency": "Monthly", "month_day": 31, "time": "10:00"}, now
    )

    assert occurrence == datetime(2026, 2, 28, 10, 0, tzinfo=UTC)


def test_missed_run_grace_skips_stale_occurrence():
    schedule = {
        "enabled": True,
        "frequency": "Daily",
        "time": "09:00",
        "missed_run_minutes": 10,
    }

    assert due_action(schedule, datetime(2026, 11, 13, 9, 8, tzinfo=UTC)) is not None
    assert due_action(schedule, datetime(2026, 11, 13, 9, 11, tzinfo=UTC)) is None


def test_overnight_maintenance_window():
    maintenance = {
        "enabled": True,
        "start": "23:00",
        "end": "02:00",
        "weekdays": list(range(7)),
    }

    assert in_maintenance(maintenance, datetime(2026, 11, 13, 1, 0, tzinfo=UTC))
    assert not in_maintenance(maintenance, datetime(2026, 11, 13, 3, 0, tzinfo=UTC))


def test_action_claim_is_atomic_and_unique(tmp_path):
    store = Store(tmp_path / "scheduler.db")

    assert store.claim_action("same", "service", "Spooler", "restart", "2026-11-13T09:00Z")
    assert not store.claim_action("same", "service", "Spooler", "restart", "2026-11-13T09:00Z")


def test_scheduler_executes_occurrence_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "scheduler.db")
    calls = []
    monkeypatch.setattr(
        "millensys_guardian.scheduler.restart_service",
        lambda target: (calls.append(target) is None, "restarted"),
    )
    scheduler = SafeScheduler(store, "UTC")
    config = {
        "schedule": {
            "enabled": True,
            "frequency": "Daily",
            "time": "09:00",
            "action": "restart",
            "missed_run_minutes": 10,
        }
    }
    now = datetime(2026, 11, 13, 9, 5, tzinfo=UTC)

    scheduler.process("service", "Spooler", config, now)
    scheduler.process("service", "Spooler", config, now)

    assert calls == ["Spooler"]


def test_global_action_switch_blocks_scheduled_action(tmp_path, monkeypatch):
    store = Store(tmp_path / "scheduler.db")
    calls = []
    monkeypatch.setattr(
        "millensys_guardian.scheduler.restart_service",
        lambda target: (calls.append(target) is None, "restarted"),
    )
    scheduler = SafeScheduler(store, "UTC")
    config = {
        "schedule": {
            "enabled": True,
            "frequency": "Daily",
            "time": "09:00",
            "action": "restart",
            "missed_run_minutes": 10,
        }
    }

    scheduler.process(
        "service",
        "Spooler",
        config,
        datetime(2026, 11, 13, 9, 5, tzinfo=UTC),
        allow_actions=False,
    )

    assert calls == []


def test_maintenance_stop_and_resume_are_each_executed_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "scheduler.db")
    calls = []
    monkeypatch.setattr(
        "millensys_guardian.scheduler.service_action",
        lambda target, action: (calls.append((target, action)) is None, action),
    )
    scheduler = SafeScheduler(store, "UTC")
    config = {
        "maintenance": {
            "enabled": True,
            "start": "00:00",
            "end": "00:30",
            "weekdays": list(range(7)),
            "suppress_monitoring": True,
            "stop_start": True,
            "resume_grace_minutes": 30,
        }
    }

    assert scheduler.process("service", "Spooler", config, datetime(2026, 11, 13, 0, 5, tzinfo=UTC))
    assert scheduler.process(
        "service", "Spooler", config, datetime(2026, 11, 13, 0, 10, tzinfo=UTC)
    )
    assert not scheduler.process(
        "service", "Spooler", config, datetime(2026, 11, 13, 0, 35, tzinfo=UTC)
    )
    scheduler.process("service", "Spooler", config, datetime(2026, 11, 13, 0, 40, tzinfo=UTC))

    assert calls == [("Spooler", "stop"), ("Spooler", "start")]
