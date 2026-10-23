#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""_deploy/run_generate.py —— 参数化生成入口（供 song_executor 子进程调用）

与「运行生成.py」的区别：不读固定的「我的歌曲.json」，不自动打开文件夹，
接受任意请求文件与输出目录，且把结果以 ###DONE### 单行 JSON 打印给父进程。

用法：
    python _deploy/run_generate.py <请求.json> <输出目录> [--budget 16] [--backend torch-eager] [--no-offload-ar]
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _friendly():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass


def main():
    _friendly()
    if len(sys.argv) < 3:
        print("###DONE### " + json.dumps({"ok": False, "error": "参数不足：需要 <请求.json> <输出目录>"}))
        return 2
    req = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    if not req.is_file():
        print("###DONE### " + json.dumps({"ok": False, "error": "请求文件不存在"}))
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    def opt(name, default):
        if name in sys.argv:
            i = sys.argv.index(name)
            if i + 1 < len(sys.argv):
                return sys.argv[i + 1]
        return default

    budget = str(opt("--budget", os.environ.get("YUE2_BUDGET", "12")))
    backend = str(opt("--backend", os.environ.get("YUE2_BACKEND", "torch-eager")))
    stage = opt("--stage", "audio")
    if stage not in ("audio", "plan"):
        raise ValueError("stage 必须是 audio 或 plan")
    song_id = opt("--id", None)

    data = json.loads(req.read_text(encoding="utf-8-sig"))
    song_id = song_id or data.get("id") or "song"

    argv = ["generate", "--request", str(req), "--output", str(out_dir),
            "--budget", budget, "--backend", backend, "--id", song_id]
    if stage == "plan":
        argv += ["--stage", "plan"]
    cfg = opt("--config", None)
    if cfg:
        argv += ["--config", str(cfg)]
    if "--no-offload-ar" not in sys.argv:
        argv.append("--offload-ar")

    print("=" * 58, flush=True)
    print("开始生成：%s" % song_id, flush=True)
    print("输出目录：%s" % out_dir, flush=True)
    print("显存预算：%s GiB  后端：%s  cot=%s" % (budget, backend, data.get("cot")), flush=True)
    print("=" * 58, flush=True)

    t0 = time.time()
    code = 1
    err = ""
    try:
        from yue2.cli import main as yue2_main
        code = int(yue2_main(argv) or 0)
    except KeyboardInterrupt:
        err = "已中断"
        code = 130
    except BaseException as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        code = 1
        err = "%s: %s" % (type(exc).__name__, exc)
    seconds = round(time.time() - t0, 1)

    result_dir = out_dir / str(song_id)
    audio = result_dir / "audio.flac"
    abc = result_dir / "score.abc"
    # 兼容引擎可能写到别的名字
    if not audio.is_file():
        for cand in sorted(result_dir.glob("audio.*")):
            if cand.suffix.lower() in (".flac", ".wav"):
                audio = cand
                break
    if not abc.is_file():
        for cand in sorted(result_dir.glob("*.abc")):
            abc = cand
            break

    # 仅规划阶段只验非空乐谱，不要求生成音频；完整生成仍必须有音频。
    abc_text = abc.read_text(encoding="utf-8", errors="replace") if abc.is_file() else ""
    artifact_ok = bool(abc_text.strip()) if stage == "plan" else audio.is_file()
    payload = {
        "ok": code == 0 and artifact_ok,
        "stage": stage,
        "code": code,
        "error": err,
        "seconds": seconds,
        "audio": str(audio) if audio.is_file() else "",
        "audio_bytes": audio.stat().st_size if audio.is_file() else 0,
        "abc_path": str(abc) if abc.is_file() else "",
        "abc_text": abc_text,
        "result_dir": str(result_dir),
        "budget_gib": budget,
        "backend": backend,
        "cot": data.get("cot"),
    }
    print("###DONE### " + json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if payload["ok"] else (code or 1)


if __name__ == "__main__":
    raise SystemExit(main())
