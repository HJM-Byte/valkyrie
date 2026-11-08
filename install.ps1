<#
瓦尔基里 Windows 一键安装器。

双击 install.cmd 即可；也可以直接运行：
  powershell -ExecutionPolicy Bypass -File .\install.ps1
  powershell -ExecutionPolicy Bypass -File .\install.ps1 -Voice

它做这些事：
  1. 找到 Python 3.11+（没有时可用 winget 安装 3.13）
  2. 创建 .venv，安装 requirements.txt 或 requirements-voice.txt
  3. 生成 .env 模板，交互式写入 DASHSCOPE_API_KEY（不回显、不写日志）
  4. 生成局域网访问口令，写入 .env 和 data\pocket_token.txt
  5. 创建桌面 / 开始菜单快捷方式
  6. 做一次语法与核心依赖检查

不会下载模型、不会写入任何真实对话数据。模型下载仍按 README 手动执行。
#>

[CmdletBinding()]
param(
    [switch]$Voice,
    [switch]$Models,
    [switch]$NoShortcuts,
    [switch]$NoLaunch,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root
$LogPath = Join-Path $Root 'install.log'
$Venv = Join-Path $Root '.venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$EnvPath = Join-Path $Root '.env'
$DataDir = Join-Path $Root 'data'

function Write-Log([string]$Message) {
    try {
        $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    } catch { }
}

function Step([string]$Message) {
    Write-Host ''
    Write-Host ('==> ' + $Message) -ForegroundColor Cyan
    Write-Log $Message
}

function Warn([string]$Message) {
    Write-Host ('[!] ' + $Message) -ForegroundColor Yellow
    Write-Log ('WARN ' + $Message)
}

function Fail([string]$Message) {
    Write-Host ''
    Write-Host ('[x] ' + $Message) -ForegroundColor Red
    Write-Log ('ERROR ' + $Message)
    exit 1
}

function Get-EnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    } catch {
        return ''
    }
    $pattern = '(?m)^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*)$'
    $m = [regex]::Match($text, $pattern)
    if (-not $m.Success) { return '' }
    return $m.Groups[1].Value.Trim().Trim([char]34).Trim([char]39)
}

function Set-EnvValue {
    param([string]$Path, [string]$Key, [string]$Value)
    $text = ''
    if (Test-Path -LiteralPath $Path) {
        $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
    }
    $pattern = '(?m)^\s*' + [regex]::Escape($Key) + '\s*=.*$'
    $line = $Key + '=' + $Value
    if ([regex]::IsMatch($text, $pattern)) {
        $text = [regex]::Replace($text, $pattern, $line)
    } else {
        if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) {
            $text += "`r`n"
        }
        $text += $line + "`r`n"
    }
    $utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($Path, $text, $utf8)
}

function Get-PythonInfo {
    $candidates = New-Object System.Collections.ArrayList
    $pythonCmd = @(Get-Command python -CommandType Application -ErrorAction SilentlyContinue) |
        Select-Object -First 1
    if ($pythonCmd) {
        [void]$candidates.Add([pscustomobject]@{ Exe = $pythonCmd.Source; Args = @() })
    }
    $pyCmd = @(Get-Command py -CommandType Application -ErrorAction SilentlyContinue) |
        Select-Object -First 1
    if ($pyCmd) {
        foreach ($version in @('3.13', '3.12', '3.11')) {
            [void]$candidates.Add([pscustomobject]@{
                Exe = $pyCmd.Source; Args = @('-' + $version)
            })
        }
    }
    $known = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'),
        'C:\Program Files\Python313\python.exe',
        'C:\Program Files\Python312\python.exe',
        'C:\Program Files\Python311\python.exe',
        'C:\Program Files\Python314\python.exe'
    )
    foreach ($path in $known) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            [void]$candidates.Add([pscustomobject]@{ Exe = $path; Args = @() })
        }
    }

    $code = 'import sys,struct; print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor)+chr(46)+str(sys.version_info.micro)+chr(124)+str(struct.calcsize(chr(80))*8))'
    foreach ($candidate in $candidates) {
        try {
            $callArgs = @($candidate.Args) + @('-c', $code)
            $out = & $candidate.Exe @callArgs 2>$null
            if ($LASTEXITCODE -ne 0) { continue }
            $line = (($out | Select-Object -Last 1) -as [string]).Trim()
            $parts = $line.Split('|')
            if ($parts.Count -ne 2) { continue }
            $version = [version]$parts[0]
            if ($version.Major -ne 3 -or $version.Minor -lt 11) { continue }
            return [pscustomobject]@{
                Exe = $candidate.Exe
                Args = $candidate.Args
                Version = $version
                Bits = [int]$parts[1]
            }
        } catch { }
    }
    return $null
}

function New-Shortcut {
    param([string]$Path, [string]$Target, [string]$Description)
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $Target
    $shortcut.WorkingDirectory = $Root
    $shortcut.Description = $Description
    $shortcut.WindowStyle = 1
    $shortcut.Save()
}

try {
    Write-Host ''
    Write-Host '  Valkyrie / 瓦尔基里 - Windows installer' -ForegroundColor White
    Write-Host '  只安装代码和依赖；不碰你的对话数据、模型和密钥。' -ForegroundColor DarkGray
    Write-Log 'installer started'

    if ($Root -match '[^\x00-\x7F]') {
        Warn '安装路径含中文或空格。若 pip / OpenCV / onnxruntime 报路径错误，请把仓库移到 C:\valkyrie 这类纯 ASCII 路径后重试。'
    }

    Step '检查 Python'
    $python = Get-PythonInfo
    if (-not $python) {
        if ($DryRun) {
            Fail '没有找到 Python 3.11+。DryRun 不安装，请先安装 Python。'
        }
        Write-Host '没有找到 Python 3.11+。' -ForegroundColor Yellow
        $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue
        if ($winget) {
            $answer = Read-Host '用 winget 安装 Python 3.13？[Y/n]'
            if ($answer -notmatch '^(n|no|否)$') {
                & winget install --id Python.Python.3.13 -e --source winget --accept-source-agreements --accept-package-agreements --silent
                $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
            }
        }
        $python = Get-PythonInfo
        if (-not $python) {
            Start-Process 'https://www.python.org/downloads/windows/'
            Fail '没有可用的 Python。请在安装 Python 3.11+ 时勾选 Add python.exe to PATH，装完重新双击 install.cmd。'
        }
    }

    if ($python.Bits -ne 64) {
        Fail ('检测到 ' + $python.Bits + ' 位 Python。请安装 64 位 Python 3.11+。')
    }
    Write-Host ('Python ' + $python.Version + ' (' + $python.Exe + ')') -ForegroundColor Green
    Write-Log ('python ' + $python.Version + ' ' + $python.Exe)

    if ($python.Version.Minor -gt 13) {
        Warn 'Python 3.14 可能还没有全部轮子；若依赖安装失败，请换 Python 3.13。'
    }

    Step '创建虚拟环境'
    if (Test-Path -LiteralPath $VenvPython) {
        Write-Host ('.venv 已存在，直接复用: ' + $Venv)
    } else {
        if (Test-Path -LiteralPath $Venv) {
            Fail '.venv 已存在但不完整。请删除 .venv 后重试（不会动 data/）。'
        }
        $venvArgs = @($python.Args) + @('-m', 'venv', $Venv)
        & $python.Exe @venvArgs
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
            Fail '虚拟环境创建失败。请确认 Python 安装完整，并检查安装路径权限。'
        }
    }

    if ($DryRun) {
        Write-Host ''
        Write-Host 'DryRun 完成：只检查环境，没有创建文件、没有安装依赖、没有改 .env。' -ForegroundColor Yellow
        exit 0
    }

    Step '安装依赖'
    $requirements = if ($Voice) { 'requirements-voice.txt' } else { 'requirements.txt' }
    $requirementsPath = Join-Path $Root $requirements
    if (-not (Test-Path -LiteralPath $requirementsPath)) {
        Fail ('缺少依赖清单: ' + $requirementsPath)
    }
    & $VenvPython -m pip install --upgrade pip --disable-pip-version-check --no-input
    if ($LASTEXITCODE -ne 0) {
        Warn 'pip 升级失败，继续尝试安装项目依赖。'
    }
    Write-Host ('安装 ' + $requirements + '（首次可能需要几分钟）...') -ForegroundColor DarkGray
    & $VenvPython -m pip install --disable-pip-version-check --no-input -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
        Fail '依赖安装失败。请检查网络或代理；也可以运行 bootstrap.ps1 查看完整输出。'
    }

    Step '配置 .env'
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        if (Test-Path -LiteralPath (Join-Path $Root '.env.example')) {
            Copy-Item -LiteralPath (Join-Path $Root '.env.example') -Destination $EnvPath
            Write-Host '已从 .env.example 生成 .env。'
        } else {
            Fail '缺少 .env.example，无法生成配置模板。'
        }
    } else {
        Write-Host '.env 已存在，保留原内容。'
    }

    $dashKey = Get-EnvValue -Path $EnvPath -Key 'DASHSCOPE_API_KEY'
    if (-not $dashKey -and $env:DASHSCOPE_API_KEY) {
        $dashKey = $env:DASHSCOPE_API_KEY
        Set-EnvValue -Path $EnvPath -Key 'DASHSCOPE_API_KEY' -Value $dashKey
    }
    if (-not $dashKey) {
        Write-Host ''
        Write-Host '需要一个 OpenAI 兼容的 LLM API Key。' -ForegroundColor Yellow
        Write-Host '直接回车可以跳过，之后编辑 .env 再填。' -ForegroundColor DarkGray
        $secure = Read-Host 'DASHSCOPE_API_KEY（输入不回显）' -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        if ($plain -and $plain.Trim()) {
            Set-EnvValue -Path $EnvPath -Key 'DASHSCOPE_API_KEY' -Value $plain.Trim()
            $env:DASHSCOPE_API_KEY = $plain.Trim()
            Write-Host '已写入 .env（不回显）。' -ForegroundColor Green
        } else {
            Warn '没有填写 Key；界面能打开，但对话会提示未配置。'
        }
        Remove-Variable plain -ErrorAction SilentlyContinue
        Remove-Variable secure -ErrorAction SilentlyContinue
    } else {
        Write-Host '已检测到 DASHSCOPE_API_KEY，跳过输入。' -ForegroundColor Green
    }

    Step '生成局域网访问口令'
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $Root 'models') | Out-Null
    $tokenFile = Join-Path $DataDir 'pocket_token.txt'
    $token = Get-EnvValue -Path $EnvPath -Key 'VALKYRIE_APP_TOKEN'
    if (-not $token -and (Test-Path -LiteralPath $tokenFile)) {
        try {
            $token = [System.IO.File]::ReadAllText($tokenFile, [System.Text.Encoding]::UTF8).Trim()
        } catch { }
    }
    if (-not $token) {
        $token = [guid]::NewGuid().ToString('N').Substring(0, 16)
    }
    Set-EnvValue -Path $EnvPath -Key 'VALKYRIE_APP_TOKEN' -Value $token
    $utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText($tokenFile, $token, $utf8)
    Write-Host '口令已保存到 .env 与 data\pocket_token.txt（不打印）。' -ForegroundColor Green

    if ($Models) {
        Step '下载可选模型'
        & $VenvPython -m pip install --disable-pip-version-check --no-input huggingface-hub
        if ($LASTEXITCODE -ne 0) { Warn 'huggingface-hub 安装失败，跳过模型下载。' }
        else {
            & $VenvPython (Join-Path $Root 'download_faces.py')
            if ($LASTEXITCODE -ne 0) { Warn '人脸模型下载失败，可稍后重试。' }
            & $VenvPython (Join-Path $Root 'download_whisper.py')
            if ($LASTEXITCODE -ne 0) { Warn '语音模型下载失败，可稍后重试。' }
        }
    }

    Step '检查安装'
    & $VenvPython -m compileall -q (Join-Path $Root 'app') (Join-Path $Root 'tools')
    if ($LASTEXITCODE -ne 0) {
        Fail '代码语法检查失败。'
    }
    & $VenvPython -c "import aiohttp, numpy, PIL, requests, yaml; import app.config; print('core imports ok')"
    if ($LASTEXITCODE -ne 0) {
        Fail '核心依赖导入失败。'
    }
    if ($Voice) {
        & $VenvPython -c "import cv2, sounddevice, soundfile, pygame, edge_tts; print('voice imports ok')"
        if ($LASTEXITCODE -ne 0) {
            Warn '语音依赖导入失败；核心功能仍可用，按 README 的语音部分排查。'
        }
    }

    if (-not $NoShortcuts) {
        Step '创建快捷方式'
        try {
            $desktop = [Environment]::GetFolderPath('Desktop')
            $programs = [Environment]::GetFolderPath('Programs')
            $startDir = Join-Path $programs 'Valkyrie'
            New-Item -ItemType Directory -Force -Path $startDir | Out-Null
            if ($desktop) {
                New-Shortcut -Path (Join-Path $desktop '瓦尔基里.lnk') `
                    -Target (Join-Path $Root 'start.cmd') -Description 'Valkyrie'
            }
            New-Shortcut -Path (Join-Path $startDir '瓦尔基里.lnk') `
                -Target (Join-Path $Root 'start.cmd') -Description 'Valkyrie'
            New-Shortcut -Path (Join-Path $startDir '网页端.lnk') `
                -Target (Join-Path $Root 'web.cmd') -Description 'Valkyrie Web'
            Write-Host '已创建桌面和开始菜单快捷方式。' -ForegroundColor Green
        } catch {
            Warn ('快捷方式创建失败：' + $_.Exception.Message)
        }
    }

    Write-Host ''
    Write-Host '安装完成。' -ForegroundColor Green
    Write-Host '  网页端    http://127.0.0.1:8766/'
    Write-Host '  启动菜单  start.cmd'
    Write-Host '  只跑网页  web.cmd'
    Write-Host '  语音链路  install.cmd -Voice 后运行 run.cmd'
    Write-Host ''

    if (-not $NoLaunch) {
        $answer = Read-Host '现在启动网页端并打开浏览器？[Y/n]'
        if ($answer -notmatch '^(n|no|否)$') {
            & (Join-Path $Root 'web.cmd')
        }
    }
    Write-Log 'installer finished'
} catch {
    Write-Host ''
    Write-Host ('[x] 安装中断：' + $_.Exception.Message) -ForegroundColor Red
    Write-Log ('FATAL ' + $_.Exception.Message)
    exit 1
}
