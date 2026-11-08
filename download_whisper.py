import os, time

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from huggingface_hub import snapshot_download

from app import config as C

MODELS = str(C.MODELS)
dst = os.path.join(MODELS, "faster-whisper-small")
t0 = time.time()
print("[start] Systran/faster-whisper-small via hf-mirror", flush=True)
p = snapshot_download(
    repo_id="Systran/faster-whisper-small",
    local_dir=dst,
    allow_patterns=["config.json", "model.bin", "tokenizer.json",
                    "vocabulary.txt", "preprocessor_config.json"],
    max_workers=4,
)
total = sum(os.path.getsize(os.path.join(dp, f))
            for dp, _, fs in os.walk(dst) for f in fs)
print(f"[ok] {dst} total {total/1048576:.1f}MB in {time.time()-t0:.0f}s")
for f in sorted(os.listdir(dst)):
    fp = os.path.join(dst, f)
    if os.path.isfile(fp):
        print(f"   {f:32s} {os.path.getsize(fp)/1048576:.1f}MB")
