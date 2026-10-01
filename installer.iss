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

(* ─── Downloaded engines on uninstall ──────────────────────────────────────
  A parenthesis-star comment on purpose: a brace comment would end at the
  first closing brace in its own text, such as the one in {app} below.
  The Docling pack (~1 GB) and GLM-OCR (~1.45 GB) are downloaded by the app into
  the engines folder, not installed by this setup, so Inno would leave them
  behind. Ask, and default to keeping them: a reinstall finds them ready.

  The engines folder is resolved the way EngineManager.get_engines_base_dir()
  does it, NOT from {app}: the app may be installed anywhere, the engines are
  always under LOCALAPPDATA (or INKDOC_ENGINES_DIR, or the short root
  %SYSTEMDRIVE%\InkDoc\engines used when LOCALAPPDATA is very long).

  Silent uninstalls (/VERYSILENT) keep the engines. The in-app updater runs the
  installer, never the uninstaller, so updates cannot reach this step. *)

function DirSize(const Dir: String): Int64;
var
  FindRec: TFindRec;
begin
  Result := 0;
  if FindFirst(AddBackslash(Dir) + '*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name <> '.') and (FindRec.Name <> '..') then
        begin
          if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
            Result := Result + DirSize(AddBackslash(Dir) + FindRec.Name)
          else
            Result := Result + (Int64(FindRec.SizeHigh) shl 32) + FindRec.SizeLow;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

function FormatSize(const Bytes: Int64): String;
var
  MB: Int64;
begin
  MB := Bytes div (1024 * 1024);
  if MB >= 1024 then
    Result := IntToStr(MB div 1024) + '.' + IntToStr(((MB mod 1024) * 10) div 1024) + ' GB'
  else
    Result := IntToStr(MB) + ' MB';
end;

procedure AddIfPresent(var Dirs: TArrayOfString; const Dir: String);
var
  I: Integer;
begin
  if (Dir = '') or not DirExists(Dir) then
    exit;
  for I := 0 to GetArrayLength(Dirs) - 1 do
    if CompareText(Dirs[I], Dir) = 0 then
      exit;
  SetArrayLength(Dirs, GetArrayLength(Dirs) + 1);
  Dirs[GetArrayLength(Dirs) - 1] := Dir;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dirs: TArrayOfString;
  I: Integer;
  Total: Int64;
begin
  if CurUninstallStep <> usUninstall then
    exit;
  AddIfPresent(Dirs, GetEnv('INKDOC_ENGINES_DIR'));
  AddIfPresent(Dirs, ExpandConstant('{localappdata}\InkDoc\engines'));
  AddIfPresent(Dirs, ExpandConstant('{sd}\InkDoc\engines'));
  if GetArrayLength(Dirs) = 0 then
    exit;
  if UninstallSilent() then
    exit;
  Total := 0;
  for I := 0 to GetArrayLength(Dirs) - 1 do
    Total := Total + DirSize(Dirs[I]);
  if MsgBox('Also remove downloaded engines and models (Docling, GLM-OCR)? They use ' + FormatSize(Total) + '.' + #13#10#13#10 +
            'Choose No to keep them; reinstalling InkDoc will find them ready.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
  begin
    for I := 0 to GetArrayLength(Dirs) - 1 do
      DelTree(Dirs[I], True, True, True);
  end;
end;

[Run]
; Interactive install: offer the usual "Launch InkDoc" checkbox on the final page.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall
; Silent in-app update: relaunch, but only when /RESTARTAPP=1 was passed.
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: ShouldRelaunchAfterSilentUpdate

