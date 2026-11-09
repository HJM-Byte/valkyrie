"""集中配置。

路径默认相对仓库，不再写死作者机器上的固定路径。
模型目录可用 ``ROBOT_MODELS`` 指到纯 ASCII 路径，避开 OpenCV/onnxruntime
在部分 Windows 组合下读不了中文路径的问题。
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# --- .env -------------------------------------------------------------------
# 只补空缺，不覆盖已经存在的环境变量；方便 CI、PowerShell 临时变量和 .env 共存。
def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_env_file(PROJECT / ".env")

# --- 路径 -------------------------------------------------------------------
ENV_ROOT = Path(os.environ.get("ROBOT_HOME", str(PROJECT))).expanduser()
MODELS = Path(os.environ.get("ROBOT_MODELS", str(ENV_ROOT / "models"))).expanduser()
DATA = Path(os.environ.get("ROBOT_DATA", str(PROJECT / "data"))).expanduser()

# --- 身份 -------------------------------------------------------------------
# 2026-10-29 用户正式起名。名字属于身份，不属于人格；出生期只注入名字，
# 不注入 PERSONA.md 里的脾气、过去和设定。
ROBOT_NAME = (os.environ.get("ROBOT_NAME", "瓦尔基里") or "瓦尔基里").strip() or "瓦尔基里"

# --- 音频 -------------------------------------------------------------------
SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000   # 480
CHANNELS = 1

# 麦克风选择（2026-10-31）：默认自动找 C920/MME；也可以明确指定：
#   set ROBOT_MIC_DEVICE=1                 按 sounddevice 序号
#   set ROBOT_MIC_DEVICE=C920              按名称片段
#   set ROBOT_MIC_PREFER=Realtek           换自动优先名称
# 打开超时与重试：C920 偶尔第一次被占用，重试一次就能开。
MIC_DEVICE = os.environ.get("ROBOT_MIC_DEVICE", "").strip()
MIC_PREFER = os.environ.get("ROBOT_MIC_PREFER", "C920").strip()
MIC_OPEN_TIMEOUT = float(os.environ.get("ROBOT_MIC_OPEN_TIMEOUT", "2.5"))
MIC_OPEN_RETRIES = int(os.environ.get("ROBOT_MIC_RETRIES", "2"))

# VAD：连续多少帧静音判定说完
VAD_AGGRESSIVENESS = 2       # 0-3，越大越激进（越容易判静音）
VAD_SILENCE_FRAMES = 24      # 24 * 30ms = 0.72s
VAD_MIN_SPEECH_FRAMES = 8    # 少于 240ms 视为噪声
VAD_MAX_RECORD_SEC = 15.0

# --- 视觉 -------------------------------------------------------------------
# 2026-10-27 用户决定：这台设备自带的摄像头不用，等新摄像头到货并确认
# 图像/表情/语气都能正常识别之后，才把 VISION_LOCKED 改成 False。
# 在那之前，任何入口（GUI/语音流水线）都不许自己打开摄像头。
VISION_LOCKED = True
CAMERA_INDEX = 0
CAMERA_BACKEND = "msmf"      # 2026-10-29 C920 实测：dshow 打不开，msmf 正常 1920x1080
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_FPS = 30

DETECT_WIDTH = 640
DETECT_HEIGHT = 480
FACE_SCORE_THRESHOLD = 0.6
FACE_NMS_THRESHOLD = 0.3
FACE_TOP_K = 5000
FACE_MATCH_THRESHOLD = 0.363   # SFace 官方默认余弦阈值

YUNET_MODEL = MODELS / "face_detection_yunet_2023mar.onnx"
SFACE_MODEL = MODELS / "face_recognition_sface_2021dec.onnx"
NANODET_MODEL = MODELS / "object_detection_nanodet_2022nov.onnx"
# 2026-10-29：日常物体识别用预训练 YOLOv8（COCO）；检测手机、椅子等，凳子先归到 chair
OBJECT_MODEL = MODELS / "yolov8s_320x320.onnx"
OBJECT_MIN_SCORE = 0.35
OBJECT_NMS_IOU = 0.45

# --- 视觉数据与表情模型 -----------------------------------------------------
# 数据放工作区（可能含中文路径，读写统一走 app.vision 的 Unicode helper）；
# 模型默认放 <repo>/models；建议用 ROBOT_MODELS 指到纯 ASCII 路径，OpenCV/onnxruntime 不认中文路径。
VISION_DATA = DATA / "vision"
VISION_FACES = VISION_DATA / "faces"
VISION_PROBE = VISION_DATA / "probe"
VISION_INDEX = VISION_DATA / "faces_index.jsonl"
EXPRESSION_DIR = MODELS / "expression"
EXPRESSION_MODEL = EXPRESSION_DIR / "expression_mlp.npz"
EXPRESSION_INPUT = 32
EXPRESSION_LABELS = ("中性", "开心", "难过", "生气", "惊讶", "害怕", "厌恶", "疲惫")
VISION_MIN_FACE_SCORE = 0.80
VISION_MIN_FACE_PX = 80

# --- 语音识别 ---------------------------------------------------------------
WHISPER_DIR = MODELS / "faster-whisper-small"
WHISPER_LANG = "zh"
WHISPER_COMPUTE = "int8"
WHISPER_BEAM = 5

# --- 语音合成 ---------------------------------------------------------------
TTS_VOICE = "zh-CN-XiaoxiaoNeural"
TTS_RATE = "+8%"

# 用户 2026-11-01 指定 BreezeBlue「寒蝉余音」（public_id=voc_wkzpcy2xxwch，
# 音色页：hanchan-yuyin-emotional-female-vwkzpcy2xxwch）。
# Key 优先从环境变量 BREEZE_API_KEY 读；8766 后台服务没有终端环境时，TTS 读
# data/breeze_key.txt（只本机当前用户可读写）。没设 Key 时自动退回 edge-tts，
# 再退回本地 VITS。voice_id 可按同样方式覆盖。
BREEZE_VOICE_NAME = "寒蝉余音"
BREEZE_VOICE_ID = os.environ.get(
    "BREEZE_VOICE_ID", "voc_wkzpcy2xxwch").strip() or "voc_wkzpcy2xxwch"
# 手机端可切换的音色；第一个是默认。2026-11-01 实测两个 ID 都能合成。
BREEZE_VOICE_CHOICES = (
    {"id": "voc_wkzpcy2xxwch", "name": "寒蝉余音"},
    {"id": "voc_dcccp6b9p466", "name": "幽兰影"},
)


def breeze_voice_choices() -> list[dict[str, str]]:
    """给手机端的可选项；环境变量改了默认音色时也不会漏掉它。"""
    items = [{"id": BREEZE_VOICE_ID, "name": BREEZE_VOICE_NAME}]
    for item in BREEZE_VOICE_CHOICES:
        if item["id"] != BREEZE_VOICE_ID:
            items.append(dict(item))
    return items


BREEZE_MODEL_ID = os.environ.get(
    "BREEZE_MODEL_ID", "breeze-tts-2").strip() or "breeze-tts-2"
BREEZE_API_KEY_ENV = "BREEZE_API_KEY"
BREEZE_INSTRUCTIONS = os.environ.get(
    "BREEZE_TTS_INSTRUCTIONS",
    "像深夜和熟人说话，温柔但不哄人，句子之间留一点呼吸。").strip()
# 2026-11-03 23:40 找到可用正式域名：api.breezeblue.ai。
# 旧 api.breeze.blue 的 Cloudflare 四节点在夜间会整段握手超时；.ai 域名
# 用同一个 Key、同一组 voice_id 实测 6/6 出音，2.3–3.7 秒。默认走新域名，
# 保留旧域名只作为文档/回滚线索，不再放进默认请求路径。
BREEZE_HOST = os.environ.get(
    "BREEZE_HOST", "api.breezeblue.ai").strip() or "api.breezeblue.ai"
BREEZE_FORCE_IPS = os.environ.get(
    "BREEZE_FORCE_IPS",
    # 2026-11-05 17:05 实测：104.19/104.27/104.21 的 TLS 握手 3/3 超时，
    # 104.26 连续 3 次 200 出音（TTFB 1.27–1.51s），104.17/104.18 也能出。
    # 把当次可用节点排前面；DNS 解析出的 Anycast 地址仍放最后兜底。
    "104.26.0.1,104.17.0.1,104.18.0.1,104.16.0.1,"
    "104.19.0.1,104.27.0.1,104.21.86.248,dns",
).strip()
# 坏节点不能吃掉整个预算：连接 3 秒、等首包 4 秒、块间 6 秒；
# 一个节点坏掉后还有时间换下一个，整体仍压在 8 秒内。
BREEZE_CONNECT_TIMEOUT = float(os.environ.get(
    "BREEZE_CONNECT_TIMEOUT", "3.0"))
BREEZE_HEADER_TIMEOUT = float(os.environ.get(
    "BREEZE_HEADER_TIMEOUT", "4.0"))
BREEZE_READ_TIMEOUT = float(os.environ.get(
    "BREEZE_READ_TIMEOUT", "6.0"))
BREEZE_TOTAL_TIMEOUT = float(os.environ.get(
    "BREEZE_TOTAL_TIMEOUT", "9.0"))
# 手机请求不能再被 Cloudflare 的握手黑洞拖 55 秒，但 5 秒对 .ai 的首次
# 合成偏紧（实测首包 1.7 秒、整段 3.7 秒，偶发 6 秒）。改成 8 秒总预算，
# 失败立刻转 edge/本地；宁可比理想慢两秒，也不能把可用的请求判死。
BREEZE_MAX_SECONDS = float(os.environ.get(
    "BREEZE_MAX_SECONDS", "8.0"))
BREEZE_COOLDOWN_SECONDS = float(os.environ.get(
    "BREEZE_COOLDOWN_SECONDS", "10.0"))
# 身体通道失败后只冷却一小段：板子下一轮通常在 10 秒后才连回来，
# 若沿用手机的 10 秒冷却，第二句话会被直接判“冷却中”而没有第二次机会。
BREEZE_BODY_COOLDOWN_SECONDS = float(os.environ.get(
    "BREEZE_BODY_COOLDOWN_SECONDS", "3.0"))

# --- 百炼 TTS 兜底 -----------------------------------------------------------
# Breeze 的 Cloudflare 线路会整段时间不可达。阿里云百炼已验证可出音：
# - 声音复刻：用寒蝉余音/幽兰影的样本在百炼建了自定义音色，音色接近原声；
# - 内置 Qwen3-TTS：复刻失败时用 Cherry/Serena 顶住，不静默回本地通用声。
# Key 只从环境变量或 robot/.env 读，不写进代码、不落日志。
DASHSCOPE_KEY_ENV = "DASHSCOPE_API_KEY"
QWEN_TTS_CLONE_MODEL = os.environ.get(
    "QWEN_TTS_CLONE_MODEL", "qwen-audio-3.1-tts-plus").strip() \
    or "qwen-audio-3.1-tts-plus"
QWEN_TTS_CLONE_VOICES = {
    "voc_wkzpcy2xxwch":
        "qwen-audio-3.1-tts-plus-vkhc-e723bb74bbb94c0fbb65157878303f1e",
    "voc_dcccp6b9p466":
        "qwen-audio-3.1-tts-plus-vkyl-9800283260c3469b8111abba1635e9da",
}
QWEN_TTS_BUILTIN_MODEL = os.environ.get(
    "QWEN_TTS_BUILTIN_MODEL", "qwen3-tts-flash").strip() or "qwen3-tts-flash"
QWEN_TTS_BUILTIN_VOICES = {
    "voc_wkzpcy2xxwch": "Cherry",
    "voc_dcccp6b9p466": "Serena",
}
QWEN_TTS_TIMEOUT = float(os.environ.get("QWEN_TTS_TIMEOUT", "12.0"))
# 用户 2026-11-03 明确不要复刻音色。百炼只保留为显式开关：默认不走，
# 避免 Breeze 一断就悄悄换成“像但不同源”的声线。需要时设
# ROBOT_TTS_QWEN_FALLBACK=1 再启用。
TTS_QWEN_FALLBACK = os.environ.get(
    "ROBOT_TTS_QWEN_FALLBACK", "0").strip().lower() in (
        "1", "true", "yes", "on")

# 本地离线声线（2026-10-31）：edge-tts 的合成 WebSocket 被本机网络拦掉，
# 但她的嘴不能因此停。VITS 中文模型放工作区（ONNX 实测能读中文路径），
# edge 可用时优先 edge，失败自动切本地；模型缺失时只报错不假装。
TTS_LOCAL_DIR = Path(os.environ.get(
    "ROBOT_TTS_LOCAL_DIR",
    str(PROJECT / "models" / "tts" / "vits-zh-aishell3"))).expanduser()
TTS_LOCAL_SID = int(os.environ.get("ROBOT_TTS_SID", "0"))
TTS_LOCAL_ENABLED = os.environ.get("ROBOT_TTS_LOCAL", "1") != "0"

# --- 大模型 -----------------------------------------------------------------
# 运行链路只走云端，不换本地：以后整个程序要搬到安卓手机上，只保留 HTTP 调用。
# 当前对话模型是 Qwen3.8-Flash（阿里云百炼，OpenAI 兼容协议）。
# 密钥只从环境变量 / robot/.env 读，不写进代码、不写进库、不落日志。
LLM_BACKEND = os.environ.get("ROBOT_LLM_BACKEND", "cloud")

# 云端：OpenAI 兼容协议。换模型只改这三个变量。
CLOUD_BASE = os.environ.get(
    "ROBOT_LLM_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1")
CLOUD_MODEL = os.environ.get("ROBOT_LLM_MODEL", "qwen3.8-flash")
CLOUD_KEY_ENV = os.environ.get("ROBOT_LLM_KEY_ENV", "DASHSCOPE_API_KEY")
CLOUD_TIMEOUT = 45.0
CLOUD_TEMPERATURE = 0.85
CLOUD_MAX_TOKENS = 300
# 身体语音默认关 thinking：qwen3.8-flash 的思考 token 占 10–32 秒里
# 的 99%，关掉后同一提示约 1.7 秒出正文。手机/桌面入口仍可用默认。
CLOUD_ENABLE_THINKING = os.environ.get(
    "ROBOT_LLM_ENABLE_THINKING", "1") != "0"

# 手机摄像头：照片只作为这一轮的临时上下文，交给视觉模型看，不落盘。
# 2026-10-31 实测该 Key 下 qwen3-vl-plus 可用；qwen-vl-plus/qwen-vl-max 返回 404。
PHONE_VISION_MODEL = os.environ.get(
    "ROBOT_VISION_MODEL", "qwen3-vl-plus").strip() or "qwen3-vl-plus"
PHONE_VISION_TIMEOUT = float(os.environ.get("ROBOT_VISION_TIMEOUT", "60"))
PHONE_VISION_MAX_BYTES = 6 * 1024 * 1024

# 本地 ollama（只留作调试，不参与运行链路；安卓端不用）
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3.5:4b")
LLM_TIMEOUT = 120.0          # 4B 模型首次冷启约 60s，给足

LLM_MAX_HISTORY = 12        # 保留最近 12 轮
LLM_THINK = False           # Qwen3 系会先输出 thinking，机器人对话关掉

# 出生期只留最近两轮（4 条消息）当「当前意识」。不落盘，重启就没了。
# 再往前的属于记忆，而她现在没有记忆。
BIRTH_HISTORY = 4

# --- 每日 token 预算（学习） -----------------------------------------------
# 默认值是初始安全值，不是产品指标；只有用户明确改配置才能提高。
BUDGET_DB = DATA / "budget.db"
BUDGET_TZ = os.environ.get("ROBOT_BUDGET_TZ", "Asia/Shanghai")
RESEARCH_DAILY_TOKENS = int(os.environ.get(
    "ROBOT_RESEARCH_DAILY_TOKENS", "120000"))

# --- 学习总开关（2026-10-31 用户要求：开闸）--------------------------------
# 默认开启；要临时停学时设 ROBOT_LEARNING_PAUSED=1。
LEARNING_PAUSED = os.environ.get("ROBOT_LEARNING_PAUSED", "0") != "0"

# --- 自主学习（服务内常驻） -------------------------------------------------
# 她不是等用户发话才学：服务开着时每隔一段时间看一次，情绪/记忆/好奇
# 长出了够强的问题，就自己去查；结果只进待审区。
AUTONOMY_INTERVAL_MINUTES = int(os.environ.get(
    "ROBOT_AUTONOMY_INTERVAL_MINUTES", "10"))

# --- 主动开口（手机/桌面通知） ---------------------------------------------
# 2026-10-31 用户要求：让她可以在手机上主动找他聊天、询问。
# 2026-11-01 用户要求：不设固定节律，她想说就说；只留总开关。
# 何时开口由 Chat 自己的由头和状态决定，不再按次数/间隔/夜间卡。
PROACTIVE_ENABLED = os.environ.get("ROBOT_PROACTIVE_ENABLED", "1") != "0"

# --- P4 记忆强化（随时间衰减） ----------------------------------------------
# 召回一次会强化，但记忆越老强化越少，且上次强化留下的影响会退。
# B0 是单次强化的最大增量；BMAX 是强度上限；两个 τ 都以天计。
MEMORY_STRENGTH_B0 = float(os.environ.get("ROBOT_MEMORY_B0", "0.18"))
MEMORY_STRENGTH_BMAX = float(os.environ.get("ROBOT_MEMORY_BMAX", "1.0"))
MEMORY_STRENGTH_TAU_AGE = float(os.environ.get(
    "ROBOT_MEMORY_TAU_AGE_DAYS", "30"))
MEMORY_STRENGTH_TAU_SINCE = float(os.environ.get(
    "ROBOT_MEMORY_TAU_SINCE_DAYS", "14"))
# 旧库没有强化旁表时，只按旧 used 给低起点；不批量回填。
MEMORY_STRENGTH_LEGACY_CAP = float(os.environ.get(
    "ROBOT_MEMORY_LEGACY_CAP", "0.35"))

# --- 自然回应与沉默（P3 N2/N3） --------------------------------------------
# 默认关闭：必须显式打开才改变回复行为；shadow 只记录，enforced 才执行。
# 客观沉默（重复/回声/过期/后台/跨通道/用户安静）与社交沉默分开控制。
RESPONSE_GATE_MODE = (os.environ.get(
    "ROBOT_RESPONSE_GATE_MODE", "off") or "off").strip().lower()
if RESPONSE_GATE_MODE not in ("off", "shadow", "enforced"):
    RESPONSE_GATE_MODE = "off"
SOCIAL_SILENCE = os.environ.get("ROBOT_SOCIAL_SILENCE", "0") != "0"
REPLY_MERGE_WINDOW_MS = int(os.environ.get(
    "ROBOT_REPLY_MERGE_WINDOW_MS", "1500"))
RESPONSE_GATE_LOG = os.environ.get(
    "ROBOT_RESPONSE_GATE_LOG", "1") != "0"
RESPONSE_GATE_DIR = DATA / "response_gate"
RESPONSE_GATE_STATE = RESPONSE_GATE_DIR / "state.json"

# --- 情绪内容与表达调制（2026-11-09 施工方案）------------------------------
# off：保持原行为；shadow：只记录计划，不改提示/输出；enforced：接入提示词。
# 内容与表达可独立关闭；第一版每轮最多一个可选追问。
AFFECT_MODULATION_MODE = (os.environ.get(
    "ROBOT_AFFECT_MODULATION_MODE", "off") or "off").strip().lower()
if AFFECT_MODULATION_MODE not in ("off", "shadow", "enforced"):
    AFFECT_MODULATION_MODE = "off"
AFFECT_CONTENT = os.environ.get("ROBOT_AFFECT_CONTENT", "1") != "0"
AFFECT_STYLE = os.environ.get("ROBOT_AFFECT_STYLE", "1") != "0"
AFFECT_MODULATION_LOG = os.environ.get(
    "ROBOT_AFFECT_MODULATION_LOG", "1") != "0"
try:
    AFFECT_MAX_OPTIONAL_QUESTIONS = int(os.environ.get(
        "ROBOT_AFFECT_MAX_OPTIONAL_QUESTIONS", "1") or "1")
except (TypeError, ValueError):
    AFFECT_MAX_OPTIONAL_QUESTIONS = 1
AFFECT_MODULATION_DIR = DATA / "affect_modulation"

# --- 下位机 -----------------------------------------------------------------
SERIAL_BAUD = 115200
SERIAL_TIMEOUT = 0.05

# --- 状态机 -----------------------------------------------------------------
TICK_HZ = 30
