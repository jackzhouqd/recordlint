"""档案视图：档案 → 单据 → 页。

一个归档包里通常含多份独立表单。切出单据这一级后，「多页记录须填页码」
「缺页」这类判定才有正确的判定单元；界面上也才能回答「这份档案整体什么情况」。
"""
from __future__ import annotations

import json
import urllib.parse

from ..render import (esc, layout, level_pill, page_head, panel, run_picker, table,
                      verdict_tag, LEVELS)


def render(store, ctx: dict, params: dict) -> bytes:
    run_id = ctx["run_id"]
    doc_id = params.get("doc", "")
    unit_id = params.get("unit", "")
    if unit_id:
        return _one_unit(store, ctx, unit_id)
    if doc_id:
        return _one_doc(store, ctx, doc_id)

    summary = store.summary(run_id)
    counts = {d["doc_id"]: d["n"] for d in summary["docs"]}
    pages = _page_counts(store, run_id)
    units = _unit_counts(store, run_id)
    levels = _level_matrix(store, run_id)

    rows = []
    for doc in sorted(set(list(counts) + list(pages))):
        rq = urllib.parse.quote(run_id)
        dq = urllib.parse.quote(doc)
        n_pages = pages.get(doc, 0)
        n = counts.get(doc, 0)
        lv = levels.get(doc, {})
        density = f"{n / n_pages:.2f}" if n_pages else "—"
        cells = "".join(f'<td class="num">{lv.get(level, 0) or "—"}</td>' for level in LEVELS)
        rows.append(
            f'<tr><td><a href="/archive?run={rq}&doc={dq}" class="mono">{esc(doc)}</a></td>'
            f'<td class="num">{n_pages}</td><td class="num">{units.get(doc, 0)}</td>'
            f'<td class="num">{n}</td><td class="num">{density}</td>{cells}'
            f'<td><a class="btn btn-sm" href="/review?run={rq}&doc={dq}">复核</a></td></tr>')

    body = page_head("档案", f'批次 <span class="mono">{esc(run_id)}</span> · {len(rows)} 份档案')
    body += panel(table(
        ["档案", ("页", True), ("单据", True), ("疑点", True), ("条/页", True),
         ("严重", True), ("较重", True), ("一般", True), ("提示", True), "操作"],
        rows, empty="本批次没有档案数据"), flush=True)
    return layout("档案", body, ctx["user"], active="archive", theme=ctx["theme"],
                  nav_right=run_picker(ctx["runs"], run_id, "/archive"))


def _one_doc(store, ctx: dict, doc_id: str) -> bytes:
    run_id = ctx["run_id"]
    rq, dq = urllib.parse.quote(run_id), urllib.parse.quote(doc_id)
    units = [u for u in store.units(run_id) if u["doc_id"] == doc_id]
    unit_hits = _unit_finding_counts(store, run_id, units)
    urows = []
    for u in units:
        uq = urllib.parse.quote(u["unit_id"])
        n = unit_hits.get(u["unit_id"], 0)
        # 声明页数与实际页数不符是最严重的档案完整性问题，表格里直接标出来
        declared = u["declared_total"]
        if declared is None:
            dec_cell = '<td class="num dim2">—</td>'
        elif declared != u["page_count"]:
            dec_cell = (f'<td class="num" style="color:var(--critical)">'
                        f'{declared} ⚠</td>')
        else:
            dec_cell = f'<td class="num">{declared}</td>'
        urows.append(
            f'<tr><td><a class="mono" href="/archive?run={rq}&unit={uq}">'
            f'{esc(u["unit_id"])}</a></td>'
            f'<td>{esc(u["form_type"] or "—")}</td>'
            f'<td class="mono">p{u["start_page"]}–{u["end_page"]}</td>'
            f'<td class="num">{u["page_count"]}</td>{dec_cell}'
            f'<td class="num">{n or "—"}</td>'
            f'<td class="small dim">{esc(_keys(u["keys"]))}</td></tr>')

    prows = [
        f'<tr><td class="num mono">{p["page_no"]}</td><td>{esc(p["form_type"] or "—")}</td>'
        f'<td class="num">{p["text_lines"] or 0}</td><td class="num">{p["seals"] or 0}</td>'
        f'<td class="num">{p["findings"] or 0}</td>'
        f'<td><a class="btn btn-sm" href="/review?run={rq}&doc={dq}">查看疑点</a></td></tr>'
        for p in _pages(store, run_id, doc_id)]

    hist = [
        f'<tr><td class="mono">{esc(h["run_id"])}</td><td class="small dim">{esc(h["started_at"])}</td>'
        f'<td class="mono small">{esc(h["rules_version"])}</td><td class="num">{h["n"]}</td></tr>'
        for h in store.history(doc_id)]

    body = page_head(f"档案 {doc_id}", f'批次 <span class="mono">{esc(run_id)}</span>',
                     f'<a class="btn btn-primary" href="/review?run={rq}&doc={dq}">复核本档案</a>',
                     back=(f"/archive?run={rq}", "全部档案"))
    body += panel(table(["单据号", "表单类型", "页范围", ("页数", True), ("声明总页", True),
                         ("疑点", True), "关键字段"],
                        urows, empty="未切出单据"), title=f"单据（{len(units)} 份）", flush=True,
                  note="点单据号进入单据视图。「声明总页」标红表示与实际页数不符——"
                       "这是缺页的直接证据。单据的页集合允许不连续："
                       "实测存在同一份供方证明单被其他零件的证明单隔开装订的情况。")
    body += panel(table([("页", True), "表单类型", ("文本行", True), ("印章", True), ("疑点", True), ""],
                        prows, empty="无页面记录", scroll=True), title="页面", flush=True)
    body += panel(table(["批次", "时间", "规则版本", ("疑点", True)], hist, empty="仅本批次"),
                  title="历次审核", flush=True,
                  note="同一份档案在不同规则版本下的结论差异，是评估规则调整效果的直接证据。")
    return layout(f"档案 {doc_id}", body, ctx["user"], active="archive", theme=ctx["theme"],
                  nav_right=run_picker(ctx["runs"], run_id, "/archive"))


def _one_unit(store, ctx: dict, unit_id: str) -> bytes:
    """单据视图——四层定位里「单据」这一级的落地页。

    从这里往下能点到页，从页能点到具体疑点（带 sel 直接选中），
    形成 档案 → 单据 → 页 → 页内坐标 的完整下钻链路。
    """
    run_id = ctx["run_id"]
    rq = urllib.parse.quote(run_id)
    unit = next((u for u in store.units(run_id) if u["unit_id"] == unit_id), None)
    if unit is None:
        from ..render import empty_state
        body = page_head("单据") + panel(empty_state("单据不存在", "可能已被新批次覆盖。"))
        return layout("单据", body, ctx["user"], active="archive", theme=ctx["theme"])

    doc_id = unit["doc_id"]
    dq = urllib.parse.quote(doc_id)
    pages = _pages_in(store, run_id, doc_id, unit)
    findings = _findings_in(store, run_id, doc_id, [p["page_no"] for p in pages])

    declared = unit["declared_total"]
    gap = ""
    if declared is not None and declared != unit["page_count"]:
        gap = (f'<div class="notice error">声明共 {declared} 页，实际 {unit["page_count"]} 页'
               f'——差 {abs(declared - unit["page_count"])} 页。'
               f'注意：只有单据首页页码为 1 时「共 N 页」才可信，'
               f'供方证明单的连续编号不作为缺页依据。</div>')

    meta = panel(
        f'<div class="fingerprint">'
        f'<div><div class="fp-k">表单类型</div><div class="fp-v">{esc(unit["form_type"] or "—")}</div></div>'
        f'<div><div class="fp-k">页范围</div><div class="fp-v">p{unit["start_page"]}–{unit["end_page"]}</div></div>'
        f'<div><div class="fp-k">实际页数</div><div class="fp-v">{unit["page_count"]}</div></div>'
        f'<div><div class="fp-k">声明总页</div><div class="fp-v">'
        f'{declared if declared is not None else "未声明"}</div></div>'
        f'<div><div class="fp-k">疑点</div><div class="fp-v">{len(findings)}</div></div>'
        f'</div>'
        f'<div class="small dim" style="margin-top:10px">关键字段：{esc(_keys(unit["keys"])) or "—"}</div>',
        title="单据信息")

    prows = [
        f'<tr><td class="num mono">{p["page_no"]}</td><td>{esc(p["form_type"] or "—")}</td>'
        f'<td class="num">{p["text_lines"] or 0}</td><td class="num">{p["seals"] or 0}</td>'
        f'<td class="num">{p["findings"] or 0}</td></tr>' for p in pages]

    frows = []
    for f in findings:
        sel = urllib.parse.quote(f["finding_key"])
        frows.append(
            f'<tr><td>{level_pill(f["level"])}</td>'
            f'<td class="mono small">{esc(f["rule_id"])}</td>'
            f'<td>{esc(f["title"])}</td>'
            f'<td class="num mono">{f["page_no"]}</td>'
            f'<td>{verdict_tag(f["verdict"])}</td>'
            f'<td><a class="btn btn-sm" href="/review?run={rq}&doc={dq}&sel={sel}">复核</a></td></tr>')

    body = page_head(f"单据 {unit_id}", f'档案 {esc(doc_id)} · 批次 {esc(run_id)}',
                     f'<a class="btn btn-primary" href="/review?run={rq}&doc={dq}">复核本档案</a>',
                     back=(f"/archive?run={rq}&doc={dq}", f"档案 {doc_id}"))
    body += gap + meta
    body += panel(table([("页", True), "表单类型", ("文本行", True), ("印章", True), ("疑点", True)],
                        prows, empty="无页面记录"),
                  title=f"页（{len(pages)} 页）", flush=True)
    body += panel(table(["级别", "规则号", "判定内容", ("页", True), "人工判定", ""],
                        frows, empty="本单据未检出疑点"),
                  title=f"本单据的疑点（{len(findings)} 条）", flush=True,
                  note="点「复核」直接跳到复核工作台并选中该条。")
    return layout(f"单据 {unit_id}", body, ctx["user"], active="archive", theme=ctx["theme"],
                  nav_right=run_picker(ctx["runs"], run_id, "/archive"))


def unit_page_nos(unit: dict) -> list[int]:
    """单据的实际页号集合。

    **不能用 start~end 区间代替**：交错装订时同一份单据的页会被别的单据隔开
    （p13 是 3102A 的第 1 页，p14 是 3103A，p15 又回到 3102A），
    按区间取会把邻居的页算进来。老批次没有这一列时才退回区间。
    """
    raw = unit.get("pages") or ""
    if raw:
        try:
            nums = json.loads(raw)
            if isinstance(nums, list) and nums:
                return [int(n) for n in nums]
        except (ValueError, TypeError):
            pass
    return list(range(int(unit["start_page"]), int(unit["end_page"]) + 1))


def _pages_in(store, run_id: str, doc_id: str, unit: dict) -> list[dict]:
    nums = unit_page_nos(unit)
    if not nums:
        return []
    marks = ",".join("?" * len(nums))
    return [dict(r) for r in store.conn.execute(
        f"SELECT * FROM page WHERE run_id=? AND doc_id=? AND page_no IN ({marks})"
        f" ORDER BY page_no", [run_id, doc_id, *nums])]


def _findings_in(store, run_id: str, doc_id: str, page_nos: list[int]) -> list[dict]:
    if not page_nos:
        return []
    marks = ",".join("?" * len(page_nos))
    return [dict(r) for r in store.conn.execute(
        f"SELECT f.*, a.verdict FROM finding f"
        f" LEFT JOIN adjudication a ON a.finding_key = f.finding_key"
        f" WHERE f.run_id=? AND f.doc_id=? AND f.page_no IN ({marks})"
        f" ORDER BY CASE f.level WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1"
        f" WHEN 'MEDIUM' THEN 2 ELSE 3 END, f.page_no",
        [run_id, doc_id, *page_nos])]


def _unit_finding_counts(store, run_id: str, units: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for u in units:
        nums = unit_page_nos(u)
        if not nums:
            out[u["unit_id"]] = 0
            continue
        marks = ",".join("?" * len(nums))
        row = store.conn.execute(
            f"SELECT COUNT(*) n FROM finding WHERE run_id=? AND doc_id=?"
            f" AND page_no IN ({marks})", [run_id, u["doc_id"], *nums]).fetchone()
        out[u["unit_id"]] = int(row["n"] or 0)
    return out


def render_units(store, ctx: dict, params: dict) -> bytes:
    """兼容旧路径 /run/<id>/units。"""
    run_id = ctx["run_id"]
    units = store.units(run_id, params.get("doc", ""))
    rows = [
        f'<tr><td class="mono">{esc(u["unit_id"])}</td><td class="mono">{esc(u["doc_id"])}</td>'
        f'<td>{esc(u["form_type"] or "—")}</td>'
        f'<td class="mono">p{u["start_page"]}–{u["end_page"]}</td>'
        f'<td class="num">{u["page_count"]}</td>'
        f'<td class="num">{u["declared_total"] if u["declared_total"] is not None else "—"}</td>'
        f'<td class="small dim">{esc(_keys(u["keys"]))}</td></tr>' for u in units]
    body = page_head("单据视图", f"{len(units)} 份",
                     back=(f"/review?run={urllib.parse.quote(run_id)}", "疑点清单"))
    body += panel(table(["单据号", "档案", "表单类型", "页范围", ("页数", True),
                         ("声明总页", True), "关键字段"], rows, empty="未切出单据"),
                  flush=True)
    return layout("单据视图", body, ctx["user"], active="archive", theme=ctx["theme"],
                  nav_right=run_picker(ctx["runs"], run_id, "/archive"))


# ---------------------------------------------------------------- 取数

def _keys(raw) -> str:
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (ValueError, TypeError):
        return str(raw)[:60]
    return "；".join(f"{k}={v}" for k, v in list(data.items())[:4])[:70]


def _page_counts(store, run_id: str) -> dict[str, int]:
    return {r["doc_id"]: r["n"] for r in store.conn.execute(
        "SELECT doc_id, COUNT(*) n FROM page WHERE run_id=? GROUP BY doc_id", (run_id,))}


def _unit_counts(store, run_id: str) -> dict[str, int]:
    return {r["doc_id"]: r["n"] for r in store.conn.execute(
        "SELECT doc_id, COUNT(*) n FROM unit WHERE run_id=? GROUP BY doc_id", (run_id,))}


def _level_matrix(store, run_id: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in store.conn.execute(
            "SELECT doc_id, level, COUNT(*) n FROM finding WHERE run_id=? GROUP BY doc_id, level",
            (run_id,)):
        out.setdefault(r["doc_id"], {})[r["level"]] = r["n"]
    return out


def _pages(store, run_id: str, doc_id: str) -> list[dict]:
    return [dict(r) for r in store.conn.execute(
        "SELECT * FROM page WHERE run_id=? AND doc_id=? ORDER BY page_no", (run_id, doc_id))]
