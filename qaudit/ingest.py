"""档案接入：PDF / TIF / JPG 统一渲染为标准化页面图像。

档案实测特征：PDF 无文字层（纯扫描），TIF 300dpi、JPG 200dpi，均为 RGB。
本模块把所有来源归一到同一长边像素，使后续像素阈值（印章面积、划改线长度）可比。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import fitz
import numpy as np

IMAGE_EXTS = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp"}
PDF_EXTS = {".pdf"}
DEFAULT_LONG_SIDE = 2200  # 归一化长边像素，约等于 A4@190dpi


@dataclass(frozen=True)
class Page:
    """一个标准化页面。image 为 BGR ndarray。"""

    doc_id: str
    page_no: int
    image: np.ndarray
    source: str

    @property
    def size(self) -> tuple[int, int]:
        h, w = self.image.shape[:2]
        return w, h


def imread_unicode(path: Path) -> np.ndarray | None:
    """cv2.imread 不支持中文路径，改用字节流解码。"""
    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is not None:
            return img
    except Exception:
        pass
    # TIFF 等格式回退到 PIL
    try:
        from PIL import Image

        with Image.open(path) as im:
            return cv2.cvtColor(np.array(im.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def prepare(img: np.ndarray, long_side: int = DEFAULT_LONG_SIDE,
            deskew_enabled: bool = False) -> np.ndarray:
    """归一化 +（可选）倾斜校正。

    倾斜校正默认关闭：本批 1453 页实测倾角中位数 0.20°、90 分位 0.75°，
    仅 7% 的页超过 1°，而开启会使 OCR 缓存整体失效（全量重识别约 2.2 小时）。
    客户实际扫描件若歪斜明显，用 --deskew 打开即可。
    """
    img = normalize(img, long_side)
    if deskew_enabled:
        from .deskew import correct

        img, _ = correct(img)
    return img


def normalize(img: np.ndarray, long_side: int = DEFAULT_LONG_SIDE) -> np.ndarray:
    """等比缩放到统一长边，返回新数组。"""
    h, w = img.shape[:2]
    cur = max(h, w)
    if cur == long_side or cur == 0:
        return img
    scale = long_side / cur
    interp = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    return cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=interp)


def _pdf_pages(path: Path, doc_id: str, long_side: int,
               deskew_enabled: bool = False) -> Iterator[Page]:
    with fitz.open(path) as doc:
        for idx, page in enumerate(doc):
            rect = page.rect
            base = max(rect.width, rect.height) or 1
            dpi = max(120, min(300, int(long_side / base * 72)))
            pix = page.get_pixmap(dpi=dpi)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            if pix.n == 4:
                arr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
            elif pix.n == 3:
                arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            else:
                arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
            yield Page(doc_id, idx + 1, prepare(arr, long_side, deskew_enabled), str(path))


def iter_pages(target: str | Path, long_side: int = DEFAULT_LONG_SIDE,
               deskew_enabled: bool = False) -> Iterator[Page]:
    """遍历一个文件或目录下的全部页面。

    目录被视为一份档案（doc_id = 目录名），目录内图片按文件名排序作为页序；
    PDF 文件本身即一份档案。
    """
    target = Path(target)
    if not target.exists():
        raise FileNotFoundError(f"路径不存在: {target}")

    if target.is_file():
        yield from _iter_single_file(target, long_side, deskew_enabled)
        return

    # 目录：先收集本级图片作为一份档案，再递归子目录与 PDF
    images = sorted(p for p in target.iterdir() if p.suffix.lower() in IMAGE_EXTS)
    for i, p in enumerate(images):
        img = imread_unicode(p)
        if img is None:
            continue
        yield Page(target.name, i + 1, prepare(img, long_side, deskew_enabled), str(p))

    for child in sorted(target.iterdir()):
        if child.is_dir():
            yield from iter_pages(child, long_side, deskew_enabled)
        elif child.suffix.lower() in PDF_EXTS:
            yield from _pdf_pages(child, child.stem, long_side, deskew_enabled)


def _iter_single_file(path: Path, long_side: int,
                      deskew_enabled: bool = False) -> Iterator[Page]:
    ext = path.suffix.lower()
    if ext in PDF_EXTS:
        yield from _pdf_pages(path, path.stem, long_side, deskew_enabled)
    elif ext in IMAGE_EXTS:
        img = imread_unicode(path)
        if img is not None:
            yield Page(path.stem, 1, prepare(img, long_side, deskew_enabled), str(path))
    else:
        raise ValueError(f"不支持的文件类型: {path}")


def load_page_image(source: str | Path, page_no: int, long_side: int = DEFAULT_LONG_SIDE):
    """按来源路径 + 页号重新载入单页图像。

    服务端据此按需裁剪证据图——系统只引用原始档案，不复制、不预存图片。
    """
    path = Path(source)
    if not path.exists():
        return None
    if path.suffix.lower() in PDF_EXTS:
        for page in _pdf_pages(path, path.stem, long_side):
            if page.page_no == page_no:
                return page.image
        return None
    img = imread_unicode(path)
    return normalize(img, long_side) if img is not None else None


def doc_page_counts(target: str | Path) -> dict[str, int]:
    """预统计每份档案的页数（不渲染），供“多页记录须填页码”等规则使用。"""
    target = Path(target)
    counts: dict[str, int] = {}

    def add(doc_id: str, n: int) -> None:
        counts[doc_id] = counts.get(doc_id, 0) + n

    if target.is_file():
        if target.suffix.lower() in PDF_EXTS:
            with fitz.open(target) as d:
                add(target.stem, d.page_count)
        elif target.suffix.lower() in IMAGE_EXTS:
            add(target.stem, 1)
        return counts

    for d in [target, *(p for p in target.rglob("*") if p.is_dir())]:
        imgs = [p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXTS]
        if imgs:
            add(d.name, len(imgs))
        for pdf in (p for p in d.iterdir() if p.suffix.lower() in PDF_EXTS):
            try:
                with fitz.open(pdf) as doc:
                    add(pdf.stem, doc.page_count)
            except Exception:
                pass
    return counts


def count_pages(target: str | Path) -> int:
    """快速统计页数，不做渲染。"""
    target = Path(target)
    if target.is_file():
        if target.suffix.lower() in PDF_EXTS:
            with fitz.open(target) as d:
                return d.page_count
        return 1 if target.suffix.lower() in IMAGE_EXTS else 0
    total = 0
    for p in target.rglob("*"):
        if p.suffix.lower() in IMAGE_EXTS:
            total += 1
        elif p.suffix.lower() in PDF_EXTS:
            try:
                with fitz.open(p) as d:
                    total += d.page_count
            except Exception:
                pass
    return total
