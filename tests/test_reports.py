from datetime import datetime
from zoneinfo import ZoneInfo

from millensys_guardian.notifier import Notifier
from millensys_guardian.reports import ReportScheduler, queue_period_report
from millensys_guardian.store import Store, utc_now


def test_daily_report_is_queued_once_per_occurrence(tmp_path):
    store = Store(tmp_path / "reports.db")
    config = store.get("configuration")
    config["email"]["recipients"] = ["ops@example.com"]
    config["reports"].update(
        {"daily_enabled": True, "daily_time": "08:00", "monthly_enabled": False}
    )
    store.set("configuration", config)
    scheduler = ReportScheduler(store)
    now = datetime(2026, 11, 13, 8, 5, tzinfo=ZoneInfo("Africa/Cairo"))

    scheduler.process(now)
    scheduler.process(now)

    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_queue").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM action_claims WHERE kind='report'").fetchone()[0] == 1
        )


def test_report_specific_recipients_override_alert_recipients(tmp_path):
    store = Store(tmp_path / "reports.db")
    config = store.get("configuration")
    config["email"]["recipients"] = ["alerts@example.com"]
    config["reports"]["recipients"] = ["reports@example.com"]
    config["reports"]["monthly_enabled"] = False
    config["reports"]["daily_time"] = "08:00"
    store.set("configuration", config)

    ReportScheduler(store).process(datetime(2026, 11, 13, 8, 1, tzinfo=ZoneInfo("Africa/Cairo")))

    with store.connect() as db:
        assert (
            db.execute("SELECT recipients FROM mail_queue").fetchone()[0]
            == '["reports@example.com"]'
        )


def test_failed_email_is_quarantined_at_attempt_limit(tmp_path, monkeypatch):
    store = Store(tmp_path / "reports.db")
    config = store.get("configuration")
    config["email"].update(
        {
            "enabled": True,
            "recipients": ["ops@example.com"],
            "password_protected": "present",
            "max_attempts": 2,
        }
    )
    store.set("configuration", config)
    notifier = Notifier(store)
    notifier.enqueue("Failure", "<p>test</p>")

    def fail_delivery(*args, **kwargs):
        raise OSError("SMTP unavailable")

    monkeypatch.setattr(notifier, "_deliver", fail_delivery)
    notifier.flush()
    with store.connect() as db:
        db.execute("UPDATE mail_queue SET next_attempt=?", (utc_now(),))
    notifier.flush()

    with store.connect() as db:
        row = db.execute("SELECT status,attempts,last_error FROM mail_queue").fetchone()
        assert tuple(row) == ("failed", 2, "SMTP unavailable")
    assert any(row["category"] == "email" for row in store.recent_events())


def test_new_defaults_are_merged_into_existing_configuration(tmp_path):
    path = tmp_path / "migration.db"
    store = Store(path)
    store.set("configuration", {"setup_complete": True, "email": {"enabled": False}})

    migrated = Store(path).get("configuration")

    assert migrated["setup_complete"] is True
    assert migrated["email"]["enabled"] is False
    assert migrated["email"]["max_attempts"] == 5
    assert migrated["reports"]["daily_enabled"] is True


def test_monthly_report_queues_full_analytics_pdf_attachment(tmp_path):
    store = Store(tmp_path / "reports.db")
    config = store.get("configuration")
    config["client"]["name"] = "Example Client"
    config["email"]["recipients"] = ["ops@example.com"]
    config["reports"]["monthly_analytics_attachment"] = True
    store.set("configuration", config)

    queue_period_report(store, "monthly", datetime(2026, 12, 1, 8, tzinfo=ZoneInfo("Africa/Cairo")))

    with store.connect() as db:
        attachment = db.execute("SELECT attachments FROM mail_queue").fetchone()[0]
    assert "Example-Client-analytics-2026-11.pdf" in attachment
    assert (tmp_path / "reports" / "Example-Client-analytics-2026-11.pdf").is_file()
