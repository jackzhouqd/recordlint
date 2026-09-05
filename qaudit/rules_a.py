"""A 类规则：文本确定性判定。

每条规则是纯函数 (PageContext, RuleSpec) -> list[Finding]，可独立单元测试。
判定必须锚定到具体文本行的 bbox，报告才能在原图上红框定位。
"""
from __future__ import annotations

import re
from typing import Callable

from .context import PageContext
from .findings import Finding, RuleSpec, RuleBook, make_finding
from .layout import column_x_range
from .ocr import TextLine

RuleFn = Callable[[PageContext, RuleSpec], list[Finding]]
_REGISTRY: dict[str, RuleFn] = {}


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
        except Exception as exc:  # 单条规则异常不得中断整批审核
            out.append(
                Finding(
                    rule_id=rule_id,
                    level="LOW",
                    title="规则执行异常",
                    clause="",
                    message=f"{rule_id} 执行失败: {exc}",
                    doc_id=ctx.doc_id,
                    page_no=ctx.page_no,
                    confidence=0.0,
                )
            )
    return out


# ---------------------------------------------------------------- 通用辅助

def _emit(ctx: PageContext, spec: RuleSpec, line: TextLine, message: str, conf: float = 1.0):
    return make_finding(
        spec,
        doc_id=ctx.doc_id,
        page_no=ctx.page_no,
        message=message,
        bbox=line.bbox,
        evidence=line.text,
        confidence=conf,
    )


NUM = r"\d+(?:\.\d+)?"


# ---------------------------------------------------------------- A01 日期

@rule("A01_date_format")
def check_date_format(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    skip_tokens = spec.params.get("skip_if_contains", [])
    pattern = re.compile(rf"(?<!\d)((?:19|20)\d{{2}})([./\-/])(\d{{1,2}})([./\-/])(\d{{1,2}})(?!\d)")
    out: list[Finding] = []
    for line in ctx.lines:
        # 预印“XX年XX月XX日”表单栏位不适用
        if all(tok in line.text for tok in skip_tokens) and skip_tokens:
            continue
        for m in pattern.finditer(line.text):
            year, s1, month, s2, day = m.groups()
            if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
                continue
            problems = []
            bad_seps = sorted({s for s in (s1, s2) if s != "."})
            if bad_seps:
                problems.append("分隔符应为“.”，实为“{}”".format("”“".join(bad_seps)))
            if len(month) != 2 or len(day) != 2:
                problems.append("月/日未补齐两位")
            if problems:
                out.append(
                    _emit(
                        ctx,
                        spec,
                        line,
                        f"日期“{m.group(0)}”不符合 8 位格式（{'；'.join(problems)}），应写作 "
                        f"{year}.{int(month):02d}.{int(day):02d}",
                        conf=0.85,
                    )
                )
    return out


# ---------------------------------------------------------------- A02 范围值符号

@rule("A02_range_symbol")
def check_range_symbol(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    units = spec.params.get("units", [])
    unit_alt = "|".join(re.escape(u) for u in sorted(units, key=len, reverse=True))
    dash = r"[-－–—]"
    pat_unit = re.compile(rf"(?<![\d\-])({NUM})\s*({unit_alt})?\s*{dash}\s*({NUM})\s*({unit_alt})?")
    pat_phi = re.compile(rf"Φ\s*{NUM}\s*{dash}\s*Φ?\s*{NUM}")
    out: list[Finding] = []
    for line in ctx.lines:
        for m in pat_unit.finditer(line.text):
            # 必须有计量单位才认定为范围值，否则可能是批次号/合同号
            if not (m.group(2) or m.group(4)):
                continue
            out.append(
                _emit(ctx, spec, line, f"范围值“{m.group(0).strip()}”使用了短横，应改用“～”", conf=0.9)
            )
        for m in pat_phi.finditer(line.text):
            out.append(
                _emit(ctx, spec, line, f"直径范围“{m.group(0)}”使用了短横，应改用“～”", conf=0.9)
            )
    return out


# ---------------------------------------------------------------- A03/A04 直径符号

@rule("A03_diameter_symbol")
def check_diameter_symbol(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    forbidden = spec.params.get("forbidden", [])
    out: list[Finding] = []
    for line in ctx.lines:
        hits = [s for s in forbidden if s in line.text]
        if hits:
            out.append(
                _emit(ctx, spec, line, f"使用了非规范直径符号 {'、'.join(hits)}，应统一为大写 Φ", conf=0.7)
            )
    return out


@rule("A04_diameter_range")
def check_diameter_range(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    pattern = re.compile(rf"Φ\s*{NUM}\s*[～~]\s*(?!Φ)({NUM})")
    out: list[Finding] = []
    for line in ctx.lines:
        for m in pattern.finditer(line.text):
            out.append(
                _emit(ctx, spec, line, f"直径范围“{m.group(0)}”缺少第二个 Φ，应写作 ΦXX～ΦXX", conf=0.85)
            )
    return out


# ---------------------------------------------------------------- A05~A08 计量单位

@rule("A05_unit_mm_redundant")
def check_mm(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    pattern = re.compile(rf"{NUM}\s*mm(?![a-zA-Z/])")
    out: list[Finding] = []
    for line in ctx.lines:
        for m in pattern.finditer(line.text):
            out.append(_emit(ctx, spec, line, f"“{m.group(0)}”中 mm 应省略（交付外厂除外）", conf=0.6))
    return out


@rule("A06_unit_case")
def check_unit_case(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    mapping: dict[str, str] = spec.params.get("wrong_to_right", {})
    out: list[Finding] = []
    for line in ctx.lines:
        for wrong, right in mapping.items():
            if re.search(rf"\d\s*{re.escape(wrong)}(?![a-zA-Z])", line.text):
                out.append(
                    _emit(ctx, spec, line, f"计量单位“{wrong}”不符合 GB3100，应为“{right}”", conf=0.8)
                )
    return out


@rule("A07_square_cubic_unit")
def check_square_cubic(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    pattern = re.compile(r"m\s*([23²³])(?![0-9])")
    out: list[Finding] = []
    for line in ctx.lines:
        for m in pattern.finditer(line.text):
            n = {"²": "2", "³": "3"}.get(m.group(1), m.group(1))
            out.append(_emit(ctx, spec, line, f"“{m.group(0)}”应统一写成 m（{n}）", conf=0.6))
    return out


@rule("A08_chinese_numeral_unit")
def check_chinese_numeral(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    pattern = re.compile(r"[零一二三四五六七八九十百千两]{1,6}\s*(公斤|千克|摄氏度|毫米|厘米|米|吨|克)")
    out: list[Finding] = []
    for line in ctx.lines:
        for m in pattern.finditer(line.text):
            out.append(_emit(ctx, spec, line, f"“{m.group(0)}”应使用阿拉伯数字与法定单位符号", conf=0.7))
    return out


# ---------------------------------------------------------------- A09 页码

@rule("A09_page_number")
def check_page_number(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    if ctx.page_count < int(spec.params.get("min_pages", 2)) or ctx.is_blank_page:
        return []
    total = re.compile(r"共\s*(\d+)\s*页")
    current = re.compile(r"第\s*\d+\s*页")
    strict = re.compile(r"第\s*\d+\s*页\s*[，,、 ]?\s*共\s*\d+\s*页")
    loose = re.compile(r"共\s*\d+\s*页|第\s*\d+\s*页")
    strict_order = bool(spec.params.get("strict_order", False))

    for line in ctx.lines:
        m = total.search(line.text)
        if m and int(m.group(1)) <= 1:
            return []  # 单页记录，本条只约束多页记录
        # 厂内预印表单多为“共X页第Y页”，与要求示例顺序相反但信息完整，默认视为合规
        if m and current.search(line.text):
            if not strict_order or strict.search(line.text):
                return []
    for line in ctx.lines:
        if loose.search(line.text):
            return [_emit(ctx, spec, line, f"页码“{line.text}”格式不规范，应为“第X页共Y页”", conf=0.6)]
    # “整页缺页码”需先识别归档单元（一份档案目录内含多份独立表单），默认不判定
    if not spec.params.get("flag_missing", False):
        return []
    return [
        make_finding(
            spec,
            doc_id=ctx.doc_id,
            page_no=ctx.page_no,
            message=f"多页记录（共 {ctx.page_count} 页）本页未见页码标注",
            confidence=0.5,
        )
    ]


# ---------------------------------------------------------------- A10/A11 数值

@rule("A10_decimal_precision")
def check_decimals(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    max_dec = int(spec.params.get("max_decimals", 3))
    required = spec.params.get("require_keywords") or []
    if required and not any(k in ctx.text for k in required):
        return []  # 本条只针对“技术文件未规定值”的称重类特性
    pattern = re.compile(rf"\d+\.(\d{{{max_dec + 1},}})")
    out: list[Finding] = []
    for line in ctx.lines:
        for m in pattern.finditer(line.text):
            if _is_identifier(line.text, m.start(), m.end()):
                continue  # 零件号 PN-2832B、文件号 DOC-131430-027 等不是实测值
            out.append(
                _emit(ctx, spec, line, f"“{m.group(0)}”小数位数为 {len(m.group(1))} 位，超过 {max_dec} 位", conf=0.7)
            )
    return out


_TOKEN_CHARS = re.compile(r"[A-Za-z0-9._\-/]")
_UNITS_OK = {"", "mm", "kg", "g", "m", "km", "mpa", "℃", "%", "hrc", "hbw", "hb",
             "s", "h", "min", "n", "kn", "t", "pa", "l"}


def _is_identifier(text: str, start: int, end: int) -> bool:
    """判断数字所在 token 是否为零件号/文件号一类标识，而非实测值。"""
    i = start
    while i > 0 and _TOKEN_CHARS.match(text[i - 1]):
        i -= 1
    j = end
    while j < len(text) and _TOKEN_CHARS.match(text[j]):
        j += 1
    token = text[i:j]
    if token.count(".") >= 2:
        return True
    head, tail = text[i:start], text[end:j]
    if re.search(r"[A-Za-z]", head):
        return True
    return tail.strip().lower() not in _UNITS_OK


@rule("A11_tolerance_format")
def check_tolerance(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    symmetric = re.compile(rf"\(\s*-\s*({NUM})\s*[~～]\s*\+?\s*({NUM})\s*\)")
    out: list[Finding] = []
    for line in ctx.lines:
        for m in symmetric.finditer(line.text):
            if abs(float(m.group(1)) - float(m.group(2))) < 1e-9:
                out.append(
                    _emit(ctx, spec, line, f"上下极限偏差数值一致，“{m.group(0)}”应写成 ±{m.group(1)}", conf=0.8)
                )
    return out


# ---------------------------------------------------------------- A12 特殊记载

@rule("A12_special_record_empty")
def check_special_record(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    anchors = [l for l in ctx.lines if "特殊记载" in l.text]
    if not anchors:
        return []
    anchor = anchors[0]
    if "无" in anchor.text.replace("特殊记载", ""):
        return []
    # 同一横带内（表格行）右侧是否有内容
    band_top, band_bottom = anchor.bbox[1] - anchor.bbox[3], anchor.y2 + anchor.bbox[3] * 3
    for line in ctx.lines:
        if line is anchor:
            continue
        if band_top <= line.cy <= band_bottom and line.cx > anchor.cx:
            return []
    return [_emit(ctx, spec, anchor, "“特殊记载”栏未见内容，无记录内容时须统一填“无”", conf=0.55)]


# ---------------------------------------------------------------- A13 复印确认章措辞

@rule("A13_copy_stamp_wording")
def check_copy_stamp_wording(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    required = spec.params.get("required_text", "此件与原件一致")
    variants = spec.params.get("known_variants", [])
    out: list[Finding] = []
    for line in ctx.lines:
        text = line.text.replace(" ", "")
        if "与原件" not in text:
            continue
        if required in text:
            continue
        hit = next((v for v in variants if v.replace(" ", "") in text), None)
        out.append(
            _emit(
                ctx,
                spec,
                line,
                f"确认章措辞为“{hit or line.text}”，管理要求为“{required}”",
                conf=0.9 if hit else 0.6,
            )
        )
    return out


# ---------------------------------------------------------------- A14 记录方式一致性

def _classify_value(text: str) -> str | None:
    t = text.replace(" ", "")
    if not t:
        return None
    if t in ("合格", "不合格", "符合", "合各"):
        return "结论"
    if re.fullmatch(rf"[Φφ]?{NUM}(\s*[～~]\s*[Φφ]?{NUM})?[a-zA-Z℃%/]*", t):
        return "实测值"
    return None


@rule("A14_result_consistency")
def check_result_consistency(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    headers = spec.params.get("column_headers", [])
    found = column_x_range(ctx.lines, headers, ctx.cells)
    if not found:
        return []
    x1, x2, header = found
    kinds: dict[str, list[TextLine]] = {"结论": [], "实测值": []}
    for line in ctx.lines:
        if line is header or line.cy <= header.cy:
            continue
        if not (x1 <= line.cx <= x2):
            continue
        kind = _classify_value(line.text)
        if kind:
            kinds[kind].append(line)
    if kinds["结论"] and kinds["实测值"]:
        sample = kinds["结论"][0]
        return [
            _emit(
                ctx,
                spec,
                sample,
                f"本页“{header.text}”栏同时出现结论式（{kinds['结论'][0].text}）与实测值式"
                f"（{kinds['实测值'][0].text}）记录，同一特性记录方式须一致",
                conf=0.6,
            )
        ]
    return []


# ---------------------------------------------------------------- 行级取值辅助

def _row_value(ctx: PageContext, anchor: TextLine, x1: int, x2: int, band_factor: float = 1.2) -> TextLine | None:
    """取与锚点同一行、落在指定列区间内的文本行。"""
    band = max(anchor.bbox[3], 20) * band_factor
    best, best_dy = None, None
    for line in ctx.lines:
        if line is anchor or not (x1 <= line.cx <= x2):
            continue
        dy = abs(line.cy - anchor.cy)
        if dy > band:
            continue
        if best_dy is None or dy < best_dy:
            best, best_dy = line, dy
    return best


def _decimals(text: str) -> int:
    """取文本中最长的小数位数。

    OCR 常把手写数字断开成「0.0 30」「43.0 2」，直接数位数会得到错误结果，
    因此先把数字之间的空格并掉。
    """
    joined = re.sub(r"(?<=\d)[ 　]+(?=\d)", "", text)
    return max((len(m.group(1)) for m in re.finditer(r"\d+\.(\d+)", joined)), default=0)


# ---------------------------------------------------------------- A16 多处测量

@rule("A16_multi_point_record")
def check_multi_point(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """要求测量多处时，5 处以内（含）写出实测值，5 处以上可写测量范围。"""
    threshold = int(spec.params.get("max_listed", 5))
    found = column_x_range(ctx.lines, spec.params.get("actual_headers", ["实际", "实测值"]), ctx.cells)
    if not found:
        return []
    x1, x2, header = found
    # 规定值里的「(N处)」「N-R5」「测量N点」三种写法
    pattern = re.compile(r"[（(](\d{1,2})\s*处[)）]|(?<![\d.])(\d{1,2})\s*-\s*[ΦR]|测量\s*(\d{1,2})\s*点")
    out: list[Finding] = []
    for line in ctx.lines:
        if line.cy <= header.cy or line.cx >= x1:
            continue
        m = pattern.search(line.text)
        if not m:
            continue
        count = int(next(g for g in m.groups() if g))
        if count > threshold or count < 2:
            continue
        actual = _row_value(ctx, line, x1, x2)
        if actual and re.search(r"[～~]", actual.text):
            out.append(
                _emit(
                    ctx, spec, actual,
                    f"该特性要求测量 {count} 处（不超过 {threshold} 处），应逐个写出实测值，"
                    f"当前记为范围值“{actual.text.strip()}”",
                    conf=0.6,
                )
            )
    return out


# ---------------------------------------------------------------- A17 修约位数

@rule("A17_rounding_digits")
def check_rounding(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """理论值与公差值有效数位不一致时，按较多有效数位进行数值修约。"""
    found_spec = column_x_range(ctx.lines, spec.params.get("spec_headers", ["规定", "规定值"]), ctx.cells)
    found_act = column_x_range(ctx.lines, spec.params.get("actual_headers", ["实际", "实测值"]), ctx.cells)
    if not found_spec or not found_act:
        return []
    sx1, sx2, spec_header = found_spec
    ax1, ax2, _ = found_act
    out: list[Finding] = []
    for line in ctx.lines:
        if line.cy <= spec_header.cy or not (sx1 <= line.cx <= sx2):
            continue
        required = _decimals(line.text)
        if required == 0:
            continue
        actual = _row_value(ctx, line, ax1, ax2)
        if actual is None:
            continue
        got = _decimals(actual.text)
        if got and got < required:
            out.append(
                _emit(
                    ctx, spec, actual,
                    f"规定值“{line.text.strip()[:20]}”为 {required} 位小数，"
                    f"实测值“{actual.text.strip()[:20]}”只有 {got} 位，应按较多有效数位修约",
                    conf=0.55,
                )
            )
    return out


# ---------------------------------------------------------------- A18 复核人栏

@rule("A18_reviewer_signature")
def check_reviewer_field(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """复印/扫描件的“复核人”栏须签全名或加盖红色检验印章。

    只在带“与原件一致”确认章的页面上判定——那是本条的适用前提。
    """
    if "与原件" not in ctx.text.replace(" ", ""):
        return []
    labels = spec.params.get("labels", ["复核人", "负责人", "审核人"])
    anchors = [l for l in ctx.lines if any(k in l.text.replace(" ", "") for k in labels)]
    if not anchors:
        # 确认章内的“负责人/复核人”是小号红字，OCR 常读不出来。
        # 缺少 OCR 证据不等于该栏不存在，默认不据此判定，避免整批误报。
        if not spec.params.get("flag_missing_field", False):
            return []
        return [
            make_finding(
                spec, doc_id=ctx.doc_id, page_no=ctx.page_no,
                message="复印/扫描件未见“复核人”栏，应签全名或加盖红色检验印章",
                confidence=0.35,
            )
        ]
    anchor = anchors[0]
    # 标签右侧同一行：有文字或有红章即视为已签署
    band = max(anchor.bbox[3], 20) * 1.4
    for line in ctx.lines:
        if line is anchor:
            continue
        if abs(line.cy - anchor.cy) <= band and line.cx > anchor.cx:
            stripped = re.sub(r"[\s:：]", "", line.text)
            if stripped and not any(k in stripped for k in labels):
                return []
    x, y, w, h = anchor.bbox
    for s in ctx.seals:
        sx, sy = s.center()
        if sx > x and abs(sy - (y + h / 2)) <= band:
            return []
    return [_emit(ctx, spec, anchor, "“复核人”栏未见签名或红色检验印章", conf=0.5)]


# ---------------------------------------------------------------- A15 特殊特性单位

@rule("A15_missing_unit_special")
def check_special_unit(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    keywords = spec.params.get("keywords", [])
    anchors = [l for l in ctx.lines if any(k in l.text for k in keywords)]
    if not anchors:
        return []
    found = column_x_range(ctx.lines, ["实际", "实 际", "实测值"], ctx.cells)
    if not found:
        return []
    x1, x2, header = found

    # 只查与关键词同一行（同一特性）的实测值。整页扫描会把尺寸实测值一并误判。
    out: list[Finding] = []
    for anchor in anchors:
        if anchor.cy <= header.cy:
            continue
        band = max(anchor.bbox[3], 20) * float(spec.params.get("row_band_factor", 1.2))
        for line in ctx.lines:
            if not (x1 <= line.cx <= x2) or abs(line.cy - anchor.cy) > band:
                continue
            t = line.text.replace(" ", "")
            if re.fullmatch(NUM, t):
                out.append(
                    _emit(
                        ctx, spec, line,
                        f"特性“{anchor.text[:16]}”的实测值“{t}”未注明计量单位", conf=0.6,
                    )
                )
    return out
