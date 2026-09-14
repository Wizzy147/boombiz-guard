# Builds dist\installer\BoombizGuardSetup-<version>.exe
#
#   powershell -ExecutionPolicy Bypass -File installer\build.ps1
#
# Needs: agent\.venv with requirements + pyinstaller, Node (desktop-ui),
# Inno Setup 6, the three model files in agent\models, and an FFmpeg build
# (GUARD_FFMPEG_DIR = the folder holding bin\ffmpeg.exe; default: the one on PATH).
# Optional: GUARD_SIGN_CERT (.pfx) + GUARD_SIGN_PASSWORD to sign the installer.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$version = [regex]::Match((Get-Content -Raw "$root\agent\app\main.py"), 'VERSION = "([^"]+)"').Groups[1].Value
if (-not $version) { throw "Couldn't read VERSION from agent\app\main.py" }

$ffmpegDir = $env:GUARD_FFMPEG_DIR
if (-not $ffmpegDir) { $ffmpegDir = Split-Path -Parent (Split-Path -Parent (Get-Command ffmpeg).Source) }
if (-not (Test-Path "$ffmpegDir\bin\ffprobe.exe")) { throw "No FFmpeg at $ffmpegDir (set GUARD_FFMPEG_DIR)" }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 is not installed (winget install JRSoftware.InnoSetup)" }

Write-Output "Boombiz Guard $version"
Push-Location "$root\desktop-ui"
try { npm run build; if ($LASTEXITCODE) { throw "setup UI build failed" } } finally { Pop-Location }

& "$root\agent\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean `
    --distpath "$root\dist" --workpath "$root\build" "$PSScriptRoot\guard.spec"
if ($LASTEXITCODE) { throw "PyInstaller failed" }

& $iscc "/DAppVersion=$version" "/DFfmpegDir=$ffmpegDir" "$PSScriptRoot\guard.iss"
if ($LASTEXITCODE) { throw "Inno Setup failed" }

$setup = "$root\dist\installer\BoombizGuardSetup-$version.exe"
if ($env:GUARD_SIGN_CERT) {
    & signtool sign /f $env:GUARD_SIGN_CERT /p $env:GUARD_SIGN_PASSWORD /fd SHA256 `
        /tr http://timestamp.digicert.com /td SHA256 $setup
    if ($LASTEXITCODE) { throw "Signing failed" }
} else {
    Write-Warning "Installer is UNSIGNED (Windows will show a SmartScreen warning)."
}
Write-Output "Built $setup"
