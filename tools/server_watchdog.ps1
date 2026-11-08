$ErrorActionPreference = 'Stop'
$robot = Split-Path -Parent $PSScriptRoot
$data = Join-Path $robot 'data'
$health = 'http://127.0.0.1:8766/api/health'
$log = Join-Path $data 'watchdog.log'
$out = Join-Path $data 'pocket_server.log'
$err = Join-Path $data 'pocket_server.err.log'

function Note([string]$msg) {
    $line = ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg)
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

function Backup-IfDue {
    $stamp = Join-Path $data 'web_backup_last.json'
    if (Test-Path -LiteralPath $stamp) {
        try {
            $s = Get-Content -LiteralPath $stamp -Raw | ConvertFrom-Json
            if ([string]$s.date -eq (Get-Date -Format 'yyyy-MM-dd')) { return }
        } catch { }
    }
    $py = Join-Path $robot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $py)) {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $py = $cmd.Source }
    }
    $backup = Join-Path $robot 'tools\web_backup.py'
    if ((Test-Path -LiteralPath $py) -and (Test-Path -LiteralPath $backup)) {
        try {
            & $py $backup --due 2>&1 | Add-Content -LiteralPath (Join-Path $data 'web_backup.log') -Encoding UTF8
        } catch {
            Note ('backup failed: ' + $_.Exception.Message)
        }
    }
}

try { Backup-IfDue } catch { Note ('backup scheduler failed: ' + $_.Exception.Message) }

try {
    $r = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 3
    if ($r.StatusCode -eq 200) { exit 0 }
} catch { }

$listen = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
if ($listen) {
    Note 'port 8766 listening but health check failed; skip duplicate start'
    exit 0
}

$py = Join-Path $robot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $py)) {
    $cmd = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not (Test-Path -LiteralPath $py)) {
    Note 'pythonw.exe not found; not started'
    exit 1
}

try {
    Start-Process -FilePath $py `
        -ArgumentList @('-m', 'app.pocket_server', '--lan', '--chat',
                        '--response-gate', 'enforced', '--social-silence', 'on') `
        -WorkingDirectory $robot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $out `
        -RedirectStandardError $err
    Note 'service not running; started pythonw'
} catch {
    Note ('start failed: ' + $_.Exception.Message)
    exit 1
}

Start-Sleep -Seconds 3
try {
    $r = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 3
    if ($r.StatusCode -eq 200) {
        Note 'started; health=200'
    } else {
        Note ('started; health=' + $r.StatusCode)
    }
} catch {
    Note ('health failed after start: ' + $_.Exception.Message)
    exit 1
}
