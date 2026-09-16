$ErrorActionPreference = 'Stop'
# Keep this file pure ASCII (PowerShell 5.1 reads .ps1 with the ANSI code page).
# Registers the daily 03:00 adb-server recycle (see adb_recycle.ps1 why).
# Remove with: Unregister-ScheduledTask -TaskName AutoCheckin-AdBRecycle -Confirm:$false
$taskRoot = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'adb_recycle.ps1'
if (-not (Test-Path -LiteralPath $script)) { throw 'adb_recycle.ps1 not found.' }
$taskName = 'AutoCheckin-AdBRecycle'
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $script + '"') `
    -WorkingDirectory $taskRoot
$daily = New-ScheduledTaskTrigger -Daily -At 03:00
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $daily -Settings $settings -Principal $principal `
    -Description 'Daily 03:00 recycle of the host-side adb server so a long-lived server can never cache the MuMu emulator as offline again (incident 2026-09-15).' -Force | Out-Null
& (Join-Path $PSScriptRoot 'check_ps_syntax.ps1')
Write-Output 'AutoCheckin-AdBRecycle installed (daily 03:00).'
