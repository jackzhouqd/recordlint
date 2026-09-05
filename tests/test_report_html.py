"""报告 HTML 的交付约束测试。

report.html 会被拷到任意一台机器上打开、也可能被打印，因此两条硬约束：
自包含（不引用任何外部资源）、且与审核服务共用同一套设计系统。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qaudit.context import PageContext  # noqa: E402
from qaudit.findings import Finding  # noqa: E402
from qaudit.report import ReportBuilder  # noqa: E402


@pytest.fixture
def html(tmp_path: Path) -> str:
    img = np.full((400, 600, 3), 250, dtype=np.uint8)
    ctx = PageContext(doc_id="D1", page_no=1, page_count=1, image=img, lines=[],
                      cells=[], seals=[], h_segments=[], form_type="质量证明单")
    finding = Finding(rule_id="A01_date_format", level="HIGH", title="日期格式不规范",
                      clause="A01", message="日期未写满 8 位 <含尖括号与 & 符号>",
                      doc_id="D1", page_no=1, bbox=(10, 20, 80, 30), confidence=0.9)
    builder = ReportBuilder(tmp_path, {"name": "测试规则库", "version": "1.0"})
    builder.add_page(ctx, [finding], 0.5)
    paths = builder.write()
    return paths["html"].read_text(encoding="utf-8")


def test_report_is_self_contained(html):
    """拷到任何一台机器上双击都要能正常显示——不许有外链。"""
    assert "<link" not in html, "不得引用外部样式表"
    assert 'src="/static' not in html, "不得引用服务端静态资源"
    assert "http://" not in html and "https://" not in html, "不得有任何外部地址"
    assert "data:image/jpeg;base64," in html, "证据图必须内联"


def test_report_inlines_the_shared_design_system(html):
    """与审核服务共用 app.css。这条测试是为了在样式表被挪走时立刻报错，
    而不是等到客户打开报告发现一片素文本。"""
    assert "--accent:" in html and "[data-theme=\"light\"]" in html
    assert "@media print" in html, "报告要能打印"


def test_report_escapes_user_content(html):
    """疑点说明来自 OCR 文本，含尖括号会把版面撑坏。"""
    assert "&lt;含尖括号与 &amp; 符号&gt;" in html
    assert "<含尖括号" not in html


def test_report_keeps_adjudication_hooks(html):
    """判定结果导出后由 `qaudit gold` 合入金标准集，这些 data-* 是契约。"""
    for attr in ("data-key=", "data-doc=", "data-page=", "data-rule=", "data-level="):
        assert attr in html, f"缺少 {attr}，导出的标注 JSON 将无法合入金标准集"
