import os, sys, time
os.environ.setdefault("HF_ENDPOINT", os.environ.get("HF_ENDPOINT") or "https://huggingface.co")
from huggingface_hub import snapshot_download
BASE = "/root/yue2-home/models"
ALLOW_MAIN = ["config.json","generation_config.json","yue2_generation_config.json",
    "weights_manifest.json","model.safetensors","model.safetensors.index.json",
    "qwen.tiktoken","modeling_yue2.py","modeling_vae.py","LICENSE","THIRD_PARTY_NOTICES.md",
    "model-*-of-*.safetensors","licenses/*"]
JOBS = [("m-a-p/YuE2-3B","YuE2-3B",ALLOW_MAIN),
        ("m-a-p/YuE2-Vae","YuE2-Vae",ALLOW_MAIN),
        ("m-a-p/SheetSage2","SheetSage2",None),
        ("m-a-p/MERT-v2-FullSong","MERT-v2-FullSong",None)]
for repo, target, allow in JOBS:
    dest = os.path.join(BASE, target)
    for attempt in range(1, 6):
        try:
            t0 = time.time()
            print(f"[下载] {repo} 第{attempt}次 -> {dest}", flush=True)
            kw = {"local_dir": dest, "max_workers": 6}
            if allow: kw["allow_patterns"] = allow
            snapshot_download(repo, **kw)
            tot = sum(os.path.getsize(os.path.join(dp,f)) for dp,_,fs in os.walk(dest) for f in fs)
            print(f"[完成] {repo} 用时{time.time()-t0:.0f}s 共{tot/2**30:.2f}GiB", flush=True)
            break
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[重试] {repo} 失败: {type(e).__name__}: {e}", flush=True)
            if attempt >= 5:
                print(f"[FAIL] {repo}", flush=True); sys.exit(1)
            time.sleep(10 * attempt)
print("[全部模型下载完成]", flush=True)
