# Tray indicator for bot + Notion sync
# Shows green/yellow/red icon near the clock

# Bot name — fetched from Telegram API at startup
$BotToken = ""
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^BOT_TOKEN=(.+)$') { $BotToken = $Matches[1] }
    }
}
$BotName = "Bot"
if ($BotToken) {
    try {
        $me = Invoke-RestMethod "https://api.telegram.org/bot$BotToken/getMe"
        if ($me.ok) { $BotName = $me.result.first_name }
    } catch {}
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# --- Single instance check ---
$mutexName = "Global\${BotName}TrayIndicator"
$mutex = New-Object System.Threading.Mutex($false, $mutexName)
if (-not $mutex.WaitOne(0, $false)) {
    # Another tray instance is already running
    exit
}

$BOT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$PROJECT_DIR = Split-Path -Parent $BOT_DIR
$SYNC_PROGRESS_FILE = Join-Path $PROJECT_DIR "notion\.sync_progress.json"
$NOTION_STATE_FILE = Join-Path $PROJECT_DIR "notion\.notion_state.json"
$POLL_INTERVAL = 5000  # ms (5 sec — fast enough to catch sync start/end)
$CHANGE_CHECK_INTERVAL = 10800000  # ms (3 hours — periodic change detection)

# --- Icon generation ---
function New-CircleIcon([System.Drawing.Color]$color) {
    $bmp = New-Object System.Drawing.Bitmap(16, 16)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.Clear([System.Drawing.Color]::Transparent)
    $brush = New-Object System.Drawing.SolidBrush($color)
    $g.FillEllipse($brush, 1, 1, 14, 14)
    # Border
    $pen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(80, 0, 0, 0), 1)
    $g.DrawEllipse($pen, 1, 1, 14, 14)
    $g.Dispose()
    $brush.Dispose()
    $pen.Dispose()
    $icon = [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
    return $icon
}

$iconGreen  = New-CircleIcon ([System.Drawing.Color]::FromArgb(76, 175, 80))
$iconRed    = New-CircleIcon ([System.Drawing.Color]::FromArgb(244, 67, 54))
$iconYellow = New-CircleIcon ([System.Drawing.Color]::FromArgb(255, 193, 7))

# --- Bot status check ---
function Get-BotStatus {
    try {
        $pythonProcs = Get-Process -Name "python" -ErrorAction SilentlyContinue
        foreach ($proc in $pythonProcs) {
            try {
                $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.Id)" -ErrorAction SilentlyContinue).CommandLine
                if ($cmd -and $cmd -match "bot\.py") {
                    return @{ Running = $true; PID = $proc.Id }
                }
            } catch {}
        }
    } catch {}
    return @{ Running = $false; PID = $null }
}

# --- Sync status check ---
function Get-SyncStatus {
    # Returns: Idle / Running / Done / Error
    if (-not (Test-Path $SYNC_PROGRESS_FILE)) {
        return @{ State = "Idle"; Detail = "" }
    }
    try {
        $json = Get-Content $SYNC_PROGRESS_FILE -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return @{ State = "Idle"; Detail = "" }
    }

    # Check if sync process is actually running
    $syncRunning = $false
    try {
        $pythonProcs = Get-Process -Name "python" -ErrorAction SilentlyContinue
        foreach ($proc in $pythonProcs) {
            try {
                $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.Id)" -ErrorAction SilentlyContinue).CommandLine
                if ($cmd -and $cmd -match "update_notion") {
                    $syncRunning = $true
                    break
                }
            } catch {}
        }
    } catch {}

    if ($syncRunning) {
        $current = if ($json.current) { $json.current } else { 0 }
        $total = if ($json.total) { $json.total } else { 0 }
        $card = if ($json.card) { $json.card } else { "" }
        if ($total -gt 0) {
            return @{ State = "Running"; Detail = "$current/$total $card" }
        }
        return @{ State = "Running"; Detail = "starting..." }
    }

    # Process not running — check last result
    if ($json.error) {
        return @{ State = "Error"; Detail = "$($json.error)" }
    }
    if ($json.done -eq $true) {
        return @{ State = "Done"; Detail = "" }
    }
    return @{ State = "Idle"; Detail = "" }
}

# --- Bot start/stop ---
function Start-Bot {
    $status = Get-BotStatus
    if ($status.Running) {
        [System.Windows.Forms.MessageBox]::Show(
            "Bot already running (PID $($status.PID))",
            $BotName, 'OK', 'Information')
        return
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "python"
    $psi.Arguments = "-u bot.py"
    $psi.WorkingDirectory = $BOT_DIR
    $psi.WindowStyle = 'Hidden'
    $psi.CreateNoWindow = $true
    $psi.UseShellExecute = $false
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
        $script:lastNotifiedState = $null  # force re-check
        # Лог для отладки
        Start-Sleep -Seconds 3
        if ($proc.HasExited) {
            $logFile = Join-Path $BOT_DIR "_tray_crash.log"
            "$(Get-Date) | PID=$($proc.Id) exit=$($proc.ExitCode) dir=$BOT_DIR" | Out-File $logFile -Append
            [System.Windows.Forms.MessageBox]::Show(
                "Bot crashed (exit $($proc.ExitCode)). Log: $logFile",
                $BotName, 'OK', 'Error')
        }
    } catch {
        [System.Windows.Forms.MessageBox]::Show(
            "Failed to start: $_",
            $BotName, 'OK', 'Error')
    }
}

function Stop-Bot {
    $status = Get-BotStatus
    if (-not $status.Running) {
        [System.Windows.Forms.MessageBox]::Show(
            "Bot is not running",
            $BotName, 'OK', 'Information')
        return
    }
    try {
        Start-Process -FilePath "taskkill" -ArgumentList "/F /PID $($status.PID)" -NoNewWindow -Wait
        $script:lastNotifiedState = $null
    } catch {
        [System.Windows.Forms.MessageBox]::Show(
            "Failed to stop: $_",
            $BotName, 'OK', 'Error')
    }
}

# --- Check for changed files since last sync ---
function Get-ChangedFiles {
    if (-not (Test-Path $NOTION_STATE_FILE)) { return @("state.json not found") }
    $stateTime = (Get-Item $NOTION_STATE_FILE).LastWriteTime
    $changed = @()
    $watchDirs = @(
        (Join-Path $PROJECT_DIR "karta-idej"),
        (Join-Path $PROJECT_DIR "realizaciya"),
        (Join-Path $PROJECT_DIR "project")
    )
    $stateFile = Join-Path $PROJECT_DIR "_sostoyaniye.md"
    if ((Test-Path $stateFile) -and (Get-Item $stateFile).LastWriteTime -gt $stateTime) {
        $changed += "_sostoyaniye.md"
    }
    foreach ($dir in $watchDirs) {
        if (Test-Path $dir) {
            Get-ChildItem -Path $dir -Filter "*.md" -Recurse | Where-Object {
                $_.LastWriteTime -gt $stateTime
            } | ForEach-Object {
                $changed += $_.FullName.Replace("$PROJECT_DIR\", "")
            }
        }
    }
    return $changed
}

# --- Sync Notion ---
function Start-Sync {
    $syncStatus = Get-SyncStatus
    if ($syncStatus.State -eq "Running") {
        [System.Windows.Forms.MessageBox]::Show(
            "Sync already running: $($syncStatus.Detail)",
            $BotName, 'OK', 'Information')
        return
    }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "python"
    $psi.Arguments = "-u update_notion.py"
    $psi.WorkingDirectory = Join-Path $PROJECT_DIR "notion"
    $psi.WindowStyle = 'Hidden'
    $psi.CreateNoWindow = $true
    $psi.UseShellExecute = $false
    try {
        [System.Diagnostics.Process]::Start($psi) | Out-Null
        $script:lastSyncState = $null  # force re-check
    } catch {
        [System.Windows.Forms.MessageBox]::Show(
            "Failed to start sync: $_",
            $BotName, 'OK', 'Error')
    }
}

# --- Tray setup ---
$notifyIcon = New-Object System.Windows.Forms.NotifyIcon
$notifyIcon.Icon = $iconRed
$notifyIcon.Text = "${BotName}: checking..."
$notifyIcon.Visible = $true

# Context menu
$menu = New-Object System.Windows.Forms.ContextMenuStrip

$menuStart = $menu.Items.Add("Start bot")
$menuStart.Add_Click({ Start-Bot })

$menuStop = $menu.Items.Add("Stop bot")
$menuStop.Add_Click({ Stop-Bot })

$menu.Items.Add("-")  # separator

$menuSync = $menu.Items.Add("Sync Notion")
$menuSync.Add_Click({ Start-Sync })

$menuCheck = $menu.Items.Add("Check changes")
$menuCheck.Add_Click({
    $changed = Get-ChangedFiles
    if ($changed.Count -eq 0) {
        $notifyIcon.BalloonTipTitle = $BotName
        $notifyIcon.BalloonTipText = "No changes since last sync"
        $notifyIcon.BalloonTipIcon = 'Info'
    } else {
        $list = ($changed | Select-Object -First 5) -join "`n"
        if ($changed.Count -gt 5) { $list += "`n...and $($changed.Count - 5) more" }
        $notifyIcon.BalloonTipTitle = "Changed: $($changed.Count) files"
        $notifyIcon.BalloonTipText = $list
        $notifyIcon.BalloonTipIcon = 'Warning'
    }
    $notifyIcon.ShowBalloonTip(5000)
})

$menu.Items.Add("-")  # separator

$menuExit = $menu.Items.Add("Exit (stop bot + indicator)")
$menuExit.Add_Click({
    # Останавливаем бота перед выходом
    $status = Get-BotStatus
    if ($status.Running) {
        try {
            Start-Process -FilePath "taskkill" -ArgumentList "/F /PID $($status.PID)" -NoNewWindow -Wait
        } catch {}
        # Уведомление об остановке
        $notifyIcon.BalloonTipTitle = $BotName
        $notifyIcon.BalloonTipText = "Bot stopped. Exiting..."
        $notifyIcon.BalloonTipIcon = 'Info'
        $notifyIcon.ShowBalloonTip(2000)
        Start-Sleep -Seconds 2
    }
    try { $timer.Stop() } catch {}
    try { $changeTimer.Stop() } catch {}
    try {
        $notifyIcon.Visible = $false
        $notifyIcon.Dispose()
    } catch {}
    try {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    } catch {}
    [System.Windows.Forms.Application]::Exit()
})

$notifyIcon.ContextMenuStrip = $menu

# Double-click = toggle bot
$notifyIcon.Add_DoubleClick({
    $status = Get-BotStatus
    if ($status.Running) { Stop-Bot } else { Start-Bot }
})

# --- Update tray icon based on bot + sync status ---
$script:lastNotifiedState = $null
$script:lastSyncState = $null

function Update-TrayStatus {
    $botStatus = Get-BotStatus
    $syncStatus = Get-SyncStatus

    # --- Icon priority: yellow (syncing) > green (bot running) > red (stopped) ---
    if ($syncStatus.State -eq "Running") {
        $notifyIcon.Icon = $iconYellow
        $notifyIcon.Text = "${BotName}: syncing $($syncStatus.Detail)"
        $menuSync.Enabled = $false
    } else {
        $menuSync.Enabled = $true
        if ($botStatus.Running) {
            $notifyIcon.Icon = $iconGreen
            $notifyIcon.Text = "${BotName}: bot running (PID $($botStatus.PID))"
        } else {
            $notifyIcon.Icon = $iconRed
            $notifyIcon.Text = "${BotName}: bot stopped"
        }
    }

    # --- Bot state notifications ---
    $menuStart.Enabled = -not $botStatus.Running
    $menuStop.Enabled = $botStatus.Running

    $botState = if ($botStatus.Running) { "running" } else { "stopped" }
    if ($script:lastNotifiedState -ne $botState) {
        if ($botState -eq "running") {
            $notifyIcon.BalloonTipTitle = $BotName
            $notifyIcon.BalloonTipText = "Bot is running"
            $notifyIcon.BalloonTipIcon = 'Info'
            $notifyIcon.ShowBalloonTip(3000)
        } else {
            $notifyIcon.BalloonTipTitle = $BotName
            $notifyIcon.BalloonTipText = "Bot is stopped"
            $notifyIcon.BalloonTipIcon = 'Warning'
            $notifyIcon.ShowBalloonTip(3000)
        }
        $script:lastNotifiedState = $botState
    }

    # --- Sync state notifications ---
    if ($script:lastSyncState -ne $syncStatus.State) {
        if ($syncStatus.State -eq "Running" -and $script:lastSyncState -ne "Running") {
            $notifyIcon.BalloonTipTitle = $BotName
            $notifyIcon.BalloonTipText = "Notion sync started..."
            $notifyIcon.BalloonTipIcon = 'Info'
            $notifyIcon.ShowBalloonTip(2000)
        }
        elseif ($syncStatus.State -eq "Done" -and $script:lastSyncState -eq "Running") {
            $notifyIcon.BalloonTipTitle = $BotName
            $notifyIcon.BalloonTipText = "Notion sync completed"
            $notifyIcon.BalloonTipIcon = 'Info'
            $notifyIcon.ShowBalloonTip(3000)
        }
        elseif ($syncStatus.State -eq "Error" -and $script:lastSyncState -eq "Running") {
            $notifyIcon.BalloonTipTitle = $BotName
            $notifyIcon.BalloonTipText = "Notion sync error: $($syncStatus.Detail)"
            $notifyIcon.BalloonTipIcon = 'Error'
            $notifyIcon.ShowBalloonTip(5000)
        }
        $script:lastSyncState = $syncStatus.State
    }
}

# Auto-start bot if not running
$initStatus = Get-BotStatus
if (-not $initStatus.Running) {
    Start-Bot
}

# Initial status update
Update-TrayStatus

# --- Fast polling timer (bot + sync status) ---
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $POLL_INTERVAL
$timer.Add_Tick({ Update-TrayStatus })
$timer.Start()

# --- Slow timer: periodic change detection (every 3 hours) ---
$changeTimer = New-Object System.Windows.Forms.Timer
$changeTimer.Interval = $CHANGE_CHECK_INTERVAL
$changeTimer.Add_Tick({
    $syncStatus = Get-SyncStatus
    if ($syncStatus.State -eq "Running") { return }
    $changed = Get-ChangedFiles
    if ($changed.Count -gt 0) {
        $notifyIcon.BalloonTipTitle = $BotName
        $notifyIcon.BalloonTipText = "$($changed.Count) files changed since last sync"
        $notifyIcon.BalloonTipIcon = 'Warning'
        $notifyIcon.ShowBalloonTip(3000)
    }
})
$changeTimer.Start()

[System.Windows.Forms.Application]::Run()
