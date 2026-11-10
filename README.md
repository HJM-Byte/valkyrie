# Valkyrie · 瓦尔基里

瓦尔基里是一个住在 Windows PC 上的 AI 伙伴。她不是套一个聊天框的 Demo：记忆、情绪、关系状态和表达都在本机，桌面窗口、网页、识海星图。

情绪不是只改语气的装饰：她这一轮先按情绪生成内容计划——先说什么、哪些必须保留、能不能多问一句、要不要轻表露；再生成表达计划——语气、篇幅、直接程度、幽默和声音。必要任务、错误说明、权限与承诺只读，不被情绪删改。

这个仓库放的是让她跑起来的代码、公开设计说明，以及一套不会把私人数据带进 Git 的默认边界。LLM、语音和模型可以换成你自己的；记忆和数据默认留在自己的电脑上。

> 当前状态：可运行，Windows 优先。第一次安装需要 Python 和一个 LLM API Key；模型按需下载。情绪的内容/表达调制默认关闭，需显式设置 `ROBOT_AFFECT_MODULATION_MODE` 才会生效。

## 功能

下面只写已经接进程序的部分，不把设计稿算成功能。

### 记忆：识海不是聊天记录库

普通聊天程序的记忆是：存聊天记录，需要时检索，塞回提示词。瓦尔基里的识海把“存”和“调”分开：**库里有，不等于此刻想得起。**

- **一条记忆是一个带属性的点。** 除了原文，还记时间、人物、场景、当时的情绪场、情绪残留、意义、关系权重、身份相关性、细节起点、重建可信度、来源、关于谁，以及这段经历留下的改变。
- **调度是一条链。** 唤醒 → 竞争 → 扩散 → 星光 → 心流。激活值过 `0.30` 才进当前意识，`0.14–0.30` 只是“存在”；扩散只走一步，带出来不等于想起来；心流是上一轮没想完的念头，会过期。
- **遗忘不是删除。** 细节掉得快，意义和情绪残留掉得慢；原文保留、可核对，但不再参与“被文字想起”。记忆会从完整经历退到片段、只剩感觉，最后只剩留下的改变。
- **残响。** 已经想不起是哪件事、但仍在影响她的那层底子。它没有原文可讲，会进评价、进语气，也会在识海图上变成灰环；她只能说“我也说不上来”，不许给它编一件具体的旧事。
- **归属与撤回。** 愿望、转述、推测不会被当成亲历；用户明确纠正或要求删除时，旧记录标记为撤回，原文不删、可核对，但不再参与召回、扩散、残响和星图。
- **星图是视图，不参与调度。** `web/sihai.html` 是纯手写透视投影，没有 three.js、没有外链，断网也能打开；图上的数字直接调用记忆层的同一组函数。
- **本地 SQLite。** 记忆存在 `data/memory.db`，桌面、网页、手机共用同一份。写入闸门在 `app/mind.py` 的 `GROWTH_LOCKED`：当前代码默认不锁（`False`），第一次真实对话就会写进去；只想先看界面，用 `desktop.cmd --demo`，或把它改成 `True`。

更细的模块与数据流见 [ARCHITECTURE.md](ARCHITECTURE.md)。

### 情感：不是给回复加表情

情绪不是关键词映射，也不是一轮一个标签。她同时有几股情绪，各自按自己的速度回落。

- **二十一种情绪，归到七情。** 喜、怒、哀、惧、爱、恶、欲是七股方向；具体强度仍按二十一种情绪算。另有六欲：目、耳、鼻、舌、身、意。
- **五路合成。** 这一轮的评价（期望、重要性、失望、威胁、控制感）、身体水位（精力、孤独）、对方语气、旧事残留与残响、六欲一起决定她此刻是什么情绪。
- **各自衰减。** 生气半衰期 4 小时，孤独 36 小时；同色情绪取较大值，不相加。所以“气消得快、低落散得慢”不是一句人设，是表里的两个数。
- **内容与表达分开调制。** 内容计划决定先说什么、哪些必须保留、能不能多问一句、要不要轻表露；表达计划决定语气、篇幅、直接程度、幽默和声音。必要任务、错误说明、权限与承诺只读，不被情绪删改；情绪数字不进提示词。
- **默认关闭，按层打开。** `ROBOT_AFFECT_MODULATION_MODE=off` 保持原行为；`shadow` 只记计划不改提示；`enforced` 才把双计划接进提示。内容与表达还可用 `ROBOT_AFFECT_CONTENT` / `ROBOT_AFFECT_STYLE` 独立关闭。
- **会留下来。** 情绪场和六欲持久化在 `mind` 表里，进程重启、换模型之后还在；老库缺列会自动补，不报错。
- **没接上的感官不演。** 六欲里真正有输入的是“意”和“耳”；目、鼻、舌、身没有对应硬件，只留低低的向往，不假装被满足，也不拿“没被满足”去演委屈。

### 表达与入口

- **桌面程序**：原生 Tkinter 窗口，对话、识海星图、记忆详情、时间轴和状态面板在一个窗口里；识海页的顶栏、胶囊导航、左侧「她此刻」、详情抽屉与底部时间轴按 `sihai.html` 原生重画，不走浏览器、不嵌 8767。
- **网页端**：`8766`，电脑宽屏是“镜面 + 识海”双栏，手机窄屏单列；可安装为 PWA。
- **识海页**：`8767`，独立的只读星图页。
- **自然回应**：`respond / brief / defer / silent / pause_stated` 五种模式；代码默认关闭，启动脚本显式打开。
- **可选语音**：麦克风 → faster-whisper → LLM → TTS（BreezeBlue / edge-tts / 本地 VITS）。
- **可选身体**：ESP32-S3 + 摄像头 + 麦克风 + 喇叭，通过本机 `8768` 服务接入；没有硬件也能只跑 PC。
- **学习区**：知识卡走待审队列，未审核的内容不进入记忆竞争。

## 使用声明

**欢迎学习、研究、借鉴，也欢迎改成你自己的伙伴。** 你可以读代码、fork、换模型、改人格、做自己的本地部署；公开引用或二次分发时，请保留本仓库来源和 [MIT 许可证](LICENSE) 文本。

**商业用途请先联系作者。** 包括但不限于付费产品、SaaS/在线服务、企业部署、二次销售、以本项目为主的培训或代部署。联系方式：GitHub [@HJM-Byte](https://github.com/HJM-Byte)（Issue 或私信）。名称与角色形象不随代码授权，见 [TRADEMARK.md](TRADEMARK.md)。

代码本身仍按 MIT 发布，MIT 允许商业使用；上面这句是作者的沟通请求，不是 MIT 之外的附加许可条件。需要明确的商业授权、定制或合作，请单独联系作者。

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

# 情绪调制：off 保持原行为；shadow 只记计划；enforced 才进提示
# ROBOT_AFFECT_MODULATION_MODE=shadow
# ROBOT_AFFECT_CONTENT=1
# ROBOT_AFFECT_STYLE=1
```

`.env` 已在 `.gitignore` 里。不要把 Key 写进代码、Issue、截图或提交记录。

### 4. 启动

```cmd
start.cmd               :: 统一菜单
install.cmd             :: 一键安装/补依赖
desktop.cmd             :: 桌面主入口（原生 Tkinter，识海界面原生复刻）
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
# 用安装器创建的虚拟环境，避免跑到系统 Python
# 人脸检测与特征模型
.\.venv\Scripts\python.exe download_faces.py

# faster-whisper-small 语音识别
.\.venv\Scripts\python.exe download_whisper.py
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

## 数据与隐私

- 记忆文件、人脸底库、截图和日记只存在本机（`data/`、`diary/`），不会上传到作者这里。
- 对话文本会发给**你配置的 LLM 服务商**，手机照片会发给**你配置的视觉模型**，TTS 文本会发给对应语音服务；这些是运行必需的外部请求，不是遥测。
- 项目没有自建遥测，不会把使用记录回传到作者这里。
- 需要联网的只有 LLM、可选的 TTS 服务和可选的模型下载。
- `web.cmd` 的 `8766` 和身体的 `8768` 是局域网服务。它们不是公网服务；在不可信网络里请设置 `VALKYRIE_APP_TOKEN`，并确认防火墙只放行需要的网段。
- 摄像头默认锁定（`VISION_LOCKED = True`），不会因为程序启动就打开。需要视觉时再显式解锁。
- 第一次真实对话会写进 `data/memory.db`；只想看界面不写库，用 `desktop.cmd --demo`。
- 删除 `data/` 等于删除她的长期记忆；先备份再删。

## 现在能跑什么，不能跑什么

**能跑：**

- Windows 桌面窗口、网页端、识海页、文字对话
- 本地 SQLite 记忆、遗忘、残响、记忆调度、识海星图
- 二十一种情绪、七情归类、六欲、关系状态与表达计划
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
.\.venv\Scripts\python.exe -m tools.selftest_emotion
```

测试数据必须写临时库，不能写 `data/memory.db`。具体约定见 [CONTRIBUTING.md](CONTRIBUTING.md)；完整检查方法与最近一次结果见 [TESTING.md](TESTING.md)。

## 安全

不要在公开仓库里提交 `.env`、`data/`、`models/`、`backups/`、`diary/`、APK keystore。报告安全问题请走 [SECURITY.md](SECURITY.md) 里的私密渠道。

## 许可证

代码以 MIT 许可证发布，见 [LICENSE](LICENSE)。第三方依赖、模型和音色服务的授权不因本仓库的 MIT 许可证而改变；详见 [NOTICE.md](NOTICE.md)。名称与角色形象不随代码授权，见 [TRADEMARK.md](TRADEMARK.md)；商业使用前请先看「使用声明」。

## 项目状态

这是一个从个人长期使用中长出来的项目。它现在适合愿意读日志、改配置、自己搭环境的人；还不适合作为“下载即用”的消费级产品。接下来优先补的是安装体验、模型下载、跨平台和文档，而不是再加功能。
