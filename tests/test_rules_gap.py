"""补齐的 A/B 缺口规则测试（A16 / A17 / A18 / B09 / B10）。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import rules_a, rules_b
from qaudit.context import PageContext
from qaudit.findings import RuleBook
from qaudit.ocr import TextLine

BOOK = RuleBook.load(Path(__file__).resolve().parent.parent / "config" / "rules.yaml")
W, H = 1600, 2200


def table_page(rows: list[tuple[str, str]], *, spec_header: str = "规定",
               actual_header: str = "实际", extra: list[TextLine] | None = None) -> PageContext:
    """构造「规定 | 实际」两列表格页。"""
    lines = [
        TextLine(spec_header, (600, 400, 90, 32), 0.99),
        TextLine(actual_header, (1000, 400, 90, 32), 0.99),
    ]
    for i, (spec_txt, actual_txt) in enumerate(rows):
        y = 500 + i * 90
        lines.append(TextLine(spec_txt, (600, y, 200, 34), 0.98))
        lines.append(TextLine(actual_txt, (1000, y, 200, 34), 0.98))
    lines += extra or []
    return PageContext(
        doc_id="D", page_no=1, page_count=1, image=np.full((H, W, 3), 255, dtype=np.uint8),
        lines=lines, cells=[], seals=[], h_segments=[], form_type="质量证明单",
    )


def hits(rule_id: str, ctx: PageContext, module=rules_a):
    return module._REGISTRY[rule_id](ctx, BOOK.get(rule_id))


# ---------------------------------------------------------------- A16 多处测量
def test_a16_flags_range_for_few_points():
    ctx = table_page([("R5(2处)", "R5.02～R5.06")])
    found = hits("A16_multi_point_record", ctx)
    assert found and "逐个写出实测值" in found[0].message


def test_a16_accepts_listed_values():
    ctx = table_page([("R5(2处)", "R5.02 R5.06")])
    assert not hits("A16_multi_point_record", ctx)


def test_a16_allows_range_when_over_threshold():
    """5 处以上允许写范围。"""
    ctx = table_page([("Φ300（测量12点）", "Φ300.01～Φ300.09")])
    assert not hits("A16_multi_point_record", ctx)


# ---------------------------------------------------------------- A17 修约位数
def test_a17_flags_fewer_decimals():
    ctx = table_page([("26.39±0.1", "26.3")])
    found = hits("A17_rounding_digits", ctx)
    assert found and "应按较多有效数位修约" in found[0].message


def test_a17_accepts_matching_decimals():
    ctx = table_page([("26.39±0.1", "26.35")])
    assert not hits("A17_rounding_digits", ctx)


def test_a17_tolerates_ocr_split_digits():
    """OCR 常把手写「0.030」断成「0.0 30」，不得据此误判位数。"""
    ctx = table_page([("0.035", "0.0 30")])
    assert not hits("A17_rounding_digits", ctx)


# ---------------------------------------------------------------- A18 复核人
def _copy_page(*extra: TextLine, seals=()) -> PageContext:
    lines = [
        TextLine("复印件与原件一致", (200, 1900, 300, 34), 0.97),
        TextLine("质量证明单", (600, 200, 200, 32), 0.98),
        *extra,
    ]
    return PageContext(
        doc_id="D", page_no=1, page_count=1, image=np.full((H, W, 3), 255, dtype=np.uint8),
        lines=lines, cells=[], seals=list(seals), h_segments=[], form_type="质量证明单",
    )


def test_a18_flags_unsigned_reviewer_field():
    ctx = _copy_page(TextLine("复核人", (200, 1960, 90, 32), 0.98))
    found = hits("A18_reviewer_signature", ctx)
    assert found and "复核人" in found[0].message


def test_a18_accepts_signed_reviewer_field():
    ctx = _copy_page(
        TextLine("复核人", (200, 1960, 90, 32), 0.98),
        TextLine("陶文宇", (330, 1960, 100, 32), 0.95),
    )
    assert not hits("A18_reviewer_signature", ctx)


def test_a18_skips_non_copy_pages():
    """本条只适用复印/扫描件。"""
    ctx = PageContext(
        doc_id="D", page_no=1, page_count=1, image=np.full((H, W, 3), 255, dtype=np.uint8),
        lines=[TextLine("质量证明单", (600, 200, 200, 32), 0.98)],
        cells=[], seals=[], h_segments=[], form_type="质量证明单",
    )
    assert not hits("A18_reviewer_signature", ctx)


# ---------------------------------------------------------------- B09 页码位置
def _page_with_marker(x_frac: float, y_frac: float) -> PageContext:
    line = TextLine("第1页共4页", (int(W * x_frac) - 60, int(H * y_frac) - 16, 120, 32), 0.98)
    return PageContext(
        doc_id="D", page_no=1, page_count=4, image=np.full((H, W, 3), 255, dtype=np.uint8),
        lines=[line, TextLine("质量证明单", (600, 200, 200, 32), 0.98)],
        cells=[], seals=[], h_segments=[], form_type="质量证明单",
    )


def test_b09_accepts_footer_center():
    from dataclasses import replace

    spec = replace(BOOK._specs["B09_page_number_position"], enabled=True)
    assert not rules_b.check_page_number_position(_page_with_marker(0.5, 0.93), spec)


def test_b09_flags_corner_marker():
    from dataclasses import replace

    spec = replace(BOOK._specs["B09_page_number_position"], enabled=True)
    found = rules_b.check_page_number_position(_page_with_marker(0.9, 0.95), spec)
    assert found and "页脚中间" in found[0].message


def test_b09_disabled_by_default():
    """厂内表单页码多印在右下角，口径未确认前默认关闭。"""
    assert BOOK.get("B09_page_number_position") is None


# ---------------------------------------------------------------- B10 笔迹颜色
def _page_with_ink(color: tuple[int, int, int] | None) -> PageContext:
    img = np.full((H, W, 3), 255, dtype=np.uint8)
    if color:
        cv2.rectangle(img, (400, 800), (700, 860), color, -1)
    return PageContext(
        doc_id="D", page_no=1, page_count=1, image=img,
        lines=[TextLine("质量证明单", (600, 200, 200, 32), 0.98),
               TextLine("检验员", (1100, 700, 90, 32), 0.99),
               TextLine("合格", (700, 820, 60, 30), 0.98)],
        cells=[], seals=[], h_segments=[], form_type="质量证明单",
    )


def test_b10_flags_blue_ink():
    found = hits("B10_pen_color", _page_with_ink((220, 60, 40)), rules_b)  # BGR 蓝
    assert found and "蓝色笔迹" in found[0].message


def test_b10_ignores_red_seal():
    """红色是印章的正常颜色，不能判为笔色违规。"""
    assert not hits("B10_pen_color", _page_with_ink((40, 40, 220)), rules_b)


def test_b10_ignores_black_ink():
    assert not hits("B10_pen_color", _page_with_ink((30, 30, 30)), rules_b)
