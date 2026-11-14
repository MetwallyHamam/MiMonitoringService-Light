from millensys_guardian import actions


def test_restart_stops_manual_related_services_then_restores_running_ones(monkeypatch):
    states = {"SQLAgent": "running", "BackupAgent": "stopped", "MSSQLSERVER": "running"}
    calls = []

    def service_action(name, action):
        calls.append((name, action))
        states[name] = "stopped" if action == "stop" else "running"
        return True, "ok"

    monkeypatch.setattr(actions, "service_state", lambda name: states[name])
    monkeypatch.setattr(actions, "service_action", service_action)
    monkeypatch.setattr(
        actions, "wait_for_service", lambda name, expected, timeout=45: states[name] == expected
    )

    ok, message = actions.restart_service(
        "MSSQLSERVER", ["SQLAgent", "BackupAgent", "SQLAgent"]
    )

    assert ok
    assert calls == [
        ("SQLAgent", "stop"),
        ("MSSQLSERVER", "stop"),
        ("MSSQLSERVER", "start"),
        ("SQLAgent", "start"),
    ]
    assert "restored related SQLAgent" in message


def test_restart_restores_related_service_when_main_start_fails(monkeypatch):
    states = {"SQLAgent": "running", "MSSQLSERVER": "running"}
    calls = []

    def service_action(name, action):
        calls.append((name, action))
        if name == "MSSQLSERVER" and action == "start":
            return False, "start rejected"
        states[name] = "stopped" if action == "stop" else "running"
        return True, "ok"

    monkeypatch.setattr(actions, "service_state", lambda name: states[name])
    monkeypatch.setattr(actions, "service_action", service_action)
    monkeypatch.setattr(
        actions, "wait_for_service", lambda name, expected, timeout=45: states[name] == expected
    )

    ok, message = actions.restart_service("MSSQLSERVER", ["SQLAgent"])

    assert not ok
    assert ("SQLAgent", "start") in calls
    assert states["SQLAgent"] == "running"
    assert "Could not start main service" in message
