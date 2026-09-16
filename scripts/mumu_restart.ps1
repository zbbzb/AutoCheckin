# Detached helper started by the service when the console asks for a restart.
# A process cannot rebind its own listening socket or relaunch its own scheduled
# task, so this waits for the old instance to disappear and starts the task again
# (whose action is mumu_task_guard.py).
param([int]$Port = 18765)

$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent $PSScriptRoot
$serviceLock = Join-Path $root 'data\service.pid'
$taskName = 'AutoCheckin-MuMu-Controller'
$python = Join-Path $root '.venv\Scripts\pythonw.exe'

function Get-ServicePid {
    try { return [int](Get-Content -LiteralPath $serviceLock -Raw -ErrorAction Stop).Trim() }
    catch { return $null }
}

function Test-ServiceAlive {
    $id = Get-ServicePid
    if (-not $id) { return $false }
    return $null -ne (Get-Process -Id $id -ErrorAction SilentlyContinue)
}

# Wait for the old instance to exit. It may first finish cancelling a running
# check-in and shutting down the emulator, so allow generous time.
$deadline = (Get-Date).AddSeconds(240)
while ((Get-Date) -lt $deadline -and (Test-ServiceAlive)) { Start-Sleep -Milliseconds 500 }

# Clear a stale pid file left behind by an unclean exit.
if ((Test-Path -LiteralPath $serviceLock) -and -not (Test-ServiceAlive)) {
    Remove-Item -LiteralPath $serviceLock -Force
}

# If the port is somehow still held, wait for the release instead of racing it.
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    $busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $busy) { break }
    Start-Sleep -Milliseconds 500
}

$marker = Join-Path $root 'data\service.stop'
Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue

Start-ScheduledTask -TaskName $taskName

# Last resort: the task may be disabled or missing, so launch the guard directly.
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline -and -not (Test-ServiceAlive)) { Start-Sleep -Milliseconds 700 }
if (-not (Test-ServiceAlive)) {
    Start-Process -FilePath $python -ArgumentList (Join-Path $PSScriptRoot 'mumu_task_guard.py') `
        -WorkingDirectory $root -WindowStyle Hidden
}
