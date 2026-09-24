#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nemtron_worker.py
============================================================
Nemotron Scratch Inspector 的 Python 推理 worker。

输入（stdin, JSON）：
    {
      "q":   "请检测图片中的划痕",
      "img": "<base64 / dataURL / http(s) URL>"
    }

输出（stdout, JSON）：
    {
      "ok": true,
      "prompt": "...",
      "intent": "scratch",          # 解析出的检测意图
      "score": 0.87,                # 文本与意图的余弦相似度（Nemotron）
      "embed_model": "nvidia/Nemotron-3-Embed-1B-BF16",
      "device": "cpu",
      "model_loaded": true,
      "img_w": 800, "img_h": 600,
      "regions": [                  # 检测到的候选区域（最多 16 个，按 score 降序）
        {"id":0, "x":123, "y":45, "w":80, "h":22, "score":0.92,
         "severity":"high", "label":"scratch", "note":"长条状高密度边缘"}
      ],
      "summary": {"total":3, "high":1, "medium":1, "low":1, "none":0},
      "png_base64": "..."           # 标注后的 PNG 图，base64 编码
    }

Nemotron 在这里是文本嵌入模型（Nemotron-3-Embed-1B-BF16）：
    1) 用它把用户 prompt 嵌入到向量空间
    2) 用同一向量空间嵌入若干意图候选（scratch / dent / stain / crack / normal …）
    3) 余弦相似度最大的即检测意图；score 用作文本侧的置信度
图片侧用 NumPy + Pillow 做传统 CV 检测：
    - 灰度化 / 自适应直方图均衡
    - 梯度（Sobel）+ Canny-like 边缘
    - 形态学闭运算 + 连通区域
    - 几何筛选（长宽比、面积、边缘密度）→ 候选 region
    - 与意图做关联（scratch → 长条 / dent → 椭圆块 / stain → 大面积低纹理 …）
"""
import os
import sys
import io
import json
import base64
import time
import math
import urllib.request
import traceback
from typing import List, Tuple, Dict, Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# -------------------- Nemotron 文本嵌入 --------------------
# 官方推荐用法（来自 HuggingFace model card）：
#   https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16
# 关键点：
#   1) query 必须带 "query:" 前缀；document 必须带 "passage:" 前缀
#   2) 使用 sentence-transformers 的 encode_query / encode_document
#   3) dtype 必须为 torch.bfloat16
#   4) attn_implementation 推荐 flash_attention_2（CPU 自动忽略）
#   5) max_seq_length 32768
#   6) 嵌入维度 2048，可用 matryoshka 截断到 1024/512
#   7) 相似度用 model.similarity() 或手工 L2-normalize + dot product
_NEMOTRON_MODEL = None
_NEMOTRON_TOKENIZER = None
_NEMOTRON_DEVICE = os.environ.get("NEMOTRON_DEVICE", "cpu")
_NEMOTRON_NAME = os.environ.get("NEMOTRON_MODEL", "nvidia/Nemotron-3-Embed-1B-BF16")
_NEMOTRON_DIM = int(os.environ.get("NEMOTRON_DIM", "2048"))  # matryoshka 截断维度
_INTENT_PROTOTYPES = None
_MODEL_LOAD_ERROR = None
_HAS_TORCH = False
_HAS_ST = False

try:
    import torch  # noqa: F401
    _HAS_TORCH = True
except Exception:
    _HAS_TORCH = False

try:
    from sentence_transformers import SentenceTransformer
    _HAS_ST = True
except Exception:
    _HAS_ST = False


# Nemotron 推荐的 query / passage 前缀常量
NEMOTRON_QUERY_PREFIX = "query: "
NEMOTRON_PASSAGE_PREFIX = "passage: "


# 候选意图原型（中英双语，每个候选用一个 group，组内取平均再归一）
INTENT_GROUPS: Dict[str, List[str]] = {
    "scratch": [
        "a long thin scratch on the surface",
        "linear scratch mark on metal or glass",
        "细长划痕", "表面划伤", "刮痕", "线性划痕",
    ],
    "dent": [
        "a circular dent or depression",
        "concave deformation on metal surface",
        "凹陷", "凹坑", "压痕",
    ],
    "crack": [
        "branching crack fracture line",
        "cracked surface with sharp jagged edges",
        "裂纹", "开裂", "裂缝",
    ],
    "stain": [
        "an irregular stain or smudge",
        "discoloration spot on surface",
        "污渍", "斑点", "污染",
    ],
    "corrosion": [
        "rusty corrosion area on metal",
        "oxidized spot with rough texture",
        "锈蚀", "氧化", "腐蚀",
    ],
    "normal": [
        "clean smooth normal surface without defects",
        "a flawless surface",
        "正常表面", "无瑕疵",
    ],
}


def _load_model_lazy():
    """懒加载 Nemotron 模型。失败不会抛，只把错误记到 _MODEL_LOAD_ERROR。

    官方推荐配置（Nemotron-3-Embed-1B-BF16）：
      - dtype=torch.bfloat16
      - attn_implementation=flash_attention_2（CPU 自动回退）
      - max_seq_length=32768
      - trust_remote_code=True
    """
    global _NEMOTRON_MODEL, _INTENT_PROTOTYPES, _MODEL_LOAD_ERROR
    if _NEMOTRON_MODEL is not None:
        return True
    if not (_HAS_TORCH and _HAS_ST):
        _MODEL_LOAD_ERROR = "torch 或 sentence-transformers 未安装"
        return False
    try:
        # Nemotron-3-Embed-1B-BF16 是 SentenceTransformer 兼容模型
        # 官方推荐的 model_kwargs：bfloat16 dtype + flash_attention_2
        model_kwargs = {
            "dtype": torch.bfloat16,
            "attn_implementation": "flash_attention_2",
        }
        # CPU 没有 flash_attn，强制指定为 sdpa/eager 让 transformers 选一个能跑的
        if str(_NEMOTRON_DEVICE).lower() == "cpu":
            model_kwargs["attn_implementation"] = "eager"

        kwargs = {
            "device": _NEMOTRON_DEVICE,
            "trust_remote_code": True,
            "model_kwargs": model_kwargs,
        }
        try:
            _NEMOTRON_MODEL = SentenceTransformer(_NEMOTRON_NAME, **kwargs)
        except TypeError:
            # 老版 sentence-transformers 不支持 model_kwargs → 退回无参形式
            kwargs.pop("model_kwargs", None)
            _NEMOTRON_MODEL = SentenceTransformer(_NEMOTRON_NAME, **kwargs)
        except Exception:
            # attn_implementation 不被支持 → 改为 sdpa
            model_kwargs["attn_implementation"] = "sdpa"
            kwargs["model_kwargs"] = model_kwargs
            _NEMOTRON_MODEL = SentenceTransformer(_NEMOTRON_NAME, **kwargs)

        # 设置最大序列长度
        try:
            _NEMOTRON_MODEL.max_seq_length = 32768
        except Exception:
            pass

        # 预计算意图原型向量（passages 必须带 "passage: " 前缀）
        protos = {}
        for name, texts in INTENT_GROUPS.items():
            # Nemotron 推荐：document 侧加 "passage: " 前缀
            passages = [NEMOTRON_PASSAGE_PREFIX + t for t in texts]
            vecs = _NEMOTRON_MODEL.encode_document(
                passages,
                batch_size=8,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            # Matryoshka 截断到 NEMOTRON_DIM（如 1024/512/2048）
            if _NEMOTRON_DIM and _NEMOTRON_DIM < vecs.shape[1]:
                vecs = vecs[:, :_NEMOTRON_DIM].copy()
                norms = np.linalg.norm(vecs, axis=1, keepdims=True)
                vecs = vecs / (norms + 1e-9)
            mean = vecs.mean(axis=0)
            mean = mean / (np.linalg.norm(mean) + 1e-9)
            protos[name] = mean.astype(np.float32)
        _INTENT_PROTOTYPES = protos
        return True
    except Exception as e:
        _MODEL_LOAD_ERROR = f"{type(e).__name__}: {e}"
        return False


def _embed_text(s: str) -> np.ndarray:
    """文本 → 单位向量。模型未加载时用哈希回退，保证 worker 不挂。

    Nemotron 推荐用 encode_query，自动加 "query: " 前缀并按 query 模式编码。
    """
    if _NEMOTRON_MODEL is None and not _load_model_lazy():
        return _fallback_embed(s)
    try:
        # encode_query 自动应用 "query: " 前缀并执行 mean-pool + L2-normalize
        v = _NEMOTRON_MODEL.encode_query(
            [s],
            batch_size=1,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )[0]
        # Matryoshka 截断（如配置了 NEMOTRON_DIM=1024/512）
        if _NEMOTRON_DIM and _NEMOTRON_DIM < v.shape[0]:
            v = v[:_NEMOTRON_DIM].copy()
            n = np.linalg.norm(v)
            v = v / (n + 1e-9) if n > 0 else v
        return v.astype(np.float32)
    except Exception:
        return _fallback_embed(s)


def _fallback_embed(s: str, dim: int = 384) -> np.ndarray:
    """无模型时，用字符 n-gram + 哈希 + 归一化做近似嵌入。"""
    v = np.zeros(dim, dtype=np.float32)
    s = (s or "").lower()
    for i, ch in enumerate(s):
        h = hash(ch) % dim
        v[h] += 1.0
    for i in range(len(s) - 1):
        h = (hash(s[i]) * 31 + hash(s[i + 1])) % dim
        v[h] += 0.5
    n = np.linalg.norm(v)
    return v / (n + 1e-9) if n > 0 else v


def _classify_intent(q: str) -> Tuple[str, float, np.ndarray]:
    """用 Nemotron 把 q 嵌入，再与各意图原型做余弦相似度，返回最优意图 + score + 向量。"""
    v = _embed_text(q)
    if _INTENT_PROTOTYPES is None:
        # 模型不可用 → 关键词回退
        ql = (q or "").lower()
        for name in INTENT_GROUPS.keys():
            for kw in INTENT_GROUPS[name]:
                if kw and kw.lower() in ql:
                    return name, 0.6, v
        return "scratch", 0.5, v
    sims = {name: float(np.dot(v, p)) for name, p in _INTENT_PROTOTYPES.items()}
    name = max(sims, key=sims.get)
    return name, sims[name], v


def _compute_intent_scores(q: str) -> Dict[str, float]:
    """计算 query 与各意图原型的余弦相似度。

    优先用 sentence-transformers 的 model.similarity()（与官方推荐对齐）；
    不可用时退化到 numpy dot product（已 L2-normalized → 等价于 cosine）。
    """
    if _INTENT_PROTOTYPES is None:
        return {n: 0.0 for n in INTENT_GROUPS}
    v = _embed_text(q)
    # 试一次 model.similarity（更高效且与官方一致）
    try:
        if _NEMOTRON_MODEL is not None and hasattr(_NEMOTRON_MODEL, "similarity"):
            import torch
            proto_matrix = np.stack([_INTENT_PROTOTYPES[n] for n in INTENT_GROUPS], axis=0)
            q_t = torch.from_numpy(v).unsqueeze(0)
            p_t = torch.from_numpy(proto_matrix)
            sims = _NEMOTRON_MODEL.similarity(q_t, p_t)
            if hasattr(sims, "cpu"):
                sims = sims.cpu().numpy()
            sims = np.asarray(sims).reshape(-1)
            return {n: round(float(s), 4) for n, s in zip(INTENT_GROUPS.keys(), sims)}
    except Exception:
        pass
    # 回退：numpy dot product（双方都已 L2 归一化 → 等价 cosine）
    return {n: round(float(np.dot(v, _INTENT_PROTOTYPES[n])), 4) for n in INTENT_GROUPS}


# -------------------- 图像加载 --------------------
def _decode_img(s: str) -> Image.Image:
    s = (s or "").strip()
    if s.startswith("http://") or s.startswith("https://"):
        with urllib.request.urlopen(s, timeout=20) as r:
            data = r.read()
        return Image.open(io.BytesIO(data)).convert("RGB")
    if "," in s and s.lstrip().startswith("data:"):
        s = s.split(",", 1)[1]
    data = base64.b64decode(s)
    return Image.open(io.BytesIO(data)).convert("RGB")


# -------------------- 视觉侧：传统 CV 检测 --------------------
def _to_gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.float32) / 255.0


def _clahe(gray: np.ndarray, k: int = 64, clip: float = 2.0) -> np.ndarray:
    """NumPy 版简化 CLAHE（局部直方图均衡）。"""
    H, W = gray.shape
    out = np.empty_like(gray)
    h0 = max(1, H // k)
    w0 = max(1, W // k)
    for y in range(0, H, h0):
        for x in range(0, W, w0):
            y1, x1 = min(H, y + h0), min(W, x + w0)
            tile = gray[y:y1, x:x1]
            lo, hi = np.percentile(tile, (clip, 100 - clip))
            if hi - lo < 1e-6:
                out[y:y1, x:x1] = tile
                continue
            t = np.clip((tile - lo) / (hi - lo), 0, 1)
            out[y:y1, x:x1] = t
    return out


def _sobel_mag(g: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(g); gy = np.zeros_like(g)
    gx[:, 1:-1] = g[:, 2:] - g[:, :-2]
    gy[1:-1, :] = g[2:, :] - g[:-2, :]
    return np.sqrt(gx * gx + gy * gy)


def _canny_edges(g: np.ndarray, lo: float = 0.04, hi: float = 0.12) -> np.ndarray:
    sm = _sobel_mag(g)
    sm = sm / (sm.max() + 1e-9)
    th, tl = hi, lo
    strong = sm >= th
    weak = (sm >= tl) & (sm < th)
    edge = np.zeros_like(sm, dtype=bool)
    edge[strong] = True
    # 简化版：weak 若 8-邻域有 strong 则提升为 edge
    for _ in range(2):
        nb = np.zeros_like(edge)
        nb[1:, :] |= edge[:-1, :]
        nb[:-1, :] |= edge[1:, :]
        nb[:, 1:] |= edge[:, :-1]
        nb[:, :-1] |= edge[:, 1:]
        promote = weak & nb
        edge = edge | promote
        weak = weak & (~promote)
    return edge.astype(np.uint8) * 255


def _morph_close(bin_img: np.ndarray, k: int = 5) -> np.ndarray:
    """形态学闭运算（先膨胀后腐蚀），连通断裂的边缘。"""
    k = max(3, int(k) | 1)
    pad = k // 2
    p = np.pad(bin_img, pad, mode="edge")
    H, W = bin_img.shape
    # 二值形态学：腐蚀 = 所有邻域都为 255
    def _dilate(src):
        out = np.zeros_like(src)
        s = (src > 0).astype(np.uint8)
        ps = np.pad(s, pad, mode="constant")
        # 用 3x3 结构元
        for dy in range(k):
            for dx in range(k):
                out |= ps[dy:dy + H, dx:dx + W]
        return (out > 0).astype(np.uint8) * 255

    def _erode(src):
        out = np.zeros_like(src)
        s = (src > 0).astype(np.uint8)
        ps = np.pad(s, pad, mode="constant")
        cnt = k * k
        for dy in range(k):
            for dx in range(k):
                out += ps[dy:dy + H, dx:dx + W]
        return (out == cnt).astype(np.uint8) * 255

    return _erode(_dilate(bin_img))


def _connected_components(mask: np.ndarray) -> List[Dict[str, Any]]:
    """4-连通域标记。返回每个组件的 bbox、像素数、质心、外接框长宽比。"""
    H, W = mask.shape
    lbl = np.zeros((H, W), dtype=np.int32)
    cur = 0
    stack: List[Tuple[int, int]] = []
    comps: List[Dict[str, Any]] = []
    for y in range(H):
        for x in range(W):
            if mask[y, x] > 0 and lbl[y, x] == 0:
                cur += 1
                lbl[y, x] = cur
                stack.append((y, x))
                minx = maxx = x; miny = maxy = y
                area = 0
                while stack:
                    cy, cx = stack.pop()
                    area += 1
                    if cx < minx: minx = cx
                    if cx > maxx: maxx = cx
                    if cy < miny: miny = cy
                    if cy > maxy: maxy = cy
                    if cy > 0 and mask[cy - 1, cx] and lbl[cy - 1, cx] == 0:
                        lbl[cy - 1, cx] = cur; stack.append((cy - 1, cx))
                    if cy < H - 1 and mask[cy + 1, cx] and lbl[cy + 1, cx] == 0:
                        lbl[cy + 1, cx] = cur; stack.append((cy + 1, cx))
                    if cx > 0 and mask[cy, cx - 1] and lbl[cy, cx - 1] == 0:
                        lbl[cy, cx - 1] = cur; stack.append((cy, cx - 1))
                    if cx < W - 1 and mask[cy, cx + 1] and lbl[cy, cx + 1] == 0:
                        lbl[cy, cx + 1] = cur; stack.append((cy, cx + 1))
                comps.append({
                    "id": cur - 1, "x": int(minx), "y": int(miny),
                    "w": int(maxx - minx + 1), "h": int(maxy - miny + 1),
                    "area": int(area),
                })
    return comps


def _shape_features(comp: Dict[str, Any]) -> Dict[str, float]:
    """从 bbox 推出粗略形状特征（无 OpenCV）。"""
    w, h = comp["w"], comp["h"]
    a = comp["area"]
    aspect = max(w, h) / max(1, min(w, h))
    fill = a / max(1, w * h)  # 填充率
    elong = (max(w, h) - min(w, h)) / max(1, max(w, h))
    return {"aspect": aspect, "fill": fill, "elong": elong, "area_log": math.log10(a + 1)}


def _intent_match_score(intent: str, feat: Dict[str, float]) -> float:
    """把意图映射到形状特征：scratch → 细长；dent → 接近圆；stain → 大面积低填充；normal → 小。"""
    a, f, e = feat["aspect"], feat["fill"], feat["elong"]
    al = feat["area_log"]
    if intent == "scratch":
        return float(min(1.0, 0.35 * (a - 1.0) + 0.35 * e + 0.20 * f + 0.10 * al / 4.0))
    if intent == "crack":
        return float(min(1.0, 0.45 * e + 0.30 * (a - 1.0) / 2.0 + 0.25 * f))
    if intent == "dent":
        # 中等面积 + 较高填充 + 接近圆
        return float(min(1.0, 0.45 * f + 0.30 * max(0, 1.0 - (a - 1.5) / 2.0) + 0.25 * (al / 4.0)))
    if intent == "stain":
        return float(min(1.0, 0.55 * al / 4.0 + 0.30 * f + 0.15 * (1.0 - e)))
    if intent == "corrosion":
        return float(min(1.0, 0.40 * al / 4.0 + 0.30 * (1.0 - f) + 0.30 * (1.0 - e)))
    return float(min(1.0, 0.4 * al / 4.0))


def detect_regions(img: Image.Image, intent: str) -> Tuple[List[Dict[str, Any]], np.ndarray]:
    """主检测流程。返回 (regions, heatmap[H,W] in [0,1])。"""
    W, H = img.size
    g = _to_gray(img)
    g = _clahe(g)
    edges = _canny_edges(g)
    closed = _morph_close(edges, k=5)
    comps = _connected_components(closed)

    # 过滤：去掉太小 / 太大 / 紧贴边框的噪声
    H_, W_ = closed.shape
    regions: List[Dict[str, Any]] = []
    for c in comps:
        area = c["area"]
        if area < 30: continue
        if area > 0.4 * W_ * H_: continue
        if c["x"] <= 1 or c["y"] <= 1 or c["x"] + c["w"] >= W_ - 1 or c["y"] + c["h"] >= H_ - 1:
            # 紧贴边缘的，保留但降权
            pass
        feat = _shape_features(c)
        s = _intent_match_score(intent, feat)
        if s < 0.05:
            continue
        regions.append({
            "id": c["id"], "x": c["x"], "y": c["y"], "w": c["w"], "h": c["h"],
            "area": area, "score": s, "feat": feat,
        })
    # 按 score 降序，最多 16 个
    regions.sort(key=lambda r: r["score"], reverse=True)
    regions = regions[:16]

    # 生成 heatmap：把每个 region 用二维高斯涂到画布上
    heat = np.zeros((H_, W_), dtype=np.float32)
    for r in regions:
        cx, cy = r["x"] + r["w"] / 2.0, r["y"] + r["h"] / 2.0
        rx, ry = max(8.0, r["w"] / 1.5), max(8.0, r["h"] / 1.5)
        ys, xs = np.mgrid[0:H_, 0:W_]
        g2d = np.exp(-((xs - cx) ** 2 / (2 * rx * rx) + (ys - cy) ** 2 / (2 * ry * ry)))
        heat += r["score"] * g2d
    if heat.max() > 0:
        heat = heat / heat.max()
    return regions, heat


# -------------------- 可视化 --------------------
def _score_to_severity(score: float) -> str:
    if score >= 0.66: return "high"
    if score >= 0.33: return "medium"
    if score >= 0.10: return "low"
    return "none"


def _draw_annotation(img: Image.Image, regions: List[Dict[str, Any]],
                     heat: np.ndarray, intent: str, prompt: str,
                     text_score: float) -> Image.Image:
    """在原图上绘制 bbox + 热力图叠加 + 顶部信息条。"""
    out = img.convert("RGBA").copy()
    W, H = out.size

    # 热力图叠加（JET 调色板）
    if heat.max() > 0:
        h8 = (np.clip(heat, 0, 1) * 255).astype(np.uint8)
        hm = Image.fromarray(h8, mode="L").resize((W, H), Image.BILINEAR)
        # 调色板：蓝→青→绿→黄→红
        palette = Image.new("P", (256, 1))
        pal = []
        for i in range(256):
            t = i / 255.0
            r = int(255 * max(0, min(1, 1.5 - abs(4 * t - 3))))
            g = int(255 * max(0, min(1, 1.5 - abs(4 * t - 2))))
            b = int(255 * max(0, min(1, 1.5 - abs(4 * t - 1))))
            pal.extend([r, g, b])
        palette.putpalette(pal)
        hm = hm.convert("L").point(lambda v: v).quantize(palette=palette).convert("RGBA")
        # 只显示热区：把低值像素变透明
        alpha = (np.array(hm.split()[0]).astype(np.float32) / 255.0)
        alpha = np.clip(alpha * 0.6, 0, 0.55) * 255
        hm.putalpha(Image.fromarray(alpha.astype(np.uint8), mode="L"))
        out.alpha_composite(hm)

    draw = ImageDraw.Draw(out)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    except Exception:
        font = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # 顶部信息条
    bar_h = 56
    bar = Image.new("RGBA", (W, bar_h), (15, 23, 42, 230))
    out.alpha_composite(bar, (0, 0))
    draw = ImageDraw.Draw(out)
    title = f"Nemotron Inspector  intent={intent}  text-score={text_score:.2f}"
    draw.text((12, 6), title, fill=(226, 232, 240, 255), font=font)
    draw.text((12, 28), f"prompt: {prompt[:80]}", fill=(148, 163, 184, 255), font=font_small)

    # bbox + 标签
    palette_rgb = [
        (239, 68, 68, 255),    # high red
        (245, 158, 11, 255),   # medium amber
        (34, 197, 94, 255),    # low green
    ]
    for i, r in enumerate(regions):
        sev = _score_to_severity(r["score"])
        idx = {"high": 0, "medium": 1, "low": 2}.get(sev, 2)
        col = palette_rgb[idx]
        x, y, w, h = r["x"] * W / heat.shape[1], r["y"] * H / heat.shape[0], \
                     r["w"] * W / heat.shape[1], r["h"] * H / heat.shape[0]
        draw.rectangle([x, y, x + w, y + h], outline=col, width=3)
        label = f"#{i} {sev} {r['score']:.2f}"
        tw = draw.textlength(label, font=font_small) if hasattr(draw, "textlength") else len(label) * 7
        ly = max(0, y - 18)
        draw.rectangle([x, ly, x + tw + 8, ly + 16], fill=col)
        draw.text((x + 4, ly + 1), label, fill=(255, 255, 255, 255), font=font_small)

    return out.convert("RGB")


# -------------------- 主入口 --------------------
def _region_to_out(r: Dict[str, Any]) -> Dict[str, Any]:
    score = float(r["score"])
    return {
        "id": int(r["id"]),
        "x": int(r["x"]), "y": int(r["y"]),
        "w": int(r["w"]), "h": int(r["h"]),
        "score": round(score, 3),
        "severity": _score_to_severity(score),
        "label": r.get("label", "scratch"),
        "note": f"area={r['area']}, aspect={r['feat']['aspect']:.1f}, "
                f"fill={r['feat']['fill']:.2f}",
    }


def main():
    t0 = time.time()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        q = (payload.get("q") or "").strip()
        img_str = (payload.get("img") or "").strip()
        if not q:
            sys.stdout.write(json.dumps({"ok": False, "error": "missing q"}))
            return
        if not img_str:
            sys.stdout.write(json.dumps({"ok": False, "error": "missing img"}))
            return

        # 1) 文本意图分类（Nemotron）
        intent, text_score, _ = _classify_intent(q)

        # 2) 模型状态
        model_loaded = (_NEMOTRON_MODEL is not None) or _load_model_lazy()

        # 3) 图像解码 + 检测
        try:
            img = _decode_img(img_str)
        except Exception as e:
            sys.stdout.write(json.dumps({"ok": False, "error": "image decode failed: " + str(e)}))
            return
        iw, ih = img.size
        regions, heat = detect_regions(img, intent)

        # 4) 输出整理
        out_regions = [_region_to_out(r) for r in regions]
        # 给每个 region 贴 label = intent
        for r in out_regions:
            r["label"] = intent

        sev_counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
        for r in out_regions:
            sev_counts[r["severity"]] += 1

        # 5) 标注图
        annotated = _draw_annotation(img, regions, heat, intent, q, text_score)
        buf = io.BytesIO()
        annotated.save(buf, format="PNG", optimize=False)
        png_b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        result = {
            "ok": True,
            "prompt": q,
            "intent": intent,
            "score": round(text_score, 4),
            "embed_model": _NEMOTRON_NAME,
            "embed_dim": _NEMOTRON_DIM,
            "device": _NEMOTRON_DEVICE,
            "model_loaded": model_loaded,
            "model_error": _MODEL_LOAD_ERROR,
            "torch_available": _HAS_TORCH,
            "sentence_transformers_available": _HAS_ST,
            "img_w": iw, "img_h": ih,
            "regions": out_regions,
            "summary": {
                "total": len(out_regions),
                **sev_counts,
            },
            "intent_scores": _compute_intent_scores(q),
            "elapsed_s": round(time.time() - t0, 3),
            "png_base64": png_b64,
        }
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
    except Exception:
        sys.stdout.write(json.dumps({
            "ok": False,
            "error": traceback.format_exc(limit=8),
        }))


if __name__ == "__main__":
    main()
