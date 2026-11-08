# 贡献指南

## 先做什么

1. 在 Issue 里说清楚要解决的问题，不要直接开一个包含大量个人数据或密钥的 PR。
2. 从主分支拉一个短分支，改动尽量只碰一个主题。
3. 在 Windows 上跑过再提交；这个项目的桌面端和身体链路目前是 Windows 优先。

## 本地环境

```powershell
git clone https://github.com/HJM-Byte/valkyrie.git C:\valkyrie
cd C:\valkyrie
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Voice
copy .env.example .env
# 编辑 .env，至少填 DASHSCOPE_API_KEY
```

如果仓库放在中文路径下，`pip`、OpenCV 或 onnxruntime 可能报路径错误。优先换到 `C:\valkyrie` 这类纯 ASCII 路径再复现。

## 不能提交的内容

- `.env`、任何 API Key、Token、Cookie
- `data/`、`diary/`、`backups/`、`models/`
- `docs/` 下的本地开发记录
- 真实记忆、对话、人物信息、本机绝对路径
- APK keystore、构建产物、带签名的 APK

`.gitignore` 已经排除了这些路径。提交前仍然要自己看一遍 `git status`，不要只信忽略规则。

## 测试

```powershell
.\.venv\Scripts\python.exe -m compileall app tools
.\.venv\Scripts\python.exe -m tools.selftest_sihai
.\.venv\Scripts\python.exe -m tools.selftest_desktop
.\.venv\Scripts\python.exe -m tools.selftest_memory
```

测试数据只能写临时库，不能写生产 `data/memory.db`、`data/learning/learn.db` 或 `data/school/school.db`。需要真实库的验证要在副本上做，并在 PR 里说明。

## 代码约定

- 不把作者本机路径写进代码；用 `ROBOT_MODELS`、`ROBOT_DATA`、`%~dp0` 或 `Path(__file__)`。
- 新功能先保证不写生产库、不自动打开摄像头、不自动发网络请求。
- 涉及记忆、情绪、人格的改动，要说明是否会改变已有数据；需要迁移时先写清回滚方式。
- 中文注释可以；变量名和文件名保持 ASCII。
- 不要为了“像 AI”而加没有数据来源的细节。

## 提交信息

短句，说清动作和对象，例如：

```text
修复 desktop 在无模型时的启动崩溃
增加 ROBOT_MODELS 环境变量
```

不要只写“更新”“优化”“修改”。

## 许可证

提交代码即表示你同意以本仓库的 MIT 许可证发布；你提交的第三方素材必须是你有权分发的，并在 PR 里说明来源和许可证。
