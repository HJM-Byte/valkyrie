"""A4.2 自检：TTS 生成一句 -> STT -> Chat（临时库）-> TTS -> Opus。

    cd robot
    C:\\robot\\venv\\Scripts\\python.exe -m tools.selftest_body_chat

重点不是“能跑”，而是：
1. 真实 memory.db / sleep.json / learn.db / flow.json 等哈希前后不变；
2. STT 能听出测试句；
3. Chat 回答非空；
4. 回答能编码成可下发的 Opus 包。
"""
from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

from app import body_audio, config as C
from app.body_brain import BodyBrain
from app.tts import TTS

ROOT = Path(__file__).resolve().parent.parent
DATA = C.DATA
PROTECTED = [
    DATA / "memory.db",
    DATA / "memory.db-wal",
    DATA / "memory.db-shm",
    DATA / "sleep.json",
    DATA / "sleep_log.jsonl",
    DATA / "flow.json",
    DATA / "learn.db",
    DATA / "learn.db-wal",
    DATA / "learn.db-shm",
    DATA / "sihai_footprint.jsonl",
    DATA / "learning" / "footprint.jsonl",
    DATA / "proactive.db",
    DATA / "proactive.db-wal",
    DATA / "proactive.db-shm",
]


def snapshot() -> dict[str, str]:
    out: dict[str, str] = {}
    for p in PROTECTED:
        try:
            out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "MISSING"
        except OSError as exc:
            out[str(p)] = f"ERROR:{exc}"
    return out


def main() -> int:
    print("—— A4.2 自检 ——")
    before = snapshot()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    work = DATA / "_selftest_body_chat" / stamp
    brain = BodyBrain(work_dir=work, warm_tts=True)
    tts = TTS(cache_dir=work / "tts_input")
    ok = 0
    fail = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ok   {name}")
        else:
            fail += 1
            print(f"  FAIL {name} {detail}")

    try:
        t0 = time.time()
        brain.prepare()
        check("大脑就绪", brain.ready)
        check("临时库不在真实 data 根", str(brain.root).startswith(str(DATA)))

        phrase = "你好，你是谁？"
        src = tts.synth(phrase)
        pcm = body_audio.decode_audio_file(src)
        check("测试音频有 PCM", pcm.size > 8000, f"samples={pcm.size}")

        stt = brain.transcribe_sync(pcm)
        text = str(stt.get("text") or "").strip()
        print(f"  STT   {text!r}  ({stt.get('seconds')}s)")
        check("STT 非空", bool(text), str(stt))
        check("STT 认出了大意", ("你好" in text or "谁" in text), repr(text))

        ans = brain.answer_sync(text)
        reply = str(ans.get("reply") or "")
        print(f"  CHAT  {reply!r}  ({ans.get('chat_sec')}s)")
        check("Chat 回答非空", bool(reply), repr(ans))
        check("Chat 没把 markdown 带进朗读", "**" not in reply and "```" not in reply)

        audio = brain.synth_sync(reply)
        check("TTS 产出 Opus", bool(audio.get("packets")),
              f"packets={len(audio.get('packets') or [])}")
        check("Opus 包是 bytes", all(isinstance(p, bytes) and p for p in audio.get("packets", [])))

        print(f"  总耗时 {time.time() - t0:.1f}s，临时库 {brain.root}")
    except Exception as exc:
        fail += 1
        import traceback
        traceback.print_exc()
        print(f"  FAIL 异常 {type(exc).__name__}: {exc}")
    finally:
        try:
            tts.close()
        except Exception:
            pass
        try:
            brain.close()
        except Exception:
            pass

    after = snapshot()
    changed = [p for p in before if before[p] != after.get(p)]
    if changed:
        fail += 1
        print("  FAIL 真实数据被改动：")
        for p in changed:
            print(f"       {p}\n         before={before[p]}\n         after ={after.get(p)}")
    else:
        ok += 1
        print("  ok   真实 memory/sleep/learn/flow/footprint 哈希全部不变")

    print(f"\n结果：{ok} ok / {fail} fail")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
