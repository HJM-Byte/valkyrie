# 验证

这份文件记录“完整功能检查”的方法与最近一次结果。日常改动至少跑 `compileall` 和相关自检；发版前跑完整套。

## 运行

先确认虚拟环境已装好，且 `data/` 没有被其他进程占用。离线自检时先停掉 8766：服务在线时会写 `p3.db` / `proactive.db`，`selftest_p3` 的生产守卫会把实时写入误判成测试写入。`selftest_web` 再启动 8766 单独跑。

```powershell
# 1. 语法与导入
.\.venv\Scripts\python.exe -m compileall app tools

# 2. 离线自检（不需要 8766，逐项跑）
Get-ChildItem tools\selftest_*.py | Sort-Object Name | ForEach-Object {
    $m = "tools." + $_.BaseName
    Write-Host "== $m =="
    .\.venv\Scripts\python.exe -m $m
}

# 3. 在线网页自检（需要 8766 正在运行）
.\.venv\Scripts\python.exe -m tools.selftest_web
```

`selftest_web` 会检查 8766 的 health、登录态、PWA 壳、manifest、watchdog 参数、启动项和备份完整性。它不写 `data/memory.db`。

## 最近一次结果

2026-11-10，Windows 10/11，Python 3.13.14，8766 在线：

- `compileall app tools`：0 错误
- 此前 38 个离线自检的基线：**1061 项通过 / 0 失败**，退出码全部为 0
- 在线 `tools.selftest_web`：**26 / 26 通过**
- `tools.selftest_mind` 在线重跑：57 项通过；其中 `/api/chat` 接口节因需要配对 token 跳过，不计入失败
- 生产库：`memory.db` / `learning/learn.db` / `proactive.db` / `p3.db` 的表级内容哈希与测试前一致；`quick_check` 全部 `ok`
- 8766 health：`ok=true`、`version=0.8.0`、`proactive=true`、`response_gate.mode=enforced`、`proactive_pacing.mode=enforced / level=low`

### 主动消息降频回归（2026-11-10）

- `tools.selftest_proactive_pacing`：55 通过 / 0 失败（F01–F16）
- `tools.selftest_proactive_topic`：58 通过 / 0 失败
- `tools.selftest_proactive`：5 通过 / 0 失败
- `tools.selftest_response_gate`：31 通过 / 0 失败
- `tools.selftest_pocket`：23 项通过
- `tools.selftest_p3`：38 通过 / 0 失败（停 8766 后跑）
- `tools.selftest_autonomy`：31 通过 / 0 失败
- `compileall app tools`：0 错误
- 生产库：`memory.db` / `p3.db` / `school.db` 表级内容哈希未变；`proactive.db` 仅新增 `pacing_state` 单行表，`messages` 仍为 16 条
- 线上 `8766` health 显示 `proactive_pacing: enforced / low`，首日非必要计数 0、无活动预留
- 识海页面：用 `app.sihai --demo --serve --port 8799` 起临时实例，`/sihai.html` 返回 200；正式 8767 由 `sihai.cmd` 按需拉起，不是常驻服务

覆盖范围包括：对话与人格、情绪与关系、记忆与识海、学习与主动消息、网页与 PWA、识海视图、日记、语音链路、视觉准备、手机协议、ESP32 身体协议（模拟设备）。

## 干净克隆检查

2026-11-10 在 `C:\valkyrie_public_test`（纯 ASCII 路径、全新克隆、没有 `.env` / `data/` / `models/`）复跑：

- `install.ps1 -NoShortcuts -NoLaunch`：退出 0；用环境变量提供测试 Key 时 `.env` 正确写入，不再跳过输入。
- `bootstrap.ps1`：退出 0，生成 `.env` 后停止；中文提示在 Windows PowerShell 5.1 下正常显示。
- `requirements.txt` 与 `requirements-voice.txt` 均安装成功；`web.cmd --no-open` 启动 `8766` 并通过 health 检查，`sihai --demo` 生成 72 节点 / 122 边，`pocket_server --demo --readonly` 的 `/api/health` 与 `/` 均返回 200。
- 桌面程序已移出公开仓库：干净克隆中不存在 `desktop.cmd`、`app/desktop.py`、`app/desktop_prep.py` 及桌面自检；默认入口是 `web.cmd`，识海和对话都在网页端。
- 核心内存/识海/协议自检通过；`selftest_web` 在带 `data/` 备份的工作区 26/26，干净克隆没有这些运行时文件时只验证 `web.cmd` 的 health 与首页，不检查备份。`selftest_apk_download` 在无 APK 时明确 SKIP，`selftest_memory_strength` 在只有核心依赖时跳过语音接线检查，`selftest_response_gate` 的 N 系列用例随仓库提供。
- 还没覆盖：第二台物理电脑、macOS/Linux、8768 真机、真实模型下载与云端 Key 调用。

## 边界

- **8768 没有接真板**。身体协议的五项自检使用回环测试设备，验证握手、Opus、STT/Chat/TTS、两轮自动 `listen start` 和只读边界；真机首声、连续两轮和断线重连要在接板后单独验收。
- **音频与视觉自检依赖本机设备/模型**。`selftest_audio` 需要麦克风和可用 TTS；`selftest_vision`/`selftest_objects` 需要 `models/`。没有这些时跳过或失败是环境问题，不是代码回归。
- **可选功能自检的边界**：`selftest_apk_download` 在没有本地构建 APK 时 SKIP；`selftest_memory_strength` 在没有 `-Voice` 依赖时跳过语音接线检查；`selftest_body_live` 需要已有 `data/memory.db`。
- **测试库边界**：自检只写 `data/acceptance/tmp/`、`data/_selftest_*` 或系统临时目录；如果测试改动了生产库，按失败处理，并先恢复 `backups/` 下的备份。
- **WAL 文件差异**：SQLite 开关进程或 checkpoint 可能导致 `.db` 文件字节哈希变化；判断数据是否变化以表行数和表级内容哈希为准，不接受只看文件哈希。
