"""命令行入口。

    python -m app                 # 跑完整流水线（有麦克风才可用）
    python -m app --no-camera     # 只测语音
    python -m app --say "你好"     # 只合成并播放一句话，测 TTS
    python -m app --probe         # 只打印各部件状态，不进主循环
"""
from __future__ import annotations

import argparse
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app", description="瓦尔基里视觉语音原型")
    ap.add_argument("--no-camera", action="store_true", help="不启用视觉")
    ap.add_argument("--no-link", action="store_true", help="不启用下位机串口")
    ap.add_argument("--no-tts", action="store_true", help="不播报")
    ap.add_argument("--turns", type=int, default=None, help="跑几轮后退出")
    ap.add_argument("--say", type=str, default=None, help="合成并播放指定文本")
    ap.add_argument("--probe", action="store_true", help="只打印状态")
    ap.add_argument("--mic", action="store_true", help="诊断麦克风：权限、设备、读帧")
    ap.add_argument("--mic-settings", action="store_true",
                    help="打开 Windows 麦克风隐私设置页")
    ap.add_argument("--response-gate", choices=("off", "shadow", "enforced"),
                    default=None,
                    help="自然回应闸门：off 保持原行为，shadow 只记录，enforced 执行")
    ap.add_argument("--social-silence", choices=("off", "on"), default=None,
                    help="社交性沉默（自然结束/无新价值）")
    args = ap.parse_args(argv)
    if args.response_gate:
        from . import config as C
        C.RESPONSE_GATE_MODE = args.response_gate
    if args.social_silence:
        from . import config as C
        C.SOCIAL_SILENCE = args.social_silence == "on"

    if args.mic_settings:
        from .audio import open_microphone_settings
        ok = open_microphone_settings()
        print("已打开 Windows 麦克风隐私设置" if ok else "打开设置失败，请手动打开 设置 > 隐私和安全性 > 麦克风")
        return 0 if ok else 2

    if args.mic:
        from .audio import diagnose_microphone
        r = diagnose_microphone()
        perm = r["permission"]
        print("=" * 62)
        print("麦克风诊断")
        print("=" * 62)
        print(f"系统权限 : {perm['detail']}")
        print(f"设置页   : {perm['settings_uri']}")
        print(f"输入设备 : {len(r['devices'])} 个")
        for d in r["devices"]:
            flags = []
            if d["default"]:
                flags.append("默认")
            if d["skipped"]:
                flags.append("跳过")
            if d["blocked_api"]:
                flags.append("WDM-KS 不支持阻塞读")
            print(f"  [{d['index']:>2}] {d['name'][:42]:42s} "
                  f"{d['hostapi'][:18]:18s} {d['default_sr']:.0f}Hz "
                  f"{' '.join(flags)}")
        print(f"选中     : {r['chosen_label']}")
        print(f"打开     : {'成功' if r['open'] else '失败'}")
        if r["open"]:
            print(f"实际设备 : {r['device_label']}")
            print(f"读帧     : {r['frames']} 帧 / 1.2s，峰值 {r['peak']:.4f}")
        else:
            print(f"失败原因 : {r['last_error']}")
            print("下一步   : 运行 run.cmd --mic-settings 打开隐私设置；"
                  "或检查麦克风是否被 Fn 静音、是否被其它程序占用")
        return 0 if r["open"] else 2

    if args.say:
        from .tts import TTS
        t = TTS()
        r = t.speak(args.say)
        print(f"合成 {r['synth_sec']:.2f}s 播放 {r['play_sec']:.2f}s "
              f"[{r['backend']}] -> {r['file']}")
        return 0

    from . import config as C
    from .pipeline import Pipeline
    use_camera = (not args.no_camera) and not C.VISION_LOCKED
    if C.VISION_LOCKED and not args.no_camera:
        print("视觉锁定：用户要求等新摄像头到货后再开，本轮不打开摄像头。")
    p = Pipeline(use_camera=use_camera, use_link=not args.no_link,
                 use_tts=not args.no_tts)
    mic_ok = p.start()

    if args.probe:
        from .audio import mic_privacy_status
        perm = mic_privacy_status()
        print("\n--- 部件状态 ---")
        print(f"摄像头   : {'可用' if p.camera_ok else '不可用'}")
        print(f"麦克风   : {'可用' if mic_ok else '不可用'}")
        if mic_ok:
            st = p.mic.status()
            print(f"  设备   : {p.mic.device_label()}")
            print(f"  采集   : {st['capture_rate']}Hz -> {st['samplerate']}Hz")
            print(f"  权限   : {perm['detail']}")
        else:
            print(f"  原因   : {p.mic.last_error}")
            print(f"  权限   : {perm['detail']}")
        print(f"STT      : {'已加载' if p.stt else '未加载（模型缺失）'}")
        print(f"TTS      : {'就绪' if p.tts else '不可用'}")
        if p.tts:
            print(f"  {C.BREEZE_VOICE_NAME}: {'可用' if p.tts.breeze_available() else '缺 BREEZE_API_KEY'}")
            print(f"  本地声线: {'可用' if p.tts.local_available() else '缺失'}")
        if p.llm:
            ok, why = p.llm.available()
            print(f"LLM      : {p.llm.model} -> {why}")
        print(f"下位机   : {type(p.link).__name__ if p.link else '未启用'}")
        if p.mem and p.mind:
            st, sn = p.mem.stats(), p.mind.snapshot()
            print(f"记忆库   : 存在 {sn['hours']:.1f}h / {sn['turns']} 轮 / "
                  f"{st['episodes']} 条记忆点（待沉淀 {st['pending']}）/ "
                  f"成长{'冻结' if sn['growth'] == 'frozen' else '已开启'}")
        p.stop()
        return 0

    if not mic_ok:
        p.stop()
        print("\n麦克风不可用，完整语音链路无法启动。")
        print("先跑 run.cmd --mic 诊断设备；权限页跑 run.cmd --mic-settings。")
        print("文字入口仍可用：chat.cmd。")
        return 2

    p.run(max_turns=args.turns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
