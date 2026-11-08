"""水镜 APK 下载路由自检：不碰真实 memory.db，只验证服务端能交出安装包。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from app.pocket_server import APK_FILE, PocketAuth, PocketStore, create_app  # noqa: E402


async def main() -> int:
    if not APK_FILE.is_file():
        print(f"FAIL: APK 不存在: {APK_FILE}")
        return 1
    with tempfile.TemporaryDirectory(prefix="pocket_apk_test_") as td:
        store = PocketStore(Path(td) / "memory.db")
        app = create_app(store, auth=PocketAuth("123456"), token="123456")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            page = await client.get("/")
            html = await page.text()
            assert page.status == 200, page.status
            assert "/download/valkyrie.apk" in html, "PWA 里没有 APK 下载链接"

            response = await client.get("/download/valkyrie.apk")
            data = await response.read()
            assert response.status == 200, response.status
            assert response.headers["Content-Type"] == "application/vnd.android.package-archive"
            assert data[:2] == b"PK", "下载内容不是 APK/ZIP"
            assert len(data) == APK_FILE.stat().st_size, (len(data), APK_FILE.stat().st_size)
            print(f"PASS: PWA link OK; APK {len(data)} bytes, sha256={__import__('hashlib').sha256(data).hexdigest()[:16]}…")
        finally:
            await client.close()
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
