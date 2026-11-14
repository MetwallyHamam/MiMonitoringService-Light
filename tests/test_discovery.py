from types import SimpleNamespace

from millensys_guardian.discovery import iis_pools


def test_iis_pool_discovery_parses_appcmd_xml(monkeypatch):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <appcmd><APPPOOL APPPOOL.NAME="DefaultAppPool" state="Started" />
    <APPPOOL APPPOOL.NAME="ApiPool" state="Stopped" /></appcmd>"""
    monkeypatch.setattr("millensys_guardian.discovery.shutil.which", lambda _: "appcmd.exe")
    monkeypatch.setattr(
        "millensys_guardian.discovery.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=xml),
    )

    assert iis_pools() == [
        {"key": "ApiPool", "name": "ApiPool", "status": "Stopped"},
        {"key": "DefaultAppPool", "name": "DefaultAppPool", "status": "Started"},
    ]


def test_iis_pool_discovery_does_not_display_appcmd_errors(monkeypatch):
    monkeypatch.setattr("millensys_guardian.discovery.shutil.which", lambda _: "appcmd.exe")
    monkeypatch.setattr(
        "millensys_guardian.discovery.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="ERROR ( hresult:80004001, message:Failed to generate item output. )"
        ),
    )

    assert iis_pools() == []
