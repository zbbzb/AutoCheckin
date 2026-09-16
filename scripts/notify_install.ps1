$ErrorActionPreference = 'Stop'
# Keep this file pure ASCII: Windows PowerShell 5.1 reads .ps1 files using the ANSI
# code page, so non-ASCII text in a BOM-less UTF-8 file is decoded as mojibake and
# breaks string quoting. This follows the same rule as install_mumu_task.ps1.
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Install the local Python environment first.' }
$daemon = Join-Path $PSScriptRoot 'notify_daemon.py'
if (-not (Test-Path -LiteralPath $daemon)) { throw 'notify_daemon.py not found.' }
$taskName = 'AutoCheckin-Notifier'
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $taskPython -Argument ('"' + $daemon + '"') -WorkingDirectory $taskRoot
$logon = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
# Self-healing: if the daemon dies, the repeat trigger restarts it within 5 minutes.
# The daemon also has its own pid lock, and IgnoreNew prevents duplicate instances.
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($logon, $watchdog) -Settings $settings -Principal $principal -Description 'AutoCheckin WeCom notifier. Reads the check-in database read-only and pushes results with evidence screenshots to a WeCom group robot. Independent from the attendance service.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
& (Join-Path $PSScriptRoot 'check_ps_syntax.ps1')
Write-Output 'AutoCheckin-Notifier installed and started.'
Write-Output 'Remove with: Unregister-ScheduledTask -TaskName AutoCheckin-Notifier -Confirm:$false'
