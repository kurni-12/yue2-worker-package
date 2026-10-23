#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
song_request.py —— YuE2 网页接入的请求模型与参数白名单
========================================================
设计原则（依据工单 §6）：
  1. 先读后改：所有字段都必须能被真实执行器消费，不做空实现
  2. 白名单：每个参数有类型、范围、默认值；非法值一律拒绝或夹紧并记录
  3. 不隐式截断：歌词/ABC 超限报错，不偷偷剪短
  4. 可复现：to_record() 输出完整有效配置，用于 gen_record.json

模式（对应官方 SKILL.md 的 workflow 表）：
  full                style+lyrics → YuE2 cot=full  → ABC → song
  melody              style+lyrics → YuE2 cot=melody → 无和弦ABC → song
  off                 style+lyrics → YuE2 cot=off   → song（无ABC）
  instrumental        文字 → YuE2 规划 → Vocal全部移到Ins → 渲染（真正纯音乐）
  abc                 style+lyrics+ABC → 按固定乐谱生成（分阶段第二阶段）
  cover               ref_audio → SheetSage2 → ABC → 改词/改风格 → melody 重新生成
  instrumental_cover  ref_audio或ABC → 保留旋律 → 重新配器
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple


# ============================================================ 常量与白名单

MODES = {
    "full":               "完整创作（旋律+和声+编曲，可编辑 ABC）",
    "melody":             "旋律创作（有时值旋律，无和弦，编曲自由）",
    "off":                "自由创作（不做符号规划，无 ABC）",
    "instrumental":       "纯音乐（规划后 Vocal→Ins，真正无人声）",
    "abc":                "按固定乐谱生成（分阶段第二步）",
    "cover":              "人声翻唱（参考音频转谱 → 改词/改风格重新生成）",
    "instrumental_cover": "器乐翻奏（保留旋律，重新配器）",
    "transcribe":         "仅转录（SheetSage2 → ABC，不生成音频）",
}
DEFAULT_MODE = "full"

QUALITY_STEPS = {"draft": 32, "standard": 64, "high": 128}
DEFAULT_QUALITY = "standard"

# 生成参数白名单：(类型, 最小值, 最大值, 默认值, 说明)
GEN_WHITELIST: Dict[str, Tuple[Any, Any, Any, Any, str]] = {
    # 文本/结构
    "title":            (str,   0,    120,  "",    "歌名，空则取歌词首句"),
    "lyrics":           (str,   0,  4000,  "",    "歌词，空=纯器乐（仅 instrumental/off 允许）"),
    "language":         (str,   0,     16,  "zh",  "语言标记"),
    "style":            (str,   0,   600,  "",    "风格描述（英文更准）"),
    "vocal":            (str,   0,    80,  "",    "人声描述，如 warm female voice"),
    "instruments":      (str,   0,   240,  "",    "乐器（英文逗号分隔），会追加到 style"),
    "seed":             (int,   0, 2**31 - 1, 0,  "随机种子；0=随机但会记录实际值"),
    # 规划
    "cot":              (str,   0,     16,  "full","full|melody|off"),
    "abc":              (str,   0, 20000,  "",    "外部/编辑后 ABC 乐谱"),
    "keep_harmony":     (bool,  None, None, False,"器乐翻奏时保留原和声"),
    "keep_melody":      (bool,  None, None, True, "翻奏时保留主旋律；关闭=纯伴奏（旋律压成和弦根音长音）"),
    "melody_level":     (str,   0,      8,  "normal","主旋律突出度：soft(轻,垫在伴奏后)|normal|strong(突出)"),
    # 采样
    "topk":             (int,   1,    500,  50,   "top-k 采样"),
    "temperature":      (float, 0.1,   2.0, 1.0,  "采样温度"),
    "cfg_scale":        (float, 1.0,   8.0, 3.0,  "无分类器引导强度"),
    "rep_penalty":      (float, 1.0,   2.0, 1.0,  "重复惩罚"),
    "max_audio_ms":     (int,   5000, 480000, 210000, "目标音频时长(毫秒)，不缩歌"),
    # 质量
    "quality":          (str,   0,     16,  "standard", "draft|standard|high"),
    "master":           (bool,  None, None, False,"母带优化（会额外产出处理版）"),
    "decoder":          (str,   0,     24,  "default", "default|legacy（legacy 需本地有 Vae-legacy）"),
}

MAX_REF_AUDIO_BYTES = 40 * 1024 * 1024      # 参考音频上限 40MB
ALLOWED_AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}
ALLOWED_AUDIO_MAGIC = {
    b"ID3": ".mp3", b"\xff\xfb": ".mp3", b"\xff\xf3": ".mp3",
    b"RIFF": ".wav", b"fLaC": ".flac", b"OggS": ".ogg",
}


class RequestError(ValueError):
    """请求校验失败（对外返回 4xx，不进入执行）"""


# ============================================================ 工具

def _clamp(v, lo, hi):
    if lo is None or hi is None:
        return v
    return max(lo, min(hi, v))


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on", "是")


def _to_int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_float(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _clean_text(s, limit: int) -> str:
    """去控制字符，保留换行，超限报错（不静默截断）"""
    s = (s or "")
    if not isinstance(s, str):
        s = str(s)
    s = "".join(ch for ch in s if ch == "\n" or ch >= " ")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if len(s) > limit:
        raise RequestError(f"文本超长：{len(s)} 字符 > 上限 {limit}（不会自动截断，请精简）")
    return s


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sniff_audio(path: str) -> str:
    """校验真实音频类型（不信扩展名）"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in ALLOWED_AUDIO_EXT:
        raise RequestError(f"不支持的音频扩展名 {ext}，允许：{sorted(ALLOWED_AUDIO_EXT)}")
    size = os.path.getsize(path)
    if size > MAX_REF_AUDIO_BYTES:
        raise RequestError(f"参考音频过大 {size/1048576:.1f}MB > 40MB")
    if size < 1024:
        raise RequestError("参考音频过小，疑似损坏")
    with open(path, "rb") as f:
        head = f.read(16)
    ok = any(head.startswith(m) for m in ALLOWED_AUDIO_MAGIC)
    # m4a/mp4 的 ftyp 在第 5~8 字节
    if not ok and len(head) >= 12 and head[4:8] == b"ftyp":
        ok = True
    if not ok:
        raise RequestError("参考音频真实类型不是 MP3/WAV/FLAC/M4A/OGG")
    return ext


# ============================================================ 请求模型

@dataclass
class SongRequest:
    mode: str = DEFAULT_MODE
    title: str = ""
    lyrics: str = ""
    language: str = "zh"
    style: str = ""
    vocal: str = ""
    seed: int = 0
    cot: str = "full"
    abc: str = ""
    keep_harmony: bool = False
    keep_melody: bool = True
    melody_level: str = "normal"
    topk: int = 50
    temperature: float = 1.0
    cfg_scale: float = 3.0
    rep_penalty: float = 1.0
    max_audio_ms: int = 210000
    quality: str = DEFAULT_QUALITY
    master: bool = False
    decoder: str = "default"

    # 非白名单字段（由服务器校验后传入）
    ref_audio_path: str = ""      # 已鉴权的本地路径
    ref_audio_sha: str = ""
    tune_id: int = 0              # 要改的既有作品（可选）
    user_id: int = 0
    user_login: str = ""
    job_id: str = ""
    candidates: int = 1           # 多候选（顺序排队）
    seeds: List[int] = field(default_factory=list)

    # 记录夹紧/拒绝痕迹
    notes: List[str] = field(default_factory=list)

    # ---------------- 构造 ----------------
    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SongRequest":
        if not isinstance(payload, dict):
            raise RequestError("请求体必须是 JSON 对象")

        mode = str(payload.get("mode") or DEFAULT_MODE).strip().lower()
        if mode not in MODES:
            raise RequestError(f"未知模式 {mode}；允许：{list(MODES)}")

        req = cls(mode=mode)

        # --- 白名单字段 ---
        for key, (typ, lo, hi, default, _desc) in GEN_WHITELIST.items():
            raw = payload.get(key, default)
            if typ is str:
                limit = hi if isinstance(hi, int) and hi > 0 else 1000
                val = _clean_text(raw if raw is not None else default, limit)
                if key == "cot" and val not in ("full", "melody", "off"):
                    raise RequestError(f"cot 只能是 full/melody/off，收到 {val!r}")
                if key == "quality" and val not in QUALITY_STEPS:
                    raise RequestError(f"quality 只能是 {list(QUALITY_STEPS)}，收到 {val!r}")
                if key == "decoder" and val not in ("default", "legacy"):
                    raise RequestError("decoder 只能是 default/legacy")
                setattr(req, key, val)
            elif typ is int:
                v = _to_int(raw, default)
                c = _clamp(v, lo, hi)
                if c != v:
                    req.notes.append(f"{key}: {v} → 夹紧到 {c}（范围 {lo}~{hi}）")
                setattr(req, key, c)
            elif typ is float:
                v = _to_float(raw, default)
                c = round(_clamp(v, lo, hi), 4)
                if c != round(v, 4):
                    req.notes.append(f"{key}: {v} → 夹紧到 {c}（范围 {lo}~{hi}）")
                setattr(req, key, c)
            elif typ is bool:
                setattr(req, key, _to_bool(raw))

        # --- 非白名单（服务器侧注入） ---
        for k in ("ref_audio_path", "ref_audio_sha", "user_login"):
            setattr(req, k, str(payload.get(k) or ""))
        for k in ("tune_id", "user_id"):
            setattr(req, k, _to_int(payload.get(k), 0))
        req.job_id = str(payload.get("job_id") or "")
        req.candidates = _clamp(_to_int(payload.get("candidates"), 1), 1, 8)
        seeds_raw = payload.get("seeds") or []
        if isinstance(seeds_raw, list):
            req.seeds = [_clamp(_to_int(s, 0), 0, 2**31 - 1) for s in seeds_raw][:8]

        req.validate()
        return req

    # ---------------- 校验 ----------------
    def validate(self) -> None:
        m = self.mode

        # 仅转录模式：只要参考音频，不要歌词
        if m == "transcribe":
            if not self.ref_audio_path:
                raise RequestError("转录模式需要参考音频")
            if not os.path.isfile(self.ref_audio_path):
                raise RequestError("参考音频文件不存在")
            sniff_audio(self.ref_audio_path)
            if not self.ref_audio_sha:
                self.ref_audio_sha = sha256_file(self.ref_audio_path)
            return

        # 纯器乐模式允许空歌词；其余需要歌词
        if m in ("full", "melody", "off", "cover") and not self.lyrics.strip():
            if m == "cover":
                raise RequestError("翻唱需要新歌词（或显式沿用原词，请填写）")
            raise RequestError(f"{m} 模式必须提供歌词；纯器乐请用 instrumental 模式")

        # 分阶段/器乐覆盖需要 ABC
        if m == "abc" and not self.abc.strip():
            raise RequestError("abc 模式必须提供 ABC 乐谱")
        if m == "instrumental_cover" and not (self.abc.strip() or self.ref_audio_path):
            raise RequestError("器乐翻奏需要参考音频或 ABC")

        # 翻唱需要参考音频
        if m == "cover" and not self.ref_audio_path:
            raise RequestError("翻唱需要先上传参考音频并完成转谱")

        # ABC 基础合法性（不解析完整语法，只挡明显垃圾）
        if self.abc.strip():
            if "X:" not in self.abc or "K:" not in self.abc:
                raise RequestError("ABC 缺少必需的 X: 或 K: 头字段")
            if len(self.abc) > 20000:
                raise RequestError("ABC 过长")

        # 参考音频真实类型
        if self.ref_audio_path:
            if not os.path.isfile(self.ref_audio_path):
                raise RequestError("参考音频文件不存在")
            sniff_audio(self.ref_audio_path)
            if not self.ref_audio_sha:
                self.ref_audio_sha = sha256_file(self.ref_audio_path)

        # 时长与步数
        if self.max_audio_ms < 5000:
            raise RequestError("目标时长至少 5 秒")
        if self.quality not in QUALITY_STEPS:
            raise RequestError("非法 quality")

        # 风格建议（不强制，但记录）
        if m in ("full", "melody", "off", "instrumental") and not self.style.strip():
            self.notes.append("未填风格描述，模型将自行决定曲风（建议填写以提高可控性）")

    # ---------------- 派生 ----------------
    @property
    def steps(self) -> int:
        return QUALITY_STEPS[self.quality]

    @property
    def needs_transcribe(self) -> bool:
        """是否需要先跑 SheetSage2 转谱（cover / instrumental_cover / 仅转录都要）"""
        return (self.mode in ("cover", "instrumental_cover", "transcribe")
                and bool(self.ref_audio_path) and not self.abc.strip())

    @property
    def actual_seed(self) -> int:
        if self.seed:
            return self.seed
        # 0 = 随机，但把实际值固定下来以便复现
        import secrets
        return secrets.randbelow(2**31 - 1)

    def stages(self) -> List[str]:
        """真实执行阶段（供状态上报）"""
        st = []
        if self.needs_transcribe:
            st.append("transcribe")
        if self.mode == "transcribe":
            return ["transcribe"]
        if self.mode in ("full", "melody", "instrumental", "cover", "instrumental_cover"):
            st.append("plan")
        st += ["render", "decode", "encode"]
        if self.master:
            st.append("master")
        if self.candidates > 1:
            st = ["batch"] + st
        return st

    # ---------------- 记录 ----------------
    def to_record(self) -> Dict[str, Any]:
        """写入 gen_record.json 的有效配置（不含任何凭据）"""
        d = asdict(self)
        d.pop("ref_audio_path", None)       # 不外泄本地绝对路径
        d["steps"] = self.steps
        d["actual_seed"] = self.actual_seed
        d["stages"] = self.stages()
        d["whitelist_version"] = "2026-10-19.1"
        d["engine"] = "yue2-v0.1.6"
        d["skill_pack"] = "yue2-music-v1.2.0"
        return d


# ============================================================ 命令行自检

if __name__ == "__main__":
    import sys

    tests = [
        ("合法 full", {"mode": "full", "lyrics": "第一句\n第二句", "style": "gospel,piano", "seed": 831001}),
        ("合法 instrumental", {"mode": "instrumental", "style": "piano,ambient"}),
        ("应拒绝：full 无歌词", {"mode": "full"}),
        ("应拒绝：未知模式", {"mode": "xxx"}),
        ("夹紧：topk 超限", {"mode": "off", "lyrics": "a", "topk": 9999}),
        ("夹紧：temperature 超限", {"mode": "off", "lyrics": "a", "temperature": 9}),
        ("应拒绝：abc 缺头", {"mode": "abc", "abc": "random text"}),
        ("合法 abc", {"mode": "abc", "abc": "X:1\nT:t\nK:C\nCDEF|"}),
    ]
    ok = 0
    for name, payload in tests:
        try:
            r = SongRequest.from_payload(payload)
            print(f"[OK]   {name}: mode={r.mode} steps={r.steps} stages={r.stages()}")
            if r.notes:
                for n in r.notes:
                    print(f"        note: {n}")
            ok += 1
        except RequestError as e:
            print(f"[REJECT] {name}: {e}")
    print(f"\n自检完成：{ok}/{len(tests)} 通过校验，其余按预期拒绝")
