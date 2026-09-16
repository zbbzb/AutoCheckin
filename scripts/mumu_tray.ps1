# AutoCheckin tray companion: the visual on/off surface for the MuMu check-in service.
# Runs in the logged-on user session, stays resident, and writes its pid so the
# scheduled task guard can avoid starting a second copy.
#
# IMPORTANT: keep this file pure ASCII. Windows PowerShell 5.1 reads .ps1 files using
# the ANSI code page, so a BOM-less UTF-8 file with non-ASCII text is decoded as
# mojibake, which breaks string quoting and makes the whole script fail to parse.
# All localized wording lives in tray_text.json and is read as UTF-8 at runtime.
#
# Two hard-won constraints:
#   * Menu clicks need a real WinForms message loop (Application.Run). Pumping with
#     DoEvents plus a 1500 ms Start-Sleep stalls the UI thread enough to feel dead.
#   * Never try to read a value back from pythonw.exe. The GUI-subsystem interpreter
#     writes nothing to stdout or a pipe, so such a call silently returns empty.
param([string]$SelfTest = '')

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $root 'data'
$trayLock = Join-Path $dataDir 'tray.pid'
$serviceLock = Join-Path $dataDir 'service.pid'
$helper = Join-Path $PSScriptRoot 'tray_helper.py'
$pythonConsole = Join-Path $root '.venv\Scripts\python.exe'
$guard = Join-Path $PSScriptRoot 'mumu_task_guard.py'
$taskName = 'AutoCheckin-MuMu-Controller'
$textPath = Join-Path $PSScriptRoot 'tray_text.json'
$logPath = Join-Path $root 'logs\tray.log'

function Say($message) {
    # Plain .NET append: the PowerShell provider cmdlets depend on runspace state that
    # a hidden, long-lived host can lose, and a tray that cannot log is undiagnosable.
    $line = "{0}  {1}{2}" -f (Get-Date -Format 'HH:mm:ss'), $message, [Environment]::NewLine
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($line)
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        try {
            $stream = [System.IO.File]::Open($logPath, [System.IO.FileMode]::Append,
                                             [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite)
            try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
            return
        } catch {
            Start-Sleep -Milliseconds 100
        }
    }
}

# A tray that dies silently is invisible to the user, so every failure path must leave
# a trace in logs/tray.log. -ErrorActionPreference Stop makes even routine cmdlet
# errors terminate, so this handler is what turns them into a readable entry.
trap {
    Say "FATAL: $($_.Exception.GetType().Name): $($_.Exception.Message)"
    Say "  at $($_.InvocationInfo.PositionMessage -replace "`r?`n", ' ')"
    Remove-Item -LiteralPath $trayLock -Force -ErrorAction SilentlyContinue
    exit 1
}

New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $root 'logs') -Force | Out-Null

# Load the localized wording explicitly with -InputObject. Piping a string into
# ConvertFrom-Json is not reliable here: under Windows PowerShell 5.1 the pipeline can
# enumerate the string into characters, which silently yields a nonsense object and a
# menu with no labels. The parsed labels are logged so this can never regress silently.
function Get-TrayText {
    # ASCII-only built-in fallback: this file must stay pure ASCII, so the Chinese
    # wording can only ever come from the UTF-8 JSON resource.
    $fallback = [ordered]@{
        consoleShortcut     = 'AutoCheckin.lnk'
        shortcutDescription = 'AutoCheckin console'
        installDone         = 'AutoCheckin controller installed'
        trayTooltipStarting = 'AutoCheckin - starting'
        trayTooltipRunning  = 'AutoCheckin - service running'
        trayTooltipEnabled  = 'auto check-in on'
        trayTooltipDisabled = 'auto check-in off'
        trayTooltipStopped  = 'AutoCheckin - service stopped'
        menuOpen            = 'Open console'
        menuRestart         = 'Restart service'
        menuStop            = 'Stop service'
        menuNotify          = 'Feishu notifications'
        menuExit            = 'Exit tray'
        balloonStarted      = 'background service started'
        balloonStopped      = 'background service stopped'
        balloonStarting     = 'background service starting'
        balloonNotifyOn     = 'Feishu notifications on'
        balloonNotifyOff    = 'Feishu notifications off'
    }
    try {
        $raw = [System.IO.File]::ReadAllText($textPath, [System.Text.Encoding]::UTF8)
        $parsed = ConvertFrom-Json -InputObject $raw
        $missing = @()
        foreach ($name in $fallback.Keys) {
            if ($null -eq $parsed.$name -or "$($parsed.$name)" -eq '') { $missing += $name }
        }
        if ($missing.Count -gt 0) {
            Say "text resource incomplete ($($missing -join ',')); using built-in English labels"
            return [pscustomobject]$fallback
        }
        return $parsed
    } catch {
        Say "text resource unreadable ($($_.Exception.Message)); using built-in English labels"
        return [pscustomobject]$fallback
    }
}

$T = Get-TrayText
# Permanent guard: the labels are the one thing that can be silently empty (a broken
# resource read once produced a menu with no text). This only speaks up when the loaded
# value is wrong, so a healthy start leaves no noise in the log.
if ([string]::IsNullOrWhiteSpace([string]$T.menuOpen) -or ([string]$T.menuOpen).Length -lt 2) {
    Say ("text guard: menuOpen looks wrong; length=$(([string]$T.menuOpen).Length)")
}
if ([string]$T.menuRestart -eq [string]$T.menuStop) {
    Say 'text guard: restart/stop labels are identical; the resource may not have loaded'
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -Namespace AutoCheckin -Name Native -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Auto)]
public static extern bool DestroyIcon(System.IntPtr handle);
[DllImport("user32.dll", CharSet = CharSet.Auto, SetLastError = true)]
public static extern uint RegisterWindowMessage(string lpString);
'@
# A NativeWindow subclass, because WndProc is a protected method and cannot be attached
# as a PowerShell event; an earlier attempt used a non-existent Add_WndProc event and
# failed (harmlessly, but it left the tray with no shell hook). -TypeDefinition is used
# rather than -MemberDefinition because the latter wraps the source in a generated class
# whose name would collide with this one.
Add-Type -TypeDefinition @'
public class CopyShellHook : System.Windows.Forms.NativeWindow
{
    private readonly int taskbarCreated;
    private readonly System.Action onRecreated;

    public CopyShellHook(int taskbarCreated, System.Action onRecreated)
    {
        this.taskbarCreated = taskbarCreated;
        this.onRecreated = onRecreated;
    }

    protected override void WndProc(ref System.Windows.Forms.Message m)
    {
        if (m.Msg == this.taskbarCreated && this.onRecreated != null) { this.onRecreated(); }
        base.WndProc(ref m);
    }
}
'@ -ReferencedAssemblies System.Windows.Forms

function Watch-TaskbarRecreated($tray, $anchor) {
    # Explorer sends TaskbarCreated when the shell restarts (a crash, "restart explorer",
    # some updates). A NotifyIcon must re-register itself then, otherwise the tray icon
    # silently disappears while the process keeps running. The message goes to top-level
    # windows, so we hook the window that already exists for the context menu.
    try {
        $message = [AutoCheckin.Native]::RegisterWindowMessage('TaskbarCreated')
        $hook = New-Object CopyShellHook ([int]$message, [Action]{
            Say 'taskbar recreated; re-registering the tray icon'
            try { $tray.Visible = $false; $tray.Visible = $true } catch { Say "re-register failed: $($_.Exception.Message)" }
        }.GetNewClosure())
        $hook.AssignHandle($anchor.Handle)
        return $hook
    } catch {
        Say "taskbar hook unavailable: $($_.Exception.Message)"
        return $null
    }
}

# python.exe, not pythonw.exe: this one must be able to answer on stdout.
function Invoke-Helper([string]$command) {
    try { return (& $pythonConsole $helper $command 2>$null | Out-String).Trim() } catch { return '' }
}

$port = 18765
$parsed = 0
try { $parsed = [int](Invoke-Helper 'port') } catch { $parsed = 0 }
if ($parsed -gt 0) { $port = $parsed }
$base = "http://127.0.0.1:$port"

function Get-ServicePid {
    # Liveness is resolved with CIM, not by reading pythonw output (see the header).
    # Exact ProcessId matching avoids the Windows pid-recycling false positive: a
    # recycled pid would otherwise make a dead service look alive forever.
    $id = 0
    try { $id = [int](Get-Content -LiteralPath $serviceLock -Raw -ErrorAction Stop).Trim() } catch { return $null }
    if ($id -le 0) {
        Remove-Item -LiteralPath $serviceLock -Force -ErrorAction SilentlyContinue
        return $null
    }
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) { return $id }
    Remove-Item -LiteralPath $serviceLock -Force -ErrorAction SilentlyContinue
    return $null
}

function Test-ServiceAlive {
    if (Get-ServicePid) { return $true }
    # No pid file but something still owns the port: treat it as alive, because
    # stopping "nothing" while a service holds the port is the worse failure.
    try { return @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue).Count -gt 0 }
    catch { return $false }
}

function Get-Health {
    if (-not (Test-ServiceAlive)) { return $null }
    try { return Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 3 } catch { return $null }
}

function Wait-Service([int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (Get-Health) { return $true }
        Start-Sleep -Milliseconds 700
    }
    return (Get-Health) -ne $null
}

function New-DotIcon([System.Drawing.Color]$color) {
    $bmp = New-Object System.Drawing.Bitmap 32, 32
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $brush = New-Object System.Drawing.SolidBrush $color
    $g.FillEllipse($brush, 3, 3, 26, 26)
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(80, 255, 255, 255)), 3
    $g.DrawArc($pen, 8, 8, 16, 16, 205, 130)
    $g.Dispose(); $brush.Dispose(); $pen.Dispose()
    $handle = $bmp.GetHicon()
    return @{ Icon = [System.Drawing.Icon]::FromHandle($handle); Handle = $handle }
}

# Windows only frees an icon handle explicitly; recreating one without releasing the
# previous handle would leak GDI objects until the tray dies.
function Set-Icon([hashtable]$slot, [System.Drawing.Color]$color) {
    $next = New-DotIcon $color
    $previous = $slot['current']
    $slot['current'] = $next
    if ($previous) {
        $previous.Icon.Dispose()
        [AutoCheckin.Native]::DestroyIcon($previous.Handle) | Out-Null
    }
    return $next.Icon
}

function Request-Service([string]$action) {
    $health = Get-Health
    if (-not $health) {
        Say "Request-Service($action): service is not answering; no request sent"
        return $false
    }
    try {
        Invoke-RestMethod -Uri "$base/api/service" -Method Post -ContentType 'application/json' `
            -Headers @{ 'X-Checkin-Token' = $health.token } `
            -Body (@{ action = $action } | ConvertTo-Json -Compress) -TimeoutSec 15 | Out-Null
        Say "Request-Service($action): accepted by pid $($health.pid)"
        return $true
    } catch {
        Say "Request-Service($action) failed: $($_.Exception.Message)"
        return $false
    }
}

function Start-Service {
    Say 'Start-Service: clearing stop marker and starting'
    $null = Invoke-Helper 'clear-stop'
    Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not (Wait-Service 25)) { & $pythonConsole $guard 2>$null | Out-Null }
    $up = Wait-Service 25
    Say "Start-Service: done, up=$up"
    return $up
}

function Stop-Service {
    Say 'Stop-Service: requesting stop'
    if (-not (Request-Service 'stop')) {
        $id = Get-ServicePid
        if ($id) {
            Say "Stop-Service: forcing pid $id"
            Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        }
    }
    # Re-assert the marker: if the request never landed, the per-minute watchdog
    # would otherwise bring the service straight back.
    $null = Invoke-Helper 'mark-stop'
    $deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline -and (Test-ServiceAlive)) { Start-Sleep -Milliseconds 500 }
    Say "Stop-Service: done, alive=$(Test-ServiceAlive)"
}

function Restart-Service {
    Say 'Restart-Service: begin'
    if (Request-Service 'restart') {
        # The service spawns a detached helper that starts it again once the port is
        # free; just wait for it to answer instead of racing it.
        if (Wait-Service 45) {
            Say 'Restart-Service: done (handled by the detached helper)'
            return
        }
        Say 'Restart-Service: helper did not bring it back; falling back'
    }
    Stop-Service
    Start-Sleep -Seconds 2
    Start-Service | Out-Null
    Say 'Restart-Service: done (fallback path)'
}

function Open-Console {
    Say 'Open-Console: opening the dashboard'
    Start-Process $base
}

$notifyConfigPath = Join-Path $root 'config\notify.json'

function Get-NotifyEnabled {
    # Read the switch straight from config/notify.json. Spawning python on every
    # 1.5 s timer tick would be far too heavy, and the 'enabled' key lives only in
    # that file (credentials in .env are never merged back into it).
    try {
        $raw = [System.IO.File]::ReadAllText($notifyConfigPath, [System.Text.Encoding]::UTF8)
        $parsed = ConvertFrom-Json -InputObject $raw
        if ($null -ne $parsed.enabled) { return [bool]$parsed.enabled }
    } catch { }
    return $true   # fail open: a missing or damaged config keeps notifications on
}

function Set-NotifyEnabled([bool]$on) {
    $state = if ($on) { 'notify-on' } else { 'notify-off' }
    $answer = Invoke-Helper $state
    if ($answer -eq 'ok') {
        Say "Set-NotifyEnabled: $state accepted"
        return $true
    }
    Say "Set-NotifyEnabled: $state failed (answer: '$answer')"
    return $false
}

if ($SelfTest) {
    Say "SELFTEST start: $SelfTest"
    switch ($SelfTest) {
        'restart' { Restart-Service }
        'stop' { Stop-Service }
        'start' { Start-Service | Out-Null }
        'open' { Open-Console }
        'notify-on' { $null = Set-NotifyEnabled $true }
        'notify-off' { $null = Set-NotifyEnabled $false }
        default { Say "SELFTEST unknown action: $SelfTest" }
    }
    Say 'SELFTEST end'
    exit 0
}

# Single instance: a leftover process would show a second, dead icon in the tray.
$mine = $PID
$others = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like '*mumu_tray*' -and $_.ProcessId -ne $mine })
if ($others.Count -gt 0) {
    Say "another tray instance is already running (pid $($others[0].ProcessId)); exiting"
    exit 0
}

Set-Content -LiteralPath $trayLock -Value $PID -Encoding ascii
Say "tray started pid=$PID port=$port"

$started = [System.Drawing.Color]::FromArgb(62, 146, 114)
$halted = [System.Drawing.Color]::FromArgb(190, 80, 80)
$slots = @{ current = $null }

$menu = New-Object System.Windows.Forms.ContextMenuStrip
# Cast every localized string explicitly: a ConvertFrom-Json value is not a plain
# [string], and ToolStripItemCollection.Add then has two candidate overloads
# (string vs ToolStripItem), which PowerShell reports as an ambiguous-overload error.
$openItem = $menu.Items.Add([string]$T.menuOpen)
$restartItem = $menu.Items.Add([string]$T.menuRestart)
$stopItem = $menu.Items.Add([string]$T.menuStop)
# A checked menu entry is the notification master switch: tick = sending enabled.
$notifyItem = $menu.Items.Add([string]$T.menuNotify)
$menu.Items.Add('-') | Out-Null
$exitItem = $menu.Items.Add([string]$T.menuExit)

$script:busy = $false
$openItem.add_Click({ Say 'menu: open console'; Open-Console })
$restartItem.add_Click({
    Say 'menu: restart'
    $script:busy = $true
    try { Restart-Service } catch { Say "restart error: $($_.Exception.Message)" } finally { $script:busy = $false }
})
$stopItem.add_Click({
    Say 'menu: stop'
    $script:busy = $true
    try { Stop-Service } catch { Say "stop error: $($_.Exception.Message)" } finally { $script:busy = $false }
})
$notifyItem.add_Click({
    $target = -not $notifyItem.Checked
    Say "menu: notify toggle -> $(if ($target) {'on'} else {'off'})"
    $script:busy = $true
    try {
        if (Set-NotifyEnabled $target) {
            $notifyItem.Checked = $target
            $tray.BalloonTipTitle = 'AutoCheckin'
            $tray.BalloonTipText = [string]$(if ($target) { $T.balloonNotifyOn } else { $T.balloonNotifyOff })
            $tray.ShowBalloonTip(2500)
        }
    } catch { Say "notify toggle error: $($_.Exception.Message)" } finally { $script:busy = $false }
})
$exitItem.add_Click({ Say 'menu: exit'; $script:running = $false; [System.Windows.Forms.Application]::ExitThread() })

$tray = New-Object System.Windows.Forms.NotifyIcon
$tray.Icon = Set-Icon $slots $started
$tray.Text = [string]$T.trayTooltipStarting
$tray.ContextMenuStrip = $menu
$tray.Visible = $true
$notifyItem.Checked = Get-NotifyEnabled
$tray.add_MouseDoubleClick({ Say 'tray: double click'; Open-Console })
# The menu strip is now a realised window, so its handle can host the shell hook.
$taskbarHook = Watch-TaskbarRecreated $tray $menu

$script:running = $true
$script:lastState = ''
$stateTimer = New-Object System.Windows.Forms.Timer
$stateTimer.Interval = 1500
$stateTimer.Add_Tick({
    if ($script:busy -or -not $script:running) { return }
    try {
        $health = Get-Health
        if ($health) {
            $state = 'running'
            $switch = if ($health.enabled) { [string]$T.trayTooltipEnabled } else { [string]$T.trayTooltipDisabled }
            $tray.Text = [string]("$([string]$T.trayTooltipRunning) | $switch")
            $tray.Icon = Set-Icon $slots $started
        } elseif (Test-ServiceAlive) {
            $state = 'starting'
            $tray.Text = [string]$T.trayTooltipStarting
        } else {
            $state = 'stopped'
            $tray.Text = [string]$T.trayTooltipStopped
            $tray.Icon = Set-Icon $slots $halted
        }
        $stopItem.Enabled = $state -ne 'stopped'
        # Cheap re-sync (file read, no process spawn) in case the switch was
        # flipped from outside the tray, e.g. by a manual config edit.
        $notifyItem.Checked = Get-NotifyEnabled
        if ($state -ne $script:lastState -and $script:lastState -ne '') {
            $tray.BalloonTipTitle = 'AutoCheckin'
            $tray.BalloonTipText = [string]$(switch ($state) {
                'running' { [string]$T.balloonStarted }
                'stopped' { [string]$T.balloonStopped }
                default { [string]$T.balloonStarting }
            })
            $tray.ShowBalloonTip(3000)
        }
        $script:lastState = $state
    } catch {
        Say "state timer error: $($_.Exception.Message)"
    }
})
$stateTimer.Start()

Say "tray ready (state=$($script:lastState))"
# Application.Run is the real message loop, so clicks are delivered promptly.
$context = New-Object System.Windows.Forms.ApplicationContext
[System.Windows.Forms.Application]::Run($context)

$stateTimer.Stop()
$stateTimer.Dispose()
if ($taskbarHook) { try { $taskbarHook.ReleaseHandle() } catch { } }
$tray.Visible = $false
$tray.Dispose()
if ($slots.current) {
    $slots.current.Icon.Dispose()
    [AutoCheckin.Native]::DestroyIcon($slots.current.Handle) | Out-Null
}
Remove-Item -LiteralPath $trayLock -Force -ErrorAction SilentlyContinue
Say 'tray exited'
