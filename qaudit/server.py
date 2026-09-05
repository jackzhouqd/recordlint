"""兼容垫片：本地审核服务已拆分到 qaudit.web 包。

保留本模块是因为 CLI 与既有测试都按 `from qaudit.server import Handler`
的形式引用。新代码请直接用 `qaudit.web`。
"""
from __future__ import annotations

from .web.app import Config, Handler, evidence_jpeg, serve  # noqa: F401
from .web.render import esc  # noqa: F401

__all__ = ["Config", "Handler", "evidence_jpeg", "serve", "esc"]
