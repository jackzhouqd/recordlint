"""金标准集与评测。

计分口径：判真算召回，判假算误报，存疑与未判定都不计分——人工往往只判了
一页里的部分疑点，把未判定当误报会低估准确率。金标准集在日常复核中自然长大，
不需要专门组织标注。
"""
from __future__ import annotations

from ..render import esc, kpi, layout, page_head, panel, table

# README「六、建议的验收指标」里的口径，界面上直接对照，避免人工换算
TARGETS = {"A": (0.02, 0.20), "B": (0.10, 0.35)}


def render(store, ctx: dict, run_id: str, result: dict | None = None) -> bytes:
    runs = store.runs()
    options = "".join(
        f'<option value="{esc(r["run_id"])}"{" selected" if r["run_id"] == run_id else ""}>'
        f'{esc(r["run_id"])}（疑点 {r["findings"]}，已判定 {r["judged"]}）</option>' for r in runs)

    form = panel(
        f'<form method="post" action="/gold" class="wrap-row">'
        f'<select name="run_id" style="min-width:280px">{options}</select>'
        f'<button class="btn btn-primary" name="action" value="merge">并入金标准并评测</button>'
        f'<button class="btn" name="action" value="eval">仅评测</button></form>',
        title="金标准集与评测",
        note='计分口径：<b>判真算召回，判假算误报，存疑与未判定都不计分</b>。'
             '人工往往只判了一页里的部分疑点，把未判定当误报会低估准确率。'
             '金标准集在日常复核中自然长大，不需要专门组织标注。')

    body = page_head("金标准与评测", "验收的唯一判据", back=("/system", "系统")) + form
    if result:
        body += _result(result)
        body += _targets()
    return layout("金标准与评测", body, ctx["user"], active="system", theme=ctx["theme"])


def _result(r: dict) -> str:
    recall, precision = r["recall"], r["precision"]
    miss = 1 - recall
    kpis = "".join([
        kpi("召回率", f"{recall:.1%}", foot=f"漏检率 {miss:.1%}",
            tone="ok" if miss <= 0.02 else "high"),
        kpi("准确率", f"{precision:.1%}", foot=f"误报 {r['fp']} 条",
            tone="ok" if precision >= 0.8 else "high"),
        kpi("命中 TP", r["tp"], foot=f"金标准正样本 {r['positives']}"),
        kpi("漏检 FN", r["fn"], foot="真正危险的是漏检", tone="critical" if r["fn"] else "ok"),
        kpi("未判定", r["unjudged"], foot="不计分", tone="info"),
    ])
    rows = [
        f'<tr><td>金标准正样本（判真累积）</td><td class="num">{r["positives"]}</td></tr>',
        f'<tr><td>金标准负样本（判假累积）</td><td class="num">{r["negatives"]}</td></tr>',
        f'<tr><td>命中 TP</td><td class="num">{r["tp"]}</td></tr>',
        f'<tr><td>误报 FP</td><td class="num">{r["fp"]}</td></tr>',
        f'<tr><td>漏检 FN</td><td class="num">{r["fn"]}</td></tr>',
        f'<tr><td>本批预测中未经人工判定（不计分）</td><td class="num">{r["unjudged"]}</td></tr>',
    ]
    return (f'<div class="grid g5" style="margin-bottom:14px">{kpis}</div>'
            + panel(table(["项目", ("数量", True)], rows), title="评测结果", flush=True))


def _targets() -> str:
    rows = [
        f'<tr><td>{cls} 类</td><td class="num">≤ {miss:.0%}</td><td class="num">≤ {fp:.0%}</td>'
        f'<td class="small dim">在双方封存的金标准集上测量</td></tr>'
        for cls, (miss, fp) in TARGETS.items()
    ]
    rows.append('<tr><td>C 类</td><td class="num">—</td><td class="num">—</td>'
                '<td class="small dim">需外部真值，本期不纳入</td></tr>')
    return panel(table(["类别", ("漏检率", True), ("误报率", True), "说明"], rows),
                 title="建议验收指标", flush=True,
                 note="召回优先——质量部真正怕的是漏检，误报只是多看一眼。"
                      "金标准集由质量部指定 300～500 页逐条人工标注，双方封存，作为唯一判据；"
                      "规则调优只允许改代码与 rules.yaml，不允许改金标准。")
