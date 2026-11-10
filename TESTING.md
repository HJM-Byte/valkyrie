# 验证

这份文件记录“完整功能检查”的方法与最近一次结果。日常改动至少跑 `compileall` 和相关自检；发版前跑完整套。

## 运行

先确认虚拟环境已装好，且 `data/` 没有被其他进程占用。

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
- 38 个离线自检模块：**1061 项通过 / 0 失败**，退出码全部为 0
- 在线 `tools.selftest_web`：**25 / 25 通过**
- `tools.selftest_mind` 在线重跑：57 项通过；其中 `/api/chat` 接口节因需要配对 token 跳过，不计入失败
- 生产库：`memory.db` / `learning/learn.db` / `proactive.db` / `p3.db` / `school.db` 的表级内容哈希与测试前一致；`quick_check` 全部 `ok`
- 8766 health：`ok=true`、`version=0.8.0`、`proactive=true`、`response_gate.mode=enforced`
- 识海页面：用 `app.sihai --demo --serve --port 8799` 起临时实例，`/sihai.html` 返回 200；正式 8767 由 `sihai.cmd` 按需拉起，不是常驻服务

覆盖范围包括：对话与人格、情绪与关系、记忆与识海、学习与主动消息、网页与 PWA、桌面视图、日记、语音链路、视觉准备、手机协议、ESP32 身体协议（模拟设备）。

## 边界

- **8768 没有接真板**。身体协议的五项自检使用回环测试设备，验证握手、Opus、STT/Chat/TTS、两轮自动 `listen start` 和只读边界；真机首声、连续两轮和断线重连要在接板后单独验收。
- **音频与视觉自检依赖本机设备/模型**。`selftest_audio` 需要麦克风和可用 TTS；`selftest_vision`/`selftest_objects` 需要 `models/`。没有这些时跳过或失败是环境问题，不是代码回归。
- **测试库边界**：自检只写 `data/acceptance/tmp/`、`data/_selftest_*` 或系统临时目录；如果测试改动了生产库，按失败处理，并先恢复 `backups/` 下的备份。
- **WAL 文件差异**：SQLite 开关进程或 checkpoint 可能导致 `.db` 文件字节哈希变化；判断数据是否变化以表行数和表级内容哈希为准，不接受只看文件哈希。
