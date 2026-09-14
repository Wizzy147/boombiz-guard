# Run by the Boombiz Guard installer (elevated) after files are copied.
#   post-install.ps1 -App "C:\Program Files\Boombiz Guard"
#
#   1. Locks %ProgramData%\Boombiz Guard to SYSTEM + Administrators: guard.db
#      and the incident media aren't readable by other local users
#      (docs/security.md open item 2).
#   2. Makes the setup token if missing and lets signed-in users READ that one
#      file: the tray app runs as the cashier and needs it to ask the agent
#      for pop-ups.
#   3. Registers the agent to start at boot as SYSTEM (before anyone signs
#      in) and restart every minute if it ever stops, then starts it.
#   4. Keeps the computer awake on mains power: no sleep, no hibernate, so
#      Guard never stops watching because Windows dozed off. The screen may
#      still turn off. "Turn back on after a power cut" is a BIOS setting
#      Windows can't change — the setup wizard's last screen asks the
#      installer to set it.
param([Parameter(Mandatory = $true)][string]$App)
$ErrorActionPreference = "Stop"

$data = Join-Path $env:ProgramData "Boombiz Guard"
New-Item -ItemType Directory -Force -Path $data | Out-Null
# SIDs, not names: S-1-5-18 SYSTEM, S-1-5-32-544 Administrators, S-1-5-32-545 Users.
& icacls $data /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null

$tok = Join-Path $data "setup-token"
$have = if (Test-Path $tok) { (Get-Content -Raw $tok).Trim() } else { "" }
if ($have.Length -lt 32) {
    $b = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    $s = [Convert]::ToBase64String($b).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    [IO.File]::WriteAllText($tok, $s)  # UTF-8, no BOM — same as the agent writes
}
& icacls $tok /grant "*S-1-5-32-545:R" | Out-Null

$task = "Boombiz Guard Agent"
$exe = Join-Path $App "BoombizGuardAgent.exe"
$action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $App
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "Boombiz Guard: watches your CCTV and sounds the alarm." -Force | Out-Null
Start-ScheduledTask -TaskName $task

# 4. Never sleep or hibernate on mains power (0 = never). Battery settings are
# left alone: a laptop on battery is already in trouble and should save itself.
# A failure here must not fail the install — Guard still works, it just may doze.
try {
    & powercfg /change standby-timeout-ac 0 | Out-Null
    & powercfg /change hibernate-timeout-ac 0 | Out-Null
    & powercfg /hibernate off | Out-Null
} catch {
    Write-Warning "Could not change the power settings: $_"
}
