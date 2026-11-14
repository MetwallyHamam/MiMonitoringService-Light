from millensys_guardian.engine import MonitorEngine
from millensys_guardian.notifier import Notifier
from millensys_guardian.store import Store


class Response:
    status_code = 503
    text = "unavailable"


def test_api_action_waits_for_failure_limit_and_is_rate_limited(tmp_path, monkeypatch):
    store = Store(tmp_path / "engine.db")
    engine = MonitorEngine(store)
    calls = []
    monkeypatch.setattr(
        "millensys_guardian.engine.requests.get", lambda *args, **kwargs: Response()
    )
    monkeypatch.setattr(
        "millensys_guardian.engine.restart_service",
        lambda target: (calls.append(target) is None, "restarted"),
    )
    config = {
        "api_check": {
            "enabled": True,
            "url": "https://example.test/health",
            "expected_status": 200,
            "action_after_failures": 2,
            "failure_action": "restart",
        }
    }

    engine._api_check("service", "Spooler", config)
    assert calls == []
    engine._api_check("service", "Spooler", config)
    engine._api_check("service", "Spooler", config)

    assert calls == ["Spooler"]


def test_individual_disk_selection_excludes_disabled_drive(tmp_path, monkeypatch):
    store = Store(tmp_path / "engine.db")
    config = store.get("configuration")
    config["disk"].update(
        {"monitor_all": False, "minimum_free_percent": 20, "minimum_free_mb": 1000}
    )
    store.set("configuration", config)
    store.upsert_target("disk", "C:", "C:\\", {})
    store.upsert_target("disk", "D:", "D:\\", {})
    store.set_target_enabled("disk", "D:", False)
    monkeypatch.setattr("millensys_guardian.engine.windows_services", list)
    monkeypatch.setattr("millensys_guardian.engine.iis_pools", list)
    monkeypatch.setattr("millensys_guardian.engine.performance", lambda: {"cpu": 1, "memory": 2})
    monkeypatch.setattr(
        "millensys_guardian.engine.disks",
        lambda: [
            {"key": "C:", "name": "C:\\", "free_mb": 100, "free_percent": 1},
            {"key": "D:", "name": "D:\\", "free_mb": 100, "free_percent": 1},
        ],
    )

    MonitorEngine(store).run_cycle()

    c_fingerprint = Notifier.fingerprint("disk", "C:", "low_space")
    d_fingerprint = Notifier.fingerprint("disk", "D:", "low_space")
    with store.connect() as db:
        assert db.execute(
            "SELECT active FROM alert_state WHERE fingerprint=?", (c_fingerprint,)
        ).fetchone()[0]
        assert (
            db.execute(
                "SELECT active FROM alert_state WHERE fingerprint=?", (d_fingerprint,)
            ).fetchone()
            is None
        )


def test_heartbeat_records_agent_state(tmp_path):
    store = Store(tmp_path / "engine.db")
    engine = MonitorEngine(store)

    engine._heartbeat("running")

    heartbeat = store.get("agent_heartbeat")
    assert heartbeat["state"] == "running"
    assert heartbeat["pid"] > 0
    assert heartbeat["version"]


def test_service_discovery_failure_suppresses_recovery_actions(tmp_path, monkeypatch):
    store = Store(tmp_path / "discovery.db")
    store.upsert_target("service", "MSSQLSERVER", "SQL Server", {"auto_restart": True})
    calls = []
    monkeypatch.setattr("millensys_guardian.engine.windows_services", list)
    monkeypatch.setattr("millensys_guardian.engine.iis_pools", list)
    monkeypatch.setattr("millensys_guardian.engine.disks", list)
    monkeypatch.setattr("millensys_guardian.engine.performance", lambda: {"cpu": 1, "memory": 2})
    monkeypatch.setattr(
        "millensys_guardian.engine.restart_service",
        lambda target: (calls.append(target) is None, "restarted"),
    )

    MonitorEngine(store).run_cycle()

    assert calls == []
    assert store.recent_events()[0]["category"] == "discovery"


def _monitoring_only_store(tmp_path, kind, key):
    store = Store(tmp_path / f"{kind}.db")
    config = store.get("configuration")
    config["email"]["recipients"] = ["engineer@millensys.com"]
    store.set("configuration", config)
    store.upsert_target(kind, key, key, {"start_if_stopped": False, "retry_count": 3})
    return store


def _stub_unrelated_discovery(monkeypatch):
    monkeypatch.setattr("millensys_guardian.engine.disks", list)
    monkeypatch.setattr("millensys_guardian.engine.performance", lambda: {"cpu": 1, "memory": 2})


def test_monitoring_only_service_notifies_once_then_recovers(tmp_path, monkeypatch):
    store = _monitoring_only_store(tmp_path, "service", "Spooler")
    statuses = iter(("running", "stopped", "stopped", "running"))
    monkeypatch.setattr(
        "millensys_guardian.engine.windows_services",
        lambda: [{"key": "Spooler", "status": next(statuses)}],
    )
    monkeypatch.setattr("millensys_guardian.engine.iis_pools", list)
    _stub_unrelated_discovery(monkeypatch)
    engine = MonitorEngine(store)

    engine.run_cycle()
    engine.run_cycle()
    engine.run_cycle()
    engine.run_cycle()

    with store.connect() as db:
        subjects = [row[0] for row in db.execute("SELECT subject FROM mail_queue ORDER BY id")]
    assert len(subjects) == 2
    assert "not_running" in subjects[0]
    assert "[RECOVERED] Spooler" in subjects[1]


def test_monitoring_only_pool_notifies_once_then_recovers(tmp_path, monkeypatch):
    store = _monitoring_only_store(tmp_path, "pool", "SupportHub")
    statuses = iter(("Started", "Stopped", "Stopped", "Started"))
    monkeypatch.setattr("millensys_guardian.engine.windows_services", list)
    monkeypatch.setattr(
        "millensys_guardian.engine.iis_pools",
        lambda: [{"key": "SupportHub", "status": next(statuses)}],
    )
    _stub_unrelated_discovery(monkeypatch)
    engine = MonitorEngine(store)

    engine.run_cycle()
    engine.run_cycle()
    engine.run_cycle()
    engine.run_cycle()

    with store.connect() as db:
        subjects = [row[0] for row in db.execute("SELECT subject FROM mail_queue ORDER BY id")]
    assert len(subjects) == 2
    assert "not_started" in subjects[0]
    assert "[RECOVERED] SupportHub" in subjects[1]


def test_initially_stopped_target_waits_for_running_baseline(tmp_path, monkeypatch):
    store = _monitoring_only_store(tmp_path, "service", "IntentionalStop")
    statuses = iter(("stopped", "running", "stopped"))
    monkeypatch.setattr(
        "millensys_guardian.engine.windows_services",
        lambda: [{"key": "IntentionalStop", "status": next(statuses)}],
    )
    monkeypatch.setattr("millensys_guardian.engine.iis_pools", list)
    _stub_unrelated_discovery(monkeypatch)

    MonitorEngine(store).run_cycle()
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_queue").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM alert_state WHERE active=1").fetchone()[0] == 0

    MonitorEngine(store).run_cycle()
    MonitorEngine(store).run_cycle()
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM mail_queue").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM alert_state WHERE active=1").fetchone()[0] == 1
