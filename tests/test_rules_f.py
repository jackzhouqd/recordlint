"""F 类（表单专项规则包）规则测试。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import rules_f
from qaudit.context import PageContext
from qaudit.findings import RuleBook
from qaudit.layout import Cell
from qaudit.ocr import TextLine

BOOK = RuleBook.load(Path(__file__).resolve().parent.parent / "config" / "rules.yaml")


def cell(text: str, rect: tuple[int, int, int, int]) -> Cell:
    x, y, w, h = rect
    texts = [TextLine(text, (x + 4, y + 4, max(8, w - 8), max(8, h - 8)), 0.99)] if text else []
    return Cell(rect=rect, texts=texts)


def ctx_of(form_type: str, *, lines: list[str] = (), cells: list[Cell] = (), page_no: int = 1):
    text_lines = [TextLine(t, (100, 80 + i * 40, 380, 30), 0.98) for i, t in enumerate(lines)]
    text_lines += [t for c in cells for t in c.texts]
    return PageContext(
        doc_id="T", page_no=page_no, page_count=1,
        image=np.zeros((1400, 1000, 3), dtype=np.uint8),
        lines=text_lines, cells=list(cells), seals=[], h_segments=[],
        form_type=form_type,
    )


def hits(rule_id: str, ctx: PageContext):
    return rules_f._REGISTRY[rule_id](ctx, BOOK.get(rule_id))


# ---------------------------------------------------------------- F01 装配处
def test_f01_flags_slash_in_assembly_field():
    ctx = ctx_of("质量证明单", cells=[cell("装配处", (100, 200, 160, 60)), cell("/", (270, 200, 240, 60))])
    found = hits("F01_assembly_slash_forbidden", ctx)
    assert found and "装配处" in found[0].message


def test_f01_accepts_blank_assembly_field():
    """该栏应留空由下游填写，留空不是缺陷。"""
    ctx = ctx_of("质量证明单", cells=[cell("装配处", (100, 200, 160, 60)), cell("", (270, 200, 240, 60))])
    assert not hits("F01_assembly_slash_forbidden", ctx)


def test_f01_accepts_filled_assembly_field():
    ctx = ctx_of(
        "质量证明单", cells=[cell("装配处", (100, 200, 160, 60)), cell("PN-2824A", (270, 200, 240, 60))]
    )
    assert not hits("F01_assembly_slash_forbidden", ctx)


# ---------------------------------------------------------------- F03 呈报页数
def test_f03_flags_report_without_page_count():
    ctx = ctx_of("质量证明单", lines=["特殊记载", "呈报单：2022-118"])
    assert hits("F03_report_page_count", ctx)


def test_f03_accepts_report_with_page_count():
    ctx = ctx_of("质量证明单", lines=["特殊记载", "呈报单：2022-118，共5页"])
    assert not hits("F03_report_page_count", ctx)


def test_f03_silent_without_report():
    ctx = ctx_of("质量证明单", lines=["特殊记载", "无"])
    assert not hits("F03_report_page_count", ctx)


# ---------------------------------------------------------------- F02 设计图版次
def test_f02_flags_missing_design_version():
    ctx = ctx_of("质量证明单", lines=["零件号 PN-2834A", "检验员", "合格"])
    assert hits("F02_design_version_missing", ctx)


def test_f02_accepts_when_present():
    ctx = ctx_of("质量证明单", lines=["设计图版次：B", "检验员", "合格"])
    assert not hits("F02_design_version_missing", ctx)


# ---------------------------------------------------------------- F05 修理闭环
def test_f05_flags_overrun_without_closure():
    ctx = ctx_of("故障修理通知单", lines=["超差 0.05，同意使用", "修理结论"])
    assert hits("F05_repair_no_closure", ctx)


def test_f05_accepts_with_closure():
    ctx = ctx_of("故障修理通知单", lines=["超差 0.05，同意使用", "已退回原单位，闭环"])
    assert not hits("F05_repair_no_closure", ctx)


# ---------------------------------------------------------------- F07 拒收单
def test_f07_flags_nonconforming_without_reject_no():
    ctx = ctx_of("检验记录", lines=["3件超差", "序列号 05"])
    assert hits("F07_reject_note_missing", ctx)


def test_f07_accepts_with_reject_no():
    ctx = ctx_of("检验记录", lines=["3件超差", "拒收单：JS2022-07"])
    assert not hits("F07_reject_note_missing", ctx)


# ---------------------------------------------------------------- F09 入库标签
def test_f09_flags_blank_quantity():
    ctx = ctx_of(
        "入库单据",
        cells=[
            cell("入库箱数", (100, 200, 160, 60)), cell("", (270, 200, 160, 60)),
            cell("装箱数量", (100, 280, 160, 60)), cell("12", (270, 280, 160, 60)),
        ],
    )
    found = hits("F09_label_quantity_empty", ctx)
    assert len(found) == 1 and "入库箱数" in found[0].message


def test_f09_accepts_both_filled():
    ctx = ctx_of(
        "入库单据",
        cells=[
            cell("入库箱数", (100, 200, 160, 60)), cell("3", (270, 200, 160, 60)),
            cell("装箱数量", (100, 280, 160, 60)), cell("12", (270, 280, 160, 60)),
        ],
    )
    assert not hits("F09_label_quantity_empty", ctx)


# ---------------------------------------------------------------- F12 合格证闭环
def test_f12_flags_report_without_closure_note():
    ctx = ctx_of("产品合格证", lines=["产品合格证", "带工艺呈报单 2022-33"])
    assert hits("F12_cert_closure_note", ctx)


@pytest.mark.parametrize("note", ["呈报已闭环", "02批不涉及呈报"])
def test_f12_accepts_closure_note(note):
    ctx = ctx_of("产品合格证", lines=["产品合格证", "带工艺呈报单 2022-33", note])
    assert not hits("F12_cert_closure_note", ctx)


# ---------------------------------------------------------------- 表单门控
def test_f_rules_are_form_scoped():
    """F 类规则强绑定表单：故障修理通知单的规则不得作用到质量证明单。"""
    spec = BOOK.get("F05_repair_no_closure")
    assert RuleBook.applies(spec, "故障修理通知单")
    assert not RuleBook.applies(spec, "质量证明单")
