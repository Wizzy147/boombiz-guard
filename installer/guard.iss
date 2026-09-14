; Boombiz Guard — Windows installer (Inno Setup 6). Build with installer\build.ps1,
; which passes AppVersion and FfmpegDir.
;
; UNSIGNED for BDO pilots (owner decision 2026-09-14): Windows SmartScreen shows
; "Windows protected your PC" → More info → Run anyway. Set GUARD_SIGN_CERT for
; build.ps1 to sign once a code-signing certificate exists.
;
; Uninstall removes the program, the boot task and the tray auto-start. It KEEPS
; %ProgramData%\Boombiz Guard (incidents, settings, cameras), so a reinstall
; picks up where it left off.

#ifndef AppVersion
  #error Build with installer\build.ps1
#endif

[Setup]
AppId={{6E0B7C2A-4C1F-4B8E-9C3D-B00B1260A4D5}
AppName=Boombiz Guard
AppVersion={#AppVersion}
AppVerName=Boombiz Guard {#AppVersion}
AppPublisher=Digital Orca Limited
AppPublisherURL=https://guard.getboombiz.com
AppSupportURL=https://guard.getboombiz.com/faq
DefaultDirName={autopf}\Boombiz Guard
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
SetupIconFile=guard.ico
UninstallDisplayIcon={app}\BoombizGuardTray.exe
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\dist\installer
OutputBaseFilename=BoombizGuardSetup-{#AppVersion}
CloseApplications=no

[Files]
Source: "..\dist\BoombizGuard\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "post-install.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#FfmpegDir}\bin\ffmpeg.exe"; DestDir: "{app}\ffmpeg"; Flags: ignoreversion
Source: "{#FfmpegDir}\bin\ffprobe.exe"; DestDir: "{app}\ffmpeg"; Flags: ignoreversion
Source: "{#FfmpegDir}\LICENSE"; DestDir: "{app}\ffmpeg"; DestName: "LICENSE.txt"; Flags: ignoreversion
; Models go where the agent looks first (config.models_dir). The agent refuses any
; file whose SHA-256 doesn't match its manifest.
Source: "..\agent\models\person\yolox_nano.onnx"; DestDir: "{commonappdata}\Boombiz Guard\models\person"; Flags: ignoreversion
Source: "..\agent\models\person\yolox_tiny.onnx"; DestDir: "{commonappdata}\Boombiz Guard\models\person"; Flags: ignoreversion
; ⚠ Licence review required before commercial launch (owner chose to include it, 2026-09-14).
Source: "..\agent\models\pose\rtmpose-t-body7.onnx"; DestDir: "{commonappdata}\Boombiz Guard\models\pose"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Boombiz Guard"; Filename: "{app}\BoombizGuardTray.exe"; Parameters: "--open"; Comment: "Open Boombiz Guard"

[Registry]
; The tray app (pop-ups + menu) starts for every Windows user who signs in.
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Boombiz Guard"; ValueData: """{app}\BoombizGuardTray.exe"""; Flags: uninsdeletevalue

[Run]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\post-install.ps1"" -App ""{app}"""; StatusMsg: "Starting Boombiz Guard..."; Flags: runhidden waituntilterminated
Filename: "{app}\BoombizGuardTray.exe"; Parameters: "--open"; Description: "Open Boombiz Guard setup"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallRun]
Filename: "schtasks.exe"; Parameters: "/End /TN ""Boombiz Guard Agent"""; Flags: runhidden; RunOnceId: "EndTask"
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""Boombiz Guard Agent"" /F"; Flags: runhidden; RunOnceId: "DeleteTask"
Filename: "taskkill.exe"; Parameters: "/F /IM BoombizGuardTray.exe"; Flags: runhidden; RunOnceId: "KillTray"
Filename: "taskkill.exe"; Parameters: "/F /IM BoombizGuardAgent.exe"; Flags: runhidden; RunOnceId: "KillAgent"

[Code]
// An upgrade can't overwrite running programs: stop the agent and trays first.
procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep = ssInstall then
  begin
    Exec('schtasks.exe', '/End /TN "Boombiz Guard Agent"', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Exec('taskkill.exe', '/F /IM BoombizGuardAgent.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
    Exec('taskkill.exe', '/F /IM BoombizGuardTray.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  end;
end;
