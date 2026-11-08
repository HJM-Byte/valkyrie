"""音频：麦克风采集 + 端点检测（VAD）。

选 webrtcvad 而不是 silero：它没有模型文件、CPU 占用极低、延迟确定，
用来做"人在说话 / 说完了"这种粗判足够。真要高精度再换 silero。

2026-10-31 麦克风实测：
- Windows 隐私权限本来就是 Allow，问题不在权限；
- C920 内置麦克风在 MME 16k 下可读，默认输入已经是它；
- Realtek 阵列仍可能阻塞，所以这里优先选 C920/MME，打开失败自动换设备/换采样率，
  并把最后一次错误留在 last_error 里，不再静默失败。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
import webrtcvad

from . import config as C


# --------------------------------------------------------------------------- #
# 设备枚举与选择
# --------------------------------------------------------------------------- #
_SKIP_NAME_PARTS = (
    "声音映射器", "Sound Mapper", "主声音捕获",
    "Primary Sound Capture", "Microsoft 声音映射器",
)

_HOST_PREF = {
    "MME": 0,
    "Windows DirectSound": 1,
    "Windows WASAPI": 2,
    "Windows WDM-KS": 9,
}


def _hostapi_name(hostapi_index: int) -> str:
    try:
        return str(sd.query_hostapis(int(hostapi_index))["name"])
    except Exception:
        return ""


def default_input() -> Optional[int]:
    try:
        value = sd.default.device[0]
        return int(value) if value is not None and int(value) >= 0 else None
    except Exception:
        return None


def _is_skipped(name: str) -> bool:
    return any(part in name for part in _SKIP_NAME_PARTS)


def list_input_devices() -> list[dict]:
    """列出所有输入设备，附带 host API 和是否默认设备。"""
    out: list[dict] = []
    default_in = default_input()
    try:
        devs = sd.query_devices()
    except Exception:
        return out
    for i, d in enumerate(devs):
        try:
            channels = int(d.get("max_input_channels", 0))
        except Exception:
            channels = 0
        if channels <= 0:
            continue
        name = str(d.get("name") or "")
        api = _hostapi_name(d.get("hostapi", -1))
        out.append({
            "index": i,
            "name": name,
            "channels": channels,
            "default_sr": float(d.get("default_samplerate") or 0.0),
            "hostapi": api,
            "default": (default_in is not None and i == default_in),
            "skipped": _is_skipped(name),
            "blocked_api": api.endswith("WDM-KS"),
        })
    return out


def _device_score(d: dict, preferred: Optional[str]) -> int:
    name = str(d["name"])
    score = 0
    if d.get("default"):
        score += 80
    if preferred and preferred.lower() in name.lower():
        score += 100
    if "麦克风" in name or "microphone" in name.lower():
        score += 10
    api = str(d.get("hostapi") or "")
    score += {0: 50, 1: 25, 2: 10}.get(_HOST_PREF.get(api, -1), 0)
    if api.endswith("WDM-KS"):
        score -= 80
    return score


def resolve_input_device(preferred: Optional[str] = None) -> Optional[int]:
    """挑一个最可能可用的输入设备。

    明确指定 `ROBOT_MIC_DEVICE`（序号或名称片段）时优先用它；
    否则优先 C920/MME，再默认设备，再其它输入设备。
    """
    wanted = str(preferred or C.MIC_DEVICE or "").strip()

    # 1) 环境变量/参数里明确给序号
    if wanted.isdigit():
        idx = int(wanted)
        for d in list_input_devices():
            if d["index"] == idx:
                return idx
        return None

    devices = [d for d in list_input_devices() if not d["skipped"]]
    if not devices:
        return None

    # 2) 环境变量/参数里明确给名称片段
    if wanted:
        for d in devices:
            if wanted.lower() in str(d["name"]).lower():
                return int(d["index"])

    # 3) 自动：名称优先（默认 C920），然后 host API
    prefer = str(preferred or C.MIC_PREFER or "").strip() or None
    ranked = sorted(devices, key=lambda d: (_device_score(d, prefer), -d["index"]),
                    reverse=True)
    return int(ranked[0]["index"]) if ranked else None


def device_label(index: Optional[int]) -> str:
    if index is None:
        return "未选到"
    try:
        d = sd.query_devices(int(index))
        return f"[{int(index)}] {d['name']} / {_hostapi_name(d['hostapi'])}"
    except Exception:
        return f"[{index}] 未知设备"


# --------------------------------------------------------------------------- #
# Windows 隐私权限
# --------------------------------------------------------------------------- #
_MIC_CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
_APP_PRIVACY = r"SOFTWARE\Policies\Microsoft\Windows\AppPrivacy"


def _reg_value(root, path: str, name: str):
    try:
        import winreg
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
        return value
    except Exception:
        return None


def mic_privacy_status() -> dict:
    """读 Windows 麦克风隐私开关；只读，不改系统设置。

    返回 allowed: True/False/None（None 表示读不到，以实际打开结果为准）。
    """
    result = {
        "platform": sys.platform,
        "allowed": None,
        "global": "unknown",
        "desktop": "unknown",
        "policy": "unknown",
        "settings_uri": "ms-settings:privacy-microphone",
        "detail": "",
    }
    if sys.platform != "win32":
        result.update(allowed=True, detail="非 Windows，跳过系统隐私检查")
        return result

    try:
        import winreg
        global_value = _reg_value(
            winreg.HKEY_CURRENT_USER, _MIC_CONSENT, "Value")
        desktop_value = _reg_value(
            winreg.HKEY_CURRENT_USER, _MIC_CONSENT + r"\NonPackaged", "Value")
        policy_value = _reg_value(
            winreg.HKEY_LOCAL_MACHINE, _APP_PRIVACY, "LetAppsAccessMicrophone")
    except Exception as e:
        result["detail"] = f"读取隐私设置失败: {type(e).__name__}: {e}"
        return result

    result["global"] = str(global_value) if global_value is not None else "missing"
    result["desktop"] = str(desktop_value) if desktop_value is not None else "missing"
    result["policy"] = str(policy_value) if policy_value is not None else "missing"

    if str(policy_value) == "2":
        result["allowed"] = False
        result["detail"] = "组策略禁止了麦克风访问"
    elif str(global_value) == "Deny" or str(desktop_value) == "Deny":
        result["allowed"] = False
        result["detail"] = "Windows 隐私设置里麦克风被关闭"
    elif str(policy_value) == "1" or str(global_value) == "Allow" or str(desktop_value) == "Allow":
        result["allowed"] = True
        result["detail"] = (
            f"系统麦克风 Allow（桌面应用 {result['desktop']}）")
    else:
        result["detail"] = "读不到隐私开关；以实际打开麦克风的结果为准"
    return result


def open_microphone_settings() -> bool:
    """打开 Windows 设置里的麦克风隐私页，让用户自己点允许。"""
    if sys.platform != "win32":
        return False
    uri = "ms-settings:privacy-microphone"
    try:
        os.startfile(uri)  # type: ignore[attr-defined]
        return True
    except Exception:
        try:
            import subprocess
            subprocess.Popen(["cmd", "/c", "start", "", uri], shell=False)
            return True
        except Exception:
            return False


# --------------------------------------------------------------------------- #
# 采集
# --------------------------------------------------------------------------- #
class Mic:
    """打开麦克风，把音频统一成 16k/mono/int16、按 30ms 帧吐出来。

    - 默认自动选 C920/MME；打不开会换设备、换采样率，并重试一次瞬时失败。
    - 设备只支持 44.1k/48k 时，在回调里线性重采样到 16k；上层不用改。
    """

    def __init__(self, device=None,
                 samplerate: int = C.SAMPLE_RATE,
                 frame_samples: int = C.FRAME_SAMPLES,
                 strict: bool = False) -> None:
        self.requested_device = device
        self.device: Optional[int] = None
        self.samplerate = int(samplerate)
        self.frame_samples = int(frame_samples)
        self.capture_rate = int(samplerate)
        self.strict = bool(strict)
        self.device_name = ""
        self.hostapi = ""
        self.last_error = ""
        self._stream: Optional[sd.InputStream] = None
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=64)
        self._attempts: list[dict] = []

    # ---- 回调与重采样 -----------------------------------------------------
    def _to_16k(self, frame: np.ndarray) -> np.ndarray:
        if self.capture_rate == self.samplerate or frame.size == 0:
            return frame.astype(np.int16, copy=False)
        n_out = self.frame_samples
        idx = np.linspace(0, frame.size - 1, n_out)
        return np.interp(idx, np.arange(frame.size),
                         frame.astype(np.float32)).astype(np.int16)

    def _cb(self, indata, frames, time_info, status) -> None:
        try:
            frame = indata[:, 0] if getattr(indata, "ndim", 1) > 1 else indata
            frame = self._to_16k(frame)
            self._q.put_nowait(np.asarray(frame, dtype=np.int16).copy())
        except queue.Full:
            pass
        except Exception:
            # 回调里抛异常会让 PortAudio 直接停流，不能让它逃出去
            pass

    # ---- 候选设备 ---------------------------------------------------------
    def _candidate_devices(self) -> list[int]:
        out: list[int] = []
        if self.requested_device is not None:
            if isinstance(self.requested_device, str):
                if self.requested_device.isdigit():
                    out.append(int(self.requested_device))
                else:
                    found = resolve_input_device(self.requested_device)
                    if found is not None:
                        out.append(found)
            elif isinstance(self.requested_device, int):
                out.append(self.requested_device)
        if self.strict:
            return list(dict.fromkeys(out))

        picked = resolve_input_device()
        if picked is not None:
            out.append(picked)
        default_in = default_input()
        if default_in is not None:
            out.append(default_in)
        ranked = sorted(
            [d for d in list_input_devices() if not d["skipped"]],
            key=lambda d: (_device_score(d, C.MIC_PREFER), -d["index"]),
            reverse=True,
        )
        out.extend(int(d["index"]) for d in ranked[:4])
        return list(dict.fromkeys(out))

    # ---- 打开 -------------------------------------------------------------
    def _try_open(self, device: int, rate: int, frame_samples: int,
                  timeout: float):
        result: list = []
        state = {"cancelled": False}

        def _do() -> None:
            stream = None
            try:
                stream = sd.InputStream(
                    device=device,
                    channels=C.CHANNELS,
                    samplerate=rate,
                    blocksize=frame_samples,
                    dtype="int16",
                    callback=self._cb,
                )
                stream.start()
                if state["cancelled"]:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass
                    return
                result.append(stream)
            except Exception as e:
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
                result.append(e)

        t = threading.Thread(target=_do, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            state["cancelled"] = True
            return False, f"打开超过 {timeout:.1f}s", None
        if not result:
            return False, "没有返回结果", None
        val = result[0]
        if isinstance(val, Exception):
            return False, f"{type(val).__name__}: {val}", None
        return True, "", val

    def open(self, timeout: Optional[float] = None,
             retries: Optional[int] = None) -> bool:
        """打开麦克风；失败会依次尝试候选设备/采样率并重试瞬时失败。"""
        if self._stream is not None:
            return True
        timeout = float(C.MIC_OPEN_TIMEOUT if timeout is None else timeout)
        retries = int(C.MIC_OPEN_RETRIES if retries is None else retries)
        self.last_error = ""
        self._attempts = []

        for device in self._candidate_devices():
            try:
                info = sd.query_devices(int(device))
                default_sr = int(float(info.get("default_samplerate") or self.samplerate))
            except Exception as e:
                self._attempts.append({"device": device, "rate": None,
                                       "error": f"query: {type(e).__name__}: {e}"})
                continue

            configs = [(self.samplerate, self.frame_samples)]
            if default_sr > 0 and default_sr != self.samplerate:
                frames = max(1, int(default_sr * C.FRAME_MS / 1000))
                configs.append((default_sr, frames))

            for rate, frame_samples in configs:
                self.capture_rate = int(rate)
                for attempt in range(max(1, retries)):
                    ok, err, stream = self._try_open(
                        int(device), int(rate), int(frame_samples), timeout)
                    self._attempts.append({
                        "device": device, "rate": rate, "attempt": attempt + 1,
                        "error": err,
                    })
                    if ok:
                        self.device = int(device)
                        self.capture_rate = int(rate)
                        self._stream = stream
                        try:
                            self.device_name = str(info.get("name") or "")
                            self.hostapi = _hostapi_name(info.get("hostapi", -1))
                        except Exception:
                            pass
                        self.last_error = ""
                        return True
                    self.last_error = f"[{device}] {rate}Hz: {err}"
                    if "Invalid sample rate" in err:
                        break
                    time.sleep(0.08)
        if not self.last_error:
            self.last_error = "没有找到可用输入设备"
        return False

    # ---- 读取 -------------------------------------------------------------
    def read_frame(self, timeout: float = 0.5) -> Optional[np.ndarray]:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def device_label(self) -> str:
        return device_label(self.device)

    def status(self) -> dict:
        return {
            "open": self._stream is not None,
            "device": self.device,
            "device_name": self.device_name,
            "hostapi": self.hostapi,
            "capture_rate": self.capture_rate,
            "samplerate": self.samplerate,
            "last_error": self.last_error,
            "attempts": self._attempts[-6:],
        }

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            finally:
                self._stream = None


# --------------------------------------------------------------------------- #
# 诊断
# --------------------------------------------------------------------------- #
def diagnose_microphone(preferred: Optional[str] = None,
                        seconds: float = 1.2) -> dict:
    """给 CLI 用的麦克风诊断：权限、设备列表、选中设备、读帧结果。"""
    permission = mic_privacy_status()
    devices = list_input_devices()
    chosen = resolve_input_device(preferred)
    report = {
        "permission": permission,
        "devices": devices,
        "chosen": chosen,
        "chosen_label": device_label(chosen),
        "open": False,
        "device": None,
        "device_label": "未选到",
        "frames": 0,
        "peak": 0.0,
        "last_error": "",
        "attempts": [],
    }

    mic = Mic(device=chosen, strict=chosen is not None)
    if chosen is None:
        mic = Mic()
    ok = mic.open()
    if not ok and chosen is not None:
        # 指定设备打不开时，再走一次自动选择，别把能用的设备一起放弃。
        mic = Mic()
        ok = mic.open()

    report.update({
        "open": bool(ok),
        "device": mic.device,
        "device_label": mic.device_label(),
        "last_error": mic.last_error,
        "attempts": mic.status().get("attempts", []),
    })

    if ok:
        mic.drain()
        frames = 0
        peak = 0.0
        t0 = time.time()
        while time.time() - t0 < seconds:
            frame = mic.read_frame(timeout=0.3)
            if frame is None:
                continue
            frames += 1
            peak = max(peak, float(np.abs(frame).max()) / 32768.0)
        report["frames"] = frames
        report["peak"] = round(peak, 4)
        mic.close()
    return report


# --------------------------------------------------------------------------- #
# 端点检测：等到开口 -> 录到静音为止
# --------------------------------------------------------------------------- #
class Endpointer:
    def __init__(self, aggressiveness: int = C.VAD_AGGRESSIVENESS,
                 samplerate: int = C.SAMPLE_RATE) -> None:
        self.vad = webrtcvad.Vad(aggressiveness)
        self.samplerate = samplerate

    def is_speech(self, frame: np.ndarray) -> bool:
        try:
            return self.vad.is_speech(frame.tobytes(), self.samplerate)
        except Exception:
            return False


def record_utterance(mic: Mic, ep: Endpointer,
                     max_sec: float = C.VAD_MAX_RECORD_SEC,
                     silence_frames: int = C.VAD_SILENCE_FRAMES,
                     min_speech_frames: int = C.VAD_MIN_SPEECH_FRAMES,
                     wait_speech_sec: float = 6.0,
                     on_level: Optional[Callable[[float, bool], None]] = None,
                     ) -> Optional[np.ndarray]:
    """阻塞录音：开口后开始录，静音超过阈值就收尾。

    返回 int16 numpy 数组；没听到人声返回 None。
    """
    if mic._stream is None and not mic.open():
        return None

    ep = ep or Endpointer()
    mic.drain()

    # 阶段 1：等开口
    t_start = time.time()
    pre: list[np.ndarray] = []
    speech_run = 0
    started = False
    while time.time() - t_start < wait_speech_sec:
        frame = mic.read_frame(timeout=0.3)
        if frame is None:
            continue
        speech = ep.is_speech(frame)
        if on_level is not None:
            on_level(float(np.abs(frame).mean()) / 32768.0, speech)
        pre.append(frame)
        if len(pre) > 10:          # 只留最近 10 帧（300ms）作为前置缓冲
            pre.pop(0)
        speech_run = speech_run + 1 if speech else 0
        if speech_run >= 3:
            started = True
            break

    if not started:
        return None

    # 阶段 2：录到安静
    frames: list[np.ndarray] = list(pre)
    silence = 0
    speech_frames = speech_run
    t_rec = time.time()
    while time.time() - t_rec < max_sec:
        frame = mic.read_frame(timeout=0.5)
        if frame is None:
            silence += 1
            if silence >= silence_frames:
                break
            continue
        speech = ep.is_speech(frame)
        if on_level is not None:
            on_level(float(np.abs(frame).mean()) / 32768.0, speech)
        frames.append(frame)
        if speech:
            speech_frames += 1
            silence = 0
        else:
            silence += 1
            if silence >= silence_frames:
                break

    if speech_frames < min_speech_frames:
        return None
    return np.concatenate(frames)


# --------------------------------------------------------------------------- #
# 播放
# --------------------------------------------------------------------------- #
def play_pcm(samples: np.ndarray, samplerate: int = C.SAMPLE_RATE) -> None:
    """直接播 int16 PCM（用于调试音）。"""
    sd.play(samples, samplerate=samplerate, blocking=True)


def to_float32(pcm16: np.ndarray) -> np.ndarray:
    return (pcm16.astype(np.float32) / 32768.0).clip(-1.0, 1.0)


def wav_write(path: str, pcm16: np.ndarray, samplerate: int = C.SAMPLE_RATE) -> None:
    """不依赖 scipy 的极简 16-bit 单声道 wav 写入。"""
    import struct
    data = pcm16.astype(np.int16).tobytes()
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(data)))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, samplerate, samplerate * 2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", len(data)))
        f.write(data)
