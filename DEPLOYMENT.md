# Deployment and Release Validation

The target Windows Server does **not** need Python, pip, PySide6, or pywin32. The build produces two
standalone executables—one GUI and one Windows Service—with the Python runtime and dependencies
embedded. No `_internal` folder is deployed.

## Build workstation

1. Create the Python 3.11 development environment described in `README.md`.
2. Run `scripts\build.ps1`.
3. Confirm `release\build\MiMonitoringService` contains both executables and `build-manifest.json`.
4. Install Inno Setup 6 on the build workstation only.
5. Run `scripts\build_installer.ps1`.
6. Sign the setup executable and both embedded executables with the Millensys code-signing
   certificate and a trusted timestamp service.

## Clean-machine acceptance test

Run these checks on every supported Windows Server version from a clean snapshot:

- Python is not installed and neither `python.exe` nor `pythonw.exe` is on `PATH`.
- Installation presents the Millensys publisher after signing and completes without a console.
- `%ProgramFiles%\Millensys\Server Guardian` contains the GUI and service executables.
- The installation-local `data` directory is writable by administrators and SYSTEM.
- The `MiMonitoringService` service is Automatic and reaches Running after installation.
- The first-run guide opens and saves configuration without a terminal window.
- A saved SMTP app password can be used by the LocalSystem monitoring service.
- Service discovery, IIS discovery, disk selection, and CPU/memory samples work.
- GUI Start, Stop, and Remove actions show UAC and update the displayed state.
- Remove stops the service before deletion. If Services.msc is open, close it before reinstalling;
  Windows can retain a deleted service registration while another process holds an SCM handle.
- Upgrade retains the database, encrypted secret, events, schedules, and target selections.
- Uninstall removes the service and application files; decide explicitly whether operational data
  in ProgramData should be retained for audit/recovery.

## Release gates

- Ruff and pytest pass.
- PyInstaller warnings contain no required Windows modules missing.
- GUI starts with a system-only `PATH` and shows the startup guide.
- Packaged service completes install, start, stop, and remove validation.
- 100%, 125%, 150%, and 200% DPI layouts are visually accepted.
- SMTP outage, API outage, stopped service, stopped pool, low disk, and performance-threshold tests
  are recorded.
- A 72-hour soak test completes without duplicate actions, unbounded database growth, or mail spam.
- SHA-256 hashes, version, signing identity, test results, and known limitations are included in the
  release notes.

