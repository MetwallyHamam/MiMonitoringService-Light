#define MyAppName "MiMonitoringService"
#define MyAppVersion "0.1.2"
#define MyAppPublisher "Millensys"
#define MyAppExeName "MiMonitoringService.exe"

[Setup]
AppId={{9C3EBD0D-9C18-48EA-A31C-0AD78EA97DC0}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppCopyright=© 2026 Millensys Support Department | Eng.Metwally Hammam
DefaultDirName={autopf}\Millensys\MiMonitoringService
DefaultGroupName=Millensys
OutputDir=..\release
OutputBaseFilename=MiMonitoringService-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=..\assets\mimonitoringservice.ico

[Files]
Source: "..\release\build\MiMonitoringService\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{app}\data"; Permissions: admins-full

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\MiMonitoringService.Service.exe"; Parameters: "--startup auto install"; Flags: runhidden waituntilterminated; StatusMsg: "Installing monitoring service..."
Filename: "{app}\MiMonitoringService.Service.exe"; Parameters: "start"; Flags: runhidden waituntilterminated; StatusMsg: "Starting monitoring service..."
Filename: "{app}\{#MyAppExeName}"; Description: "Open {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\MiMonitoringService.Service.exe"; Parameters: "stop"; Flags: runhidden waituntilterminated; RunOnceId: "StopMonitoringService"
Filename: "{app}\MiMonitoringService.Service.exe"; Parameters: "remove"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveMonitoringService"

[Code]
var
  KeepOperationalData: Boolean;

function InitializeUninstall(): Boolean;
begin
  KeepOperationalData := MsgBox(
    'Keep MiMonitoringService configuration, encrypted credentials, event history, and logs?',
    mbConfirmation, MB_YESNO) = IDYES;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and (not KeepOperationalData) then
    DelTree(ExpandConstant('{app}\data'), True, True, True);
end;

