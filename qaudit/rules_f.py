"""F 类规则：表单专项填写规则（示例规则包口径）。

与 A/B 类的区别在于强绑定表单类型：每条规则只对特定表单生效，
适用范围写在 rules.yaml 的 applies_to 里。
"""
from __future__ import annotations

import re
from typing import Callable

from .context import PageContext
from .findings import Finding, RuleSpec, RuleBook, make_finding
from .layout import Cell
from .ocr import TextLine
from .seal import red_pixel_ratio

RuleFn = Callable[[PageContext, RuleSpec], list[Finding]]
_REGISTRY: dict[str, RuleFn] = {}

BATCH_RE = re.compile(r"\d{2,4}\s*-\s*\d{1,2}\s*-\s*\d{1,3}")  # 批次顺序号形如 22-02-1-2
PAGE_COUNT_RE = re.compile(r"共\s*\d+\s*页")


def rule(rule_id: str):
    def deco(fn: RuleFn) -> RuleFn:
        _REGISTRY[rule_id] = fn
        return fn

    return deco


def run(ctx: PageContext, book: RuleBook) -> list[Finding]:
    out: list[Finding] = []
    for rule_id, fn in _REGISTRY.items():
        spec = book.get(rule_id)
        if spec is None or not RuleBook.applies(spec, ctx.form_type):
            continue
        try:
            out.extend(fn(ctx, spec))
        except Exception as exc:
            out.append(
                Finding(
                    rule_id=rule_id, level="LOW", title="规则执行异常", clause="",
                    message=f"{rule_id} 执行失败: {exc}",
                    doc_id=ctx.doc_id, page_no=ctx.page_no, confidence=0.0,
                )
            )
    return out


# ---------------------------------------------------------------- 辅助

def _emit(ctx: PageContext, spec: RuleSpec, bbox, message: str, conf: float, evidence: str = ""):
    return make_finding(
        spec, doc_id=ctx.doc_id, page_no=ctx.page_no, message=message,
        bbox=tuple(int(v) for v in bbox) if bbox else None,
        evidence=evidence, confidence=conf,
    )


def _label_cell(ctx: PageContext, label: str) -> Cell | None:
    """找到内容等于/包含指定栏目名的单元格。"""
    exact = [c for c in ctx.cells if c.content.replace(" ", "") == label]
    if exact:
        return exact[0]
    loose = [c for c in ctx.cells if label in c.content and len(c.content) <= len(label) + 4]
    return loose[0] if loose else None


def _value_cell(ctx: PageContext, label: str) -> Cell | None:
    """取栏目名右侧同行最近的单元格作为其值。"""
    anchor = _label_cell(ctx, label)
    if anchor is None:
        return None
    ax, ay, aw, ah = anchor.rect
    best, best_gap = None, None
    for c in ctx.cells:
        cx, cy, cw, ch = c.rect
        if cx < ax + aw * 0.8:
            continue
        overlap = min(ay + ah, cy + ch) - max(ay, cy)
        if overlap <= 0 or overlap / min(ah, ch) < 0.5:
            continue
        gap = cx - (ax + aw)
        if best_gap is None or gap < best_gap:
            best, best_gap = c, gap
    return best


def _bottom_lines(ctx: PageContext, frac: float = 0.62) -> list[TextLine]:
    return [l for l in ctx.lines if l.cy > ctx.height * frac]


# ---------------------------------------------------------------- 质量证明单

@rule("F01_assembly_slash_forbidden")
def check_assembly_slash(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """示例包 F01：“装配处”由下游填写，本单位不允许划“/”。"""
    cell = _value_cell(ctx, "装配处")
    if cell is None:
        return []
    content = cell.content.replace(" ", "")
    if content in ("/", "／", "\\"):
        return [
            _emit(
                ctx, spec, cell.rect,
                "“装配处”栏被划了“/”，该栏应留空由下游配套（装配）检验组填写", conf=0.8, evidence=content,
            )
        ]
    return []


@rule("F02_design_version_missing")
def check_design_version(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """表头无“设计图版次”栏时，须在特殊记载栏记录“设计图版次：*版”。"""
    if "设计图版次" in ctx.text or "图纸版次" in ctx.text:
        return []
    if ctx.is_blank_page:
        return []
    return [
        _emit(ctx, spec, None, "全页未见“设计图版次”信息，须在表头或“特殊记载”栏记录", conf=0.5)
    ]


@rule("F03_report_page_count")
def check_report_page_count(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """b/c) 特殊记载栏的呈报单须写明名称、编号及页数（含传真件与分析报告）。"""
    hits = [l for l in ctx.lines if "呈报" in l.text]
    if not hits:
        return []
    if PAGE_COUNT_RE.search(ctx.text):
        return []
    return [
        _emit(
            ctx, spec, hits[0].bbox,
            "“特殊记载”栏记载了呈报单但未见“共**页”，页数须含呈报传真件与不合格品分析报告",
            conf=0.6, evidence=hits[0].text,
        )
    ]


# ---------------------------------------------------------------- 故障修理通知单

@rule("F04_repair_no_seal")
def check_repair_seal(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """修理结论应逐条闭环、盖章并填写日期。"""
    if ctx.is_blank_page:
        return []
    if red_pixel_ratio(ctx.image) >= float(spec.params.get("min_red_pixel_ratio", 0.00002)):
        return []
    return [_emit(ctx, spec, None, "“故障修理通知单”整页未检出检验印章，修理结论须盖章确认", conf=0.7)]


@rule("F05_repair_no_closure")
def check_repair_closure(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """设计批复的超差内容须有相应的检验闭环结论。"""
    triggers = spec.params.get("trigger_keywords", [])
    closures = spec.params.get("closure_keywords", [])
    hits = [l for l in ctx.lines if any(k in l.text for k in triggers)]
    if not hits or any(k in ctx.text for k in closures):
        return []
    return [
        _emit(
            ctx, spec, hits[0].bbox,
            f"出现超差处理内容（{hits[0].text[:18]}）但未见检验闭环结论（如“已退回原单位”“废品已隔离”）",
            conf=0.5, evidence=hits[0].text,
        )
    ]


# ---------------------------------------------------------------- 检验记录

@rule("F06_inspection_header_incomplete")
def check_inspection_header(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """首页检验记录表头必须填写完整。"""
    if ctx.page_no != 1 or ctx.is_blank_page:
        return []
    required = spec.params.get("required_fields", ["零件号", "批次号"])
    missing = [f for f in required if f not in ctx.text]
    if not missing:
        return []
    return [
        _emit(ctx, spec, None, f"首页表头缺少 {'、'.join(missing)}，检验记录首页表头须填写完整", conf=0.55)
    ]


@rule("F07_reject_note_missing")
def check_reject_note(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """不合格记录须备注拒收单编号，且拒收单须与成品检验记录一同存档。"""
    triggers = spec.params.get("trigger_keywords", ["超差", "不合格", "废品"])
    hits = [l for l in ctx.lines if any(k in l.text for k in triggers)]
    if not hits or "拒收单" in ctx.text:
        return []
    return [
        _emit(
            ctx, spec, hits[0].bbox,
            f"出现不合格/超差记录（{hits[0].text[:18]}）但未见拒收单编号备注",
            conf=0.5, evidence=hits[0].text,
        )
    ]


@rule("F08_hardness_record_format")
def check_hardness(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """无零件序号时硬度值记录范围值；抽检时应记录抽检数量/序号。"""
    pattern = re.compile(r"\d+(?:\.\d+)?\s*(HRC|HBW|HRB|HB|HV)")
    hits = [l for l in ctx.lines if pattern.search(l.text)]
    if not hits:
        return []
    has_range = any("～" in l.text or "~" in l.text for l in hits)
    sampling = "抽检" in ctx.text
    if has_range and not sampling:
        return []
    if sampling and re.search(r"抽检\s*\d+", ctx.text):
        return []
    msg = (
        "硬度值为抽检但未见抽检数量/序号" if sampling
        else f"硬度值“{hits[0].text[:20]}”未按范围值记录（无零件序号时应记范围值）"
    )
    return [_emit(ctx, spec, hits[0].bbox, msg, conf=0.45, evidence=hits[0].text)]


# ---------------------------------------------------------------- 入库单据

@rule("F09_label_quantity_empty")
def check_label_quantity(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """“入库箱数”和“装箱数量”两栏必须填写，不得空白。"""
    out: list[Finding] = []
    for field in spec.params.get("required_fields", ["入库箱数", "装箱数量"]):
        anchor = _label_cell(ctx, field)
        if anchor is None:
            continue
        value = _value_cell(ctx, field)
        if value is None or value.is_blank:
            out.append(
                _emit(
                    ctx, spec, (value or anchor).rect,
                    f"“{field}”栏空白，该栏必须填写不得空白", conf=0.6, evidence=field,
                )
            )
    return out


# ---------------------------------------------------------------- 代料单 / 呈报单 / 合格证

@rule("F10_substitute_no_pairing")
def check_substitute(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """“代料单”传递总装单位时须在下方备注配套关系与零件序列号。"""
    if ctx.is_blank_page:
        return []
    bottom = "".join(l.text for l in _bottom_lines(ctx))
    if any(k in bottom for k in spec.params.get("keywords", ["配套", "序列号", "序号"])):
        return []
    return [_emit(ctx, spec, None, "“代料单”下方未见配套关系/零件序列号备注", conf=0.5)]


@rule("F11_report_batch_note")
def check_report_batch(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """“产品质量审查报告单”页面下方空白处需备注装于对应组件的批次号及序列号。"""
    if ctx.is_blank_page:
        return []
    bottom = "".join(l.text for l in _bottom_lines(ctx))
    if BATCH_RE.search(bottom) or "不涉及呈报" in bottom:
        return []
    return [_emit(ctx, spec, None, "页面下方未见装于对应组件的批次号及序列号备注", conf=0.45)]


@rule("F12_cert_closure_note")
def check_cert_closure(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """合格证上的工艺呈报在下游闭环后须备注“呈报已闭环”；
    多批次时未办呈报的批次须备注“XX批不涉及呈报”。"""
    hits = [l for l in ctx.lines if "呈报" in l.text]
    if not hits:
        return []
    if "闭环" in ctx.text or "不涉及呈报" in ctx.text:
        return []
    return [
        _emit(
            ctx, spec, hits[0].bbox,
            "合格证备注了工艺呈报但未见“呈报已闭环”或“XX批不涉及呈报”标注",
            conf=0.5, evidence=hits[0].text,
        )
    ]
