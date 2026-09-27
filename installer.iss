; Inno Setup script for InkDoc.
;
; Builds a per-user (no admin required) Windows installer around the
; PyInstaller --onedir output at dist\inkdoc\.
;
; Local test compile (uses the fallback AppVersion below):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
;
; CI / release compile (version injected via /D, see release.yml):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=1.0.0 installer.iss

#define MyAppName "InkDoc"
#define MyAppPublisher "Abdoslam Baabbad"
#define MyAppURL "https://github.com/AbdoslamB/inkdoc"
#define MyAppExeName "inkdoc.exe"

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

[Setup]
; Fixed AppId (GUID) — generated once, must never change across releases so
; Windows/Inno Setup correctly recognizes upgrades and the uninstaller stays
; associated with the right installation.
AppId={{C4AA8317-64C4-49E2-AE33-9B9857DEFA52}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
; Free single-user desktop tool: no UAC prompt, no admin requirement.
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll
PrivilegesRequired=lowest
OutputDir=dist-installer
OutputBaseFilename=inkdoc-setup
UninstallDisplayIcon={app}\assets\logo.ico
SetupIconFile=assets\logo.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\inkdoc\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "assets\logo.ico"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\logo.ico"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"; IconFilename: "{app}\assets\logo.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\assets\logo.ico"; Tasks: desktopicon

[Code]
function ShouldRelaunchAfterSilentUpdate: Boolean;
begin
  { Inno skips [Run] entries flagged "postinstall" in silent mode, because those are
    checkboxes on the "Completing Setup" wizard page and that page is never shown.
    The in-app updater runs this installer with /VERYSILENT, so an update installed
    successfully and then never restarted, while the app reported "InkDoc is
    restarting...".

    Relaunch only when the updater asks for it explicitly. A plain silent install
    must not spawn a GUI: scripts/smoke_test_artifact.py installs with exactly the
    same /VERYSILENT /SUPPRESSMSGBOXES /NORESTART flags on every release build, and
    an unattended deployment should stay unattended. }
  Result := WizardSilent() and (ExpandConstant('{param:RESTARTAPP|0}') = '1');
end;

[Run]
; Interactive install: offer the usual "Launch InkDoc" checkbox on the final page.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall
; Silent in-app update: relaunch, but only when /RESTARTAPP=1 was passed.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: ShouldRelaunchAfterSilentUpdate

