from pathlib import Path

from millensys_guardian import service_control


def test_remove_stops_before_delete(monkeypatch):
    commands = []
    states = iter(["Running", "Stopped", "Stopped"])
    monkeypatch.setattr(service_control, "_packaged_service", lambda: None)
    monkeypatch.setattr(service_control, "status", lambda: next(states))
    monkeypatch.setattr(
        service_control,
        "_run",
        lambda command, timeout=45: (commands.append(command) is None, "ok"),
    )

    ok, _ = service_control.manage("remove")

    assert ok
    assert commands == [
        ["sc.exe", "stop", service_control.SERVICE_NAME],
        ["sc.exe", "delete", service_control.SERVICE_NAME],
    ]


def test_packaged_install_uses_elevated_service_helper(monkeypatch):
    executable = Path("MiMonitoringService.Service.exe")
    calls = []
    monkeypatch.setattr(service_control, "_packaged_service", lambda: executable)
    monkeypatch.setattr(
        service_control,
        "_run_elevated",
        lambda exe, arguments, timeout=60: (
            calls.append((exe, arguments)) is None,
            "completed",
        ),
    )

    assert service_control.manage("install")[0]
    assert calls == [(executable, ["--startup", "auto", "install"])]
