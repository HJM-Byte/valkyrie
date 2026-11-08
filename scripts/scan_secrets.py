#!/usr/bin/env python3
"""扫描仓库里可能被误提交的密钥。

只读、无第三方依赖。CI 和本地都可以跑：

    python scripts/scan_secrets.py

发现疑似项时退出码为 1；只打印文件名、行号和掩码后的片段，不打印完整值。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".venv",
    "data",
    "models",
    "backups",
    "docs",
    "diary",
    "_tmp",
    "__pycache__",
    "app/pocket_apk",
}
TEXT_SUFFIXES = {
    ".py", ".ps1", ".cmd", ".bat", ".md", ".txt", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".html", ".js", ".css", ".xml", ".csv", ".env",
}

PATTERNS = [
    ("阿里云 AccessKey", re.compile(r"\b(LTAI[A-Za-z0-9]{12,}|AKID[A-Za-z0-9]{12,})\b")),
    ("OpenAI/兼容 Key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub Token", re.compile(r"\b(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("赋值式 Key", re.compile(
        r"(?i)\b(?:DASHSCOPE|BREEZE|OPENAI|ANTHROPIC|GEMINI|SERPAPI|TAVILY)_(?:API_)?KEY"
        r"\s*[:=]\s*['\"]?([A-Za-z0-9_\-]{12,})"
    )),
]


def mask(value: str) -> str:
    value = value.strip()
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-2:]


def iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if any(rel == d or rel.startswith(d + "/") for d in SKIP_DIRS):
            continue
        if path.name == ".env" or path.name.startswith(".env."):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            continue
        yield path


def main() -> int:
    findings = 0
    for path in iter_files():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, 1):
            for label, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    findings += 1
                    value = match.group(1) if match.lastindex else match.group(0)
                    rel = path.relative_to(ROOT).as_posix()
                    print(f"{rel}:{lineno}: {label}: {mask(value)}")
    if findings:
        print(f"\n发现 {findings} 处疑似密钥。不要提交；先轮换 Key，再清理文件。", file=sys.stderr)
        return 1
    print("未发现疑似密钥。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
