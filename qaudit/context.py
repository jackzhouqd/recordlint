"""页面上下文：规则引擎的唯一输入。"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .layout import Cell
from .ocr import TextLine
from .seal import Seal


@dataclass(frozen=True)
class PageContext:
    doc_id: str
    page_no: int
    page_count: int
    image: np.ndarray
    lines: list[TextLine]
    cells: list[Cell]
    seals: list[Seal]
    h_segments: list[tuple[int, int, int, int]]
    form_type: str = "未识别"
    source: str = ""  # 原始文件路径。系统只引用不复制，证据图按需从原图裁剪
    # 已上线的印章状态模型（numpy 权重字典）。为 None 时依赖模型的规则自动跳过——
    # 未训练/未上线不应导致审核失败，只是少一类判定。
    seal_model: dict | None = None

    @property
    def width(self) -> int:
        return self.image.shape[1]

    @property
    def height(self) -> int:
        return self.image.shape[0]

    @property
    def text(self) -> str:
        ordered = sorted(self.lines, key=lambda l: (l.cy // 20, l.cx))
        return "\n".join(l.text for l in ordered)

    @property
    def is_blank_page(self) -> bool:
        """空白页/扉页不参与大部分规则判定。"""
        return len(self.lines) < 3
