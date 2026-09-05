"""单据切分：把一个归档包切成若干份独立表单。

定位分四级：档案 → **单据** → 页 → 页内坐标。单据这一级此前缺失，
导致“多页记录须填页码”“呈报单页数与实物一致”这类规则无从判起——
一个 180 页的 PDF 里其实是几十份独立表单，按整包判页码毫无意义。

切分只用页面自身的信息，不依赖外部系统：
  1) 页码标记「第X页共Y页」——最强信号，第 1 页即单据起始
  2) 关键字段（零件号 / 批次顺序号 / 型别）变化
  3) 表单类型变化
  4) 页面顶部出现表单标题
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .context import PageContext
from .ocr import TextLine

# 用于判定“换了一份单据”的关键字段。取值变化即认为进入新单据。
KEY_FIELDS = (
    "零件号", "零件件号", "零（组）件号", "零件图号", "图号",
    "批次顺序号", "批次号", "型别",
)

_CUR_RE = re.compile(r"第\s*(\d+)\s*页")
_TOTAL_RE = re.compile(r"共\s*(\d+)\s*页")
# 供方证明单常用「13/19页」这类写法（含「1/1份-13/19页」），厂内表单则用「第X页共Y页」
_SLASH_RE = re.compile(r"(?<!\d)(\d{1,3})\s*/\s*(\d{1,3})\s*页")
_TITLE_RE = re.compile(r"证明单|流水卡片|配套单|装配信息单|检验记录|报告单|通知单|合格证|检查表")


@dataclass(frozen=True)
class PageMarker:
    """页面上声明的页码，如「共2页第1页」。"""

    current: int
    total: int
    line: TextLine | None = None


@dataclass(frozen=True)
class PageDigest:
    """页面摘要：单据级规则的输入。

    刻意不含图像——一个归档包上百页，缓存整图会吃掉几个 GB。
    """

    doc_id: str
    page_no: int
    form_type: str
    keys: dict[str, str]
    marker: PageMarker | None
    has_title: bool
    text_len: int
    fingerprint: str  # 页面内容指纹，用于识别重复传递的记录


@dataclass
class DocUnit:
    """一份独立单据。"""

    unit_id: str
    doc_id: str
    form_type: str
    pages: list[int] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)
    declared_total: int | None = None  # 仅当单据首页页码为 1 时才可信
    first_marker: PageMarker | None = None
    markers: list[PageMarker] = field(default_factory=list)
    fingerprints: list[str] = field(default_factory=list)

    @property
    def start_page(self) -> int:
        return self.pages[0] if self.pages else 0

    @property
    def end_page(self) -> int:
        return self.pages[-1] if self.pages else 0

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def label(self) -> str:
        key = self.keys.get("零件号") or self.keys.get("零件件号") or self.keys.get("批次顺序号") or ""
        span = f"p{self.start_page}" if self.page_count == 1 else f"p{self.start_page}-{self.end_page}"
        return f"{self.form_type}{('·' + key) if key else ''}（{span}）"


# ---------------------------------------------------------------- 页面摘要

def digest(ctx: PageContext) -> PageDigest:
    return PageDigest(
        doc_id=ctx.doc_id,
        page_no=ctx.page_no,
        form_type=ctx.form_type,
        keys=extract_keys(ctx),
        marker=parse_marker(ctx),
        has_title=_has_title(ctx),
        text_len=len(ctx.text),
        fingerprint=fingerprint(ctx),
    )


def parse_marker(ctx: PageContext) -> PageMarker | None:
    """解析页码标记。

    覆盖三种写法：「第1页共4页」「共4页第1页」（厂内预印表单）、
    「13/19页」（供方证明单，常见形式为 1/1份-13/19页）。
    """
    for line in ctx.lines:
        marker = _parse_marker_text(line.text, line)
        if marker:
            return marker
    return _parse_marker_text(ctx.text, None)  # 页码可能被 OCR 拆成两行，退而在整页文本里找


def _parse_marker_text(text: str, line: TextLine | None) -> PageMarker | None:
    cur, total = _CUR_RE.search(text), _TOTAL_RE.search(text)
    if cur and total:
        return PageMarker(int(cur.group(1)), int(total.group(1)), line)
    m = _SLASH_RE.search(text)
    if m:
        current, tot = int(m.group(1)), int(m.group(2))
        if 1 <= current <= tot <= 999:
            return PageMarker(current, tot, line)
    return None


def extract_keys(ctx: PageContext) -> dict[str, str]:
    """取表头关键字段的值。优先用单元格的「标签→右侧值」，退化到文本正则。"""
    out: dict[str, str] = {}
    for field_name in KEY_FIELDS:
        value = _cell_value(ctx, field_name) or _text_value(ctx, field_name)
        if value:
            out[field_name] = value
    return out


def _cell_value(ctx: PageContext, label: str) -> str | None:
    anchors = [c for c in ctx.cells if c.content.replace(" ", "") == label]
    if not anchors:
        return None
    ax, ay, aw, ah = anchors[0].rect
    best, best_gap = None, None
    for c in ctx.cells:
        cx, cy, cw, ch = c.rect
        if cx < ax + aw * 0.8 or not c.content:
            continue
        overlap = min(ay + ah, cy + ch) - max(ay, cy)
        if overlap <= 0 or overlap / min(ah, ch) < 0.5:
            continue
        gap = cx - (ax + aw)
        if best_gap is None or gap < best_gap:
            best, best_gap = c, gap
    return _clean(best.content) if best else None


def _text_value(ctx: PageContext, label: str) -> str | None:
    pattern = re.compile(re.escape(label) + r"[:：\s]*([A-Za-z0-9./\-]{3,30})")
    for line in ctx.lines:
        m = pattern.search(line.text.replace(" ", ""))
        if m:
            return _clean(m.group(1))
    return None


def _clean(value: str) -> str:
    return re.sub(r"\s+", "", value)[:30]


def _has_title(ctx: PageContext) -> bool:
    head = "".join(l.text for l in ctx.lines if l.cy < ctx.height * 0.3)
    return bool(_TITLE_RE.search(head))


def fingerprint(ctx: PageContext) -> str:
    """页面内容指纹：用于识别“同一份记录被重复传递”。

    只取文本，不取像素——同一份记录的两次扫描像素必然不同，但文字一致。
    """
    import hashlib

    text = re.sub(r"\s+", "", ctx.text)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16] if len(text) >= 20 else ""


# ---------------------------------------------------------------- 切分

def segment(digests: list[PageDigest]) -> list[DocUnit]:
    """把一个归档包的页面摘要切成若干单据。"""
    units: list[DocUnit] = []
    current: DocUnit | None = None

    for d in digests:
        if current is None or _starts_new_unit(current, d):
            current = DocUnit(
                unit_id=f"{d.doc_id}#U{len(units) + 1:03d}",
                doc_id=d.doc_id,
                form_type=d.form_type,
            )
            units.append(current)
        current.pages.append(d.page_no)
        if d.marker:
            if not current.markers:
                current.first_marker = d.marker
                # 只有从第 1 页开始的单据，其“共 N 页”才可信。
                # 供方证明单的「13/19页」是他们整套文件的连续编号，
                # 里面本就含多个零件的证明单，据此判缺页会大批误报。
                if d.marker.current == 1:
                    current.declared_total = d.marker.total
            current.markers.append(d.marker)
        for k, v in d.keys.items():
            current.keys.setdefault(k, v)
        if d.fingerprint:
            current.fingerprints.append(d.fingerprint)
    return _merge_interleaved(units)


def _merge_interleaved(units: list[DocUnit]) -> list[DocUnit]:
    """合并交错装订的同一份单据。

    实测档案里，同一份供方证明单的多页会被其他零件的证明单隔开
    （p13 是 3102A 的第 1 页，p14 是 3103A，p15 又回到 3102A）。
    连续切分会把它切碎，导致“声明 3 页实际 1 页”的假缺页。
    因此按关键字段签名合并——单据的页集合允许不连续。
    """
    merged: dict[tuple, DocUnit] = {}
    out: list[DocUnit] = []
    for u in units:
        sig = _key_signature(u)
        if sig is None:
            out.append(u)
            continue
        head = merged.get(sig)
        if head is None:
            merged[sig] = u
            out.append(u)
            continue
        head.pages.extend(u.pages)
        head.pages.sort()
        head.markers.extend(u.markers)
        head.fingerprints.extend(u.fingerprints)
        if head.declared_total is None:
            head.declared_total = u.declared_total
        if head.first_marker is None:
            head.first_marker = u.first_marker
    return out


def _key_signature(unit: DocUnit) -> tuple | None:
    """单据身份签名。无关键字段时返回 None，不参与合并。"""
    if not unit.keys:
        return None
    return (unit.form_type,) + tuple(sorted(unit.keys.items()))


def _starts_new_unit(unit: DocUnit, d: PageDigest) -> bool:
    # 1) 关键字段取值变化——最硬的证据：换了零件/批次就是换了单据。
    #    优先级高于页码顺延，因为供方文件的连续页码会跨越多份零件证明单。
    for k, v in d.keys.items():
        if k in unit.keys and unit.keys[k] != v:
            return True

    # 2) 页码标记：声明为第 1 页即新单据；顺延页号则继续当前单据
    if d.marker:
        if d.marker.current == 1:
            return True
        last = unit.markers[-1].current if unit.markers else None
        if last is not None and d.marker.current == last + 1 and d.marker.total == unit.markers[-1].total:
            return False

    # 3) 当前单据已按声明页数收满
    if unit.declared_total and unit.page_count >= unit.declared_total:
        return True

    # 4) 表单类型变化（“未识别”不作依据，避免把续页切碎）
    if d.form_type != unit.form_type and "未识别" not in (d.form_type, unit.form_type):
        return True

    # 5) 出现表单标题，且当前单据已有内容
    if d.has_title and unit.page_count > 0 and d.keys:
        return True

    return False
