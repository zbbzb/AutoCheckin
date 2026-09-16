# Authoritative syntax and encoding check for this project's Windows script hosts.
#
# Run it with Windows PowerShell 5.1 (the interpreter the scheduled task and the tray
# actually use):
#
#     powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\check_ps_syntax.ps1
#
# Three traps this catches, each of which has already broken this project once:
#   * PS 5.1 reads .ps1 files using the ANSI code page, so non-ASCII text in a
#     BOM-less UTF-8 file is decoded as mojibake that breaks string quoting;
#   * a parser invoked in one PowerShell version does not always agree with another;
#   * VBScript's Trim() does not remove CR/LF, so comparing a helper's answer against
#     "ok" silently fails and blocks the tray from ever starting.
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$scripts = Join-Path $root 'scripts'
$failed = 0

foreach ($file in (Get-ChildItem $scripts -Filter '*.ps1' | Sort-Object Name)) {
    $errors = $null
    $tokens = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
    $nonAscii = @([System.IO.File]::ReadAllBytes($file.FullName) | Where-Object { $_ -gt 127 }).Count
    if ($errors.Count -eq 0 -and $nonAscii -eq 0) {
        Write-Output ("OK   {0}" -f $file.Name)
    } else {
        $failed++
        Write-Output ("FAIL {0}  (non-ascii bytes: {1})" -f $file.Name, $nonAscii)
        $errors | Select-Object -First 4 | ForEach-Object {
            Write-Output ("     line {0}: {1}" -f $_.Extent.StartLineNumber, $_.Message)
        }
    }
}

foreach ($file in (Get-ChildItem $scripts -Filter '*.vbs' | Sort-Object Name)) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $nonAscii = @($bytes | Where-Object { $_ -gt 127 }).Count
    $text = [System.Text.Encoding]::ASCII.GetString($bytes)
    $problems = @()
    if ($nonAscii -gt 0) { $problems += "non-ascii bytes: $nonAscii" }
    # Reading a helper's answer must go through HelperLine, which strips CR/LF.
    if ($text -match 'ReadAll\(\)' -and $text -notmatch 'Function HelperLine') {
        $problems += 'reads a helper answer without HelperLine (CR/LF not stripped)'
    }
    if ($problems.Count -eq 0) {
        Write-Output ("OK   {0}" -f $file.Name)
    } else {
        $failed++
        Write-Output ("FAIL {0}  ({1})" -f $file.Name, ($problems -join '; '))
    }
}

if ($failed) { exit 1 }
