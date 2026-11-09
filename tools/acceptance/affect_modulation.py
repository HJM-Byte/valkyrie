"""A01-A14：情绪内容与表达调制的确定性验收。

不调用模型、不连接生产库、不生成回复文本。每个 case 只断言计划：
    required_intents / eligible_claim_ids / required_notices 是否原样保留
    content_plan / style_plan 是否符合场景与强度规则
    是否出现额外模型调用、关系写入或沉默被重新打开

用法：
    python -m tools.acceptance.affect_modulation
    python -m tools.acceptance.affect_modulation --list
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from app.affect_modulator import (  # noqa: E402
    AffectContext, BaseTurnPlan, modulate, prompt_block,
)
from .runner import ProductionGuard  # noqa: E402

CASES_DIR = HERE / "cases"
OUT_ROOT = ROOT / "data" / "acceptance" / "affect_modulation"
FIELDS = ("applied", "core_intent", "tone", "length", "question_budget",
          "self_disclosure", "scene", "fallback_reason",
          "required_contract_unchanged", "extra_llm_calls")


def _load() -> list[dict]:
    cases: list[dict] = []
    for path in sorted(CASES_DIR.glob("A*.jsonl")):
        for ln, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path.name}:{ln} JSON 不合法: {exc}") from exc
            case["_source"] = f"{path.name}:{ln}"
            cases.append(case)
    return cases


def _ctx(case: dict) -> AffectContext:
    g = case.get("given") or {}
    return AffectContext(
        event_id=str(g.get("event_id") or case["case_id"]),
        self_state=dict(g.get("self_affect") or {}),
        user_state=dict(g.get("user_affect") or {}),
        relationship_climate=dict(g.get("relationship") or {}),
        scene=str(g.get("scene") or "unknown"),
        explicit_style_override=str(g.get("override") or ""),
        response_mode=str(g.get("response_mode") or "respond"),
    )


def _base(case: dict) -> BaseTurnPlan:
    g = case.get("given") or {}
    raw = dict(g.get("base_plan") or {})
    return BaseTurnPlan(
        required_intents=list(raw.get("required_intents") or ["answer"]),
        eligible_claim_ids=list(raw.get("eligible_claim_ids") or []),
        required_notices=list(raw.get("required_notices") or []),
        forbidden_topics=list(raw.get("forbidden_topics") or []),
        base_order=list(raw.get("base_order") or ["answer", "optional"]),
        decision_key=str(raw.get("decision_key") or "answer"),
        core_intent=str(raw.get("core_intent") or "answer"),
    )


def _run_one(case: dict) -> dict:
    g = case.get("given") or {}
    exp = case.get("expect") or {}
    ctx = _ctx(case)
    base = _base(case)
    kwargs = {
        "mode": g.get("mode", "enforced"),
        "content_enabled": g.get("content", True),
        "style_enabled": g.get("style", True),
        "max_optional_questions": g.get("max_questions", 1),
    }
    res = modulate(ctx, base, **kwargs)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: Any = "") -> None:
        checks.append({"name": name, "pass": bool(ok), "detail": str(detail)})

    for field in FIELDS:
        if field not in exp:
            continue
        if field == "core_intent":
            got = res.content_plan.core_intent
        elif field == "self_disclosure":
            got = res.content_plan.self_disclosure
        elif field in ("tone", "length"):
            got = getattr(res.style_plan, field)
        elif field == "question_budget":
            got = res.content_plan.question_budget
        else:
            got = getattr(res, field)
        add(field, got == exp[field], f"{got!r} != {exp[field]!r}")

    for field, got in (
        ("required_intents", res.content_plan.required_intents),
        ("eligible_claim_ids", res.content_plan.eligible_claim_ids),
        ("required_notices", res.content_plan.required_notices),
    ):
        if field in exp:
            add(field, sorted(got) == sorted(exp[field]),
                f"{sorted(got)} != {sorted(exp[field])}")

    if "content_order" in exp:
        add("content_order", list(res.content_plan.content_order) == list(exp["content_order"]),
            res.content_plan.content_order)
    if "optional_intents" in exp:
        add("optional_intents", list(res.content_plan.optional_intents) == list(exp["optional_intents"]),
            res.content_plan.optional_intents)
    if "prompt_empty" in exp:
        block = prompt_block(res)
        add("prompt_empty", (not block) == bool(exp["prompt_empty"]), block[:80])
    if exp.get("cross_channel_equal"):
        res2 = modulate(ctx, base, **kwargs)
        add("cross_channel_equal", res.as_dict() == res2.as_dict(), "same context replay")
    if exp.get("no_extra_llm"):
        add("no_extra_llm", res.extra_llm_calls == 0, res.extra_llm_calls)
    if exp.get("no_relationship_writes"):
        add("no_relationship_writes", res.affect_relationship_writes == 0,
            res.affect_relationship_writes)

    # 禁止副作用：模块没有生成文本、动作或 TTS，也没有把 silent/defer 变成可见回复。
    forbidden = case.get("forbidden") or {}
    if forbidden.get("affect_relationship_writes"):
        add("forbidden_affect_relationship_writes",
            res.affect_relationship_writes == 0, res.affect_relationship_writes)
    if forbidden.get("extra_llm_calls"):
        add("forbidden_extra_llm_calls", res.extra_llm_calls == 0,
            res.extra_llm_calls)
    if forbidden.get("punitive_silence"):
        add("forbidden_punitive_silence",
            res.applied is False if ctx.response_mode in ("silent", "defer")
            else "silent" not in prompt_block(res).lower(),
            res.fallback_reason)
    if forbidden.get("empty_assistant_turns") and ctx.response_mode in ("silent", "defer"):
        add("forbidden_empty_assistant_turns", not res.applied, res.fallback_reason)

    return {
        "case_id": case["case_id"],
        "suite": case.get("suite", ""),
        "title": case.get("title", ""),
        "source": case.get("_source", ""),
        "pass": all(c["pass"] for c in checks),
        "checks": checks,
        "result": res.as_dict(),
        "prompt_block": prompt_block(res),
    }


def run_all(out_root: Path | None = None) -> dict:
    cases = _load()
    guard = ProductionGuard()
    rows = [_run_one(c) for c in cases]
    after = guard.compare()
    passed = sum(1 for r in rows if r["pass"])
    ok = passed == len(rows) and after.get("pass")
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "total": len(rows), "passed": passed, "failed": len(rows) - passed,
        "pass": bool(ok), "production_guard": after, "cases": rows,
    }


def write_outputs(payload: dict, out_root: Path | None = None) -> tuple[Path, Path]:
    out = Path(out_root) if out_root else OUT_ROOT
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out / f"run_{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    lines = [
        "# 情绪内容与表达调制验收",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 合计：{payload['total']}，通过：{payload['passed']}，失败：{payload['failed']}",
        f"- ProductionGuard：{'PASS' if payload['production_guard'].get('pass') else 'FAIL'}",
        "",
        "| 用例 | 标题 | 结果 | 失败项 |",
        "|---|---|---|---|",
    ]
    for row in payload["cases"]:
        failed = [c["name"] for c in row["checks"] if not c["pass"]]
        lines.append(
            f"| {row['case_id']} | {row['title']} | "
            f"{'PASS' if row['pass'] else 'FAIL'} | {'、'.join(failed) or '—'} |")
    report = out / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report, json_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列用例")
    ap.add_argument("--out", default="", help="输出目录")
    a = ap.parse_args(argv)
    cases = _load()
    if a.list:
        for c in cases:
            print(f"{c['case_id']:<36} {c.get('title', '')}")
        return 0
    payload = run_all(Path(a.out) if a.out else None)
    report, raw = write_outputs(payload, Path(a.out) if a.out else None)
    for row in payload["cases"]:
        print(("  ✓ " if row["pass"] else "  ✗ ") + row["case_id"])
        for c in row["checks"]:
            if not c["pass"]:
                print(f"      {c['name']}: {c['detail']}")
    print(f"\n合计 {payload['total']} / 通过 {payload['passed']} / 失败 {payload['failed']}")
    print(f"报告：{report}")
    print(f"明细：{raw}")
    print(f"ProductionGuard：{'PASS' if payload['production_guard'].get('pass') else 'FAIL'}")
    return 0 if payload["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
