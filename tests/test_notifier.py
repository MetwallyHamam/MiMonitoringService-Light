from millensys_guardian.notifier import Notifier, branded_html
from millensys_guardian.store import Store


def test_branded_html_escapes_content():
    html = branded_html("<unsafe>", "body")
    assert "&lt;unsafe&gt;" in html


def test_alert_deduplicates_queue(tmp_path):
    store = Store(tmp_path / "test.db")
    config = store.get("configuration")
    config["email"]["recipients"] = ["ops@example.com"]
    config["email"]["cooldown_minutes"] = 60
    store.set("configuration", config)
    notifier = Notifier(store)
    notifier.alert("disk", "C:", "low", "Low", "warning")
    notifier.alert("disk", "C:", "low", "Low", "warning")
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_queue").fetchone()[0] == 1


def test_recovery_commits_alert_before_queueing_email(tmp_path):
    store = Store(tmp_path / "test.db")
    config = store.get("configuration")
    config["email"]["recipients"] = ["ops@example.com"]
    store.set("configuration", config)
    notifier = Notifier(store)
    notifier.alert("disk", "C:", "low_space", "Low disk space", "warning")

    notifier.recover("disk", "C:", "low_space", "Disk space recovered")

    fingerprint = notifier.fingerprint("disk", "C:", "low_space")
    with store.connect() as db:
        state = db.execute(
            "SELECT active FROM alert_state WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        assert state["active"] == 0
        assert db.execute("SELECT COUNT(*) FROM mail_queue").fetchone()[0] == 2
