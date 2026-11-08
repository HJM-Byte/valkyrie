param(
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = 'Stop'
$robot = Split-Path -Parent $PSScriptRoot
$startup = [Environment]::GetFolderPath('Startup')
$link = Join-Path $startup 'Valkyrie Web.lnk'
$loop = Join-Path $PSScriptRoot 'server_watchdog_loop.ps1'
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function Get-LoopProcesses {
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*server_watchdog_loop.ps1*' }
}

function Show-Status {
    Write-Host ("startup link : {0} ({1})" -f $link, (Test-Path -LiteralPath $link))
    $procs = @(Get-LoopProcesses)
    Write-Host ("watchdog loop: {0}" -f $procs.Count)
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8766/api/health' -TimeoutSec 3
        Write-Host ("health       : {0} {1}" -f $r.StatusCode, $r.Content)
    } catch {
        Write-Host ("health       : DOWN ({0})" -f $_.Exception.Message)
    }
}

if ($Status) {
    Show-Status
    exit 0
}

if ($Uninstall) {
    if (Test-Path -LiteralPath $link) {
        Remove-Item -LiteralPath $link -Force
        Write-Host "removed startup link"
    }
    foreach ($p in @(Get-LoopProcesses)) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { }
    }
    Write-Host "watchdog loop stopped"
    exit 0
}

if (-not (Test-Path -LiteralPath $loop)) {
    Write-Host "missing: $loop"
    exit 1
}

$ws = New-Object -ComObject WScript.Shell
$shortcut = $ws.CreateShortcut($link)
$shortcut.TargetPath = $ps
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$loop`""
$shortcut.WorkingDirectory = $robot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Valkyrie local web service watchdog'
$shortcut.Save()

$running = @(Get-LoopProcesses)
if ($running.Count -eq 0) {
    Start-Process -FilePath $ps `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', $loop) `
        -WorkingDirectory $robot -WindowStyle Hidden
    Start-Sleep -Seconds 1
}

Show-Status
Write-Host "installed. The web service now starts with Windows and is checked every 60 seconds."
exit 0
