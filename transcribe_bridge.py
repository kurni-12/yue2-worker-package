# -*- coding: utf-8 -*-
"""SheetSage2 转录桥（用 .venv-sheetsage2 运行）。

用法：
    .venv-sheetsage2/Scripts/python.exe _deploy/transcribe_bridge.py <音频文件> <输出目录> [--full]

最后一行输出： ###RESULT### {"ok": true, "abc": "...", ...}
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def preseed_module_cache(model_dir):
    """transformers 4.45 的动态模块缓存会漏拷被间接引用的 .py 文件
    （报 FileNotFoundError: ...modules/transformers_modules/<name>/xxx.py）。
    这里先把模型目录下所有 .py 补齐进 HF_MODULES_CACHE，加载前自愈。"""
    import os
    import shutil

    base = os.environ.get("HF_MODULES_CACHE")
    if not base:
        hf_home = os.environ.get("HF_HOME") or str(Path.home() / ".cache" / "huggingface")
        base = str(Path(hf_home) / "modules")
    target = Path(base) / "transformers_modules" / model_dir.name
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sorted(model_dir.glob("*.py")):
        dst = target / src.name
        try:
            same = dst.is_file() and dst.read_bytes() == src.read_bytes()
        except OSError:
            same = False
        if not same:
            shutil.copyfile(src, dst)
            copied += 1
    return target, copied


def main():
    if len(sys.argv) < 3:
        print("###RESULT### " + json.dumps({"ok": False, "error": "参数不足：需要 <音频> <输出目录>"}))
        return 2
    src = Path(sys.argv[1])
    out = Path(sys.argv[2])
    melody_only = "--full" not in sys.argv
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    result = {"ok": False}
    try:
        import torch
        from transformers import AutoModel

        model_dir = ROOT / "models" / "SheetSage2"
        cache_dir, copied = preseed_module_cache(model_dir)
        print("[bridge] module cache %s (补齐 %d 个文件)" % (cache_dir, copied), flush=True)
        print("[bridge] loading SheetSage2 from %s" % model_dir, flush=True)
        model = AutoModel.from_pretrained(
            str(model_dir), trust_remote_code=True, local_files_only=True,
            base_model_path=str(ROOT / "models" / "MERT-v2-FullSong"),
        )
        model = model.to("cuda").eval()
        print("[bridge] transcribing %s (melody_only=%s)" % (src, melody_only), flush=True)
        warnings = []
        try:
            r = model.transcribe(str(src), output_dir=str(out), melody_only=melody_only)
        except RuntimeError as exc:
            if not melody_only or not getattr(exc, "result", None):
                raise
            warnings.append("旋律谱生成失败（%s），已退回完整转录（含和弦）" % exc)
            r = exc.result
        abc = r.get("abc") or ""
        if not abc:
            warnings = list(warnings) + ["未生成 ABC 乐谱，只有转录事件（可改用完整转录重试）"]
        else:
            warnings = list(warnings) + list(r.get("warnings") or [])
        if not abc:
            abc_file = out / "score.abc"
            if abc_file.is_file():
                abc = abc_file.read_text(encoding="utf-8", errors="replace")
        result = {
            "ok": bool(abc) and not r.get("abc_error"),
            "abc": abc,
            "abc_error": r.get("abc_error"),
            "warnings": r.get("warnings", []),
            "output_dir": str(out),
            "seconds": round(time.time() - t0, 1),
        }
    except BaseException as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                  "seconds": round(time.time() - t0, 1)}
    print("###RESULT### " + json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
