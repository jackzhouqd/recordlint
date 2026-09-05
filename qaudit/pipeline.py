"""审核流水线：接入 → OCR → 版面 → 印章 → 页级规则 → 单据切分 → 单据级规则。

单据级规则（U 类）必须等一个归档包的全部页面处理完才能跑，因此流水线在
档案边界处额外产出一个 UnitResult。缓存的是页面摘要而不是图像——
一个归档包上百页，缓存整图会吃掉几个 GB。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import formtype, ingest, layout, rules_a, rules_b, rules_f, rules_u, seal, segment
from .context import PageContext
from .findings import Finding, RuleBook
from .ocr import OcrEngine
from .segment import DocUnit, PageDigest


@dataclass(frozen=True)
class PageResult:
    ctx: PageContext
    findings: list[Finding]
    elapsed: float


@dataclass(frozen=True)
class UnitResult:
    """一个归档包切分完成后的单据清单与单据级疑点。"""

    doc_id: str
    units: list[DocUnit]
    findings: list[Finding]
    elapsed: float = 0.0


@dataclass
class _ArchiveBuffer:
    doc_id: str
    digests: list[PageDigest] = field(default_factory=list)


def audit_pages(
    target: str | Path,
    book: RuleBook,
    ocr: OcrEngine,
    *,
    limit: int | None = None,
    long_side: int = ingest.DEFAULT_LONG_SIDE,
    deskew_enabled: bool = False,
    seal_model: dict | None = None,
    on_page: Callable[[int, PageContext], None] | None = None,
) -> Iterator[PageResult | UnitResult]:
    """逐页审核并产出结果；每个归档包结束时追加一条 UnitResult。"""
    counts = ingest.doc_page_counts(target)
    buffer: _ArchiveBuffer | None = None

    for idx, page in enumerate(ingest.iter_pages(target, long_side, deskew_enabled)):
        if limit is not None and idx >= limit:
            break
        if buffer is not None and buffer.doc_id != page.doc_id:
            yield _close_archive(buffer, book)
            buffer = None
        if buffer is None:
            buffer = _ArchiveBuffer(doc_id=page.doc_id)

        started = time.perf_counter()
        ctx = build_context(page, counts.get(page.doc_id, 1), ocr, seal_model=seal_model,
                            classifier=book.classifier)
        findings = _dedupe(
            rules_a.run(ctx, book) + rules_b.run(ctx, book) + rules_f.run(ctx, book)
        )
        buffer.digests.append(segment.digest(ctx))
        if on_page:
            on_page(idx, ctx)
        yield PageResult(ctx=ctx, findings=findings, elapsed=time.perf_counter() - started)

    if buffer is not None:
        yield _close_archive(buffer, book)


def _close_archive(buffer: _ArchiveBuffer, book: RuleBook) -> UnitResult:
    started = time.perf_counter()
    units = segment.segment(buffer.digests)
    findings = _dedupe(rules_u.run(units, buffer.digests, book))
    return UnitResult(
        doc_id=buffer.doc_id,
        units=units,
        findings=findings,
        elapsed=time.perf_counter() - started,
    )


def _dedupe(findings: list[Finding]) -> list[Finding]:
    """同一页同一规则、同样的证据与定位只报一次。"""
    seen: set[tuple] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.rule_id, f.doc_id, f.page_no, f.bbox, f.message)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def build_context(page: ingest.Page, page_count: int, ocr: OcrEngine,
                  seal_model: dict | None = None, classifier=None) -> PageContext:
    """``classifier`` 为空时退回 formtype 模块默认分类器（便捷入口）；流水线一律显式传 book.classifier。"""
    lines = ocr.run(page.image)
    lay = layout.analyze(page.image)
    cells = layout.assign_texts(lay.cells, lines)
    seals = seal.detect(page.image)
    form_type = formtype.classify(lines, page.image.shape[0], classifier)
    return PageContext(
        form_type=form_type,
        source=page.source,
        doc_id=page.doc_id,
        page_no=page.page_no,
        page_count=page_count,
        image=page.image,
        lines=lines,
        cells=cells,
        seals=seals,
        h_segments=lay.h_segments,
        seal_model=seal_model,
    )
