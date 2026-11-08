$ErrorActionPreference = 'Stop'
$created = $false
$mutex = [System.Threading.Mutex]::new($true, 'Local\ValkyrieServerWatchdog', [ref]$created)
if (-not $created) { exit 0 }

$oneShot = Join-Path $PSScriptRoot 'server_watchdog.ps1'
$keepalive = Join-Path $PSScriptRoot 'adb_reverse_keepalive.ps1'
$ps = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

function Ensure-Keepalive {
    try {
        $running = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like '*adb_reverse_keepalive.ps1*' }
        if (-not $running -and (Test-Path -LiteralPath $keepalive)) {
            Start-Process -FilePath $ps -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$keepalive`"" -WindowStyle Hidden
        }
    } catch { }
}

try {
    while ($true) {
        Ensure-Keepalive
        try {
            & $ps -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File $oneShot | Out-Null
        } catch { }
        Start-Sleep -Seconds 60
    }
} finally {
    $mutex.ReleaseMutex()
}
