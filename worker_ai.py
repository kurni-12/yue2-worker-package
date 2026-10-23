# -*- coding: utf-8 -*-
"""
YuE2 网页任务 worker —— 让 www.cbann.com/ai-studio/ 上的任务在本机生成。

用法：
  1. 第一次运行前，把网站后台（AI 歌曲任务 → 显示 worker 密钥）里的密钥
     粘贴到同目录的 worker_secret.txt 里（只一行）。
  2. 双击 启动worker.bat，或运行： python worker_ai.py
  3. 保持这个窗口不关。它会每 15 秒问一次网站"有新任务吗？"

只做三件事：取任务 → 调 运行生成.py → 把 mp3 传回网站。
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
API = "https://www.cbann.com/wp-json/cbann/v1"
OUT_DIR = ROOT / "输出"
PY = sys.executable or "python"
POLL = 15
BUDGET = "12"   # 必须 <=12：引擎源码 budget<=12 时 vae_core_frames=512（显存减半），>12 会变 1024 直接爆
MAX_LYRICS = 100000   # 不截断歌词（用户要求完整保留）

def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)

def secret():
    f = ROOT / "worker_secret.txt"
    if not f.exists():
        log("✗ 缺少 worker_secret.txt —— 请把后台「AI 歌曲任务」页里的密钥粘进去（一行）")
        sys.exit(1)
    s = f.read_text(encoding="utf-8-sig").strip()
    if not s:
        log("✗ worker_secret.txt 是空的")
        sys.exit(1)
    return s


# —— 单实例锁（PID 文件 + 进程存活检查）——
_PIDF = ROOT / "worker.pid"
if _PIDF.exists():
    try:
        _old = int(_PIDF.read_text().strip())
        _o = subprocess.run(["tasklist", "/FI", "PID eq %d" % _old], capture_output=True, text=True, encoding="utf-8", errors="replace")
        if str(_old) in (_o.stdout or ""):
            print("已有 worker 在运行（pid %d），本实例退出 —— 保证一次只跑一首" % _old)
            sys.exit(0)
    except Exception:
        pass
_PIDF.write_text(str(os.getpid()))

SECRET = secret()
HDR = {"x-cbann-secret": SECRET,
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
       "Accept": "application/json"}

# 2026-10-21 网站已改 Cloudflare 灰云直连香港，本机可直连 www.cbann.com。
# 原先强制走本地代理 127.0.0.1:7890，代理抖动会导致间歇 SSL 失败（取任务失败）。
# 改为：优先直连；直连失败再用代理兜底。
PROXY = "http://127.0.0.1:7890"

# 清掉系统里可能写坏的代理环境变量（https_proxy=http://:7890 缺主机名会让 requests/urllib 崩）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
           "http_proxy", "https_proxy", "all_proxy", "ftp_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# CUDA 显存碎片缓解（官方 OOM 报错信息推荐）——传给生成子进程，降低长歌词 OOM 概率
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 无代理
PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))

def http(url, data=None, headers=None, timeout=90, method=None):
    h = dict(HDR)
    h["Cache-Control"] = "no-cache"
    h["Pragma"] = "no-cache"
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    # 优先直连；失败再走代理兜底
    last_err = None
    for opener in (DIRECT_OPENER, PROXY_OPENER):
        try:
            with opener.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last_err = e
            # 重建一次 request（body 可能已被消费）
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            continue
    raise last_err

def next_job():
    try:
        j = http(API + "/ai/jobs/next?s=" + SECRET + "&_t=" + str(time.time_ns()))
        return j.get("job")
    except Exception as e:
        log("取任务失败：%s" % e)
        return None


SPLIT_CHARS = 300   # 每段控制在 300 字内（RTX3060 12GB 可稳定生成）


def split_lyrics(text):
    """把长歌词按段落/标签切分成多段，每段 <= SPLIT_CHARS 字，句意不断。"""
    text = (text or "").strip()
    if len(text) <= SPLIT_CHARS:
        return [text]
    lines = text.splitlines()
    parts, cur, cur_len = [], [], 0
    for ln in lines:
        add = len(ln) + 1
        is_tag = ln.strip().startswith("[") and ln.strip().endswith("]")
        if cur and cur_len + add > SPLIT_CHARS and (is_tag or ln.strip() == ""):
            parts.append(chr(10).join(cur).strip()); cur, cur_len = [], 0
            if is_tag:
                continue
        cur.append(ln); cur_len += add
        if cur_len >= SPLIT_CHARS and not is_tag:
            pass
    if cur: parts.append(chr(10).join(cur).strip())
    return [x for x in parts if x]


def merge_audio(files, dest):
    """用 soundfile 把多段 flac 拼成一个（保持采样率/声道一致）。"""
    import soundfile as sf, numpy as np
    datas, sr = [], None
    for f in files:
        d, r = sf.read(str(f), dtype="float32")
        if sr is None: sr = r
        if d.ndim == 1: d = d[:, None]
        datas.append(d)
    ch = max(x.shape[1] for x in datas)
    fixed = []
    for d in datas:
        if d.shape[1] < ch:
            d = np.repeat(d, ch, axis=1) if d.shape[1] == 1 else np.pad(d, ((0,0),(0,ch-d.shape[1])))
        fixed.append(d)
    out = np.concatenate(fixed, axis=0)
    if out.shape[1] == 1: out = out[:, 0]
    sf.write(str(dest), out, sr)
    return dest


def _trim_lyrics(text):
    if len(text) <= MAX_LYRICS:
        return text
    out, total = [], 0
    for line in text.splitlines():
        if total + len(line) + 1 > MAX_LYRICS:
            break
        out.append(line)
        total += len(line) + 1
    log("!! lyrics too long (%d chars) -> trimmed to %d chars" % (len(text), total))
    return chr(10).join(out).strip()


def build_request(job):
    """把网页任务转成统一请求模型（song_request.SongRequest）的 payload。

    网页可以传：mode / cot / abc / quality / master / candidates / seeds /
               topk / temperature / cfg_scale / rep_penalty / max_audio_ms /
               keep_harmony / language / ref_audio_path
    未传的走默认值（full 模式），不再硬编码 cot。
    """
    try:
        seed = int(str(job.get("seed") or "").strip() or 0)
    except Exception:
        seed = 0
    if seed <= 0:
        seed = int(time.time()) % 999999

    style = (job.get("prompt") or "").strip() or \
        "Chinese, warm pop ballad, expressive male voice, acoustic piano, soft strings, lyrical melody"
    voice = (job.get("voice") or "auto")
    vmap = {"female": "expressive female voice", "male": "expressive male voice",
            "child": "child voice", "duet": "male and female duet"}
    if voice in vmap and vmap[voice] not in style:
        style = style + ", " + vmap[voice]

    # 乐器自定义：网页传 instruments（数组或逗号串），拼进 style（引擎只认 style 文本）
    insts = job.get("instruments")
    if not insts and isinstance(job.get("params"), dict):
        insts = job["params"].get("instruments")
    if isinstance(insts, (list, tuple)):
        insts = ", ".join([str(x).strip() for x in insts if str(x).strip()])
    insts = (insts or "").strip()
    if insts:
        low = style.lower()
        add = [x.strip() for x in insts.replace("，", ",").split(",")
               if x.strip() and x.strip().lower() not in low]
        if add:
            style = (style.rstrip(" ,") + ", " + ", ".join(add)).strip(" ,")

    # 模式：网页显式传 mode 优先；否则按 cot 兼容旧任务
    mode = (job.get("mode") or "").strip().lower()
    if not mode:
        cot_hint = (job.get("cot") or "full").strip().lower()
        mode = cot_hint if cot_hint in ("full", "melody", "off") else "full"

    params = job.get("params") if isinstance(job.get("params"), dict) else {}
    keep_harmony = bool(params.get("keep_harmony") if "keep_harmony" in params else job.get("keep_harmony"))
    km_raw = params.get("keep_melody", job.get("keep_melody", True))
    keep_melody = km_raw not in (False, 0, "0", "false", "", None)
    payload = {
        "mode": mode,
        "title": (job.get("title") or "").strip(),
        "lyrics": (job.get("lyrics") or "").strip(),
        "style": style,
        "vocal": (job.get("vocal") or ("" if voice == "auto" else voice) or "").strip(),
        "instruments": insts,
        "language": (job.get("language") or "zh").strip(),
        "seed": seed,
        "cot": (job.get("cot") or ("melody" if mode in ("melody", "cover") else "full")),
        "abc": (job.get("abc") or "").strip(),
        "keep_harmony": keep_harmony,
        "keep_melody": keep_melody,
        "quality": (job.get("quality") or "standard").strip(),
        "master": bool(job.get("master")),
        "topk": job.get("topk") or 50,
        "temperature": job.get("temperature") or 1.0,
        "cfg_scale": job.get("cfg_scale") or 3.0,
        "rep_penalty": job.get("rep_penalty") or 1.0,
        "max_audio_ms": job.get("max_audio_ms") or 210000,
        "candidates": job.get("candidates") or 1,
        "seeds": job.get("seeds") or [],
        # 服务器侧注入
        "job_id": str(job.get("id") or ""),
        "user_id": int(job.get("uid") or job.get("user_id") or 0),
        "user_login": str(job.get("username") or job.get("user_login") or ""),
        "ref_audio_path": str(job.get("ref_audio_path") or ""),
        "ref_audio_sha": str(job.get("ref_audio_sha") or ""),
    }
    return payload

def run_generation(job):
    """统一走 SongExecutor：串行锁 + 分阶段 + 可取消 + 双格式归档 + 记录。

    不再写「我的歌曲.json」，不再硬编码 cot，不再靠扫描输出目录猜产物。
    """
    from song_request import SongRequest, RequestError
    from song_executor import SongExecutor

    payload = build_request(job)

    # 参考音频：服务端只给鉴权 URL，worker 下载到本地后用真实路径
    ref_url = str(job.get("ref_audio_url") or "")
    if ref_url and not payload.get("ref_audio_path"):
        try:
            from urllib.parse import urlparse, quote, urlunparse
            import hashlib as _hl

            # #6 安全：只允许本站域名、只允许 https、禁止重定向、不把 secret 发到别处
            api_host = urlparse(API).netloc
            u = urlparse(ref_url)
            if u.scheme != "https":
                raise RuntimeError("参考音频 URL 必须是 https")
            if u.netloc != api_host:
                raise RuntimeError("参考音频 URL 域名不被信任：%s（只允许 %s）" % (u.netloc, api_host))
            if any(x in u.path for x in ("..", "//")):
                raise RuntimeError("参考音频 URL 路径可疑")
            # 只允许指向本站 ai/job/<id>/ref 的地址
            if not re.search(r"/ai/job/\d+/ref$", u.path):
                raise RuntimeError("参考音频 URL 不是本站 ref 端点")
            # 去掉任何 query 里可能夹带的其他 secret，只保留我们自己的 s
            q = "s=" + quote(SECRET, safe="")
            safe_url = urlunparse((u.scheme, u.netloc, u.path, "", q, ""))

            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    raise RuntimeError("参考音频下载禁止重定向")

            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": "http://127.0.0.1:7890",
                                             "https": "http://127.0.0.1:7890"}),
                _NoRedirect)
            ext = os.path.splitext(u.path)[1].lower() or ".mp3"
            if ext not in (".mp3", ".wav", ".flac", ".m4a", ".ogg"):
                ext = ".mp3"
            refdir = ROOT / "输出" / "_ref"
            refdir.mkdir(parents=True, exist_ok=True)
            dest = refdir / ("job%s%s" % (job["id"], ext))
            req = urllib.request.Request(safe_url, headers=HDR)
            with opener.open(req, timeout=300) as resp:
                data = resp.read(40 * 1024 * 1024 + 1)   # 硬上限 40MB
            if len(data) > 40 * 1024 * 1024:
                raise RuntimeError("参考音频超过 40MB")
            if len(data) < 1024:
                raise RuntimeError("参考音频下载过小")
            got = _hl.sha256(data).hexdigest()
            want = str(job.get("ref_audio_sha") or "")
            if want and got != want:
                raise RuntimeError("参考音频哈希不符：期望 %s 实际 %s" % (want[:12], got[:12]))
            dest.write_bytes(data)
            payload["ref_audio_path"] = str(dest)
            payload["ref_audio_sha"] = got
            log("参考音频已下载：%s（%.1f MB，sha256 %s）" % (dest.name, len(data) / 1048576, got[:12]))
        except Exception as e:
            log("参考音频下载失败：%s" % e)
            try:
                http(API + "/ai/job/%s/fail?s=" % job["id"] + SECRET,
                     data=json.dumps({"error": "参考音频下载失败：%s" % e}).encode(),
                     headers={"Content-Type": "application/json"}, method="POST")
            except Exception:
                pass
            return False

    try:
        req = SongRequest.from_payload(payload)
    except RequestError as e:
        log("任务 #%s 请求校验失败：%s" % (job["id"], e))
        try:
            http(API + "/ai/job/%s/fail?s=" % job["id"] + SECRET,
                 data=json.dumps({"error": "请求校验失败：%s" % e}).encode(),
                 headers={"Content-Type": "application/json"}, method="POST")
        except Exception:
            pass
        return False

    cancel_box = {"flag": False}

    def _emit(ev):
        log("  [%s] %s %s %s" % (ev.stage, ev.status,
                                 ("%.0fs" % ev.seconds) if ev.seconds else "",
                                 ev.detail or ""))
        # 真实阶段上报；服务端回 cancel=true 时立刻置取消标志
        try:
            payload = json.dumps({"stage": ev.stage, "status": ev.status,
                                  "detail": (ev.detail or "")[:300]}).encode("utf-8")
            # http() 已经返回 dict（内部 json.loads），不要再 decode
            j = http(API + "/ai/job/%s/stage?s=" % job["id"] + SECRET, data=payload,
                     headers={"Content-Type": "application/json"}, method="POST")
            if isinstance(j, dict) and j.get("cancel"):
                cancel_box["flag"] = True
                log("!! 收到服务端取消指令，停止生成")
        except Exception as e:
            # 不再静默：阶段上报失败必须留痕，否则取消功能会悄悄失效
            log("!! 阶段上报失败（%s）：%s" % (ev.stage, e))

    log("任务 #%s：mode=%s cot=%s 歌词 %d 字 quality=%s master=%s"
        % (job["id"], req.mode, req.cot, len(req.lyrics), req.quality, req.master))
    ex = SongExecutor(emit=_emit, cancel=lambda: cancel_box["flag"])
    ctx = {"job_id": job["id"],
           "user_id": int(job.get("uid") or job.get("user_id") or 0),
           "user_login": str(job.get("username") or job.get("user_login") or "")}
    res = ex.run(req, ctx)

    if not res.ok:
        msg = res.error or "生成失败"
        log("任务 #%s 失败：%s" % (job["id"], msg))
        try:
            http(API + "/ai/job/%s/fail?s=" % job["id"] + SECRET,
                 data=json.dumps({"error": msg[:800], "canceled": res.canceled}).encode(),
                 headers={"Content-Type": "application/json"}, method="POST")
        except Exception:
            pass
        return False

    # 仅转录模式：只回传 ABC，不上传音频
    if req.mode == "transcribe":
        log("任务 #%s 转录完成：%d 字符 ABC" % (job["id"], len(res.abc or "")))
        try:
            body = json.dumps({"stage": "abc", "status": "ok",
                               "detail": "转录 %d 字符" % len(res.abc or ""),
                               "abc": res.abc or ""}).encode("utf-8")
            http(API + "/ai/job/%s/stage?s=" % job["id"] + SECRET, data=body,
                 headers={"Content-Type": "application/json"}, method="POST")
            http(API + "/ai/job/%s/done?s=" % job["id"] + SECRET,
                 data=json.dumps({"title": "转录 #%s" % job["id"],
                                  "abc": res.abc or ""}).encode("utf-8"),
                 headers={"Content-Type": "application/json"}, method="POST")
        except Exception as e:
            log("ABC 回传失败：%s" % e)
            return False
        return True

    log("任务 #%s 生成完成：%.0f 秒 | MP3 %.1fMB | FLAC %.1fMB"
        % (job["id"], res.seconds,
           os.path.getsize(res.mp3) / 1048576 if res.mp3 else 0,
           os.path.getsize(res.flac) / 1048576 if res.flac else 0))
    try:
        # upload 内部会再调 archive_song（幂等：同源哈希直接返回已校验归档）
        # 上传的文件按 play_version 决定：开了母带传母带，否则传原版；原版永远保留在归档里
        up_file = Path(res.upload_file or res.mp3)
        upload(job["id"], Path(res.audio_source), job, upload_file=up_file,
               play_version=getattr(res, "play_version", "original"))
        log("任务 #%s 完成并已回传" % job["id"])
        return True
    except Exception as e:
        log("上传失败：%s" % e)
        return False


def upload(job_id, audio, job, upload_file=None, play_version="original"):
    """先永久归档 MP3/FLAC，仅上传保留 30 天的 MP3。

    upload_file：实际上传的文件（母带版或原版），必须已存在于同一归档目录。
    play_version：网页播放/下载的是哪一版（original | master），写进服务端记录。
    """
    from web_archive import archive_song
    archived = archive_song(audio, ROOT / "输出" / "web", dict(job, id=job_id))
    log("本地归档完成：%s" % archived["directory"])
    boundary = "----cbann%s" % time.time_ns()
    payload_file = Path(upload_file) if upload_file else archived["mp3"]
    if not payload_file.is_file():
        raise FileNotFoundError(payload_file)
    fields = {"title":archived["title"], "storage_policy":"mp3_30d",
              "play_version":play_version,
              "original_kept":"1",
              "archive_receipt":json.dumps(archived["receipt"], ensure_ascii=False)}
    parts = []
    for field, value in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" %
                      (boundary, field, value)).encode("utf-8"))
    head = ("--%s\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"song-%s.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n" %
            (boundary, job_id)).encode("utf-8")
    body = b"".join(parts) + head + payload_file.read_bytes() + ("\r\n--%s--\r\n" % boundary).encode()
    up_headers = dict(HDR, **{"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    # 优先直连上传，失败走代理兜底
    response = None
    _last = None
    for _opener in (DIRECT_OPENER, PROXY_OPENER):
        try:
            req = urllib.request.Request("%s/ai/job/%s/audio" % (API, job_id), data=body, headers=up_headers)
            response = _opener.open(req, timeout=600)
            break
        except Exception as _e:
            _last = _e
            continue
    if response is None:
        raise _last
    with response:
        result = json.loads(response.read().decode("utf-8", "replace"))
        # #7 验收要求：必须能证明服务端仍执行 30 天策略，仅 {ok,url} 不算通过
        if not result.get("ok") or not (result.get("url") or result.get("formats", {}).get("mp3")):
            raise RuntimeError("MP3 上传未确认；本地双格式归档已保留：" + json.dumps(result, ensure_ascii=False))
        if result.get("storage_policy") != "mp3_30d":
            raise RuntimeError("服务端未确认 mp3_30d 存储策略：" + json.dumps(result, ensure_ascii=False))
        if not result.get("expires_at"):
            raise RuntimeError("服务端未返回到期时间 expires_at：" + json.dumps(result, ensure_ascii=False))
        (archived["directory"] / "upload_result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        return result


# —— 平滑切换：调度台在本目录放 drain.flag 表示「收尾」——
#    worker 不再接新任务；手上这首做完、回传后自己退出。
# CBANN_DRAIN: drain.flag 原生支持（调度台远端补丁检测此标记后跳过注入）
DRAIN_FLAG = ROOT / "drain.flag"
STATUS_FILE = ROOT / "worker_status.json"


def write_status(state, job_id=None):
    try:
        STATUS_FILE.write_text(json.dumps({
            "state": state, "job": job_id, "pid": os.getpid(),
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def draining():
    return DRAIN_FLAG.exists()


def main():
    log("worker 启动 ✓ 目标 %s" % API)
    log("轮询间隔 %s 秒。保持此窗口开启；Ctrl+C 退出。" % POLL)
    idle = 0
    write_status("idle")
    while True:
        if draining():
            log("收到收尾指令（drain.flag），手上没有任务，worker 退出")
            write_status("stopped")
            try:
                _PIDF.unlink()
            except Exception:
                pass
            return
        job = next_job()
        if not job:
            idle += 1
            if idle % 20 == 1:
                log("暂无任务（已等待 %d 分钟）" % (idle * POLL // 60))
            time.sleep(POLL)
            continue
        idle = 0
        log("收到任务 #%s：歌词 %d 字，风格 %s" % (job["id"], len(job.get("lyrics") or ""), (job.get("prompt") or "")[:60]))
        write_status("busy", job["id"])
        try:
            run_generation(job)
        except Exception as e:
            log("✗ 未处理异常：%s" % e)
            try:
                http(API + "/ai/job/%s/fail?s=" % job["id"] + SECRET, data=json.dumps({"error": str(e)[:400]}).encode(),
                     headers={"Content-Type": "application/json"}, method="POST")
            except Exception:
                pass
        write_status("idle")
        if draining():
            log("任务 #%s 已收尾完成，按收尾指令退出" % job["id"])
            write_status("stopped")
            try:
                _PIDF.unlink()
            except Exception:
                pass
            return
        time.sleep(2)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("已退出")
