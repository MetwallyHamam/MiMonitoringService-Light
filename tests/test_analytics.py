import sqlite3
from datetime import UTC, datetime, timedelta

from millensys_guardian.analytics import assess_health
from millensys_guardian.notifier import Notifier
from millensys_guardian.store import Store


def test_health_score_explains_active_alerts(tmp_path):
    store = Store(tmp_path / "analytics.db")
    store.open_score_incident(
        "active:spooler",
        "active",
        "service",
        "Spooler",
        "not_running",
        "Stopped after recovery retries",
        2,
    )

    assessment = assess_health(store)

    assert assessment.score == 98
    assert assessment.label == "Healthy"
    assert "Spooler" in assessment.contributors[0]


def test_health_score_sums_only_unresolved_operational_incidents(tmp_path):
    store = Store(tmp_path / "analytics.db")
    store.open_score_incident("a", "active", "service", "ServiceA", "down", "Down", 2)
    store.open_score_incident(
        "repeat:a", "recurring", "service", "ServiceA", "down", "Repeated", 3, persistent=True
    )

    assessment = assess_health(store)

    assert assessment.score == 95
    assert assessment.label == "Attention"
    assert assessment.contributors == ("-3 ServiceA — Repeated", "-2 ServiceA — Down")


def test_stopped_monitoring_agent_does_not_reduce_health(tmp_path):
    store = Store(tmp_path / "analytics.db")
    store.set("agent_heartbeat", {"state": "stopped"})

    assessment = assess_health(store)

    assert assessment.score == 100
    assert assessment.label == "Healthy"
    assert assessment.contributors == ()


def test_recovery_removes_alert_from_health_score(tmp_path):
    store = Store(tmp_path / "analytics.db")
    notifier = Notifier(store)
    notifier.alert("disk", "C:", "low_space", "Low disk", "warning")
    fingerprint = notifier.fingerprint("disk", "C:", "low_space")
    store.open_score_incident(
        f"active:{fingerprint}", "active", "disk", "C:", "low_space", "Low disk", 2
    )
    notifier.recover("disk", "C:", "low_space", "Disk recovered")

    assert assess_health(store).score == 100
    assert store.active_alerts() == []


def test_repeated_issue_remains_until_engineer_resolves_it(tmp_path):
    store = Store(tmp_path / "analytics.db")
    notifier = Notifier(store)
    for _ in range(3):
        notifier.alert("service", "Spooler", "not_running", "Stopped", "critical")
        notifier.recover("service", "Spooler", "not_running", "Recovered")

    incident = store.open_score_incidents()[0]
    assert incident["deduction"] == 3
    assert incident["persistent"] == 1
    assert assess_health(store).score == 97

    assert store.resolve_score_incident(incident["id"], "Resolved manually")
    assert assess_health(store).score == 100


def test_event_filter_and_target_history(tmp_path):
    store = Store(tmp_path / "analytics.db")
    store.add_event("warning", "disk", "C:", "Low disk")
    store.add_event("info", "agent", "monitor", "Started")

    assert len(store.filtered_events(severity="Warning")) == 1
    assert len(store.filtered_events(query="disk")) == 1
    assert (
        len(
            store.filtered_events(
                since=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds")
            )
        )
        == 2
    )
    assert store.target_events("C:")[0]["message"] == "Low disk"


def test_health_samples_and_retention(tmp_path):
    store = Store(tmp_path / "analytics.db")
    store.add_health_sample(25.0, 40.0, 70.0)
    since = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()

    samples = store.health_samples(since)

    assert len(samples) == 1
    assert samples[0]["cpu"] == 25.0
    store.apply_retention(30, 1000)
    assert len(store.health_samples(since)) == 1


def test_old_alert_schema_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE alert_state (fingerprint TEXT PRIMARY KEY, active INTEGER NOT NULL, "
            "last_sent TEXT, last_seen TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1)"
        )

    store = Store(path)

    with store.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(alert_state)")}
    assert {"category", "target", "condition", "message", "severity", "first_seen"} <= columns
