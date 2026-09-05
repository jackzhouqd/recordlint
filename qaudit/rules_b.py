"""B 类规则：视觉判定（红章合规 / 划改 / 空栏未划“/”）。

全部基于经典 CV，无需标注数据与训练。手写内容 OCR 常识别失败，因此凡涉及
“是否为空”的判定，一律追加墨迹率兜底，宁可漏报不可误报。
"""
from __future__ import annotations

import re
from typing import Callable

import cv2
import numpy as np

from .context import PageContext
from .findings import Finding, RuleSpec, RuleBook, make_finding
from .layout import Cell, column_x_range
from .seal import Seal, iou, overlap_ratio, red_pixel_ratio

RuleFn = Callable[[PageContext, RuleSpec], list[Finding]]
_REGISTRY: dict[str, RuleFn] = {}

DATE_RE = re.compile(r"(19|20)\d{2}\s*[.\-/年]\s*\d{1,2}\s*[.\-/月]\s*\d{1,2}")


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


def _emit(ctx: PageContext, spec: RuleSpec, bbox, message: str, conf: float, evidence: str = ""):
    return make_finding(
        spec,
        doc_id=ctx.doc_id,
        page_no=ctx.page_no,
        message=message,
        bbox=tuple(int(v) for v in bbox) if bbox else None,
        evidence=evidence,
        confidence=conf,
    )


def _stamps(ctx: PageContext) -> list[Seal]:
    """排除红线框（复印确认章外框），只保留实体印章。"""
    return [s for s in ctx.seals if not s.is_frame]


def _is_copy_page(ctx: PageContext) -> bool:
    """判断本页是否为复印/扫描件。

    复印件上原始红色印章会被复印成黑色，红章检测天然看不见；按 A13，
    复印件只需加盖红色“此件与原件一致”确认章即合规，因此不能据红章缺失判缺章。
    """
    return any("与原件" in l.text.replace(" ", "") for l in ctx.lines)


def _is_red_text(ctx: PageContext, bbox: tuple[int, int, int, int], thresh: float = 0.02) -> bool:
    """判断一段文字本身是否为红色印文。"""
    x, y, w, h = bbox
    roi = ctx.image[max(0, y) : y + h, max(0, x) : x + w]
    return roi.size > 0 and red_pixel_ratio(roi) > thresh


# ---------------------------------------------------------------- B01 歪斜

@rule("B01_seal_tilt")
def check_seal_tilt(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    max_tilt = float(spec.params.get("max_tilt_deg", 8.0))
    out: list[Finding] = []
    for s in _stamps(ctx):
        if not s.angle_reliable:
            continue
        if abs(s.angle) > max_tilt:
            out.append(
                _emit(
                    ctx,
                    spec,
                    s.bbox,
                    f"印章倾角约 {abs(s.angle):.1f}°，超过 {max_tilt:.0f}° 限值，未水平正向加盖",
                    conf=0.6,
                )
            )
    return out


# ---------------------------------------------------------------- B02 重叠

@rule("B02_seal_overlap")
def check_seal_overlap(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    # 判据用“小者被覆盖的比例”而非 IoU：真正的重叠盖章是一枚压在另一枚上，
    # 而相邻印章彼此只是接边，覆盖率很低。同一枚章断连出的圈内碎片
    # （会被外接框 100% 包含）已在 seal.merge_nested 中并回，不会走到这里。
    min_cover = float(spec.params.get("min_coverage", 0.5))
    min_area_ratio = float(spec.params.get("min_seal_area_ratio", 0.0006))
    page_area = ctx.width * ctx.height
    stamps = [s for s in _stamps(ctx) if s.area >= page_area * min_area_ratio]
    out: list[Finding] = []
    for i in range(len(stamps)):
        for j in range(i + 1, len(stamps)):
            v = max(
                overlap_ratio(stamps[i].bbox, stamps[j].bbox),
                overlap_ratio(stamps[j].bbox, stamps[i].bbox),
            )
            if v >= min_cover:
                x = min(stamps[i].bbox[0], stamps[j].bbox[0])
                y = min(stamps[i].bbox[1], stamps[j].bbox[1])
                x2 = max(stamps[i].bbox[0] + stamps[i].bbox[2], stamps[j].bbox[0] + stamps[j].bbox[2])
                y2 = max(stamps[i].bbox[1] + stamps[i].bbox[3], stamps[j].bbox[1] + stamps[j].bbox[3])
                out.append(
                    _emit(ctx, spec, (x, y, x2 - x, y2 - y), f"两枚印章重叠（较小一枚被覆盖 {v:.0%}）", conf=0.7)
                )
    return out


# ---------------------------------------------------------------- B03 压日期

@rule("B03_seal_on_date")
def check_seal_on_date(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    min_ratio = float(spec.params.get("min_overlap_ratio", 0.25))
    out: list[Finding] = []
    for line in ctx.lines:
        if not DATE_RE.search(line.text):
            continue
        if _is_red_text(ctx, line.bbox):
            continue  # 日期本身是红色印文（如复印确认章内的日期），非“印章压日期”
        for s in _stamps(ctx):
            r = overlap_ratio(line.bbox, s.bbox)
            if r >= min_ratio:
                out.append(
                    _emit(
                        ctx, spec, s.bbox,
                        f"印章与日期“{line.text}”重合（覆盖 {r:.0%}）", conf=0.65, evidence=line.text,
                    )
                )
    return out


# ---------------------------------------------------------------- B04 空栏未划 /

def _ink_ratio(image: np.ndarray, rect: tuple[int, int, int, int]) -> float:
    """单元格内墨迹占比。手写/浅色复印常被 OCR 漏识，用它兜底避免误判为空栏。

    阈值取单元格自身背景中位数的相对值，适应复印件整体偏灰的情况。
    """
    x, y, w, h = rect
    pad = 4
    roi = image[y + pad : y + h - pad, x + pad : x + w - pad]
    if roi.size == 0:
        return 1.0
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi
    bg = float(np.median(gray))
    dark = np.count_nonzero(gray < max(60.0, bg - 45))
    return dark / float(gray.size)


def _column_header(cells: list[Cell], target: Cell) -> Cell | None:
    """向上找同列最近的有内容单元格，作为该列表头。"""
    tx, ty, tw, _ = target.rect
    best, best_gap = None, None
    for c in cells:
        cx, cy, cw, ch = c.rect
        if cy + ch > ty or not c.content:
            continue
        overlap = min(tx + tw, cx + cw) - max(tx, cx)
        if overlap <= 0 or overlap / min(tw, cw) < 0.6:
            continue
        gap = ty - (cy + ch)
        if best_gap is None or gap < best_gap:
            best, best_gap = c, gap
    return best


def _row_label(cells: list[Cell], target: Cell) -> Cell | None:
    """向左找同行最近的有内容单元格。质量证明单表头是“标签:值”的横向布局，
    仅靠列表头会漏掉“组合件号”“装配处”这类下游填写栏。"""
    tx, ty, _, th = target.rect
    best, best_gap = None, None
    for c in cells:
        cx, cy, cw, ch = c.rect
        if cx + cw > tx or not c.content:
            continue
        overlap = min(ty + th, cy + ch) - max(ty, cy)
        if overlap <= 0 or overlap / min(th, ch) < 0.5:
            continue
        gap = tx - (cx + cw)
        if best_gap is None or gap < best_gap:
            best, best_gap = c, gap
    return best


def _labels(cells: list[Cell], target: Cell) -> tuple[str, str]:
    header = _column_header(cells, target)
    left = _row_label(cells, target)
    return (header.content[:14] if header else "", left.content[:14] if left else "")


@rule("B04_blank_cell_no_slash")
def check_blank_cell(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    whitelist = spec.params.get("downstream_whitelist", [])
    lo = float(spec.params.get("min_cell_area_ratio", 0.0008))
    hi = float(spec.params.get("max_cell_area_ratio", 0.06))
    group_min = int(spec.params.get("group_as_column", 3))
    page_area = ctx.width * ctx.height

    blanks: list[Cell] = []
    for cell in ctx.cells:
        ratio = cell.area / page_area
        if not (lo <= ratio <= hi) or not cell.is_blank:
            continue
        if _ink_ratio(ctx.image, cell.rect) > 0.004:  # 有墨迹，OCR 未识别，不判空
            continue
        if any(overlap_ratio(cell.rect, s.bbox) > 0.15 for s in ctx.seals):  # 已盖章
            continue
        blanks.append(cell)

    # 按所属列聚合：整列空白只报一条，避免同一问题刷屏
    fields = [] if spec.params.get("check_all_fields") else (spec.params.get("field_whitelist") or [])
    groups: dict[str, list[Cell]] = {}
    for cell in blanks:
        header, left = _labels(ctx.cells, cell)
        if any(w in header or w in left for w in whitelist):  # 下游单位填写栏允许留空
            continue
        name = header or left
        # 只判定已登记的表单栏目：表格数据区的切分噪声不当作缺项
        if fields and not any(f in name for f in fields):
            continue
        groups.setdefault(name, []).append(cell)

    out: list[Finding] = []
    for name, cells in groups.items():
        label = f"“{name}”栏" if name else "该栏目"
        if len(cells) >= group_min:
            x1 = min(c.rect[0] for c in cells)
            y1 = min(c.rect[1] for c in cells)
            x2 = max(c.rect[0] + c.rect[2] for c in cells)
            y2 = max(c.rect[1] + c.rect[3] for c in cells)
            out.append(
                _emit(
                    ctx, spec, (x1, y1, x2 - x1, y2 - y1),
                    f"{label}连续 {len(cells)} 格空白且未见“/”明示", conf=0.5, evidence=name,
                )
            )
            continue
        for c in cells:
            out.append(
                _emit(ctx, spec, c.rect, f"{label}空白且未见“/”明示", conf=0.4, evidence=name)
            )
    return out


# ---------------------------------------------------------------- B05 复印章位置

@rule("B05_copy_stamp_position")
def check_copy_stamp_position(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    rx = float(spec.params.get("right_bottom_x", 0.5))
    ry = float(spec.params.get("right_bottom_y", 0.6))
    out: list[Finding] = []
    for line in ctx.lines:
        if "与原件" not in line.text.replace(" ", ""):
            continue
        fx, fy = line.cx / ctx.width, line.cy / ctx.height
        if fx >= rx and fy >= ry:
            continue
        pos = f"页面{'左' if fx < 0.5 else '右'}{'上' if fy < 0.5 else '下'}部（x={fx:.0%}, y={fy:.0%}）"
        out.append(
            _emit(
                ctx, spec, line.bbox,
                f"“与原件一致”确认章位于{pos}，要求加盖在右下角空白处", conf=0.75, evidence=line.text,
            )
        )
    return out


# ---------------------------------------------------------------- B06 划改处数

def _strikethroughs(ctx: PageContext) -> list[tuple[int, int, int, int]]:
    """短横线穿过文字行中部 → 疑似单杠划改。

    划改线必须“又长又细”：长度既要覆盖文字行大部分，也要显著长于字高，
    否则数字“5”“2”等字形内部的横笔会被误当成划改。
    """
    hits: list[tuple[int, int, int, int]] = []
    for seg in ctx.h_segments:
        sx, sy, sw, sh = seg
        if sw < 40 or sw / max(1, sh) < 8:
            continue
        for line in ctx.lines:
            lx, ly, lw, lh = line.bbox
            if lh < 8 or lw < 30:
                continue
            if sw < lh * 2.0:  # 短于两倍字高的横笔不是划改
                continue
            mid_lo, mid_hi = ly + lh * 0.2, ly + lh * 0.8
            if not (mid_lo <= sy + sh / 2 <= mid_hi):
                continue
            inter = min(sx + sw, lx + lw) - max(sx, lx)
            if inter > 0 and inter / lw >= 0.5 and sw <= lw * 1.3:
                hits.append(seg)
                break
    return hits


@rule("B06_strikethrough_limit")
def check_strikethrough(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    max_per_page = int(spec.params.get("max_per_page", 3))
    hits = _strikethroughs(ctx)
    if len(hits) <= max_per_page:
        return []
    xs = [h[0] for h in hits]
    ys = [h[1] for h in hits]
    x2 = max(h[0] + h[2] for h in hits)
    y2 = max(h[1] + h[3] for h in hits)
    return [
        _emit(
            ctx, spec, (min(xs), min(ys), x2 - min(xs), max(4, y2 - min(ys))),
            f"本页检出疑似划改 {len(hits)} 处，超过每页 {max_per_page} 处限值", conf=0.4,
        )
    ]


# ---------------------------------------------------------------- B07 检验员栏无章

@rule("B07_inspector_seal_missing")
def check_inspector_seal(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    headers = spec.params.get("column_headers", [])
    if spec.params.get("skip_copy_pages", True) and _is_copy_page(ctx):
        return []
    found = column_x_range(ctx.lines, headers, ctx.cells)
    if not found or ctx.is_blank_page:
        return []
    x1, x2, header = found
    # “检验员”有两种版式：表格列（章在下方逐行盖）与表头栏位（章盖在标签右侧同一行）。
    # 两处都要看，只看列会把表头版式整批误判为缺章。
    hh = max(header.bbox[3], 20)
    row_lo, row_hi = header.cy - hh * 1.2, header.cy + hh * 1.6
    for s in _stamps(ctx):
        sx, sy = s.center()
        if x1 <= sx <= x2 and sy > header.cy:
            return []
        if sx > header.cx and row_lo <= sy <= row_hi:
            return []

    # 兜底：连通域切分失效时仍可能有章，直接看红色像素占比，宁可漏报不误报
    min_red = float(spec.params.get("region_red_ratio", 0.002))
    regions = [
        ctx.image[int(header.cy) : ctx.height, max(0, int(x1)) : int(x2)],          # 列区域
        ctx.image[max(0, int(row_lo)) : int(row_hi), int(header.cx) : ctx.width],   # 标签右侧同行
    ]
    if any(r.size and red_pixel_ratio(r) > min_red for r in regions):
        return []
    # 该列下方确有待签署的行才判定
    rows = [l for l in ctx.lines if l.cy > header.cy + header.bbox[3] and l.cx < x1]
    if len(rows) < 2:
        return []
    bbox = (x1, header.cy, max(10, x2 - x1), int(ctx.height - header.cy) // 3)
    return [
        _emit(
            ctx, spec, bbox,
            f"“{header.text}”栏下方未检出检验印章，持印章人员应履行盖章手续", conf=0.5,
        )
    ]


# ---------------------------------------------------------------- B09 页码位置

@rule("B09_page_number_position")
def check_page_number_position(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """记录页码要记在每页脚页的中间位置。"""
    pattern = re.compile(r"第\s*\d+\s*页|共\s*\d+\s*页")
    bottom = float(spec.params.get("footer_from", 0.88))
    lo = float(spec.params.get("center_from", 0.33))
    hi = float(spec.params.get("center_to", 0.67))
    for line in ctx.lines:
        if not pattern.search(line.text):
            continue
        fx, fy = line.cx / ctx.width, line.cy / ctx.height
        if fy >= bottom and lo <= fx <= hi:
            return []
        where = ("页脚" if fy >= bottom else "页面中部" if fy >= 0.4 else "页首")
        side = "左侧" if fx < lo else "右侧" if fx > hi else "中间"
        return [
            _emit(
                ctx, spec, line.bbox,
                f"页码“{line.text.strip()}”位于{where}{side}（x={fx:.0%}, y={fy:.0%}），"
                f"要求记在页脚中间位置",
                conf=0.6, evidence=line.text,
            )
        ]
    return []


# ---------------------------------------------------------------- B10 笔迹颜色

@rule("B10_pen_color")
def check_pen_color(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """一律用黑色中性笔填写（需复写的记录可用圆珠笔）。

    只查蓝色笔迹：红色是印章与确认章的正常颜色，不能一并判定。
    """
    if ctx.is_blank_page:
        return []
    min_ratio = float(spec.params.get("min_blue_ratio", 0.0006))
    hsv = cv2.cvtColor(ctx.image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([95, 70, 50]), np.array([135, 255, 255]))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    ratio = float(np.count_nonzero(mask)) / max(1, mask.size)
    if ratio < min_ratio:
        return []
    ys, xs = np.nonzero(mask)
    x, y = int(xs.min()), int(ys.min())
    bbox = (x, y, int(xs.max()) - x, int(ys.max()) - y)
    return [
        _emit(
            ctx, spec, bbox,
            f"检出蓝色笔迹（占比 {ratio:.3%}），管理要求一律用黑色中性笔填写",
            conf=0.5,
        )
    ]


# ---------------------------------------------------------------- B08 整页无红章

@rule("B08_no_red_seal_on_page")
def check_page_red(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    if ctx.is_blank_page:
        return []
    if spec.params.get("skip_copy_pages", True) and _is_copy_page(ctx):
        return []
    min_ratio = float(spec.params.get("min_red_pixel_ratio", 0.00002))
    ratio = red_pixel_ratio(ctx.image)
    if ratio >= min_ratio:
        return []
    return [
        _emit(
            ctx, spec, None,
            f"整页未检出红色印章（红色像素占比 {ratio:.5%}），黑白输出记录须手工加盖质检工红章",
            conf=0.7,
        )
    ]

# ------------------------------------------------- B12/B13 印章状态（模型驱动）

# 与其余 B 类规则不同，这两条依赖已上线的印章状态分类器；但**判定权仍在规则里**——
# 模型只回答“这枚章是什么状态”，哪些状态算违规、置信度门槛多少，全写在 rules.yaml。
# 验收时要能解释“为什么判它不合格”，不做黑盒。
#
# 拆成两条而不是一条，是因为两者的交付节奏本就不同：
#   B13 倒盖 —— 合成即真实（旋转 180°），不需要人工标注就能训到可用；
#   B12 清晰/完整 —— 真实缺陷与合成退化差得远，必须有真实标注才能验收。
# 合成一条会逼着质量部要么全开要么全关。
#
# 模型未训练或未上线时 ctx.seal_model 为 None，两条都直接跳过：
# 少一类判定可以接受，审核因此报错不可接受。

_STATE_CACHE: tuple | None = None   # (页标识, 预测结果)。流水线逐页处理，一条足够


def _seal_states(ctx: PageContext, pad: float) -> list[tuple[Seal, str, float]]:
    """对本页每枚实体印章跑一次分类。B12 与 B13 共用结果，不重复推理。"""
    global _STATE_CACHE
    key = (ctx.doc_id, ctx.page_no, id(ctx.image), pad)
    if _STATE_CACHE and _STATE_CACHE[0] == key:
        return _STATE_CACHE[1]

    from .train.features import predict_one
    from .train.imaging import crop_seal

    out: list[tuple[Seal, str, float]] = []
    for s in _stamps(ctx):
        crop = crop_seal(ctx.image, s.bbox, pad)
        if crop is None:
            continue
        label, conf = predict_one(ctx.seal_model, crop)
        out.append((s, label, conf))
    _STATE_CACHE = (key, out)
    return out


def _seal_state_findings(ctx: PageContext, spec: RuleSpec, default_labels: list[str],
                         wording: str) -> list[Finding]:
    if ctx.seal_model is None or ctx.is_blank_page:
        return []
    from .train.features import LABEL_CN

    reject = set(spec.params.get("reject_labels") or default_labels)
    min_conf = float(spec.params.get("min_confidence", 0.75))
    pad = float(spec.params.get("pad_ratio", 0.15))
    cap = int(spec.params.get("max_per_page", 12))

    out: list[Finding] = []
    for s, label, conf in _seal_states(ctx, pad):
        if len(out) >= cap:
            break
        if label not in reject or conf < min_conf:
            continue
        out.append(_emit(
            ctx, spec, s.bbox,
            f"印章疑似{LABEL_CN.get(label, label)}（模型标签 {label}，置信度 {conf:.0%}），{wording}",
            conf=conf, evidence=label,
        ))
    return out


@rule("B12_seal_clarity")
def check_seal_clarity(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """印章清晰、完整（缺角 / 模糊 / 漏墨断线）。"""
    return _seal_state_findings(
        ctx, spec, ["chipped", "blurred", "faint"],
        "通用做法要求印章须清晰、完整")


@rule("B13_seal_upside_down")
def check_seal_upside_down(ctx: PageContext, spec: RuleSpec) -> list[Finding]:
    """印章倒盖 180°。合成即真实，是模型规则里最先可用的一条。"""
    return _seal_state_findings(
        ctx, spec, ["upside_down"],
        "通用做法要求印章水平正向加盖，不允许倒盖")
