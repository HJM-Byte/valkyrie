"""Read-only checks for the long-lived web entry.

No production database is opened for writing. The only filesystem reads are
the static shell, the launcher/guard scripts, the startup shortcut and the
latest web backup manifest.

    python tools\\selftest_web.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "pocket_web"
DATA = ROOT / "data"
TOKEN = (DATA / "pocket_token.txt").read_text(encoding="utf-8").strip() if (DATA / "pocket_token.txt").exists() else ""
BASE = "http://127.0.0.1:8766"

checks: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    checks.append((bool(ok), label))


def get(path: str, token: str = "", expect: int = 200) -> tuple[int, bytes]:
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("X-Valkyrie-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    health = {}
    try:
        status, body = get("/api/health")
        health = json.loads(body.decode("utf-8"))
        check(status == 200 and health.get("app") == "valkyrie-pocket", "health is valkyrie-pocket")
        check(health.get("version") == "0.8.0", "health version 0.8.0")
        check(health.get("response_gate", {}).get("mode") == "enforced", "response gate enforced")
        check(health.get("proactive") is True, "proactive worker enabled")
    except Exception as e:
        check(False, f"health request: {type(e).__name__}: {e}")

    try:
        status, body = get("/")
        html = body.decode("utf-8")
        check(status == 200 and "瓦尔基里" in html, "index page serves")
        check('id="installWeb"' in html, "index has install entry")
        check('class="home-side"' in html, "index has desktop home wrapper")
    except Exception as e:
        check(False, f"index request: {type(e).__name__}: {e}")

    try:
        status, body = get("/assets/app.css")
        css = body.decode("utf-8")
        check(status == 200 and "@media (min-width: 980px)" in css, "desktop breakpoint present")
        check("home-side" in css and "grid-template-columns" in css, "desktop home grid present")
        check("--amber: #f1a46a" in css and "--sea: #79d2c2" in css, "shared palette present")
    except Exception as e:
        check(False, f"css request: {type(e).__name__}: {e}")

    try:
        status, body = get("/manifest.webmanifest")
        manifest = json.loads(body.decode("utf-8"))
        check(status == 200 and manifest.get("orientation") == "any", "manifest orientation any")
        check("display_override" in manifest, "manifest has display_override")
    except Exception as e:
        check(False, f"manifest request: {type(e).__name__}: {e}")

    try:
        status, body = get("/sw.js")
        check(status == 200 and b"valkyrie-pocket-shell-v8" in body, "service worker v8")
    except Exception as e:
        check(False, f"sw request: {type(e).__name__}: {e}")

    try:
        status, _ = get("/api/state", TOKEN)
        check(status == 200, "authenticated state endpoint")
        status, _ = get("/api/state")
        check(status == 401, "unauthenticated state is rejected")
    except Exception as e:
        check(False, f"auth request: {type(e).__name__}: {e}")

    web_cmd = ROOT / "web.cmd"
    check(web_cmd.exists() and "--no-open" in web_cmd.read_text(encoding="utf-8", errors="ignore"),
          "web.cmd exists with --no-open")
    guard = ROOT / "tools" / "server_watchdog.ps1"
    guard_text = guard.read_text(encoding="utf-8", errors="ignore") if guard.exists() else ""
    check(guard.exists() and "--chat" in guard_text, "watchdog starts chat")
    check("--proactive" in guard_text,
          "watchdog starts proactive")
    check("--autonomy" not in guard_text,
          "watchdog does not start autonomy")
    check((ROOT / "tools" / "install_web_persistence.ps1").exists(), "persistence installer exists")
    startup = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Valkyrie Web.lnk"
    check(startup.exists(), "startup shortcut exists")

    backups = sorted((ROOT / "backups" / "web_auto").glob("*/MANIFEST.json")) if (ROOT / "backups" / "web_auto").exists() else []
    check(bool(backups), "web backup exists")
    if backups:
        try:
            manifest = json.loads(backups[-1].read_text(encoding="utf-8"))
            dbs = manifest.get("databases", [])
            check(len(dbs) >= 4, "backup has four databases")
            check(all(d.get("integrity") == "ok" for d in dbs if d.get("exists")), "backup databases integrity ok")
            check(any("memory.db" in d.get("src", "") for d in dbs), "backup includes memory.db")
        except Exception as e:
            check(False, f"backup manifest: {type(e).__name__}: {e}")

    fails = [label for ok, label in checks if not ok]
    for ok, label in checks:
        print(("ok   " if ok else "FAIL ") + label)
    print(f"\n{len(checks) - len(fails)} / {len(checks)} ok")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
