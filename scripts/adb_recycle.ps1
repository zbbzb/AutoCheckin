$ErrorActionPreference = 'Stop'
# Keep this file pure ASCII (see mumu_tray.ps1 for the reason: Windows
# PowerShell 5.1 reads .ps1 using the ANSI code page).
# Recycle the host-side adb server before it can wedge. The 2026-09-15 incident
# showed a server that ran for eight days cached the emulator as permanently
# offline, failing every check-in run until it was killed by hand.
$root = Split-Path -Parent $PSScriptRoot
$adb = Join-Path $root 'android-sdk\platform-tools\adb.exe'
if (-not (Test-Path -LiteralPath $adb)) { throw 'adb.exe not found.' }
$logPath = Join-Path $root 'logs\adb-recycle.log'
$line = "{0}  recycled{1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), [Environment]::NewLine
try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($line)
    $stream = [System.IO.File]::Open($logPath, [System.IO.FileMode]::Append,
                                     [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
} catch { }
& $adb kill-server
