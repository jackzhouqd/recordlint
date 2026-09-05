"""合成质量记录表单，供演示、测试与印章模型的样本合成使用。

生成的是**虚构**的表单页面（A4 竖版 PNG）：表格线、预印栏目、填写值、红色印章，
并按需注入违规（日期未写满 8 位、范围值用短横、单位大小写、空栏未划「/」、印章歪斜、
印章压日期、划改超限、复制件确认章措辞与位置）。仓内 `samples/synthetic/` 全部由本脚本产出，
**任何真实客户记录都不得进入本仓**（见 CLAUDE.md）。

用法：
  python tools/synth_forms.py --out samples/synthetic --seed 7
  python tools/synth_forms.py --out out/tmp --docs 3 --pages 6

字体：优先系统中文字体（SimHei / 微软雅黑 / 思源黑体），找不到则退回 Pillow 默认字体
（此时中文会显示为方框，OCR 判定不可用，仅用于版面/印章测试）。
"""
from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1654, 2339  # A4 @ 200dpi
import os

_WIN = os.environ.get("WINDIR", r"C:\Windows")
FONT_CANDIDATES = [
    os.path.join(_WIN, "Fonts", "simhei.ttf"), os.path.join(_WIN, "Fonts", "msyh.ttc"),
    os.path.join(_WIN, "Fonts", "simsun.ttc"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]
FORMS = ("质量证明单", "检验记录", "流水卡片", "呈报单", "供方合格证")


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


class Page:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.img = Image.new("RGB", (W, H), (252, 251, 247))
        self.draw = ImageDraw.Draw(self.img)
        self.f_title = _font(52)
        self.f_cell = _font(30)
        self.f_hand = _font(34)
        self.violations: list[str] = []

    # ------------------------------------------------------------ 版面
    def title(self, text: str, sub: str = "") -> None:
        tw = self.draw.textlength(text, font=self.f_title)
        self.draw.text(((W - tw) / 2, 110), text, fill=(20, 20, 20), font=self.f_title)
        if sub:
            self.draw.text((120, 200), sub, fill=(40, 40, 40), font=self.f_cell)

    def grid(self, x0: int, y0: int, cols: list[int], rows: int, rh: int) -> list[list[tuple[int, int, int, int]]]:
        """画表格线，返回单元格 (x, y, w, h) 矩阵。"""
        xs = [x0]
        for c in cols:
            xs.append(xs[-1] + c)
        cells = []
        for r in range(rows):
            y = y0 + r * rh
            row = []
            for i in range(len(cols)):
                row.append((xs[i], y, cols[i], rh))
            cells.append(row)
        for x in xs:
            self.draw.line([(x, y0), (x, y0 + rows * rh)], fill=(30, 30, 30), width=3)
        for r in range(rows + 1):
            self.draw.line([(x0, y0 + r * rh), (xs[-1], y0 + r * rh)], fill=(30, 30, 30), width=3)
        return cells

    def text(self, cell: tuple[int, int, int, int], s: str, hand: bool = False, color=(15, 15, 15)) -> None:
        x, y, w, h = cell
        f = self.f_hand if hand else self.f_cell
        self.draw.text((x + 14, y + (h - 36) // 2), s, fill=color, font=f)

    def strike(self, cell: tuple[int, int, int, int], n: int = 1) -> None:
        """划改：穿过文字中部的细横线。"""
        x, y, w, h = cell
        for i in range(n):
            yy = y + h // 2 + (i - n // 2) * 6
            self.draw.line([(x + 16, yy), (x + w - 16, yy)], fill=(20, 20, 20), width=3)

    # ------------------------------------------------------------ 印章
    def seal(self, cx: int, cy: int, label: str, tilt: float = 0.0, r: int = 95,
             upside_down: bool = False, faded: float = 0.0) -> None:
        size = 2 * r + 20
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        red = (205, 30, 30, int(255 * (1 - faded)))
        d.ellipse([10, 10, size - 10, size - 10], outline=red, width=7)
        d.ellipse([28, 28, size - 28, size - 28], outline=red, width=3)
        f = _font(26)
        tw = d.textlength(label, font=f)
        d.text(((size - tw) / 2, size / 2 - 16), label, fill=red, font=f)
        star = _font(30)
        d.text((size / 2 - 12, size / 2 - 58), "★", fill=red, font=star)
        angle = 180 if upside_down else tilt
        layer = layer.rotate(angle, resample=Image.BICUBIC, expand=False)
        self.img.paste(layer, (cx - size // 2, cy - size // 2), layer)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.img.save(path, "PNG")


# ---------------------------------------------------------------- 表单模板
def _date(rng: random.Random, bad: bool) -> str:
    y, m, d = 2026, rng.randint(1, 12), rng.randint(1, 28)
    return f"{y}.{m}.{d}" if bad else f"{y}.{m:02d}.{d:02d}"


def gen_cert_sheet(pg: Page, page_no: int, total: int, part: str, batch: str, inject: set[str]) -> None:
    rng = pg.rng
    pg.title("质量证明单", f"零件号 {part}    批次顺序号 {batch}    型号 GX-2")
    cells = pg.grid(120, 280, [300, 320, 320, 240, 234], 9, 92)
    heads = ["检验项目", "规定", "实际", "检验员", "日期"]
    for i, hd in enumerate(heads):
        pg.text(cells[0][i], hd)
    items = [("外径", "Φ40～Φ41", "Φ40.5"), ("长度", "120±0.1", "120.05"),
             ("硬度", "HRC 38～42", "HRC 40"), ("重量", "1.20～1.30kg", "1.25kg"),
             ("表面粗糙度", "Ra 1.6", "Ra 1.5"), ("平行度", "≤0.02", "0.01"),
             ("装配处", "", ""), ("特殊记载", "", "")]
    for r, (name, spec, actual) in enumerate(items, start=1):
        pg.text(cells[r][0], name)
        pg.text(cells[r][1], spec)
        if name == "装配处":
            if "F01" in inject:
                pg.text(cells[r][2], "/", hand=True); pg.violations.append("F01 装配处划斜杠")
            continue
        if name == "特殊记载":
            if "A12" not in inject:
                pg.text(cells[r][2], "无", hand=True)
            else:
                pg.violations.append("A12 特殊记载栏空白")
            continue
        val = actual
        if "A02" in inject and "～" in spec and r == 1:
            pg.text(cells[r][1], spec.replace("～", "-")); pg.violations.append("A02 范围值用短横")
        if "A06" in inject and "kg" in actual:
            val = actual.replace("kg", "Kg"); pg.violations.append("A06 单位大小写")
        pg.text(cells[r][2], val, hand=True)
        pg.text(cells[r][3], "李工" if r % 2 else "王工", hand=True)
        pg.text(cells[r][4], _date(rng, bad=("A01" in inject and r == 2)), hand=True)
        if "A01" in inject and r == 2:
            pg.violations.append("A01 日期未写满 8 位")
    if "B06" in inject:
        pg.strike(cells[3][2], 1); pg.strike(cells[4][2], 1); pg.strike(cells[5][2], 1); pg.strike(cells[6][2], 1)
        pg.violations.append("B06 单页划改 4 处")
    # 检验章
    tilt = rng.uniform(18, 30) if "B01" in inject else rng.uniform(-3, 3)
    if "B01" in inject:
        pg.violations.append("B01 印章歪斜")
    sx, sy = 1180, 1220
    if "B03" in inject:
        sx, sy = cells[2][4][0] + 110, cells[2][4][1] + 40
        pg.violations.append("B03 印章压日期")
    pg.seal(sx, sy, "检验专用章", tilt=tilt, upside_down=("B13" in inject), faded=(0.55 if "B12" in inject else 0.0))
    if "B13" in inject:
        pg.violations.append("B13 印章倒盖")
    if "B12" in inject:
        pg.violations.append("B12 印章不清晰")
    # 页码与复制件确认章
    pg.draw.text((W // 2 - 80, H - 140), f"第{page_no}页共{total}页", fill=(20, 20, 20), font=pg.f_cell)
    if "A13" in inject:
        pg.draw.text((W - 560, H - 300), "复印件与原件一致", fill=(205, 30, 30), font=pg.f_cell)
        pg.violations.append("A13 确认章措辞不符")
    elif "B05" in inject:
        pg.draw.text((140, H - 300), "此件与原件一致", fill=(205, 30, 30), font=pg.f_cell)
        pg.violations.append("B05 确认章位置不在右下角")
    elif page_no % 2 == 0:
        pg.draw.text((W - 560, H - 300), "此件与原件一致", fill=(205, 30, 30), font=pg.f_cell)
        pg.draw.text((W - 560, H - 250), "复核人：张工", fill=(15, 15, 15), font=pg.f_hand)


def gen_inspection_record(pg: Page, page_no: int, total: int, part: str, batch: str, inject: set[str]) -> None:
    rng = pg.rng
    pg.title("成品检验记录", f"零件号 {part}    批次 {batch}")
    cells = pg.grid(120, 280, [240, 300, 300, 300, 274], 10, 92)
    for i, hd in enumerate(["序号", "零件号", "重量", "检验员", "结论"]):
        pg.text(cells[0][i], hd)
    for r in range(1, 10):
        pg.text(cells[r][0], str(r))
        pg.text(cells[r][1], f"{part}-{r:02d}", hand=True)
        wt = f"{rng.uniform(1.2, 1.3):.3f}kg"
        if "A10" in inject and r == 3:
            wt = f"{rng.uniform(1.2, 1.3):.5f}kg"; pg.violations.append("A10 小数位超限")
        pg.text(cells[r][2], wt, hand=True)
        pg.text(cells[r][3], "王工", hand=True)
        if "B04" in inject and r == 5:
            pg.violations.append("B04 结论栏空白未划斜杠")
        else:
            pg.text(cells[r][4], "合格", hand=True)
    pg.seal(1300, 1380, "检验专用章", tilt=rng.uniform(-3, 3))
    pg.draw.text((W // 2 - 80, H - 140), f"第{page_no}页共{total}页", fill=(20, 20, 20), font=pg.f_cell)


def gen_supplier_cert(pg: Page, page_no: int, total: int, part: str, batch: str, inject: set[str]) -> None:
    """供方随带证明文件：格式由供方自定，故意用 2026-03-05 这种日期——不应被判为格式违规。"""
    pg.title("产品质量证明书", "Certificate of Quality")
    cells = pg.grid(120, 300, [400, 1014], 6, 100)
    rows = [("产品名称", "六角螺栓 M12×40"), ("材质", "35CrMo"), ("数量", "500 件"),
            ("检验结论", "符合 GB/T 5782"), ("签发日期", "2026-03-05"), ("签发人", "供方质检部")]
    for r, (k, v) in enumerate(rows):
        pg.text(cells[r][0], k)
        pg.text(cells[r][1], v, hand=True)
    pg.seal(1250, 1150, "供方质检章", tilt=pg.rng.uniform(-4, 4))


GENERATORS = {"质量证明单": gen_cert_sheet, "检验记录": gen_inspection_record, "供方合格证": gen_supplier_cert}
INJECTABLE = ["A01", "A02", "A06", "A10", "A12", "A13", "B01", "B03", "B04", "B05", "B06", "B12", "B13", "F01"]


def build(out: Path, docs: int, pages: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    manifest: list[str] = []
    for d in range(1, docs + 1):
        doc = out / f"batch-{d:04d}"
        part = f"PN-{rng.randint(1000, 9999)}{rng.choice('ABC')}"
        batch = f"26-{rng.randint(1, 12):02d}-{rng.randint(1, 30):02d}-{rng.randint(1, 9)}"
        forms = ["供方合格证"] + [rng.choice(["质量证明单", "质量证明单", "检验记录"]) for _ in range(pages - 1)]
        for p, form in enumerate(forms, start=1):
            pg = Page(rng)
            inject = set(rng.sample(INJECTABLE, k=rng.randint(0, 3))) if form != "供方合格证" else set()
            GENERATORS[form](pg, p, len(forms), part, batch, inject)
            path = doc / f"page_{p:03d}.png"
            pg.save(path)
            manifest.append(f"{path.relative_to(out).as_posix()}\t{form}\t{'; '.join(pg.violations) or '无注入'}")
    (out / "manifest.tsv").write_text("路径\t表单\t注入的违规\n" + "\n".join(manifest) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="合成质量记录表单（虚构数据）")
    ap.add_argument("--out", default="samples/synthetic")
    ap.add_argument("--docs", type=int, default=2)
    ap.add_argument("--pages", type=int, default=6, help="每份档案页数（含 1 页供方证明文件）")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    m = build(Path(args.out), args.docs, args.pages, args.seed)
    print(f"生成 {len(m)} 页 → {args.out}（manifest.tsv 记录每页注入的违规）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
