"""B 类视觉规则测试。

全量基线暴露的两个版式坑，用测试锁住：
- “检验员”既可能是表格列（章在下方逐行盖），也可能是表头栏位（章在标签右侧）；
- 纵向密排的印章不得被形态学闭运算粘成一个连通域。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import rules_b, seal
from qaudit.context import PageContext
from qaudit.findings import RuleBook
from qaudit.ocr import TextLine

BOOK = RuleBook.load(Path(__file__).resolve().parent.parent / "config" / "rules.yaml")
W, H = 1600, 2200


def blank_page() -> np.ndarray:
    return np.full((H, W, 3), 255, dtype=np.uint8)


def draw_seal(img: np.ndarray, cx: int, cy: int, r: int = 26) -> None:
    cv2.rectangle(img, (cx - r, cy - r), (cx + r, cy + r), (40, 40, 220), -1)


def make_ctx(image: np.ndarray, lines: list[TextLine]) -> PageContext:
    return PageContext(
        doc_id="T", page_no=1, page_count=1, image=image, lines=lines,
        cells=[], seals=seal.detect(image), h_segments=[], form_type="质量证明单",
    )


def inspector_lines() -> list[TextLine]:
    """表头“检验员”+ 左侧若干数据行，构成待签署的版面。"""
    return [
        TextLine("检验员", (1100, 700, 90, 32), 0.99),
        TextLine("零级篦齿尺寸", (300, 800, 200, 30), 0.98),
        TextLine("一级篦齿尺寸", (300, 880, 200, 30), 0.98),
        TextLine("二级篦齿尺寸", (300, 960, 200, 30), 0.98),
    ]


def test_b07_accepts_seal_beside_header_label():
    """表头栏位版式：印章盖在“检验员”右侧同一行，不应判为缺章。"""
    img = blank_page()
    draw_seal(img, 1280, 712)
    found = rules_b.check_inspector_seal(make_ctx(img, inspector_lines()), BOOK.get("B07_inspector_seal_missing"))
    assert not found


def test_b07_accepts_seals_in_column():
    """表格列版式：印章在表头下方逐行加盖。"""
    img = blank_page()
    for y in (800, 880, 960):
        draw_seal(img, 1140, y)
    found = rules_b.check_inspector_seal(make_ctx(img, inspector_lines()), BOOK.get("B07_inspector_seal_missing"))
    assert not found


def test_b07_flags_truly_missing_seal():
    img = blank_page()
    found = rules_b.check_inspector_seal(make_ctx(img, inspector_lines()), BOOK.get("B07_inspector_seal_missing"))
    assert found and "检验员" in found[0].message


def test_seals_stacked_vertically_are_not_merged():
    """纵向密排的检验章必须逐枚检出，粘连会导致连通域超限被整体丢弃。"""
    img = blank_page()
    for y in range(800, 1400, 74):
        draw_seal(img, 1140, y)
    seals = seal.detect(img)
    assert len(seals) >= 7, f"仅检出 {len(seals)} 枚，印章被纵向粘连"


def test_b02_flags_overlapping_seals():
    img = blank_page()
    draw_seal(img, 700, 900, r=40)
    draw_seal(img, 730, 920, r=40)
    ctx = make_ctx(img, [TextLine("检验", (300, 900, 90, 30), 0.98)])
    # 两枚重叠的章会被检出为一个或两个连通域，规则不得因此崩溃
    rules_b.check_seal_overlap(ctx, BOOK.get("B02_seal_overlap"))


@pytest.mark.parametrize("ratio_ok", [True, False])
def test_b08_red_seal_presence(ratio_ok):
    img = blank_page()
    if ratio_ok:
        draw_seal(img, 800, 1000, r=60)
    ctx = make_ctx(img, [TextLine("质量证明单", (300, 200, 200, 30), 0.98),
                         TextLine("检验员", (1100, 700, 90, 32), 0.99),
                         TextLine("合格", (700, 800, 60, 30), 0.98)])
    found = rules_b.check_page_red(ctx, BOOK.get("B08_no_red_seal_on_page"))
    assert bool(found) != ratio_ok


def test_b07_skips_copy_pages():
    """复印件上原始红章被复印成黑色，不得据红章缺失判缺章（A13）。"""
    img = blank_page()
    lines = inspector_lines() + [TextLine("复印件与原件一致", (200, 1900, 300, 34), 0.97)]
    assert not rules_b.check_inspector_seal(make_ctx(img, lines), BOOK.get("B07_inspector_seal_missing"))


def test_b08_skips_copy_pages():
    img = blank_page()
    lines = [TextLine("质量证明单", (300, 200, 200, 30), 0.98),
             TextLine("检验员", (1100, 700, 90, 32), 0.99),
             TextLine("此件与原件一致", (200, 1900, 300, 34), 0.97)]
    assert not rules_b.check_page_red(make_ctx(img, lines), BOOK.get("B08_no_red_seal_on_page"))


def test_nested_fragments_merged_into_one_seal():
    """印色断连时圆章碎成“外圈 + 圈内文字”，圈内碎片须并回外圈（220-164 误报案）。"""
    img = blank_page()
    cv2.circle(img, (800, 1000), 140, (40, 40, 220), 10)   # 外圈
    cv2.rectangle(img, (770, 970), (830, 1030), (40, 40, 220), -1)  # 圈内大字
    seals = seal.detect(img)
    assert len(seals) == 1, f"检出 {len(seals)} 枚，圈内碎片未并回外圈"


def test_b02_not_fired_by_nested_fragments():
    img = blank_page()
    cv2.circle(img, (800, 1000), 140, (40, 40, 220), 10)
    cv2.rectangle(img, (770, 970), (830, 1030), (40, 40, 220), -1)
    ctx = make_ctx(img, [TextLine("检验", (300, 900, 90, 30), 0.98)])
    assert not rules_b.check_seal_overlap(ctx, BOOK.get("B02_seal_overlap"))


def test_b02_ignores_adjacent_seals():
    """相邻但不相压的两枚章不算重叠。"""
    img = blank_page()
    draw_seal(img, 700, 900, r=30)
    draw_seal(img, 775, 900, r=30)
    ctx = make_ctx(img, [TextLine("检验", (300, 900, 90, 30), 0.98)])
    assert not rules_b.check_seal_overlap(ctx, BOOK.get("B02_seal_overlap"))
