# Changelog

## Unreleased

- README 重写：补充记忆（识海存储、调度、遗忘、残响、归属与撤回）与情感（二十一种情绪、七情、六欲、衰减、表达计划）的功能说明，并增加学习借鉴与商业使用联系作者的声明。
- README 明确 `GROWTH_LOCKED` 默认写入真实库、`--demo` 不写库，以及日记目录与数据目录的边界。
- 从公开仓库移除学校模块：删除 `app/school.py`、`school.cmd`、`tools/selftest_school.py` 及其配置、预算、对话回接、服务入口和验收引用；学习与成长链路保持不变。
- 识海滚动渲染：滚动面板改为不透明底色，去掉 `backdrop-filter`，加入 `overscroll-behavior: contain` 与 `scrollbar-gutter: stable`；桌面滚动步长固定，只在内容尺寸变化时更新 `scrollregion`，修复滚动时星海透底造成的闪动。
- 桌面识海原生复刻：不再使用 Edge 承载网页，`app/desktop.py` 直接按 `sihai.html` 重画顶栏、八块胶囊导航、左侧面板、详情抽屉、思考线、时间轴与卡片；新增 `分寸`、`意义`、`人格` 三块只读视图，数据与 `Chat.panel_payload()` / `Memory` 同源。
- 情绪改「说什么」：新增 `affect.content_rules`，把最强的一两股情绪落成这一轮要说的事（如委屈要说出来、好奇要追问一个细节）；`think.decide` 先算情绪场再重跑决策，内容以 `decision.content` 和独立提示段 `【这一轮想说的】` 进系统提示。语气仍由 `emotion.plan` 的【怎么说】与 TTS 参数负责。

## v0.1.0 — 2026-11-08

首个预发布。它是可运行的本地伙伴框架，不是消费级安装包。

### 包含

- Windows 桌面端、网页端（8766）、识海星图（8767）、可选身体服务（8768）。
- 本地 SQLite 长期记忆、识海调度、情绪与关系、自然回应闸门。
- 可选语音链路：麦克风 → faster-whisper → LLM → TTS。
- Windows 一键安装器 `install.cmd` / `install.ps1`：核心、`-Voice`、`-Models` 三档。
- 自检脚本、架构说明、安全说明、第三方依赖边界。

### 不包含

- 任何 API Key、记忆库、对话、日记、真人截图或模型权重。
- 手机 APK、签名 keystore、作者本地开发记录。
- 对第三方模型、音色和素材的再分发授权。

### 已知限制

- 首次安装仍需自己的 Python 和 LLM API Key。
- 语音/视觉/身体链路依赖可选硬件与模型，未在每台机器上验证。
- macOS / Linux 桌面端未做。
- 仓库不承诺“下载即用”的消费级体验。
