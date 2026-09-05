"""表单类型识别与规则门控测试。

供方合格证不适用公司内部表单的填写格式要求——这是压制误报的关键门控，
必须有测试锁住行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import formtype, rules_a
from qaudit.context import PageContext
from qaudit.findings import RuleBook
from qaudit.ocr import TextLine

BOOK = RuleBook.load(Path(__file__).resolve().parent.parent / "config" / "rules.yaml")


def page(*texts: str) -> PageContext:
    lines = [TextLine(t, (100, 60 + i * 40, 400, 30), 0.98) for i, t in enumerate(texts)]
    return PageContext(
        doc_id="T", page_no=1, page_count=1,
        image=np.zeros((1200, 900, 3), dtype=np.uint8),
        lines=lines, cells=[], seals=[], h_segments=[],
        form_type=formtype.classify(lines, 1200),
    )


@pytest.mark.parametrize(
    "title,expected",
    [
        ("产品质量证明书 Certificate of Quality", "供方合格证"),
        ("NO.1 密封装置证明单", "质量证明单"),
        ("流水卡片", "流水卡片"),
        ("配套单", "配套单"),
        ("产品质量审查报告单", "呈报单"),
        ("成品检验记录", "检验记录"),
        ("某种未知文件", "未识别"),
    ],
)
def test_classify(title, expected):
    assert formtype.classify([TextLine(title, (100, 50, 400, 30), 0.99)], 1200) == expected


def test_supplier_cert_skips_date_format_rule():
    """供方合格证上的 2021-11-17 不应被判为公司日期格式违规。"""
    ctx = page("产品质量证明书 Certificate of Quality", "签发日期 2021-11-17")
    assert ctx.form_type == "供方合格证"
    assert not rules_a.run(ctx, BOOK)


def test_internal_form_still_flags_date_format():
    ctx = page("NO.1 密封装置证明单", "2021-11-17")
    assert ctx.form_type == "质量证明单"
    ids = {f.rule_id for f in rules_a.run(ctx, BOOK)}
    assert "A01_date_format" in ids


def test_applies_helper():
    spec = BOOK.get("B04_blank_cell_no_slash")
    assert RuleBook.applies(spec, "质量证明单")
    assert not RuleBook.applies(spec, "供方合格证")


def test_internal_form_set_excludes_supplier_cert():
    assert formtype.is_internal("质量证明单")
    assert not formtype.is_internal("供方合格证")
