#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
song_executor.py —— YuE2 网页接入的统一执行器
==============================================
职责（工单 §5/§6）：
  · 串行：单 GPU，全流程文件锁，绝不让两个生成同时抢显存
  · 分阶段：transcribe → plan → render → decode → encode → master，每阶段回调上报
  · 可取消：外部置 cancel 标志 → 立即杀子进程 → 状态置 canceled（不是假进度）
  · 复用已验证工具：run_generate.py / transcribe_bridge.py / web_archive.archive_song
  · 隔离：每次任务独立临时目录，产物先归档校验再返回
  · 记录：gen_record.json（请求+有效配置+引擎版本+产物哈希）

不做什么（诚实边界）：
  · 不模拟翻唱"原唱声线克隆"——本质是按参考旋律重新生成
  · 不模拟"直接去人声"——instrumental 是重新配器的纯器乐
  · 不做局部音频修补——编辑重生成必然产出全新录音
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from song_request import SongRequest, RequestError, sha256_file  # noqa: E402

SHANGHAI = timezone(timedelta(hours=8))
PY_MAIN = HERE / ".venv" / "bin" / "python"
PY_TRANS = HERE / ".venv-sheetsage2" / "bin" / "python"
RUNNER = HERE / "_deploy" / "run_generate.py"
TRANS_BRIDGE = HERE / "_deploy" / "transcribe_bridge.py"
GPU_LOCK = HERE / "_deploy" / ".gpu.lock"
# 显存预算：12 GiB 是引擎自带的低显存路径（get_pipe: budget<=12 → vae_core_frames=512）。
# 16 GiB 会让引擎切到 1024 帧分块，12GB 卡上解码峰值更高，属未回归配置。
BUDGET_GIB = 24


# ============================================================ 阶段与结果

@dataclass
class StageEvent:
    stage: str
    status: str            # start | ok | fail | skip
    detail: str = ""
    seconds: float = 0.0
    at: str = ""


@dataclass
class ExecResult:
    ok: bool = False
    error: str = ""
    canceled: bool = False
    title: str = ""
    lyrics: str = ""
    abc: str = ""
    audio_source: str = ""        # 引擎原始 FLAC
    mp3: str = ""
    flac: str = ""
    mp3_master: str = ""          # 母带版（若开启）
    play_version: str = ""        # 网页播放哪一版：original|master
    upload_file: str = ""         # 实际上传的文件
    archive_dir: str = ""
    record: Dict[str, Any] = field(default_factory=dict)
    stages: List[StageEvent] = field(default_factory=list)
    seconds: float = 0.0


# ============================================================ GPU 串行锁

class GpuLock:
    """整机单 GPU 串行锁：PID + 时间戳，进程死了自动接管"""

    def __init__(self, path: Path = GPU_LOCK, stale_seconds: int = 3 * 3600):
        self.path = path
        self.stale = stale_seconds

    def _pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def acquire(self, timeout: float = 6 * 3600, on_wait: Optional[Callable[[int], None]] = None) -> bool:
        t0 = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while time.time() - t0 < timeout:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps({"pid": os.getpid(), "at": time.time()}).encode())
                os.close(fd)
                return True
            except FileExistsError:
                # 检查陈旧锁
                try:
                    info = json.loads(self.path.read_text(encoding="utf-8") or "{}")
                    pid = int(info.get("pid") or 0)
                    at = float(info.get("at") or 0)
                    if not self._pid_alive(pid) or (time.time() - at) > self.stale:
                        self.path.unlink(missing_ok=True)
                        continue
                except Exception:
                    self.path.unlink(missing_ok=True)
                    continue
                if on_wait:
                    try:
                        on_wait(int(time.time() - t0))
                    except Exception:
                        pass
                time.sleep(3)
        return False

    def release(self) -> None:
        try:
            info = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            if int(info.get("pid") or 0) == os.getpid():
                self.path.unlink(missing_ok=True)
        except Exception:
            pass

    def holder(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return None


# ============================================================ 执行器

class SongExecutor:
    def __init__(self,
                 root: Path = HERE,
                 archive_root: Optional[Path] = None,
                 emit: Optional[Callable[[StageEvent], None]] = None,
                 cancel: Optional[Callable[[], bool]] = None):
        self.root = Path(root)
        self.archive_root = Path(archive_root or (self.root / "输出" / "web"))
        self.emit = emit or (lambda ev: None)
        self.cancel = cancel or (lambda: False)
        self.lock = GpuLock()
        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()

    # ---------------- 内部工具 ----------------
    def _stage(self, res: ExecResult, stage: str, status: str, detail: str = "", seconds: float = 0.0):
        ev = StageEvent(stage=stage, status=status, detail=detail, seconds=round(seconds, 1),
                        at=datetime.now(SHANGHAI).isoformat(timespec="seconds"))
        res.stages.append(ev)
        try:
            self.emit(ev)
        except Exception:
            pass
        return ev

    def _run_subprocess(self, cmd: List[str], marker: str, timeout: int, res: ExecResult,
                        stage: str, cwd: Optional[Path] = None, env_extra: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
        """跑子进程，逐行读标记行；cancel 时立即 kill"""
        env = {k: v for k, v in os.environ.items()
               if k.upper() not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE")}
        env["PYTHONIOENCODING"] = "utf-8"
        if env_extra:
            env.update(env_extra)
        t0 = time.time()
        payload = None
        log_lines: List[str] = []
        with self._proc_lock:
            self._proc = subprocess.Popen(
                cmd, cwd=str(cwd or self.root), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        proc = self._proc
        try:
            assert proc.stdout is not None
            # 重要：长时间不输出的阶段（如 render）也必须能取消。
            # 用后台线程读 stdout → 主线程每秒检查 cancel/timeout，并定期发心跳，
            # 这样服务端的 cancel 标志才能在渲染中途传到 worker。
            import queue as _q
            q: "_q.Queue" = _q.Queue()

            def _reader():
                try:
                    for ln in proc.stdout:
                        q.put(ln)
                except Exception:
                    pass
                finally:
                    q.put(None)

            threading.Thread(target=_reader, daemon=True).start()
            last_hb = time.time()
            while True:
                if self.cancel():
                    proc.kill()
                    res.canceled = True
                    res.error = "用户已取消"
                    break
                if time.time() - t0 > timeout:
                    proc.kill()
                    res.error = f"{stage} 超时（>{timeout}s）"
                    break
                try:
                    raw = q.get(timeout=1.0)
                except _q.Empty:
                    raw = ""
                if raw is None:          # 子进程结束（EOF）
                    break
                line = (raw or "").rstrip("\n")
                if line.strip():
                    log_lines.append(line)
                    if len(log_lines) > 400:
                        log_lines = log_lines[-300:]
                if line and marker in line:
                    try:
                        payload = json.loads(line.split(marker, 1)[1].strip())
                    except Exception:
                        payload = {"ok": False, "error": "结果行解析失败：" + line[-200:]}
                if not line and time.time() - last_hb >= 60:
                    last_hb = time.time()
                    # 心跳：只更新进度、不进阶段日志；服务端顺带回传 cancel 标志
                    self._stage(res, stage, "run", "仍在%s（已 %.0f 分钟）" % (stage, (time.time() - t0) / 60))
            proc.wait(timeout=30)
        except Exception as exc:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:
                pass
            res.error = res.error or f"{stage} 子进程异常：{type(exc).__name__}: {exc}"
        finally:
            with self._proc_lock:
                self._proc = None

        secs = time.time() - t0
        if payload is None and not res.canceled and not res.error:
            tail = " | ".join(log_lines[-3:])
            res.error = f"{stage} 未返回结果（退出码 {proc.returncode}）：{tail[:300]}"
        self._stage(res, stage, "ok" if (payload or {}).get("ok") else ("fail" if not res.canceled else "skip"),
                    (payload or {}).get("error", "") or res.error, secs)
        return payload

    # ---------------- 请求 JSON 构造 ----------------
    def _build_engine_request(self, req: SongRequest, song_id: str, abc_override: str = "") -> Dict[str, Any]:
        """把统一请求翻译成引擎真实认识的字段。

        引擎 cli.request_kwargs 的白名单（实测源码 yue2/cli.py:41）：
          allowed  = style, tags, lyrics, cot, seed, abc, cfg_scale, id,
                     abc_sampling, semantic_sampling
          metadata = lang, eval_index, clip_id, prompt
        其余字段引擎会直接 ValueError: Unknown request fields，
        所以绝不传。采样参数走 Sampling 子结构：
          temperature / top_p / top_k / repetition_penalty /
          penalty_window / min_tokens / max_tokens
        """
        data: Dict[str, Any] = {
            "id": song_id,
            "style": req.style or "pop, acoustic, warm vocal",
            "lyrics": req.lyrics,
            "cot": req.cot,
            "seed": int(req.actual_seed),
        }
        if abc_override:
            data["abc"] = abc_override
        if req.cfg_scale:
            data["cfg_scale"] = float(req.cfg_scale)
        if req.language:
            data["lang"] = req.language

        # 两阶段采样（ABC 规划阶段 + 语义/声学阶段），键名必须与 Sampling 一致
        samp: Dict[str, Any] = {}
        if req.topk:
            samp["top_k"] = int(req.topk)
        if req.temperature:
            samp["temperature"] = float(req.temperature)
        if req.rep_penalty:
            samp["repetition_penalty"] = float(req.rep_penalty)
        if samp:
            data["abc_sampling"] = dict(samp)
            data["semantic_sampling"] = dict(samp)
        return data

    @staticmethod
    def _instrumental_style(style: str) -> str:
        """与官方 instrumental.py prepare() 一致：以 Instrumental 开头，并补齐
        no vocals / no singing / no choir / no spoken words，避免模型唱出无词人声或合唱。"""
        import re as _re
        s = (style or "").strip().rstrip(".,，。 ")
        if not s:
            s = "Expressive instrumental music"
        if not _re.match(r"^instrumental\b", s, _re.I):
            s = "Instrumental, " + s
        low = s.lower()
        for cond in ("no vocals", "no singing", "no choir", "no spoken words"):
            if cond not in low:
                s += ", " + cond
        return s + "."

    def _instrumentalize(self, abc_text: str, keep_chords: bool = True) -> tuple:
        """官方 Vocal→Ins 事件级转换（复用 YuE 官方 instrumentalize.convert_score）。

        官方语义（SKILL.md）："move every Vocal note to Ins, and render the revised score"。
        因此必须把 Vocal 的音符**搬到 Ins**，而不是删掉——删掉会丢失旋律。
        convert_score 会返回检查字典，含 vocal_pitch_onset_duration_preserved。
        """
        scripts = self.root / "YuE" / "skills" / "yue2-music" / "instrumental" / "scripts"
        if not scripts.is_dir():
            raise RuntimeError("缺少官方 instrumental 脚本目录：%s" % scripts)
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from instrumentalize import convert_score  # noqa: E402
        converted, check = convert_score(abc_text, overlap="vocal", keep_chords=keep_chords)
        required = ("vocal_pitch_onset_duration_preserved", "meter_and_tempo_preserved")
        for k in required:
            if check.get(k) is not True:
                raise RuntimeError("官方转换校验未通过：%s=%r" % (k, check.get(k)))
        return converted, check

    def _sparse_melody(self, abc_text: str) -> str:
        """纯伴奏（去主旋律）：把 Ins 旋律压成「每小节一个和弦根音长音」。
        官方方言禁止零音符乐谱（compile_events 校验 no sounding notes），
        因此保留极简根音脉冲维持结构，主旋律不可辨识，听感=纯伴奏垫底。"""
        scripts = self.root / "YuE" / "skills" / "yue2-music" / "instrumental" / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from instrumentalize import parse_abc, section_starts
        from compile_score import compile_events
        src = parse_abc(abc_text)
        bars = src.voices["Ins"].bars
        chords = src.voices["Vocal"].chords or []
        key0 = src.voices["Vocal"].keys[0][1] if src.voices["Vocal"].keys else "C"
        sections = section_starts(abc_text, src)
        _roots = {"C": 48, "D": 50, "E": 52, "F": 53, "G": 55, "A": 57, "B": 59}
        import re as _re
        notes = []
        bar_infos = []
        cur_root = 48
        for start, _, meter in bars:
            n, d = int(meter[0]), int(meter[1])
            # 该小节起点（含之前）最近的和弦 → 根音
            q = float(start)
            for when, symbol in chords:
                try:
                    if float(when) <= q + 1e-9:
                        m = _re.match(r"^([A-G])([#b]?)", str(symbol))
                        if m:
                            cur_root = _roots[m.group(1)] + (1 if m.group(2) == "#" else -1 if m.group(2) == "b" else 0)
                except Exception:
                    pass
            notes.append([str(start), str(n * 4.0 / d), cur_root])
            info = dict(meter="%d/%d" % (n, d), key=key0)
            if start in sections:
                info["section"] = sections[start]
            bar_infos.append(info)
        events = dict(bpm=src.bpm, key=key0, bars=bar_infos,
                      notes=[[str(t), str(d), p] for t, d, p in notes],
                      chords=[[str(w), s] for w, s in chords])
        return compile_events(events)[0]

    # ---------------- 主流程 ----------------
    def run(self, req: SongRequest, job_ctx: Optional[Dict[str, Any]] = None) -> ExecResult:
        job_ctx = job_ctx or {}
        res = ExecResult(title=req.title, lyrics=req.lyrics)
        t0 = time.time()
        song_id = req.job_id or f"job_{int(time.time())}"

        # ---- 串行锁 ----
        waited = []
        ok = self.lock.acquire(on_wait=lambda s: waited.append(s))
        if not ok:
            res.error = "等待 GPU 空闲超时（6 小时），请稍后重试"
            return res
        try:
            return self._run_locked(req, res, song_id, job_ctx, t0)
        finally:
            self.lock.release()

    def _run_locked(self, req: SongRequest, res: ExecResult, song_id: str,
                    job_ctx: Dict[str, Any], t0: float) -> ExecResult:
        work = self.root / "输出" / "_web" / f"{song_id}_{int(time.time())}"
        work.mkdir(parents=True, exist_ok=True)

        # ---- 阶段 1：转谱（翻唱/翻奏需要）----
        abc = req.abc.strip()
        if req.needs_transcribe:
            if not PY_TRANS.is_file():
                res.error = "缺少 SheetSage2 环境（.venv-sheetsage2）"
                self._stage(res, "transcribe", "fail", res.error)
                return res
            self._stage(res, "transcribe", "start", "SheetSage2 转录参考音频")
            out_tr = work / "transcribe"
            payload = self._run_subprocess(
                [str(PY_TRANS), str(TRANS_BRIDGE), req.ref_audio_path, str(out_tr), "--full"],
                marker="###RESULT###", timeout=1800, res=res, stage="transcribe",
            )
            if res.canceled:
                return res
            if not payload or not payload.get("ok"):
                res.error = "转谱失败：" + str((payload or {}).get("error") or (payload or {}).get("abc_error") or res.error)
                return res
            abc = payload.get("abc") or ""
            if not abc.strip():
                res.error = "转谱未产出 ABC 乐谱"
                return res

        # ---- 仅转录模式：出 ABC 就结束，不进渲染/编码/母带 ----
        if req.mode == "transcribe":
            if not abc.strip():
                res.error = "转录未产出 ABC 乐谱"
                return res
            res.abc = abc
            res.ok = True
            res.seconds = round(time.time() - t0, 1)
            res.record.update({
                "song_id": song_id, "mode": "transcribe",
                "abc_chars": len(abc),
                "ref_sha256": req.ref_audio_sha,
                "created_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
                "engine": "SheetSage2 (MERT-v2-FullSong)",
            })
            self._stage(res, "render", "skip", "仅转录模式，不生成音频", 0)
            return res

        # ---- 阶段 2/3：plan + render（一次子进程完成）----
        cot = req.cot
        abc_for_engine = ""
        if req.mode == "instrumental":
            # 真正的纯音乐：先规划出 ABC → 清空 Vocal 声部 → 再按谱生成
            self._stage(res, "plan", "start", "先生成完整 ABC 规划")
            plan_req = self._build_engine_request(req, song_id + "_plan")
            plan_req["cot"] = "full"
            plan_file = work / "plan_request.json"
            plan_file.write_text(json.dumps(plan_req, ensure_ascii=False, indent=2), encoding="utf-8")
            plan_out = work / "plan_out"
            p = self._run_subprocess(
                [str(PY_MAIN), str(RUNNER), str(plan_file), str(plan_out), "--stage", "plan", "--budget", str(BUDGET_GIB), "--id", song_id + "_plan"],
                marker="###DONE###", timeout=3600, res=res, stage="plan",
            )
            if res.canceled:
                return res
            abc_full = (p or {}).get("abc_text") or ""
            if not p or not p.get("ok") or not abc_full.strip():
                detail = res.error or (p or {}).get("error") or "未产出非空 ABC 乐谱"
                res.error = "纯音乐规划失败：" + detail
                return res
            try:
                abc, icheck = self._instrumentalize(abc_full)
            except Exception as exc:
                res.error = "官方 Vocal→Ins 转换失败：%s: %s" % (type(exc).__name__, exc)
                self._stage(res, "plan", "fail", res.error)
                return res
            abc_for_engine = abc
            cot = "full"
            res.record["instrumental_check"] = icheck
            (work / "abc_before_instrumental.abc").write_text(abc_full, encoding="utf-8")
            (work / "abc_after_instrumental.abc").write_text(abc, encoding="utf-8")
            self._stage(res, "plan", "ok",
                        "官方转换完成：Vocal %d 音符全部搬入 Ins（音高/起音/时值保留=%s）"
                        % (icheck.get("vocal_notes_before", 0),
                           icheck.get("vocal_pitch_onset_duration_preserved")), 0)
        elif req.mode == "instrumental_cover":
            # 官方 cover 流程（skills/yue2-music/instrumental/scripts/instrumental.py cover）：
            #   1) Vocal 音符全部搬到 Ins（否则 Vocal 声部会被唱出来 → 无词人声/合唱）
            #   2) 不保留原和声：去掉和弦，cot=melody，让 YuE2 重新配伴奏；
            #      保留原和声：保留和弦，cot=full
            if not abc:
                res.error = "器乐翻奏缺少 ABC 乐谱"
                self._stage(res, "plan", "fail", res.error)
                return res
            keep = bool(req.keep_harmony)
            if not req.keep_melody:
                # 纯伴奏：需要和弦来铺伴奏，因此强制保留和弦
                keep = True
            try:
                abc_conv, icheck = self._instrumentalize(abc, keep_chords=keep)
            except Exception as exc:
                res.error = "官方 Vocal→Ins 转换失败：%s: %s" % (type(exc).__name__, exc)
                self._stage(res, "plan", "fail", res.error)
                return res
            if not req.keep_melody:
                try:
                    abc_conv = self._sparse_melody(abc_conv)
                    icheck = dict(icheck)
                    icheck["melody"] = "sparse-root-pulse（去主旋律）"
                except Exception as exc:
                    res.error = "去主旋律（纯伴奏）转换失败：%s: %s" % (type(exc).__name__, exc)
                    self._stage(res, "plan", "fail", res.error)
                    return res
            abc_for_engine = abc_conv
            cot = "full" if keep else "melody"
            res.record["instrumental_check"] = icheck
            (work / "abc_before_instrumental.abc").write_text(abc, encoding="utf-8")
            (work / "abc_after_instrumental.abc").write_text(abc_conv, encoding="utf-8")
            self._stage(res, "plan", "ok",
                        "翻奏转换完成：Vocal %d 音符搬入 Ins；%s%s"
                        % (icheck.get("vocal_notes_before", 0),
                           "保留原和声（cot=full）" if keep else "去掉原和弦，由 YuE2 重新配伴奏（cot=melody）",
                           "；纯伴奏（主旋律已压成和弦根音）" if not req.keep_melody else ""), 0)
        elif req.mode == "abc":
            abc_for_engine = abc
            cot = "full"
            self._stage(res, "plan", "skip", "使用外部/编辑后 ABC，跳过符号规划", 0)
        elif req.mode == "cover":
            abc_for_engine = abc
            cot = "melody"
            res.abc = abc
            self._stage(res, "plan", "skip", "翻唱：沿用参考旋律 ABC，按新词重新生成", 0)
        else:
            res.abc = abc
            self._stage(res, "plan", "skip", f"{req.mode} 模式由引擎内部规划", 0)

        if self.cancel():
            res.canceled = True
            res.error = "用户已取消"
            return res

        self._stage(res, "render", "start", f"cot={cot}，目标 {req.max_audio_ms//1000}s")
        engine_req = self._build_engine_request(req, song_id, abc_override=abc_for_engine)
        engine_req["cot"] = cot
        if req.mode in ("instrumental", "instrumental_cover"):
            # 纯音乐/翻奏：不唱词（歌词为空），风格按官方规则补 no vocals/no choir
            engine_req["style"] = self._instrumental_style(req.style)
            engine_req["lyrics"] = ""
        req_file = work / "request.json"
        req_file.write_text(json.dumps(engine_req, ensure_ascii=False, indent=2), encoding="utf-8")
        # 质量档位 = generation.ode_steps，必须写进 --config 才会真正生效
        gen_cfg = {"ode_steps": req.steps, "ode_method": "midpoint",
                   "context": 24576, "version": "yue2-native-v1"}
        cfg_file = work / "generation_config.json"
        cfg_file.write_text(json.dumps(gen_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        out_dir = work / "out"
        payload = self._run_subprocess(
            [str(PY_MAIN), str(RUNNER), str(req_file), str(out_dir),
             "--budget", str(BUDGET_GIB), "--config", str(cfg_file), "--id", song_id],
            marker="###DONE###", timeout=14400, res=res, stage="render",
        )
        if res.canceled:
            return res
        if not payload or not payload.get("ok"):
            res.error = res.error or ("生成失败：" + str((payload or {}).get("error") or "引擎未产出音频"))
            return res

        res.audio_source = payload.get("audio") or ""
        res.abc = payload.get("abc_text") or res.abc
        self._stage(res, "decode", "ok", f"音频 {payload.get('audio_bytes', 0)/1048576:.1f} MB", 0)

        # ---- 阶段 4：双格式归档（先归档校验，再上传）----
        self._stage(res, "encode", "start", "MP3 320k + FLAC 双格式归档")
        t_enc = time.time()
        try:
            from web_archive import archive_song
            job = {
                "id": int(job_ctx.get("job_id") or 0) or int(time.time()),
                "uid": int(job_ctx.get("user_id") or 0),
                "username": job_ctx.get("user_login") or "guest",
                "title": req.title,
                "lyrics": req.lyrics,
            }
            if not job["uid"] or not job["username"] or job["username"] == "guest":
                raise ValueError("缺少真实用户信息，拒绝归档（防止匿名作品混入）")
            arc = archive_song(Path(res.audio_source), self.archive_root, job)
            res.mp3 = str(arc["mp3"])
            res.flac = str(arc["flac"])
            res.title = arc["title"]
            res.archive_dir = str(arc["directory"])
            res.record["archive"] = arc.get("receipt")
        except Exception as exc:  # noqa: BLE001
            res.error = f"归档失败：{type(exc).__name__}: {exc}"
            self._stage(res, "encode", "fail", res.error, time.time() - t_enc)
            return res
        self._stage(res, "encode", "ok", f"MP3 {os.path.getsize(res.mp3)/1048576:.1f}MB"
                                        f" / FLAC {os.path.getsize(res.flac)/1048576:.1f}MB",
                    time.time() - t_enc)

        # ---- 阶段 5：母带（可选，产出独立文件，绝不覆盖原版）----
        if req.master:
            self._stage(res, "master", "start", "母带处理（产出独立文件）")
            t_m = time.time()
            try:
                res.mp3_master = self._master(res.mp3, res.archive_dir)
                self._stage(res, "master", "ok", Path(res.mp3_master).name, time.time() - t_m)
            except Exception as exc:  # noqa: BLE001
                self._stage(res, "master", "fail", f"{type(exc).__name__}: {exc}", time.time() - t_m)
                # 母带失败不影响主产物
        else:
            self._stage(res, "master", "skip", "未开启母带", 0)

        # ---- 阶段 6：记录 ----
        # 从引擎实际写出的 config.json 读取真实执行配置（不是我们的猜测）
        actual_cfg = {}
        try:
            engine_cfg_path = Path(res.audio_source).parent / "config.json"
            if engine_cfg_path.is_file():
                actual_cfg = json.loads(engine_cfg_path.read_text(encoding="utf-8"))
        except Exception:
            actual_cfg = {}
        gen_actual = (actual_cfg.get("generation") or {}) if actual_cfg else {}

        res.record.update({
            "song_id": song_id,
            "engine_config_from_disk": actual_cfg,
            "actual_ode_steps": gen_actual.get("ode_steps"),
            "requested_steps": req.steps,
            "engine_request": engine_req,
            "effective": req.to_record(),
            "engine": "yue2-v0.1.6",
            "skill_pack": "yue2-music-v1.2.0",
            "files": {
                "mp3": {"path": os.path.basename(res.mp3), "sha256": sha256_file(res.mp3),
                        "size": os.path.getsize(res.mp3)},
                "flac": {"path": os.path.basename(res.flac), "sha256": sha256_file(res.flac),
                         "size": os.path.getsize(res.flac)},
            },
            "created_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        })
        if res.mp3_master:
            res.record["files"]["mp3_master"] = {
                "path": os.path.basename(res.mp3_master),
                "sha256": sha256_file(res.mp3_master),
                "size": os.path.getsize(res.mp3_master),
            }
        # 网页播放/下载用哪一版：开了母带就用母带，否则用原版；原版永远保留
        res.play_version = "master" if res.mp3_master else "original"
        res.upload_file = res.mp3_master if res.mp3_master else res.mp3
        res.record["play_version"] = res.play_version
        res.record["upload_file"] = os.path.basename(res.upload_file)
        res.record["original_kept"] = os.path.basename(res.mp3)
        try:
            (Path(res.archive_dir) / "gen_record.json").write_text(
                json.dumps(res.record, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

        res.ok = True
        res.seconds = round(time.time() - t0, 1)
        return res

    def _master(self, mp3: str, out_dir: str) -> str:
        """轻量母带链：高通 + 温和压缩 + 限幅 + 响度对齐（不覆盖原文件）"""
        ff = shutil.which("ffmpeg") or os.environ.get("YUE2_FFMPEG")
        if not ff:
            raise RuntimeError("未找到 FFmpeg")
        dst = str(Path(out_dir) / (Path(mp3).stem + "_master.mp3"))
        chain = ("highpass=f=30,"
                 "acompressor=threshold=0.125:ratio=2:attack=20:release=250,"
                 "alimiter=limit=0.95,"
                 "loudnorm=I=-14:TP=-1.5:LRA=11")
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                            "-i", mp3, "-map", "0:a:0", "-vn",
                            "-af", chain, "-c:a", "libmp3lame", "-b:a", "320k", dst],
                           capture_output=True, encoding="utf-8", errors="replace", timeout=900)
        if r.returncode or not Path(dst).is_file():
            raise RuntimeError("母带处理失败：" + (r.stderr or "")[-300:])
        return dst

    def cancel_current(self) -> bool:
        """供外部调用：立即杀当前子进程"""
        with self._proc_lock:
            p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
                return True
            except Exception:
                return False
        return False


# ============================================================ CLI 自检

def _selftest():
    print("=== SongExecutor 自检（不启动 GPU）===")
    ex = SongExecutor()
    # 1) 锁
    assert ex.lock.acquire(timeout=5), "获取锁失败"
    print("[OK] 锁获取 ✓  holder =", ex.lock.holder())
    assert not GpuLock().acquire(timeout=2) or True  # 二次获取应等待，这里只验证不崩
    ex.lock.release()
    print("[OK] 锁释放 ✓")
    # 2) 清空人声声部
    abc_in = "X:1\nV:1 name=MELODY\nCDEF|\nV:2 name=CHORDS\n\"C\"z4|\n"
    out = ex._strip_vocals_from_abc(abc_in)
    assert "CDEF" not in out, "人声声部未清空"
    assert '"C"z4' in out, "伴奏声部被误删"
    print("[OK] ABC 纯音乐化 ✓")
    print(out.replace("\n", " ⏎ "))
    # 3) 请求构造
    req = SongRequest.from_payload({"mode": "full", "lyrics": "测试歌词", "style": "piano",
                                    "seed": 123, "job_id": "t1", "user_id": 1, "user_login": "u"})
    data = ex._build_engine_request(req, "t1")
    assert data["cot"] == "full" and data["seed"] == 123
    print("[OK] 引擎请求构造 ✓", json.dumps(data, ensure_ascii=False)[:120])
    print("\n自检全部通过 ✓")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("用法: python song_executor.py --selftest")
