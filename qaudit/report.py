"""审核报告：疑点清单 JSON + 带原图红框定位的自包含 HTML。

人工终审是本系统的落点，因此每条疑点必须给出“原图证据 + 条款出处”，
让质量部一眼能判 真/假 阳性，而不是只给一个结论。
"""
from __future__ import annotations

import base64
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .context import PageContext
from .findings import Finding, LEVELS, sort_findings
from .segment import DocUnit
from .web.render import LEVEL_CN, esc as _esc, hbars, kpi, level_pill, panel, table

# 设计系统与本地审核服务共用一份 app.css。报告必须自包含（图片是内联 base64、
# 打开时不联网），所以这里把样式表读进来内联，而不是 <link> 引用。
_CSS_PATH = Path(__file__).resolve().parent / "web" / "static" / "app.css"

LEVEL_COLOR = {"CRITICAL": "#b3001b", "HIGH": "#d9480f", "MEDIUM": "#b8860b", "LOW": "#5a6570"}


def _encode(img: np.ndarray, max_w: int = 760) -> str:
    h, w = img.shape[:2]
    if w > max_w:
        scale = max_w / w
        img = cv2.resize(img, (max_w, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 72])
    return base64.b64encode(buf).decode() if ok else ""


def crop_evidence(ctx: PageContext, finding: Finding) -> str:
    """裁出疑点区域并画红框；无 bbox 时给整页缩略图。"""
    img = ctx.image
    if finding.bbox is None:
        return _encode(img, max_w=520)
    x, y, w, h = finding.bbox
    pad_x, pad_y = max(60, w // 2), max(40, h)
    x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
    x2, y2 = min(img.shape[1], x + w + pad_x), min(img.shape[0], y + h + pad_y)
    crop = img[y1:y2, x1:x2].copy()
    cv2.rectangle(crop, (x - x1, y - y1), (x - x1 + w, y - y1 + h), (0, 0, 255), 3)
    return _encode(crop)


class ReportBuilder:
    def __init__(
        self,
        out_dir: str | Path,
        rulebook_meta: dict,
        *,
        max_evidence: int = 600,
        systemic_min_count: int = 5,
        systemic_min_ratio: float = 0.6,
        batch_min_count: int = 30,
        max_thumbs: int = 4000,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.meta = rulebook_meta
        self.max_evidence = max_evidence
        self.systemic_min_count = systemic_min_count
        self.systemic_min_ratio = systemic_min_ratio
        self.batch_min_count = batch_min_count
        self.max_thumbs = max_thumbs
        self._systemic: list[dict] = []
        self._entries: list[tuple[Finding, str]] = []
        self._display: list[tuple[Finding, str]] = []
        self._page_rows: list[dict] = []
        self._units: list[dict] = []
        self._thumbs: dict[tuple[str, int], str] = {}
        self._pages = 0
        self._docs: set[str] = set()
        self._elapsed = 0.0

    def add_page(self, ctx: PageContext, findings: list[Finding], elapsed: float) -> None:
        self._pages += 1
        self._docs.add(ctx.doc_id)
        self._elapsed += elapsed
        self._page_rows.append(
            {
                "doc_id": ctx.doc_id,
                "page_no": ctx.page_no,
                "form_type": ctx.form_type,
                "findings": len(findings),
                "text_lines": len(ctx.lines),
                "seals": len(ctx.seals),
                "source": ctx.source,
            }
        )
        # 单据级疑点在整包处理完之后才产生，那时已拿不到原图，故预留一张缩略图
        if len(self._thumbs) < self.max_thumbs:
            self._thumbs[(ctx.doc_id, ctx.page_no)] = _encode(ctx.image, max_w=360)

        for f in findings:
            # 全量批处理时限制内嵌截图数量，避免报告文件膨胀到无法打开
            crop = crop_evidence(ctx, f) if len(self._entries) < self.max_evidence else ""
            self._entries.append((f, crop))

    def add_units(self, doc_id: str, units: list[DocUnit], findings: list[Finding]) -> None:
        """登记一个归档包的单据切分结果与单据级疑点。"""
        for u in units:
            self._units.append(
                {
                    "unit_id": u.unit_id,
                    "doc_id": u.doc_id,
                    "form_type": u.form_type,
                    "start_page": u.start_page,
                    "end_page": u.end_page,
                    "page_count": u.page_count,
                    "declared_total": u.declared_total,
                    "keys": u.keys,
                    # 页集合允许不连续（交错装订会把同一份单据的页隔开），
                    # 只存首末页号会把别的单据的页也算进来
                    "pages": list(u.pages),
                }
            )
        for f in findings:
            self._entries.append((f, self._thumbs.get((f.doc_id, f.page_no), "")))

    # ------------------------------------------------------------ 输出

    def _collapse_systemic(self) -> list[tuple[Finding, str]]:
        """同一档案内某条规则命中面过大时折叠为一条档案级疑点。

        “整批档案都缺设计图版次”是一个系统性问题，不是 24 个独立问题；
        逐页罗列会淹没真正零散的缺陷。
        """
        pages_per_doc = Counter(r["doc_id"] for r in self._page_rows)
        buckets: dict[tuple[str, str], list[tuple[Finding, str]]] = {}
        for item in self._entries:
            buckets.setdefault((item[0].doc_id, item[0].rule_id), []).append(item)

        out: list[tuple[Finding, str]] = []
        self._systemic = []
        for (doc_id, rule_id), items in buckets.items():
            total = pages_per_doc.get(doc_id, 1)
            hit_pages = sorted({f.page_no for f, _ in items})
            ratio = len(hit_pages) / max(1, total)
            if len(items) < self.systemic_min_count or ratio < self.systemic_min_ratio:
                out.extend(items)
                continue
            head, crop = items[0]
            preview = "、".join(str(p) for p in hit_pages[:8])
            more = f" 等 {len(hit_pages)} 页" if len(hit_pages) > 8 else ""
            merged = replace(
                head,
                message=(
                    f"【系统性】本档案 {len(hit_pages)}/{total} 页命中同一问题："
                    f"{head.message}（页码：{preview}{more}）"
                ),
            )
            out.append((merged, crop))
            self._systemic.append(
                {"doc_id": doc_id, "rule_id": rule_id, "hit_pages": hit_pages, "total_pages": total}
            )
        return self._collapse_batch(out)

    def _collapse_batch(self, entries: list[tuple[Finding, str]]) -> list[tuple[Finding, str]]:
        """跨档案折叠：一条规则在整批里散命中过多时，合成一条批次级疑点。

        全量审核 1453 页时，“622 页缺设计图版次”是一个批次级结论，
        逐页列出只会把几百条零散的真问题埋掉。
        """
        by_rule: dict[str, list[tuple[Finding, str]]] = {}
        for item in entries:
            by_rule.setdefault(item[0].rule_id, []).append(item)

        out: list[tuple[Finding, str]] = []
        for rule_id, items in by_rule.items():
            singles = [it for it in items if "【系统性】" not in it[0].message]
            folded = [it for it in items if "【系统性】" in it[0].message]
            out.extend(folded)
            if len(singles) < self.batch_min_count:
                out.extend(singles)
                continue
            per_doc = Counter(f.doc_id for f, _ in singles)
            head, crop = singles[0]
            top = "；".join(f"{d} {n} 页" for d, n in per_doc.most_common(5))
            more = f"，另有 {len(per_doc) - 5} 份档案" if len(per_doc) > 5 else ""
            merged = replace(
                head,
                message=(
                    f"【批次级】全批 {len(singles)} 页命中同一问题：{head.message}"
                    f"（分布：{top}{more}）"
                ),
            )
            out.append((merged, crop))
            self._systemic.append(
                {
                    "scope": "batch",
                    "rule_id": rule_id,
                    "hit_count": len(singles),
                    "docs": dict(per_doc.most_common()),
                }
            )
        return out

    def write(self) -> dict[str, Path]:
        # JSON 保留逐页完整清单（评测与对接以它为准），折叠只用于人看的 HTML
        findings = sort_findings([f for f, _ in self._entries])
        self._display = self._collapse_systemic()
        json_path = self.out_dir / "findings.json"
        json_path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now().strftime("%Y.%m.%d %H:%M:%S"),
                    "rulebook": self.meta,
                    "stats": self.stats(),
                    "systemic": self._systemic,
                    "units": self._units,
                    "pages": self._page_rows,
                    "findings": [f.to_dict() for f in findings],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        html_path = self.out_dir / "report.html"
        html_path.write_text(self._render_html(), encoding="utf-8")
        return {"json": json_path, "html": html_path}

    def stats(self) -> dict:
        by_level = Counter(f.level for f, _ in self._entries)
        by_rule = Counter(f.rule_id for f, _ in self._entries)
        return {
            "pages": self._pages,
            "docs": len(self._docs),
            "findings": len(self._entries),
            "seconds": round(self._elapsed, 1),
            "sec_per_page": round(self._elapsed / max(1, self._pages), 2),
            "units": len(self._units),
            "by_level": {lv: by_level.get(lv, 0) for lv in LEVELS},
            "by_rule": dict(by_rule.most_common()),
        }

    def _render_html(self) -> str:
        st = self.stats()
        display = self._display or self._entries
        ordered = sort_findings([f for f, _ in display])
        crops = {id(f): c for f, c in display}

        cards = []
        for i, f in enumerate(ordered, 1):
            crop = crops.get(id(f), "")
            evidence = (f'<div class="evidence"><img loading="lazy" alt="证据图"'
                        f' src="data:image/jpeg;base64,{crop}"></div>') if crop else \
                '<div class="small dim2">本条未内嵌截图（超出内嵌上限），请在审核服务里查看原图</div>'
            key = f"{f.doc_id}|{f.page_no}|{f.rule_id}"
            cards.append(f"""
<div class="rcard" data-level="{f.level}" data-key="{_esc(key)}" data-doc="{_esc(f.doc_id)}"
     data-page="{f.page_no}" data-rule="{f.rule_id}">
  <div class="rcard-hd">
    <span class="seq mono">{i:04d}</span>
    {level_pill(f.level)}
    <span class="mono dim">{_esc(f.rule_id)}</span>
    <b>{_esc(f.title)}</b>
    <span class="mono dim2 small">{_esc(f.doc_id)} · 第 {f.page_no} 页</span>
    <span class="grow"></span>
    <span class="tag">置信度 {f.confidence:.0%}</span>
  </div>
  <div class="rcard-msg">{_esc(f.message)}</div>
  <div class="rcard-clause">依据：{_esc(f.clause)}</div>
  {evidence}
  <div class="judge"></div>
</div>""")

        peak = max(st["by_level"].values()) or 1
        level_bars = "".join(
            f'<div class="hbar"><span class="lbl">{LEVEL_CN[lv]}（{lv}）</span>'
            f'<div class="track"><i style="width:{st["by_level"][lv] * 100 / peak:.1f}%;'
            f'background:var(--{lv.lower()})"></i></div>'
            f'<span class="val">{st["by_level"][lv]}</span></div>' for lv in LEVELS)

        rule_panel = panel(
            hbars([(rid, n, "") for rid, n in st["by_rule"].items()], top=25),
            title=f"按规则统计（{len(st['by_rule'])} 条规则命中）",
            note="命中特别集中的规则通常是系统性问题，而不是几百个独立缺陷。")

        form_counter = Counter(r["form_type"] for r in self._page_rows)
        form_rows = [f'<tr><td>{_esc(ft)}</td><td class="num">{n}</td>'
                     f'<td class="num">{n * 100 / max(1, st["pages"]):.1f}%</td></tr>'
                     for ft, n in form_counter.most_common()]
        page_rows = [
            f'<tr><td class="mono small">{_esc(r["doc_id"])}</td>'
            f'<td class="num">{r["page_no"]}</td><td>{_esc(r["form_type"])}</td>'
            f'<td class="num">{r["text_lines"]}</td><td class="num">{r["seals"]}</td>'
            f'<td class="num">{r["findings"]}</td></tr>' for r in self._page_rows]

        form_panel = panel(
            table(["表单类型", ("页数", True), ("占比", True)], form_rows)
            + '<details style="margin-top:12px"><summary>逐页明细</summary>'
            + '<div style="margin-top:8px">'
            + table(["档案", ("页", True), "表单类型", ("文本行", True), ("印记", True),
                     ("疑点", True)], page_rows, scroll=True)
            + "</div></details>",
            title="表单类型识别",
            note="表单类型决定规则适用范围——供方合格证是外单位自制文件，"
                 "不适用公司内部表单的填写格式要求。")

        kpis = "".join([
            kpi("档案", st["docs"], unit="份"),
            kpi("页数", st["pages"], foot=f"{st['sec_per_page']}s/页"),
            kpi("疑点", st["findings"],
                foot=f"{st['findings'] / max(1, st['pages']):.2f} 条/页", tone="info"),
            kpi("严重 + 较重", st["by_level"]["CRITICAL"] + st["by_level"]["HIGH"],
                foot="优先处置", tone="critical"),
            kpi("耗时", st["seconds"], unit="s", foot=f"单据 {st['units']} 份"),
        ])

        body = "".join(cards) if cards else panel(
            '<div class="empty-state"><div class="big">未检出疑点</div></div>')

        return f"""<!doctype html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RecordLint 审核疑点清单</title>
<script>
/* 首屏就把主题定下来，避免刷新时先白后黑地闪一下 */
try {{ var t = localStorage.getItem('qaudit_theme');
      if (t) document.documentElement.setAttribute('data-theme', t); }} catch (e) {{}}
</script>
<style>
{_CSS_PATH.read_text(encoding='utf-8')}
{REPORT_CSS}
</style>
</head>
<body>
<header class="rep-hd">
  <div class="brand"><span class="mark">QA</span>
    <span class="name">RecordLint · 审核疑点清单<small>QUALITY DOSSIER PRE-AUDIT REPORT</small></span></div>
  <span class="grow"></span>
  <button class="icon-btn" id="theme-btn" type="button" title="切换深色 / 浅色主题">☾</button>
</header>
<div class="page">
  <div class="notice">
    规则库 <b>{_esc(self.meta.get('name', '-'))} v{_esc(self.meta.get('version', '-'))}</b>
    ｜ 生成时间 {datetime.now().strftime('%Y.%m.%d %H:%M')}
    ｜ <b>本清单供人工终审，不作为最终判定结论。</b>
    手写件 OCR 与印章视觉判定不可能达到 100%，最终判定由贵单位质量职能按其程序签署。
  </div>
  <div class="grid g5" style="margin-bottom:14px">{kpis}</div>
  <div class="grid g2">
    {panel(f'<div class="hbars">{level_bars}</div>', title="疑点级别分布")}
    {rule_panel}
  </div>
  {form_panel}
  <div class="page-hd" style="margin:22px 0 12px">
    <h2>疑点清单</h2><span class="sub">共 {len(ordered)} 条可读条目</span>
  </div>
  {body}
</div>
""" + REVIEW_UI + "</body></html>"


# 报告专属样式。设计系统的 token 与通用组件来自内联的 app.css，这里只补报告独有的部分。
# 用普通字符串保存，避免与 f-string 模板的花括号冲突。
REPORT_CSS = """
.rep-hd{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:12px;
  padding:12px 20px;background:linear-gradient(180deg,var(--bg-2),var(--bg-1));
  border-bottom:1px solid var(--line)}
.rep-hd::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;
  background:linear-gradient(90deg,transparent,var(--accent-line) 20%,var(--accent-line) 80%,transparent)}
.rep-hd .brand .name{font-size:15px}

.rcard{position:relative;background:var(--bg-1);border:1px solid var(--line);
  border-left:3px solid var(--low);border-radius:var(--r);padding:12px 14px;margin-bottom:12px}
.rcard[data-level=CRITICAL]{border-left-color:var(--critical)}
.rcard[data-level=HIGH]{border-left-color:var(--high)}
.rcard[data-level=MEDIUM]{border-left-color:var(--medium)}
.rcard[data-level=LOW]{border-left-color:var(--low)}
.rcard-hd{display:flex;gap:9px;align-items:center;flex-wrap:wrap;font-size:13px}
.rcard-hd .seq{color:var(--fg-3);font-size:11px}
.rcard-msg{margin:9px 0 3px;font-size:13.5px}
.rcard-clause{color:var(--fg-3);font-size:12px;margin-bottom:9px}
.rcard .evidence{padding:6px;max-height:520px}
.rcard.judged{opacity:.55}
.rcard.hide{display:none}
.rcard.cursor{outline:2px solid var(--accent);outline-offset:2px}

.judge{margin-top:10px;display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.judge .who{color:var(--fg-3);font-size:11.5px;margin-left:4px}
.judge button.on[data-v=true]{background:color-mix(in srgb,var(--ok) 18%,transparent);
  border-color:var(--ok);color:var(--ok);font-weight:600}
.judge button.on[data-v=false]{background:color-mix(in srgb,var(--danger) 18%,transparent);
  border-color:var(--danger);color:var(--danger);font-weight:600}
.judge button.on[data-v=unsure]{background:color-mix(in srgb,var(--medium) 18%,transparent);
  border-color:var(--medium);color:var(--medium);font-weight:600}

.judgebar{position:sticky;bottom:0;z-index:40;display:flex;gap:10px;align-items:center;
  flex-wrap:wrap;padding:9px 20px;background:var(--bg-2);border-top:1px solid var(--line);
  box-shadow:0 -6px 18px rgba(0,0,0,.25)}
.judgebar .stat{font-family:var(--mono);font-size:12px;color:var(--fg-2)}

details>summary{cursor:pointer;color:var(--accent);font-size:12.5px;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▸ ";color:var(--accent)}
details[open]>summary::before{content:"▾ "}

@media print{.judgebar,.rep-hd .icon-btn{display:none!important}
  .rcard{break-inside:avoid;border-color:#bbb}}
"""


# 人工复核回填界面：判定结果存 localStorage，导出 JSON 后用 `qaudit gold` 合入金标准集。
# 与审核服务的复核工作台保持同一套键位（1/2/3 判定、J/K 移动），避免两处习惯打架。
REVIEW_UI = """
<div class="judgebar">
  <span class="stat" id="jstat">未判定 0</span>
  <div class="seg" id="jfilter">
    <button data-filter="all" class="on">全部</button>
    <button data-filter="todo">仅未判定</button>
    <button data-filter="severe">仅严重 / 较重</button>
  </div>
  <span class="grow"></span>
  <span class="small dim2">
    <span class="kbd">J</span><span class="kbd">K</span> 移动
    <span class="kbd">1</span><span class="kbd">2</span><span class="kbd">3</span> 判真/判假/存疑
    <span class="kbd">T</span> 主题</span>
  <button class="btn btn-primary btn-sm" id="jexport">导出标注 JSON</button>
  <button class="btn btn-sm btn-ghost" id="jclear">清空本机判定</button>
</div>
<script>
(function () {
  /* ---- 主题 ---- */
  var TKEY = 'qaudit_theme';
  function setTheme(name) {
    document.documentElement.setAttribute('data-theme', name);
    try { localStorage.setItem(TKEY, name); } catch (e) {}
    var b = document.getElementById('theme-btn');
    if (b) b.textContent = name === 'dark' ? '☾' : '☀';
  }
  function curTheme() { return document.documentElement.getAttribute('data-theme') || 'dark'; }
  var tbtn = document.getElementById('theme-btn');
  if (tbtn) tbtn.onclick = function () { setTheme(curTheme() === 'dark' ? 'light' : 'dark'); };
  setTheme(curTheme());

  /* ---- 判定 ---- */
  /* 判定存浏览器本地。报告是自包含文件，可能被拷到任何一台机器上看，
     没有服务端可写；正式判定请在审核服务里做，那里能追溯到人。 */
  var KEY = 'qaudit_review::' + document.title + '::' + (location.pathname || '');
  var store = {};
  try { store = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { store = {}; }
  var cards = Array.prototype.slice.call(document.querySelectorAll('.rcard'));
  var VERDICTS = [['true', '判真'], ['false', '判假'], ['unsure', '存疑']];
  var cursor = null;

  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(store)); } catch (e) {}
    paint();
  }
  function paint() {
    var todo = 0, t = 0, f = 0, u = 0;
    cards.forEach(function (card) {
      var rec = store[card.dataset.key];
      card.classList.toggle('judged', !!rec);
      if (!rec) todo++;
      else if (rec.verdict === 'true') t++;
      else if (rec.verdict === 'false') f++;
      else u++;
      card.querySelectorAll('.judge button').forEach(function (b) {
        b.classList.toggle('on', !!rec && rec.verdict === b.dataset.v);
      });
    });
    document.getElementById('jstat').textContent =
      '判真 ' + t + ' ｜ 判假 ' + f + ' ｜ 存疑 ' + u + ' ｜ 未判定 ' + todo;
  }
  function mark(card, verdict) {
    var k = card.dataset.key, cur = store[k];
    if (cur && cur.verdict === verdict) { delete store[k]; }
    else {
      store[k] = { verdict: verdict, doc_id: card.dataset.doc,
                   page_no: parseInt(card.dataset.page, 10), rule_id: card.dataset.rule };
    }
    save();
  }
  cards.forEach(function (card) {
    var bar = card.querySelector('.judge');
    VERDICTS.forEach(function (pair, i) {
      var b = document.createElement('button');
      b.className = 'btn btn-sm';
      b.innerHTML = pair[1] + ' <span class="kbd">' + (i + 1) + '</span>';
      b.dataset.v = pair[0];
      b.onclick = function () { focus(card); mark(card, pair[0]); };
      bar.appendChild(b);
    });
    card.addEventListener('click', function () { focus(card); });
  });

  function focus(card) {
    cards.forEach(function (c) { c.classList.toggle('cursor', c === card); });
    cursor = card;
  }
  function visible() {
    return cards.filter(function (c) { return !c.classList.contains('hide'); });
  }
  function move(step) {
    var list = visible();
    if (!list.length) return;
    var i = list.indexOf(cursor);
    var next = list[Math.min(list.length - 1, Math.max(0, i + step))];
    focus(next);
    next.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  document.querySelectorAll('#jfilter [data-filter]').forEach(function (btn) {
    btn.onclick = function () {
      var mode = btn.dataset.filter;
      document.querySelectorAll('#jfilter button').forEach(function (b) {
        b.classList.toggle('on', b === btn);
      });
      cards.forEach(function (card) {
        var judged = !!store[card.dataset.key];
        var severe = card.dataset.level === 'CRITICAL' || card.dataset.level === 'HIGH';
        var show = mode === 'all' || (mode === 'todo' && !judged) || (mode === 'severe' && severe);
        card.classList.toggle('hide', !show);
      });
    };
  });

  document.addEventListener('keydown', function (e) {
    var el = e.target;
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    var k = e.key.toLowerCase();
    if (k === 'j' || k === 'arrowdown') { e.preventDefault(); move(1); }
    else if (k === 'k' || k === 'arrowup') { e.preventDefault(); move(-1); }
    else if (k === 't') { e.preventDefault(); setTheme(curTheme() === 'dark' ? 'light' : 'dark'); }
    else if (['1', '2', '3'].indexOf(k) >= 0) {
      if (!cursor) { move(1); return; }
      e.preventDefault();
      mark(cursor, VERDICTS[parseInt(k, 10) - 1][0]);
      move(1);
    }
  });

  document.getElementById('jexport').onclick = function () {
    var items = Object.keys(store).map(function (k) { return store[k]; });
    var payload = {
      reviewer: '', reviewed_at: new Date().toISOString().slice(0, 10).replace(/-/g, '.'),
      source_report: document.title, adjudications: items
    };
    var blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'review_' + payload.reviewed_at.replace(/\\./g, '') + '.json';
    a.click();
  };
  document.getElementById('jclear').onclick = function () {
    if (confirm('清空本机已保存的判定？该操作不可撤销。')) { store = {}; save(); }
  };
  paint();
})();
</script>
"""
