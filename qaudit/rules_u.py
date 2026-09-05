"""U 类规则：单据级判定（需要先完成单据切分才能判）。

这一类规则的输入不是单页，而是一份完整单据的页面序列，
因此只能在一个归档包的全部页面处理完之后再跑。
"""
from __future__ import annotations

from collections import Counter
from typing import Callable

from .findings import Finding, RuleBook, RuleSpec, make_finding
from .segment import DocUnit, PageDigest

RuleFn = Callable[[list[DocUnit], list[PageDigest], RuleSpec], list[Finding]]
_REGISTRY: dict[str, RuleFn] = {}


def rule(rule_id: str):
    def deco(fn: RuleFn) -> RuleFn:
        _REGISTRY[rule_id] = fn
        return fn

    return deco


def run(units: list[DocUnit], digests: list[PageDigest], book: RuleBook) -> list[Finding]:
    out: list[Finding] = []
    for rule_id, fn in _REGISTRY.items():
        spec = book.get(rule_id)
        if spec is None:
            continue
        try:
            out.extend(fn(units, digests, spec))
        except Exception as exc:
            doc = units[0].doc_id if units else (digests[0].doc_id if digests else "?")
            out.append(
                Finding(
                    rule_id=rule_id, level="LOW", title="规则执行异常", clause="",
                    message=f"{rule_id} 执行失败: {exc}", doc_id=doc, page_no=0, confidence=0.0,
                )
            )
    return out


def _emit(unit: DocUnit, spec: RuleSpec, message: str, conf: float, page_no: int | None = None):
    return make_finding(
        spec,
        doc_id=unit.doc_id,
        page_no=page_no if page_no is not None else unit.start_page,
        message=message,
        evidence=unit.label(),
        confidence=conf,
    )


def _applies(spec: RuleSpec, unit: DocUnit) -> bool:
    return RuleBook.applies(spec, unit.form_type)


# ---------------------------------------------------------------- U01 缺页

@rule("U01_unit_page_missing")
def check_missing_pages(units, digests, spec):
    """声明「共 N 页」但实际不足 N 页——缺页是最严重的档案完整性问题。"""
    flag_head = bool(spec.params.get("flag_missing_head", True))
    out: list[Finding] = []
    for u in units:
        if not _applies(spec, u):
            continue
        # 情形一：单据从第 1 页开始且声明了总页数，据此核对实际页数
        if u.declared_total and u.page_count < u.declared_total:
            out.append(
                _emit(
                    u, spec,
                    f"{u.label()} 声明共 {u.declared_total} 页，实际只有 {u.page_count} 页，疑似缺页",
                    conf=0.7,
                )
            )
            continue
        # 情形二：单据首页的页码不是第 1 页，说明前面的页没在档案里
        first = u.first_marker
        if flag_head and first and first.current > 1:
            out.append(
                _emit(
                    u, spec,
                    f"{u.label()} 首页页码为第 {first.current} 页（共 {first.total} 页），"
                    f"其前 {first.current - 1} 页未见于本档案",
                    conf=0.45,
                )
            )
    return out


# ---------------------------------------------------------------- U02 页码不连续

@rule("U02_unit_page_sequence")
def check_page_sequence(units, digests, spec):
    """单据内页码应从 1 连续递增，出现跳号或重号说明装订/扫描有误。"""
    out: list[Finding] = []
    for u in units:
        if not _applies(spec, u) or len(u.markers) < 2:
            continue
        seq = [m.current for m in u.markers]
        dup = [n for n, c in Counter(seq).items() if c > 1]
        if dup:
            out.append(
                _emit(u, spec, f"{u.label()} 内页码重复：第 {'、'.join(map(str, sorted(dup)))} 页出现多次", conf=0.6)
            )
            continue
        expected = list(range(min(seq), min(seq) + len(seq)))
        if sorted(seq) != expected:
            missing = sorted(set(range(min(seq), max(seq) + 1)) - set(seq))
            if missing:
                out.append(
                    _emit(u, spec, f"{u.label()} 内页码不连续，缺第 {'、'.join(map(str, missing))} 页", conf=0.6)
                )
    return out


# ---------------------------------------------------------------- U03 多页单据缺页码

@rule("U03_unit_page_number_missing")
def check_page_number_missing(units, digests, spec):
    """凡多页记录均应填写页码。

    这一条此前无法判定——按整个归档包算“多页”毫无意义，
    切出单据后才有了正确的判定单元。
    """
    min_pages = int(spec.params.get("min_pages", 2))
    out: list[Finding] = []
    by_page = {(d.doc_id, d.page_no): d for d in digests}
    for u in units:
        if not _applies(spec, u) or u.page_count < min_pages:
            continue
        marked = {m.current for m in u.markers}
        unmarked = [
            p for p in u.pages
            if not by_page.get((u.doc_id, p), None) or not by_page[(u.doc_id, p)].marker
        ]
        if len(marked) >= u.page_count or not unmarked:
            continue
        preview = "、".join(str(p) for p in unmarked[:6])
        more = f" 等 {len(unmarked)} 页" if len(unmarked) > 6 else ""
        out.append(
            _emit(
                u, spec,
                f"{u.label()} 为 {u.page_count} 页的多页记录，其中 {len(unmarked)} 页未见页码"
                f"（第 {preview}{more}）",
                conf=0.55, page_no=unmarked[0],
            )
        )
    return out


# ---------------------------------------------------------------- U04 重复传递

@rule("U04_duplicate_record")
def check_duplicate_record(units, digests, spec):
    """同一批产品不允许将同一份记录重复传递。

    用页面文本指纹比对：同一份记录两次扫描像素必然不同，但文字内容一致。
    """
    min_pages = int(spec.params.get("min_text_len", 0))
    seen: dict[str, tuple[int, str]] = {}
    dup: dict[str, list[tuple[str, int]]] = {}
    for d in digests:
        if not d.fingerprint or d.text_len < min_pages:
            continue
        if d.fingerprint in seen:
            dup.setdefault(d.fingerprint, []).append((d.doc_id, d.page_no))
        else:
            seen[d.fingerprint] = (d.page_no, d.doc_id)

    out: list[Finding] = []
    for fp, occurrences in dup.items():
        first_page, doc_id = seen[fp]
        pages = [first_page] + [p for _, p in occurrences]
        unit = next((u for u in units if first_page in u.pages), None)
        if unit is None:
            continue
        if not _applies(spec, unit):
            continue
        out.append(
            _emit(
                unit, spec,
                f"第 {'、'.join(map(str, pages))} 页内容完全相同，疑为同一份记录重复传递",
                conf=0.6, page_no=first_page,
            )
        )
    return out
