# Millensys Server Guardian

Windows monitoring agent and desktop console for Windows Services, IIS application pools,
disk capacity, CPU, and memory. It is designed to run the monitoring engine as a Windows
Service while the GUI is opened only when an administrator needs it.

## Safety and security

- SMTP passwords are encrypted with Windows DPAPI for the current machine.
- Configuration contains no plaintext credentials.
- Automatic actions have cooldowns, retry limits, maintenance windows, and an audit trail.
- Alert fingerprints suppress duplicate mail until the affected item recovers.
- Removing the Windows Service always requests a stop and waits before deletion.

## Developer start

Use Python 3.11 or 3.12 on Windows (PySide6/pywin32 support may lag on newer Python releases).

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m millensys_guardian.app
```

The first launch opens the setup guide. Runtime data is stored in
`%PROGRAMDATA%\Millensys\ServerGuardian` so the GUI and Windows Service share one database.

## Service commands (elevated terminal, or use Settings in the GUI)

```powershell
.venv\Scripts\python -m millensys_guardian.service_host install
.venv\Scripts\python -m millensys_guardian.service_host start
```

For production, package with `scripts/build.ps1`, sign the generated executable, and install it
with an administrator account. The packaged GUI uses the windowed subsystem, so no console is
shown.

## Current release scope

This foundation includes live discovery, dashboard/event views, persistent settings, monitoring
and guarded restart actions, SMTP queueing/deduplication, and daily/monthly HTML report plumbing.
Scheduled stop/start/recycle editors and MSI deployment are the next release milestone; the data
model intentionally leaves room for them.