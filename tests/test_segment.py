"""单据切分与 U 类规则测试。

锁住三个从真实档案里发现的坑：
- 供方证明单用「13/19页」而非「第X页共Y页」；
- 那个「共19页」是供方整套文件的连续编号，跨越多份零件证明单，不能据此判缺页；
- 同一份单据的多页会被其他零件的证明单隔开（交错装订），连续切分会切碎。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import rules_u, segment
from qaudit.context import PageContext
from qaudit.findings import RuleBook
from qaudit.ocr import TextLine
from qaudit.segment import PageDigest, PageMarker

BOOK = RuleBook.load(Path(__file__).resolve().parent.parent / "config" / "rules.yaml")


def page(*texts: str, page_no: int = 1, form: str = "质量证明单") -> PageContext:
    lines = [TextLine(t, (100, 80 + i * 40, 400, 30), 0.98) for i, t in enumerate(texts)]
    return PageContext(
        doc_id="D", page_no=page_no, page_count=10,
        image=np.zeros((1400, 1000, 3), dtype=np.uint8),
        lines=lines, cells=[], seals=[], h_segments=[], form_type=form,
    )


def dg(page_no: int, *, keys: dict | None = None, marker: tuple | None = None,
       form: str = "质量证明单", fp: str = "", title: bool = False) -> PageDigest:
    return PageDigest(
        doc_id="D", page_no=page_no, form_type=form, keys=keys or {},
        marker=PageMarker(*marker) if marker else None,
        has_title=title, text_len=200, fingerprint=fp,
    )


# ---------------------------------------------------------------- 页码解析
@pytest.mark.parametrize(
    "text,expect",
    [
        ("第1页共4页", (1, 4)),
        ("共4页第1页", (1, 4)),
        ("1/1份-13/19页", (13, 19)),
        ("11份-1/3页", (1, 3)),
    ],
)
def test_parse_marker_formats(text, expect):
    m = segment.parse_marker(page(text, "零件号", "检验员"))
    assert m and (m.current, m.total) == expect


def test_parse_marker_ignores_dates():
    """日期里的斜杠不得被当成页码。"""
    assert segment.parse_marker(page("2021/07/27", "检验员", "合格")) is None


# ---------------------------------------------------------------- 切分
def test_key_change_starts_new_unit():
    units = segment.segment([
        dg(1, keys={"零件号": "A"}), dg(2, keys={"零件号": "A"}), dg(3, keys={"零件号": "B"}),
    ])
    assert [u.pages for u in units] == [[1, 2], [3]]


def test_page_marker_sequence_keeps_unit():
    """页码顺延且总页数一致时属于同一单据。"""
    units = segment.segment([dg(1, marker=(1, 3)), dg(2, marker=(2, 3)), dg(3, marker=(3, 3))])
    assert len(units) == 1 and units[0].page_count == 3


def test_page_one_starts_new_unit():
    units = segment.segment([dg(1, marker=(1, 2)), dg(2, marker=(2, 2)), dg(3, marker=(1, 2))])
    assert [u.pages for u in units] == [[1, 2], [3]]


def test_declared_total_only_trusted_from_first_page():
    """供方「13/19页」的 19 是整套文件编号，不能当作本单据的总页数。"""
    units = segment.segment([dg(1, marker=(13, 19), keys={"零件号": "A"})])
    assert units[0].declared_total is None
    assert units[0].first_marker.current == 13


def test_interleaved_pages_are_merged():
    """交错装订：同一零件的两页被别的零件隔开，应合并为一份单据。"""
    units = segment.segment([
        dg(1, keys={"零件号": "A"}, marker=(1, 3)),
        dg(2, keys={"零件号": "B"}),
        dg(3, keys={"零件号": "A"}),
    ])
    a = [u for u in units if u.keys.get("零件号") == "A"]
    assert len(a) == 1 and a[0].pages == [1, 3]


def test_units_without_keys_are_not_merged():
    """无关键字段的单据不参与合并，避免把不相干的页揉在一起。"""
    units = segment.segment([dg(1, form="卷宗目录"), dg(2, form="检验记录"), dg(3, form="卷宗目录")])
    assert len(units) == 3


# ---------------------------------------------------------------- U 类规则
def _run(digests: list[PageDigest]):
    units = segment.segment(digests)
    return units, rules_u.run(units, digests, BOOK)


def test_u01_flags_short_unit():
    _, found = _run([dg(1, marker=(1, 3), keys={"零件号": "A"})])
    ids = {f.rule_id for f in found}
    assert "U01_unit_page_missing" in ids


def test_u01_ignores_complete_unit():
    _, found = _run([dg(1, marker=(1, 2), keys={"零件号": "A"}), dg(2, marker=(2, 2), keys={"零件号": "A"})])
    assert "U01_unit_page_missing" not in {f.rule_id for f in found}


def test_u01_head_missing_off_by_default():
    """“首页页码非第1页”默认不报——供方证明单按零件抽页装订属常态。"""
    _, found = _run([dg(1, marker=(13, 19), keys={"零件号": "A"})])
    assert "U01_unit_page_missing" not in {f.rule_id for f in found}


def test_u02_flags_duplicate_page_number():
    _, found = _run([
        dg(1, marker=(2, 5), keys={"零件号": "A"}), dg(2, marker=(2, 5), keys={"零件号": "A"}),
    ])
    msgs = [f.message for f in found if f.rule_id == "U02_unit_page_sequence"]
    assert msgs and "重复" in msgs[0]


def test_u03_flags_unmarked_pages_in_multipage_unit():
    _, found = _run([
        dg(1, marker=(1, 2), keys={"零件号": "A"}), dg(2, keys={"零件号": "A"}),
    ])
    assert "U03_unit_page_number_missing" in {f.rule_id for f in found}


def test_u04_flags_duplicate_record():
    _, found = _run([
        dg(1, keys={"零件号": "A"}, fp="samefingerprint00"),
        dg(2, keys={"零件号": "A"}, fp="samefingerprint00"),
    ])
    hits = [f for f in found if f.rule_id == "U04_duplicate_record"]
    assert hits and "重复传递" in hits[0].message


def test_u04_ignores_distinct_pages():
    _, found = _run([
        dg(1, keys={"零件号": "A"}, fp="aaaaaaaaaaaaaaaa"),
        dg(2, keys={"零件号": "A"}, fp="bbbbbbbbbbbbbbbb"),
    ])
    assert "U04_duplicate_record" not in {f.rule_id for f in found}
