from __future__ import annotations

import json
import sys
from pathlib import Path

from millensys_guardian.security import protect


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: provision_support_bundle.py OUTPUT_DIRECTORY")
    project = Path(__file__).resolve().parents[1]
    source = project / "support-private"
    destination = Path(sys.argv[1]).resolve() / "support-private"
    destination.mkdir(parents=True, exist_ok=True)
    private_pem = (source / "license-signing-private.pem").read_text(encoding="ascii")
    smtp_password = (source / "smtp-app-password.txt").read_text(encoding="utf-8").strip()
    (destination / "license-signing-private.dpapi").write_text(
        protect(private_pem), encoding="utf-8"
    )
    smtp_config = {
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "millensysnotifications@gmail.com",
        "sender_name": "MiMonitoringService Licensing",
        "use_tls": True,
        "password_protected": protect(smtp_password.replace(" ", "")),
    }
    (destination / "generator-smtp.json").write_text(
        json.dumps(smtp_config, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
