# Valkyrie 架构速览

这份文档只说明代码怎么分、数据怎么流，不重复 README 的安装步骤。

## 两个入口

```text
web.cmd      →  app.pocket_server --lan --chat  →  8766 浏览器/手机（对话 + 识海）
sihai.cmd    →  app.sihai --serve --port 8767  →  独立识海网页（只读）
```

两个入口共用同一份 `data/memory.db`、同一套 `Chat`、`Memory`、`Sihai` 和 `Mind`。网页端和识海页不是两套大脑。

## 核心数据流

```text
输入（键盘 / 麦克风 / 手机网页 / 身体）
        ↓
Chat.turn / turn_stream
        ↓
记忆召回（Memory / Sihai / Dispatch） + 情绪与关系（Affect / Mind）
        ↓
情绪调制（AffectModulator：ContentPlan / StylePlan，off/shadow/enforced）
        ↓
LLM（OpenAI 兼容接口）
        ↓
自然回应闸门（ResponseGate）
        ↓
回复文本 / TTS / 身体动作
        ↓
写回记忆、情绪、心流（只在真实入口，不写临时库）
```

## 目录

- `app/chat.py`：一轮对话的编排，提示拼装、记忆召回、写回。
- `app/memory.py`：SQLite 记忆存储，经历、事实、轮次、强度、来源、撤回。
- `app/dispatch.py`：唤醒 → 竞争 → 扩散 → 星光 → 心流。
- `app/affect.py` / `app/emotion.py` / `app/desire.py` / `app/mind.py`：情绪、七情六欲、关系与状态。
- `app/affect_modulator.py`：把当轮情绪转成内容计划与表达计划；只读上游契约，不写库、不调模型。
- `app/sihai.py`：星图数据与本地 HTTP 服务。
- `app/pocket_server.py`：手机/网页服务，`/api/chat` 与 SSE。
- `app/pocket_web/`：手机/网页前端资源。
- `app/body_*.py` / `app/xiaozhi_server.py`：ESP32 身体链路，可选。
- `app/llm.py` / `app/tts.py` / `app/stt.py`：模型与语音适配层。
- `tools/`：自检、维护、采集与训练脚本。
- `hardware/`：身体设计与接线资料，可忽略。

## 边界

- `VISION_LOCKED` 默认 `True`；视觉不是启动就开。
- `GROWTH_LOCKED` 控制她是否把新对话写进长期记忆；部署时按自己的意愿设置。
- `data/`、`models/`、`docs/`、`backups/` 不进入公开仓库。
- 8766 / 8768 是局域网服务，不是公网服务。
- 模型权重与音色服务有各自的授权，不因代码的 MIT 许可证而改变。
