# Start the Boombiz Guard tray app when this Windows user logs in.
# Dev/pilot helper — the signed installer will do this in production.
$agent = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $agent ".venv\Scripts\pythonw.exe"
$script = Join-Path $PSScriptRoot "guard_tray.py"
$startup = [Environment]::GetFolderPath("Startup")
$lnk = Join-Path $startup "Boombiz Guard.lnk"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = $pythonw
$s.Arguments = "`"$script`""
$s.WorkingDirectory = $agent
$s.Description = "Boombiz Guard pop-up notifications"
$s.Save()
Write-Output "Installed: $lnk"
