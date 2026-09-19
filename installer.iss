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
PrivilegesRequired=lowest
OutputDir=dist-installer
OutputBaseFilename=inkdoc-setup
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "dist\inkdoc\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
