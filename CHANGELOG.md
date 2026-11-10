# Changelog

## Unreleased

- 公开前可移植性修复：`install.ps1` 的 `.env` 解析不再跨行吞注释，首次安装能正常询问 Key；`install.ps1` / `bootstrap.ps1` 补回 UTF-8 BOM，Windows PowerShell 5.1 不再乱码；硬件 README 移除指向不公开 `docs/` 的断链。
- 公开仓库补上 `tools/acceptance/cases/N*.jsonl`（自然回应闸门自检所需）；`selftest_apk_download` 在未构建 APK 时 SKIP，`selftest_memory_strength` 在只有核心依赖时跳过语音接线检查。
- `app/llm.py` 移除对 `~/.dsh/.credentials.yaml` 的隐式读取，LLM Key 只从环境变量或 `robot/.env` 读取。
- README 重写：补充记忆（识海存储、调度、遗忘、残响、归属与撤回）与情感（二十一种情绪、七情、六欲、衰减、表达计划）的功能说明，并增加学习借鉴与商业使用联系作者的声明。
- README 明确 `GROWTH_LOCKED` 默认写入真实库、`--demo` 不写库，以及日记目录与数据目录的边界。
- 从公开仓库移除学校模块：删除 `app/school.py`、`school.cmd`、`tools/selftest_school.py` 及其配置、预算、对话回接、服务入口和验收引用；学习与成长链路保持不变。
- 识海滚动渲染：滚动面板改为不透明底色，去掉 `backdrop-filter`，加入 `overscroll-behavior: contain` 与 `scrollbar-gutter: stable`；桌面滚动步长固定，只在内容尺寸变化时更新 `scrollregion`，修复滚动时星海透底造成的闪动。
- 从公开仓库移除桌面 Tkinter 程序（`app/desktop.py`、`app/desktop_prep.py`、`desktop.cmd`、相关自检与 `breeze.cmd`），网页端成为对话与识海的默认入口；本地副本不再随仓库发布。
- 主动消息降频：新增 `app/proactive_pacing.py`，在现有主动候选之后、模型调用之前接入等级/安静/用户宽限/每日上限/最小间隔/未回复退避与原子预留；默认 `off`，支持 `shadow` 观察与 `enforced` 生效。`low` 只放行高价值 `share`/`continue`，必要承诺与到期提醒不占非必要额度；发不出去的候选不补发、不换通道，也不写关系与长期记忆。
- 情绪内容与表达调制：新增 `app/affect_modulator.py`，把当轮情绪转成可审计的 `ContentPlan`（内容顺序、必要契约、可选关心/追问、自我表露）与 `StylePlan`（语气、篇幅、直接程度、幽默、情绪可见度）；`think()` 在基础决策后接入，`Persona` 增加独立调制段。默认 `off`，支持 `shadow` 只记录、`enforced` 生效，内容/表达可独立关闭；不改事实、证据资格、关系变量或长期偏好。

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
