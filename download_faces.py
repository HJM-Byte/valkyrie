import os, shutil, ssl, time, urllib.request

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from huggingface_hub import hf_hub_download

from app import config as C

MODELS = str(C.MODELS)
os.makedirs(MODELS, exist_ok=True)
os.makedirs(os.path.join(MODELS, "samples"), exist_ok=True)

JOBS = [
    ("opencv/face_detection_yunet", "face_detection_yunet_2023mar.onnx"),
    ("opencv/face_recognition_sface", "face_recognition_sface_2021dec.onnx"),
]

for repo, fname in JOBS:
    dst = os.path.join(MODELS, fname)
    t0 = time.time()
    try:
        p = hf_hub_download(repo_id=repo, filename=fname, local_dir=os.path.join(MODELS, "_hf_" + repo.split("/")[1]))
        shutil.copy2(p, dst)
        print(f"[ok] {fname} {os.path.getsize(dst)/1024:.0f}KB in {time.time()-t0:.1f}s")
    except Exception as e:
        print(f"[fail] {fname}: {type(e).__name__}: {e}")

# lena test image
lena = os.path.join(MODELS, "samples", "lena.jpg")
if not (os.path.exists(lena) and os.path.getsize(lena) > 10_000):
    ctx = ssl.create_default_context()
    for u in ("https://hf-mirror.com/datasets/opencv/lena/resolve/main/lena.jpg",
              "https://ghfast.top/https://github.com/opencv/opencv/raw/master/samples/data/lena.jpg"):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
                d = r.read()
            if len(d) > 10_000:
                open(lena, "wb").write(d)
                print(f"[ok] lena.jpg {len(d)} bytes")
                break
            print(f"[warn] {u} only {len(d)} bytes")
        except Exception as e:
            print(f"[fail] lena via {u}: {type(e).__name__}: {e}")

print("\n=== models dir ===")
for f in sorted(os.listdir(MODELS)):
    p = os.path.join(MODELS, f)
    if os.path.isfile(p):
        print(f"  {f:48s} {os.path.getsize(p)/1024:.0f}KB")
