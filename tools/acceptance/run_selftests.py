"""跑旧自检并记录基线数字。

只跑指定的 5 个自检，全部用临时库；不启动服务、不联网。
输出 `data/acceptance/baseline_selftests_YYYYMMDD.json`。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "acceptance"
MODULES = [
    "tools.selftest_autonomy",
    "tools.selftest_memory",
    "tools.selftest_learning",
    "tools.selftest_school",
    "tools.selftest_mind",
]


def parse_counts(text: str) -> tuple[int, int, str]:
    passed = failed = 0
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("PASS ", "✓", "PASS\t")):
            passed += 1
        elif line.startswith(("FAIL ", "✗", "FAIL\t")):
            failed += 1

    summary = ""
    patterns = [
        r"(\d+)\s*通过\s*/\s*(\d+)\s*失败",
        r"(\d+)\s*通过[，,]\s*(\d+)\s*失败",
        r"(\d+)\s*/\s*(\d+)\s*通过",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            summary = m.group(0)
            if len(m.groups()) == 2 and "通过" in summary and "/" in summary and "失败" not in summary:
                # 形如 8/8 通过：第一个是成功数，第二个是总数
                passed = int(m.group(1))
                failed = int(m.group(2)) - int(m.group(1))
            else:
                passed = int(m.group(1))
                failed = int(m.group(2))
            break
    return passed, failed, summary


def run_one(module: str, timeout: int) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", module],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        passed, failed, summary = parse_counts(out)
        return {
            "module": module,
            "exit_code": proc.returncode,
            "pass": passed,
            "fail": failed,
            "summary": summary,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "output_tail": out.splitlines()[-80:],
            "output_chars": len(out),
        }
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return {
            "module": module,
            "exit_code": None,
            "pass": 0,
            "fail": 0,
            "summary": f"TIMEOUT {timeout}s",
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "output_tail": out.splitlines()[-40:],
            "output_chars": len(out),
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑旧自检并记录基线")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--out", default="")
    ap.add_argument("--modules", nargs="*", default=MODULES)
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = (Path(args.out) if args.out
           else OUT_DIR / f"baseline_selftests_{datetime.now():%Y%m%d}.json")
    results = []
    for module in args.modules:
        print(f"== {module} ==")
        result = run_one(module, args.timeout)
        results.append(result)
        print(f"  exit={result['exit_code']} pass={result['pass']} "
              f"fail={result['fail']} {result['summary']} "
              f"{result['duration_ms']}ms")
        # 明细已写进 JSON；控制台不打印可能撞 GBK 的勾选符号。

    summary = {
        "modules": len(results),
        "pass": sum(r["pass"] for r in results),
        "fail": sum(r["fail"] for r in results),
        "exit_nonzero": sum(1 for r in results if r["exit_code"] not in (0,)),
        "all_ok": all(r["exit_code"] == 0 and r["fail"] == 0 for r in results),
    }
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": sys.executable,
        "root": str(ROOT),
        "summary": summary,
        "results": results,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out), **summary}, ensure_ascii=False, indent=2))
    return 0 if summary["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
