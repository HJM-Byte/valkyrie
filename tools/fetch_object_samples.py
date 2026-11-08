"""从 Wikimedia Commons 搜图并下载物体样本，只用于本地测试/验收集。

不训练模型，不冒充用户家里的物品；图片来自公开图库，版权按 Commons 页面。
用法：
    python tools/fetch_object_samples.py --query "cell phone" --limit 5
    python tools/fetch_object_samples.py --all
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "objects_web"
API = "https://commons.wikimedia.org/w/api.php"
UA = "xiaoling-object-samples/1.0 (local test; contact: local-user)"


def search(query: str, limit: int = 5) -> list[dict]:
    params = {
        "action": "query", "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": "6", "gsrlimit": str(limit),
        "prop": "imageinfo", "iiprop": "url|extmetadata",
        "iiurlwidth": "800", "format": "json",
    }
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    pages = data.get("query", {}).get("pages", {})
    out = []
    for page in pages.values():
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        out.append({
            "title": page.get("title", ""),
            "url": info.get("thumburl") or info.get("url"),
            "page": info.get("descriptionurl", ""),
            "license": (info.get("extmetadata", {}) or {}).get("LicenseShortName", {}).get("value", ""),
        })
    return [x for x in out if x.get("url")]


def download(items: list[dict], folder: Path) -> list[dict]:
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for i, item in enumerate(items, 1):
        suffix = Path(urllib.parse.urlparse(item["url"]).path).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
            suffix = ".jpg"
        path = folder / f"{i:02d}{suffix}"
        try:
            req = urllib.request.Request(item["url"], headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                path.write_bytes(r.read())
            saved.append({**item, "file": str(path.relative_to(ROOT))})
        except Exception as e:
            print(f"  跳过 {i}: {type(e).__name__}")
        time.sleep(0.2)
    return saved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default=None)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    queries = ["cell phone", "chair", "stool"] if args.all else [args.query]
    if not queries or queries == [None]:
        ap.error("--query 或 --all 二选一")
    manifest = []
    for q in queries:
        items = search(q, args.limit)
        print(f"{q}: 找到 {len(items)} 张")
        folder = OUT / q.replace(" ", "_")
        saved = download(items, folder)
        for x in saved:
            x["query"] = q
        manifest.extend(saved)
        print(f"  下载 {len(saved)} 张 -> {folder}")
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"清单: {OUT / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
