"""样本图像处理：裁图、写盘、退化合成。

从 tools/make_dataset.py 提到运行时包里，因为界面上的训练工作台也要用；
tools/ 下的命令行入口改为复用本模块，避免两份实现走偏。
"""
from __future__ import annotations

import hashlib
import random
import re
from pathlib import Path

import cv2
import numpy as np

from .. import seal

# 标签集与通用做法「印章须清晰、完整、水平正向加盖」对应
LABELS = {
    "1": ("ok", "合格（清晰完整正向）"),
    "2": ("chipped", "缺角/残缺"),
    "3": ("blurred", "模糊"),
    "4": ("faint", "漏墨/断线"),
    "5": ("upside_down", "倒盖"),
    "6": ("not_seal", "非印章（检测误报）"),
}

MAX_SIDE = 256   # 裁图长边上限，统一后便于标注与训练


def safe_name(name: str) -> str:
    """档案名含中文，而 Windows 下 cv2 无法按非 ASCII 路径写盘，故文件名一律转 ASCII。

    截断可能撞名，追加原名哈希保证唯一；原始档案名仍保留在样本记录的 doc_id 里。
    """
    ascii_part = "".join(
        c if (c.isascii() and (c.isalnum() or c in "-_.")) else "_" for c in name)
    ascii_part = re.sub(r"_+", "_", ascii_part).strip("_")[:32]
    return f"{ascii_part or 'doc'}_{hashlib.sha1(name.encode()).hexdigest()[:6]}"


def imwrite_jpg(path: Path, img: np.ndarray, quality: int = 92) -> None:
    """unicode 安全写盘。cv2.imwrite 遇非 ASCII 路径会静默返回 False——
    曾因此在 4396 枚样本里丢掉 3187 枚而清单照常生成。"""
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError(f"JPEG 编码失败: {path}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(buf.tobytes())


def crop_seal(image: np.ndarray, bbox: tuple[int, int, int, int],
              pad_ratio: float = 0.15) -> np.ndarray | None:
    """按外扩比例裁出印章小图。留边才看得出缺角。"""
    x, y, w, h = bbox
    pad = int(max(w, h) * pad_ratio)
    x1, y1 = max(0, x - pad), max(0, y - pad)
    x2, y2 = min(image.shape[1], x + w + pad), min(image.shape[0], y + h + pad)
    if x2 - x1 < 12 or y2 - y1 < 12:
        return None
    crop = image[y1:y2, x1:x2]
    side = max(crop.shape[:2])
    if side > MAX_SIDE:
        scale = MAX_SIDE / side
        crop = cv2.resize(crop, (int(crop.shape[1] * scale), int(crop.shape[0] * scale)),
                          interpolation=cv2.INTER_AREA)
    return crop


# ---------------------------------------------------------------- 退化合成

def syn_upside_down(img: np.ndarray, rng: random.Random) -> np.ndarray:
    return cv2.rotate(img, cv2.ROTATE_180)


def syn_chipped(img: np.ndarray, rng: random.Random) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    cw, ch = int(w * rng.uniform(0.18, 0.35)), int(h * rng.uniform(0.18, 0.35))
    corner = rng.choice(["tl", "tr", "bl", "br"])
    x = 0 if corner in ("tl", "bl") else w - cw
    y = 0 if corner in ("tl", "tr") else h - ch
    out[y:y + ch, x:x + cw] = 255  # 缺角处为纸白
    return out


def syn_blurred(img: np.ndarray, rng: random.Random) -> np.ndarray:
    k = rng.choice([5, 7, 9])
    return cv2.GaussianBlur(img, (k, k), 0)


def syn_faint(img: np.ndarray, rng: random.Random) -> np.ndarray:
    """漏墨/断线：腐蚀红色笔画并叠加随机白噪声。"""
    mask = seal.red_mask(img)
    eroded = cv2.erode(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
                       iterations=rng.choice([1, 2]))
    lost = cv2.bitwise_and(mask, cv2.bitwise_not(eroded))
    out = img.copy()
    out[lost > 0] = 255
    noise = (np.random.default_rng(rng.randint(0, 10 ** 6)).random(mask.shape) > 0.92) & (mask > 0)
    out[noise] = 255
    return out


# 合成能可靠覆盖这四类退化，因此训练集不需要人工标注，人工只标验证集即可——
# 这是把标注成本从「千页」降到「百枚」的关键。
SYNTH_MAKERS = {
    "upside_down": syn_upside_down,
    "chipped": syn_chipped,
    "blurred": syn_blurred,
    "faint": syn_faint,
}
