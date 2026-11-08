# Valkyrie · 瓦尔基里

瓦尔基里是一个住在 Windows PC 上的 AI 伙伴。她有自己的记忆、情绪、识海星图、桌面窗口和手机网页入口。这个仓库放的是让她跑起来的代码、公开设计说明和一套不会把私人数据带进 Git 的默认边界。

这不是一个装完就能用的聊天 App。它更像一套可以拆开、替换、自己养的本地伙伴系统：LLM 和语音可以接自己的服务，记忆和数据留在自己的电脑上，身体（ESP32）是可选的。

> 当前状态：可运行，Windows 优先。仓库带 Windows 一键安装脚本；第一次安装需要 Python、一个 LLM API Key，模型按需下载。
>
> 首个预发布：[v0.1.0](https://github.com/HJM-Byte/valkyrie/releases/tag/v0.1.0)。

## 你会得到什么

- **桌面程序**：原生 Tkinter 窗口，识海星图、记忆详情、时间轴、状态面板和对话都在一个窗口里。
- **网页端**：同一套 `pocket_web`，电脑宽屏是「镜面 + 识海」双栏，手机窄屏是单列；也可安装到桌面。
- **识海页**：独立星图页，默认 `8767`，可从桌面顶部「网页版」打开。
- **长期记忆**：SQLite 本地库，记录经历、对话轮次、事实、意义候选和章节；默认不写云端。
- **自然回应**：`respond / brief / defer / silent / pause_stated` 五种模式，代码默认关闭，启动脚本显式打开。
- **可选语音链路**：麦克风 → faster-whisper → LLM → TTS（BreezeBlue / edge-tts / 本地 VITS）。
- **可选身体**：ESP32-S3 + 摄像头 + 麦克风 + 喇叭，通过本机 `8768` 服务接入；没有硬件也能只跑 PC。

## 快速开始

### 1. 环境

- Windows 10/11
- Python 3.11 或更高；实际验证过 Python 3.13.14
- Git（可选；不装 Git 也可以直接下载 ZIP）
- 一个 OpenAI 兼容的 LLM API Key；默认示例是阿里云百炼的 `qwen3.8-flash`

**把仓库放在纯 ASCII 路径下**，例如 `C:\valkyrie`。不要放在含中文或空格的用户目录里：OpenCV、onnxruntime 和 `pip` 在部分 Windows 组合下会读不了中文路径。代码会尽量兼容，但这是最省事的做法。

不想装 Git 的话，在 GitHub 页面点 **Code → Download ZIP**，解压到 `C:\valkyrie` 后同样可以继续。

```powershell
git clone https://github.com/HJM-Byte/valkyrie.git C:\valkyrie
cd C:\valkyrie
```

### 2. 一键安装

双击 `install.cmd`。它会检查 Python 3.11+（没有时可用 winget 安装 3.13）、创建 `.venv`、安装核心依赖、生成 `.env`、询问一次 LLM API Key，并创建桌面和开始菜单快捷方式。

```cmd
:: 只装核心：桌面、网页、识海、LLM 调用
install.cmd

:: 需要麦克风、摄像头、本地语音时
install.cmd -Voice

:: 连可选模型一起下载（人脸 + faster-whisper，体积较大）
install.cmd -Voice -Models
```

API Key 的输入不回显，也不会写进 `install.log`；只写入已被 Git 忽略的 `.env`。不想现在填可以回车跳过，之后编辑 `.env`。

如果已经装过依赖，想手动控制每一步：

```powershell
# 核心
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1

# 语音/麦克风/本地 TTS
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Voice
```

`bootstrap.ps1` 只创建 `.venv`、安装依赖并复制 `.env` 模板；不下载模型、不写入 Key。

### 3. 配置 Key

编辑 `.env`：

```dotenv
DASHSCOPE_API_KEY=你的百炼Key
```

至少填这一项。没有 Key 时程序仍能打开界面，但对话会明确报“未配置”，不会假装回答。

可选：

```dotenv
# BreezeBlue 音色（不填则退 edge-tts，再退本地 VITS）
BREEZE_API_KEY=
BREEZE_VOICE_ID=voc_wkzpcy2xxwch

# 模型不在默认位置时指定；建议纯 ASCII 路径
# ROBOT_MODELS=C:\valkyrie-models

# 手机网页在局域网开放时，建议设置访问口令
# VALKYRIE_APP_TOKEN=换成一串随机字符

# 改名字；默认“瓦尔基里”
# ROBOT_NAME=瓦尔基里
```

`.env` 已在 `.gitignore` 里。不要把 Key 写进代码、Issue、截图或提交记录。

### 4. 启动

```cmd
start.cmd               :: 统一菜单
install.cmd             :: 一键安装/补依赖
desktop.cmd             :: 桌面主入口
web.cmd                 :: 网页端 8766
sihai.cmd               :: 识海页 8767
chat.cmd                :: 纯文字入口，不需要麦克风
run.cmd                 :: 完整语音流水线
```

第一次建议用 `desktop.cmd --demo` 看示例数据，不写真实库：

```cmd
desktop.cmd --demo
```

确认界面正常后，再运行 `desktop.cmd` 使用真实数据。

### 5. 模型（可选，但语音/视觉需要）

仓库不附带模型权重。人脸、语音识别和本地 TTS 按需下载。先装过 `-Voice`（它包含 `huggingface-hub`），或单独 `pip install huggingface-hub`：

```powershell
# 人脸检测与特征模型
python download_faces.py

# faster-whisper-small 语音识别
python download_whisper.py
```

默认下载到 `.\models`；可以用 `ROBOT_MODELS` 指到别的目录。模型来自 OpenCV、Systran 等第三方项目，使用前请遵守各自许可证。本地 VITS 声线不随仓库分发，见 [NOTICE.md](NOTICE.md)。

## 目录

```text
app/                核心代码：对话、记忆、情绪、桌面、网页、识海、身体
  pocket_web/       手机/网页前端
  pocket_apk/       可选的 Android 壳（首版不随仓库发布，需要 Android SDK）
tools/              自检、维护、数据采集与模型训练脚本
hardware/           可选的 ESP32 身体设计与接线资料
web/sihai.html      识海网页视图
PERSONA.md          默认人格提示词；每个人可以改成自己的那份
.env.example        配置模板，不含真实 Key
install.cmd         Windows 一键安装入口
install.ps1         安装器本体
bootstrap.ps1       手动创建 .venv 并安装依赖
data/               运行时数据；已被 Git 忽略
models/             模型目录；已被 Git 忽略
docs/               本地开发记录；默认不进入公开仓库
```

更细的模块与数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 数据与隐私

- 记忆、对话、日记、人脸底库、截图都在 `data/`，默认不离开本机。
- 项目没有遥测，不会把使用记录回传到作者这里。
- 需要联网的只有 LLM、可选的 TTS 服务和可选的模型下载。
- `web.cmd` 的 `8766` 和身体的 `8768` 是局域网服务。它们不是公网服务；在不可信网络里请设置 `VALKYRIE_APP_TOKEN`，并确认防火墙只放行需要的网段。
- 摄像头默认锁定（`VISION_LOCKED = True`），不会因为程序启动就打开。需要视觉时再显式解锁。
- 删除 `data/` 等于删除她的长期记忆；先备份再删。

## 现在能跑什么，不能跑什么

**能跑：**

- Windows 桌面窗口、网页端、识海页、文字对话
- 本地 SQLite 记忆、情绪、关系、识海星图
- 接入 OpenAI 兼容 LLM
- 可选语音输入/输出；可选 ESP32 身体
- 自检脚本（见 `tools/selftest_*.py`）

**还没做：**

- 首次安装仍需自己提供一个 LLM API Key（安装器会引导）
- macOS / Linux 的桌面端
- 对所有第三方模型的授权再分发
- 一个干净的、面向新用户的完整模型下载器
- 手机 APK 的通用签名与发布流程

## 开发

常用自检：

```powershell
.\.venv\Scripts\python.exe -m compileall app tools
.\.venv\Scripts\python.exe -m tools.selftest_sihai
.\.venv\Scripts\python.exe -m tools.selftest_desktop
.\.venv\Scripts\python.exe -m tools.selftest_memory
```

测试数据必须写临时库，不能写 `data/memory.db`。具体约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 安全

不要在公开仓库里提交 `.env`、`data/`、`models/`、`backups/`、`diary/`、APK keystore。报告安全问题请走 [SECURITY.md](SECURITY.md) 里的私密渠道。

## 许可证

代码以 MIT 许可证发布，见 [LICENSE](LICENSE)。第三方依赖、模型和音色服务的授权不因本仓库的 MIT 许可证而改变；详见 [NOTICE.md](NOTICE.md)。名称与角色形象不随代码授权，见 [TRADEMARK.md](TRADEMARK.md)。

## 项目状态

这是一个从个人长期使用中长出来的项目。它现在适合愿意读日志、改配置、自己搭环境的人；还不适合作为“下载即用”的消费级产品。公开之后，优先补的是安装体验、模型下载、跨平台和文档，而不是再加功能。
