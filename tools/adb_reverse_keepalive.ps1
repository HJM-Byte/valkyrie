$ErrorActionPreference = 'Continue'

# Keep the USB reverse tunnel alive while the phone is attached.
# adb reverse is lost when adb restarts or the phone reconnects; this loop
# restores it, so the Android app can keep using http://127.0.0.1:8766.

$adb = (Get-Command adb -ErrorAction SilentlyContinue).Source
if (-not $adb) {
    $sdk = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } elseif ($env:ANDROID_SDK_ROOT) { $env:ANDROID_SDK_ROOT } else { Join-Path $env:LOCALAPPDATA 'Android\Sdk' }
    $adb = Join-Path $sdk 'platform-tools\adb.exe'
}
$dataDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'data'
$log = Join-Path $dataDir 'adb_reverse.log'

function Write-Log([string]$message) {
    try {
        $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        Add-Content -LiteralPath $log -Value "[$stamp] $message" -Encoding UTF8
    } catch {
    }
}

$created = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\ValkyrieAdbReverseKeepalive', [ref]$created)
if (-not $created) { exit 0 }

Write-Log 'started'
try {
    while ($true) {
        try {
            if (Test-Path -LiteralPath $adb) {
                $devices = @(& $adb devices 2>$null)
                $online = $false
                foreach ($line in $devices) {
                    if ($line -match '^\s*\S+\s+device\s*$') {
                        $online = $true
                        break
                    }
                }

                if ($online) {
                    $reverse = @(& $adb reverse --list 2>$null)
                    $hasReverse = $false
                    foreach ($line in $reverse) {
                        if ($line -match 'tcp:8766\s+tcp:8766') {
                            $hasReverse = $true
                            break
                        }
                    }
                    if (-not $hasReverse) {
                        $out = @(& $adb reverse tcp:8766 tcp:8766 2>&1)
                        $text = ($out | ForEach-Object { "$_" }) -join ' '
                        Write-Log "reverse added: $text"
                    }
                }
            }
        } catch {
            Write-Log ('error: ' + $_.Exception.Message)
        }
        Start-Sleep -Seconds 10
    }
} finally {
    Write-Log 'stopped'
    $mutex.ReleaseMutex()
}
