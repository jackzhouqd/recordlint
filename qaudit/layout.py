"""版面分析：从扫描页提取表格线与单元格。

质量证明单、流水卡片等均为规整表格，单元格是判定“空栏未划 /”“检验员栏无印章”
这类 B 类规则的基础。用形态学提取横竖线，取线掩膜中的“洞”作为单元格。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .ocr import TextLine


@dataclass
class Cell:
    """一个表格单元格。rect = (x, y, w, h)。"""

    rect: tuple[int, int, int, int]
    texts: list[TextLine] = field(default_factory=list)

    @property
    def area(self) -> int:
        return self.rect[2] * self.rect[3]

    @property
    def content(self) -> str:
        return "".join(t.text for t in sorted(self.texts, key=lambda t: t.cx)).strip()

    @property
    def is_blank(self) -> bool:
        return self.content == ""

    def center(self) -> tuple[float, float]:
        x, y, w, h = self.rect
        return x + w / 2, y + h / 2


@dataclass(frozen=True)
class Layout:
    cells: list[Cell]
    h_segments: list[tuple[int, int, int, int]]  # 短横线段 (x, y, w, h)，划改候选
    table_mask: np.ndarray


def _binarize(image: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    # 复印件底色重，用自适应阈值而非全局 Otsu
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 15
    )


def analyze(image: np.ndarray) -> Layout:
    """提取表格结构。返回不可变的 Layout。"""
    h, w = image.shape[:2]
    binary = _binarize(image)

    h_len = max(20, w // 30)
    v_len = max(20, h // 40)

    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_len, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_len))

    h_lines = cv2.dilate(cv2.erode(binary, h_kernel), h_kernel)
    v_lines = cv2.dilate(cv2.erode(binary, v_kernel), v_kernel)
    table = cv2.bitwise_or(h_lines, v_lines)
    table = cv2.dilate(table, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))

    cells = _extract_cells(table, w, h)
    segments = _short_h_segments(binary, w)
    return Layout(cells=cells, h_segments=segments, table_mask=table)


def _extract_cells(table: np.ndarray, w: int, h: int) -> list[Cell]:
    """线掩膜中的封闭空洞即为单元格。"""
    contours, hierarchy = cv2.findContours(table, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    page_area = w * h
    cells: list[Cell] = []
    for i, cnt in enumerate(contours):
        if hierarchy[0][i][3] == -1:  # 外轮廓，不是洞
            continue
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cw * ch
        if area < page_area * 0.0004 or area > page_area * 0.25:
            continue
        if cw < 25 or ch < 15:
            continue
        cells.append(Cell(rect=(x, y, cw, ch)))
    return sorted(cells, key=lambda c: (c.rect[1], c.rect[0]))


def _short_h_segments(binary: np.ndarray, page_w: int) -> list[tuple[int, int, int, int]]:
    """提取短横线段（长度介于字宽与表格线之间），作为“划改”候选。"""
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (12, 1))
    horiz = cv2.dilate(cv2.erode(binary, kernel), kernel)
    n, _, stats, _ = cv2.connectedComponentsWithStats(horiz, connectivity=8)
    out: list[tuple[int, int, int, int]] = []
    min_len, max_len = 18, int(page_w * 0.35)
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if h <= 5 and min_len <= w <= max_len:
            out.append((int(x), int(y), int(w), int(h)))
    return out


def assign_texts(cells: list[Cell], lines: list[TextLine]) -> list[Cell]:
    """把 OCR 文本行归入单元格（按行中心点落位），返回新的 Cell 列表。"""
    out = [Cell(rect=c.rect, texts=[]) for c in cells]
    for line in lines:
        best: Cell | None = None
        best_area = None
        for cell in out:
            x, y, w, h = cell.rect
            if x <= line.cx <= x + w and y <= line.cy <= y + h:
                if best_area is None or cell.area < best_area:  # 取最小的包含单元格
                    best, best_area = cell, cell.area
        if best is not None:
            best.texts.append(line)
    return out


def column_x_range(
    lines: list[TextLine], headers: list[str], cells: list[Cell] | None = None
) -> tuple[int, int, TextLine] | None:
    """定位表头列（如“检验员”“实际”）的 x 区间，用于列级规则。

    同一页可能出现多处同名文字（如表头“检验员”与上方栏目标签“检验员”），
    取最靠下的一处——数据行在表头之下，取错会导致整列判定落空。
    列宽优先用表格单元格边界，比按文字宽度外扩准确得多。
    """
    wanted = {h.replace(" ", "") for h in headers}
    matches = [l for l in lines if l.text.replace(" ", "") in wanted]
    if not matches:
        return None
    header = max(matches, key=lambda l: l.cy)

    if cells:
        owner = min(
            (
                c
                for c in cells
                if c.rect[0] <= header.cx <= c.rect[0] + c.rect[2]
                and c.rect[1] <= header.cy <= c.rect[1] + c.rect[3]
            ),
            key=lambda c: c.area,
            default=None,
        )
        if owner is not None:
            return owner.rect[0], owner.rect[0] + owner.rect[2], header

    x, _, w, _ = header.bbox
    pad = int(w * 0.6)
    return max(0, x - pad), x + w + pad, header
