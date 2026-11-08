# 创建本地 Python 环境并安装依赖。
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
#   powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Voice
#   powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Dev
param(
    [switch]$Voice,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if ($Root -match '[^\x00-\x7F]') {
    Write-Warning "仓库路径包含非 ASCII 字符。pip、OpenCV 或 onnxruntime 可能报路径错误；建议换到 C:\valkyrie 这类路径。"
}

$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) {
    $PyLauncher = (Get-Command py -ErrorAction SilentlyContinue).Source
    if ($PyLauncher) {
        $Python = $PyLauncher
    }
}
if (-not $Python) {
    throw "找不到 Python。请先安装 Python 3.11+，并勾选 Add python.exe to PATH。"
}

$Venv = Join-Path $Root ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "创建虚拟环境: $Venv"
    if ($Python -like "*py.exe") {
        & $Python -3 -m venv $Venv
    } else {
        & $Python -m venv $Venv
    }
}

if (-not (Test-Path $VenvPython)) {
    throw "虚拟环境创建失败: $VenvPython"
}

Write-Host "升级 pip"
& $VenvPython -m pip install --upgrade pip

$Req = if ($Dev) { "requirements-dev.txt" } elseif ($Voice) { "requirements-voice.txt" } else { "requirements.txt" }
Write-Host "安装 $Req"
& $VenvPython -m pip install -r (Join-Path $Root $Req)

$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
    Write-Host "已生成 .env；请填入 DASHSCOPE_API_KEY 后启动。"
} else {
    Write-Host ".env 已存在，未覆盖。"
}

Write-Host ""
Write-Host "下一步："
Write-Host "  1. 编辑 .env，至少填 DASHSCOPE_API_KEY"
Write-Host "  2. .\desktop.cmd --demo   先看示例数据"
Write-Host "  3. .\desktop.cmd          使用真实数据"
