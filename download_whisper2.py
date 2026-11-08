"""faster-whisper-small 直链下载（绕开 huggingface_hub 的 Xet 传输）。"""
import os
import ssl
import time
import urllib.request

from app import config as C

os.environ["HF_HUB_DISABLE_XET"] = "1"

BASE = "https://hf-mirror.com/Systran/faster-whisper-small/resolve/main/"
DST = str(C.MODELS / "faster-whisper-small")
FILES = [
    ("config.json", 100),
    ("tokenizer.json", 100_000),
    ("vocabulary.txt", 100_000),
    ("preprocessor_config.json", 100),
    ("model.bin", 400_000_000),
]
CTX = ssl.create_default_context()
UA = {"User-Agent": "Mozilla/5.0"}

os.makedirs(DST, exist_ok=True)


def dl(name, min_bytes, retries=3):
    url = BASE + name
    dst = os.path.join(DST, name)
    if os.path.exists(dst) and os.path.getsize(dst) >= min_bytes:
        print(f"[skip] {name} {os.path.getsize(dst)/1048576:.1f}MB")
        return True
    for attempt in range(1, retries + 1):
        tmp = dst + ".part"
        got = os.path.getsize(tmp) if os.path.exists(tmp) else 0
        t0 = time.time()
        try:
            h = dict(UA)
            if got:
                h["Range"] = f"bytes={got}-"
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                final_url = r.geturl()
                if "xethub" in final_url or "cas-server" in final_url:
                    print(f"[xet] {name} redirected to Xet, abort")
                    return False
                total = r.headers.get("Content-Length")
                total = int(total) + got if total else None
                mode = "ab" if (got and r.status == 206) else "wb"
                if mode == "wb":
                    got = 0
                last = time.time()
                with open(tmp, mode) as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if time.time() - last > 5:
                            pct = f"{got*100//total}%" if total else "?"
                            print(f"  {name} {got/1048576:.0f}MB {pct} {time.time()-t0:.0f}s", flush=True)
                            last = time.time()
            size = os.path.getsize(tmp)
            if size < min_bytes:
                print(f"[warn] {name} {size}B < {min_bytes}, retry")
                os.remove(tmp)
                continue
            os.replace(tmp, dst)
            print(f"[ok] {name} {size/1048576:.1f}MB in {time.time()-t0:.0f}s")
            return True
        except Exception as e:
            print(f"[fail] {name} try{attempt}: {type(e).__name__}: {e}", flush=True)
            time.sleep(2 * attempt)
    return False


ok = sum(dl(n, m) for n, m in FILES)
print(f"=== {ok}/{len(FILES)} files ===")
total = sum(os.path.getsize(os.path.join(DST, f)) for f in os.listdir(DST)
            if os.path.isfile(os.path.join(DST, f)))
print(f"total {total/1048576:.1f}MB")
