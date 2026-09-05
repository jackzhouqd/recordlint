"""全局夹具。

`qaudit.formtype` 有一个模块级默认分类器（供便捷入口用）。各测试模块在顶层
`RuleBook.load(...)` 时会替换它；为避免测试之间互相污染，每个测试结束后恢复到
「通用层 + 全部示例规则包」这一仓库默认口径。
"""
from pathlib import Path

import pytest

from qaudit.findings import RuleBook

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "config" / "rules.yaml"


@pytest.fixture(autouse=True)
def _restore_default_classifier():
    yield
    RuleBook.load(RULES)
