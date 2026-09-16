$ErrorActionPreference = 'Stop'
# Keep this file pure ASCII: Windows PowerShell 5.1 reads .ps1 files using the ANSI
# code page, so non-ASCII text in a BOM-less UTF-8 file is decoded as mojibake and
# breaks string quoting. Localized wording comes from tray_text.json as UTF-8.
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Install the local Python environment first.' }
$text = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'tray_text.json'), [System.Text.Encoding]::UTF8) | ConvertFrom-Json
$taskName = 'AutoCheckin-MuMu-Controller'
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskAction = New-ScheduledTaskAction -Execute $taskPython -Argument ('"' + (Join-Path $PSScriptRoot 'mumu_task_guard.py') + '"') -WorkingDirectory $taskRoot
$taskLogon = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$taskWatchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger @($taskLogon, $taskWatchdog) -Settings $taskSettings -Principal $taskPrincipal -Description 'Local MuMu attendance dashboard and scheduler. The action is mumu_task_guard.py, which starts the service and the tray companion and honours a manual stop.' -Force | Out-Null
$legacy = Get-ScheduledTask -TaskName 'AutoCheckin' -ErrorAction SilentlyContinue
if ($legacy) {
    $backupDir = Join-Path $taskRoot 'data'
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    $backup = Join-Path $backupDir 'legacy-AutoCheckin.xml'
    if (-not (Test-Path -LiteralPath $backup)) { Export-ScheduledTask -TaskName 'AutoCheckin' | Set-Content -LiteralPath $backup -Encoding Unicode }
    Disable-ScheduledTask -TaskName 'AutoCheckin' | Out-Null
}
$shortcutShell = New-Object -ComObject WScript.Shell
$shortcut = $shortcutShell.CreateShortcut((Join-Path $taskRoot $text.consoleShortcut))
$shortcut.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
$shortcut.Arguments = '"' + (Join-Path $PSScriptRoot 'start_panel.vbs') + '"'
$shortcut.WorkingDirectory = $taskRoot
$shortcut.Description = $text.shortcutDescription
$shortcut.Save()
# The tray companion is a per-session GUI process, so it is started from the Startup
# folder rather than from the task that gets re-launched every minute.
$startup = [Environment]::GetFolderPath('Startup')
$trayLink = $shortcutShell.CreateShortcut((Join-Path $startup 'AutoCheckin Tray.lnk'))
$trayLink.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
$trayLink.Arguments = '"' + (Join-Path $PSScriptRoot 'start_tray.vbs') + '"'
$trayLink.WorkingDirectory = $taskRoot
$trayLink.Description = 'AutoCheckin tray companion'
$trayLink.Save()
Start-ScheduledTask -TaskName $taskName
# Surface PowerShell encoding/parse problems immediately instead of letting the task
# fail silently with a syntax error (this has happened once already).
& (Join-Path $PSScriptRoot 'check_ps_syntax.ps1')
Write-Output $text.installDone
