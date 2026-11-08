"""音频链路自检：麦克风权限/设备 -> TTS 合成 -> 解码 -> STT 识别。

麦克风只测读帧，不需要人说话。TTS 优先级：BreezeBlue 寒蝉余音 / 幽兰影（有
BREEZE_API_KEY 时）-> edge -> 本地 VITS。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np  # noqa: E402


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return x
    n_out = int(round(len(x) * sr_out / sr_in))
    idx = np.linspace(0, len(x) - 1, n_out)
    return np.interp(idx, np.arange(len(x)), x).astype(np.float32)


def main() -> int:
    print("=" * 62)
    print("音频链路自检")
    print("=" * 62)

    # 1. soundfile / mp3 能力
    try:
        import soundfile as sf
        print(f"soundfile {sf.__version__}  libsndfile {sf.__libsndfile_version__}")
        fmts = [k.lower() for k in sf.available_formats()]
        print(f"  mp3 解码: {'支持' if 'mp3' in fmts else '不支持'}")
    except Exception as e:
        print(f"soundfile 不可用: {e}")
        sf = None

    # 2. 设备
    import sounddevice as sd
    ins = [(i, d) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    outs = [(i, d) for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]
    print(f"\n输入设备 {len(ins)} 个:")
    for i, d in ins[:6]:
        print(f"  [{i}] {d['name'][:52]:52s} ch={d['max_input_channels']}")
    print(f"输出设备 {len(outs)} 个:")
    for i, d in outs[:4]:
        print(f"  [{i}] {d['name'][:52]:52s} ch={d['max_output_channels']}")
    print(f"默认 输入={sd.default.device[0]} 输出={sd.default.device[1]}")

    # 3. 麦克风能否打开并读到帧
    from app.audio import Mic, mic_privacy_status
    perm = mic_privacy_status()
    print(f"\n麦克风权限: {perm['detail']}")
    mic = Mic()
    ok = mic.open()
    print(f"麦克风打开: {'成功' if ok else '失败'}")
    if ok:
        print(f"  设备: {mic.device_label()}")
        mic.drain()
        got = []
        t0 = time.time()
        while time.time() - t0 < 1.2:
            f = mic.read_frame(timeout=0.3)
            if f is not None:
                got.append(f)
        mic.close()
        n = sum(len(x) for x in got)
        print(f"  1.2 秒内读到 {len(got)} 帧 / {n} 采样 (期望约 40 帧 / 19200)")
        if got:
            peak = max(float(np.abs(x).max()) for x in got)
            print(f"  峰值 {peak:.0f} / 32768")
    else:
        print(f"  !! 没有可用麦克风，语音输入不可用: {mic.last_error}")

    # 4. TTS 合成
    from app.tts import TTS
    SENT = "你好，我是瓦尔基里。今天的天气很适合出门。"
    print(f"\nTTS 合成: {SENT}")
    tts = TTS()
    t0 = time.time()
    try:
        wav = tts.synth(SENT)
        print(f"  OK [{tts.last_backend}] {wav.name} "
              f"{wav.stat().st_size/1024:.0f}KB in {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"  FAIL {type(e).__name__}: {e}")
        return 2

    # 5. 解码 mp3/wav -> 16k mono float
    pcm16 = None
    if sf is not None:
        try:
            data, sr = sf.read(str(wav), dtype="float32", always_2d=True)
            mono = data[:, 0]
            pcm16 = (resample(mono, sr, 16000) * 32768).astype(np.int16)
            print(f"  解码 sr={sr} -> 16k，{len(pcm16)/16000:.2f}s")
        except Exception as e:
            print(f"  解码失败: {type(e).__name__}: {e}")

    # 6. STT 识别刚合成的语音
    if pcm16 is not None:
        from app.stt import STT
        stt = STT()
        r = stt.transcribe(pcm16)
        print(f"\nSTT 识别 -> {r['text']!r}")
        print(f"  耗时 {r['seconds']:.2f}s / 音频 {r['audio_sec']:.2f}s "
              f"(RTF {r['seconds']/max(r['audio_sec'],0.01):.2f})")
        hit = sum(1 for ch in "天气出门瓦尔基里" if ch in r["text"])
        print(f"  关键字命中 {hit}/{len('天气出门瓦尔基里')}  {'OK' if hit >= 3 else '偏差较大'}")

    # 7. 静音幻觉回归
    from app.stt import STT as _
    sil = np.zeros(16000, dtype=np.int16)
    r = stt.transcribe(sil)
    print(f"\n静音输入 -> {r['text']!r} filtered={r.get('filtered')} "
          f"{'OK（已挡住）' if r['text'] == '' else '!! 仍有幻觉'}")

    print("\n=== 音频链路自检结束 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
