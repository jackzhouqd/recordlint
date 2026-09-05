"""A 类规则单元测试。

重点覆盖“误伤”边界：批次号、合同号、零件号里的短横与数字，绝不能被判为范围值错误。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import rules_a
from qaudit.context import PageContext
from qaudit.findings import RuleBook
from qaudit.ocr import TextLine

BOOK = RuleBook.load(Path(__file__).resolve().parent.parent / "config" / "rules.yaml")


def ctx_of(*texts: str, page_count: int = 1) -> PageContext:
    lines = [TextLine(t, (100, 100 + i * 40, 300, 30), 0.98) for i, t in enumerate(texts)]
    return PageContext(
        doc_id="T", page_no=1, page_count=page_count,
        image=np.zeros((1000, 800, 3), dtype=np.uint8),
        lines=lines, cells=[], seals=[], h_segments=[],
    )


def hits(rule_id: str, *texts: str, page_count: int = 1) -> list:
    fn = rules_a._REGISTRY[rule_id]
    return fn(ctx_of(*texts, page_count=page_count), BOOK.get(rule_id))


# ---------------------------------------------------------------- A01 日期
@pytest.mark.parametrize("text", ["2023.6.1", "2022-04-24", "2023/06/01", "2021.8.20"])
def test_a01_flags_bad_dates(text):
    assert hits("A01_date_format", text), f"应判定不合规: {text}"


@pytest.mark.parametrize(
    "text",
    [
        "2023.06.01",
        "2021 年 08 月 20 日",          # 预印表单栏位
        "1409-1072",                    # 批次顺序号
        "2021-407- 602A-1636",          # 合同号
        "2112-407-BMJ2891",             # 生产编号
        "GB/T4162-2008",                # 标准号
    ],
)
def test_a01_ignores_valid(text):
    assert not hits("A01_date_format", text), f"不应判定: {text}"


# ---------------------------------------------------------------- A02 范围值
@pytest.mark.parametrize("text", ["15℃-20℃", "36.1HRC-39.5HRC", "0.5-0.6MPa", "Φ50-Φ60"])
def test_a02_flags_dash_range(text):
    assert hits("A02_range_symbol", text)


@pytest.mark.parametrize(
    "text",
    ["15℃～20℃", "22-02-10-7", "PN-3811B", "1409-1072", "Z01220214071", "26.29～26.49"],
)
def test_a02_ignores_non_measurement(text):
    assert not hits("A02_range_symbol", text)


# ---------------------------------------------------------------- A03/A04 直径
def test_a03_flags_lowercase_phi():
    assert hits("A03_diameter_symbol", "φ48.00")


def test_a03_accepts_uppercase_phi():
    assert not hits("A03_diameter_symbol", "Φ48.00")


def test_a04_flags_missing_second_phi():
    assert hits("A04_diameter_range", "Φ50.02～50.06")


def test_a04_accepts_full_form():
    assert not hits("A04_diameter_range", "Φ50.02～Φ50.06")


# ---------------------------------------------------------------- A06/A07/A08 单位
def test_a06_flags_wrong_case():
    assert hits("A06_unit_case", "净重 30Kg")


def test_a06_accepts_gb3100():
    assert not hits("A06_unit_case", "净重 30kg")


def test_a07_flags_square_meter():
    assert hits("A07_square_cubic_unit", "面积 5m2")


def test_a08_flags_chinese_numeral():
    assert hits("A08_chinese_numeral_unit", "重量三十公斤")


# ---------------------------------------------------------------- A09 页码
def test_a09_missing_page_number_is_off_by_default():
    """档案目录内含多份独立表单，默认不因“无页码”报警，避免整批噪声。"""
    assert not hits("A09_page_number", "质量证明单", "检验员", "合格", page_count=4)


def test_a09_missing_page_number_when_enabled():
    from dataclasses import replace

    spec = replace(BOOK.get("A09_page_number"), params={"min_pages": 2, "flag_missing": True})
    ctx = ctx_of("质量证明单", "检验员", "合格", page_count=4)
    assert rules_a.check_page_number(ctx, spec)


def test_a09_accepts_correct_format():
    assert not hits("A09_page_number", "第1页共4页", "质量证明单", "检验员", page_count=4)


def test_a09_skips_single_page():
    assert not hits("A09_page_number", "质量证明单", "检验员", "合格", page_count=1)


# ---------------------------------------------------------------- A10/A11 数值
def test_a10_flags_excess_decimals():
    assert hits("A10_decimal_precision", "称重 12.34567")


def test_a10_accepts_three_decimals():
    assert not hits("A10_decimal_precision", "称重 12.345")


@pytest.mark.parametrize("text", ["PN-2832", "DOC-131430-027", "PN-3811B"])
def test_a10_ignores_part_numbers(text):
    """零件号/文件号中的多位小数段不得误判为实测值精度问题。"""
    assert not hits("A10_decimal_precision", text)


def test_a11_flags_symmetric_range():
    assert hits("A11_tolerance_format", "10(-0.1~0.1)")


def test_a11_accepts_asymmetric_range():
    assert not hits("A11_tolerance_format", "10(-0.1~0)")


# ---------------------------------------------------------------- A13 复印章措辞
def test_a13_flags_wrong_wording():
    found = hits("A13_copy_stamp_wording", "复印件与原件一致")
    assert found and "此件与原件一致" in found[0].message


def test_a13_accepts_required_wording():
    assert not hits("A13_copy_stamp_wording", "此件与原件一致")


# ---------------------------------------------------------------- A14 记录方式一致性
def test_a14_flags_mixed_record_style():
    lines = [
        TextLine("实际", (500, 100, 60, 30), 0.99),
        TextLine("合格", (500, 200, 60, 30), 0.99),
        TextLine("26.35", (500, 260, 60, 30), 0.99),
    ]
    ctx = PageContext(
        doc_id="T", page_no=1, page_count=1,
        image=np.zeros((1000, 800, 3), dtype=np.uint8),
        lines=lines, cells=[], seals=[], h_segments=[],
    )
    assert rules_a.check_result_consistency(ctx, BOOK.get("A14_result_consistency"))


def test_a14_accepts_uniform_style():
    lines = [
        TextLine("实际", (500, 100, 60, 30), 0.99),
        TextLine("26.35", (500, 200, 60, 30), 0.99),
        TextLine("26.41", (500, 260, 60, 30), 0.99),
    ]
    ctx = PageContext(
        doc_id="T", page_no=1, page_count=1,
        image=np.zeros((1000, 800, 3), dtype=np.uint8),
        lines=lines, cells=[], seals=[], h_segments=[],
    )
    assert not rules_a.check_result_consistency(ctx, BOOK.get("A14_result_consistency"))


# ---------------------------------------------------------------- A09 页码顺序（全量基线后收窄）
@pytest.mark.parametrize("text", ["第1页共4页", "共4页第1页", "共2页、第2页", "（共2页、第1页）"])
def test_a09_accepts_both_orders(text):
    """厂内预印表单多为“共X页第Y页”，信息完整即视为合规。"""
    assert not hits("A09_page_number", text, "质量证明单", "检验员", page_count=4)


def test_a09_skips_single_page_record():
    assert not hits("A09_page_number", "共1页第1页", "质量证明单", "检验员", page_count=4)


def test_a09_flags_incomplete_marker():
    """只有“第X页”没有“共Y页”，信息不完整仍要报。"""
    assert hits("A09_page_number", "第1页", "质量证明单", "检验员", page_count=4)


def test_a09_strict_order_can_be_enabled():
    from dataclasses import replace

    spec = replace(BOOK.get("A09_page_number"), params={"min_pages": 2, "strict_order": True})
    ctx = ctx_of("共4页第1页", "质量证明单", "检验员", page_count=4)
    assert rules_a.check_page_number(ctx, spec)


# ---------------------------------------------------------------- A15 行内定位（全量基线后收窄）
def _ctx_with_column(rows: list[tuple[str, str, int]]) -> PageContext:
    """rows = [(检查内容, 实际值, y)]，构造带“实际”列的表格页。"""
    lines = [TextLine("实际", (600, 100, 60, 30), 0.99)]
    for name, value, y in rows:
        lines.append(TextLine(name, (200, y, 260, 30), 0.98))
        lines.append(TextLine(value, (600, y, 80, 30), 0.98))
    return PageContext(
        doc_id="T", page_no=1, page_count=1,
        image=np.zeros((1400, 1000, 3), dtype=np.uint8),
        lines=lines, cells=[], seals=[], h_segments=[], form_type="质量证明单",
    )


def test_a15_flags_only_keyword_row():
    ctx = _ctx_with_column([("记录实测的静态空气泄漏率", "3.1", 200), ("尺寸实测值", "0.020", 400)])
    found = rules_a.check_special_unit(ctx, BOOK.get("A15_missing_unit_special"))
    assert len(found) == 1 and "3.1" in found[0].message


def test_a15_accepts_value_with_unit():
    ctx = _ctx_with_column([("记录实测的静态空气泄漏率", "3.1m3/h", 200)])
    assert not rules_a.check_special_unit(ctx, BOOK.get("A15_missing_unit_special"))
