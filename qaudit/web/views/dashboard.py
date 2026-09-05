"""总览与批次列表。

总览回答质量部主任每天开机后的三个问题：这批审得怎么样了、问题集中在哪、
我该从哪继续。所以首屏必须有「继续复核」的直达入口，而不是让人自己去筛。
"""
from __future__ import annotations

import json
import urllib.parse

from ..render import (bar, esc, fingerprint, hbars, kpi, layout, level_pill, panel,
                      page_head, run_picker, table, LEVELS, LEVEL_CN)


def render(store, ctx: dict) -> bytes:
    runs = ctx["runs"]
    if not runs:
        return _welcome(ctx)

    run_id = ctx["run_id"]
    run = store.run(run_id) or {}
    summary = store.summary(run_id)
    by_level = summary["by_level"]
    verdicts = summary["by_verdict"]

    total = int(run.get("findings") or 0)
    judged = sum(verdicts.values())
    todo = max(0, total - judged)
    true_n, false_n = verdicts.get("true", 0), verdicts.get("false", 0)
    hit_rate = f"{true_n / (true_n + false_n):.0%}" if (true_n + false_n) else "—"
    urgent = by_level.get("CRITICAL", 0) + by_level.get("HIGH", 0)

    q = urllib.parse.urlencode({"run": run_id, "judged": "todo"})
    kpis = "".join([
        kpi("页 / 单据", f"{run.get('pages', 0)}", unit=f"/ {run.get('units', 0)}",
            foot=f"范围 {esc(str(run.get('target', ''))[:34])}"),
        kpi("疑点总数", total, foot=f"{_density(total, run.get('pages', 0))} 条/页", tone="info"),
        kpi("待复核", todo, foot=f'<a href="/review?{q}">继续复核 →</a>',
            tone="high" if todo else "ok"),
        kpi("严重 + 较重", urgent, foot="优先处置", tone="critical" if urgent else "ok"),
        kpi("判真率", hit_rate, foot=f"判真 {true_n} · 判假 {false_n} · 存疑 {verdicts.get('unsure', 0)}",
            tone="ok"),
    ])

    progress = panel(
        f'<div class="row"><span class="dim">复核进度</span>'
        f'<span class="grow">{bar(judged, total)}</span>'
        f'<span class="mono">{judged} / {total}</span></div>',
        title="复核进度", actions=_continue_btn(run_id, todo))

    level_chart = panel(
        '<div class="chart-box"><canvas id="lvChart"></canvas></div>'
        f'<div class="legend">{_legend(by_level)}</div>',
        title="疑点级别分布")

    rule_top = panel(
        hbars([(r["rule_id"], r["n"],
                f"/review?run={urllib.parse.quote(run_id)}&rule={urllib.parse.quote(r['rule_id'])}")
               for r in summary["by_rule"]]),
        title="规则命中 Top 10",
        note="点规则号直接跳到该规则的疑点清单。命中特别集中的规则通常是系统性问题，适合批量判定。")

    doc_top = panel(
        hbars([(d["doc_id"], d["n"],
                f"/review?run={urllib.parse.quote(run_id)}&doc={urllib.parse.quote(d['doc_id'])}")
               for d in summary["docs"]]),
        title="档案疑点密度 Top 10")

    body = (
        page_head("总览", f'批次 <span class="mono">{esc(run_id)}</span> · {esc(run.get("started_at", ""))}',
                  _head_actions(run_id))
        + f'<div class="grid g5" style="margin-bottom:14px">{kpis}</div>'
        + _running_task(store)
        + progress
        + f'<div class="grid g2">{level_chart}{rule_top}</div>'
        + f'<div class="grid g2">{doc_top}{_fingerprint_panel(run)}</div>'
    )
    return layout("总览", body, ctx["user"], active="home", theme=ctx["theme"],
                  nav_right=run_picker(runs, run_id, "/"),
                  scripts=_chart_script(by_level))


def _density(total: int, pages: int) -> str:
    return f"{total / pages:.2f}" if pages else "—"


def _continue_btn(run_id: str, todo: int) -> str:
    if not todo:
        return '<span class="tag ok">本批已全部判定</span>'
    q = urllib.parse.urlencode({"run": run_id, "judged": "todo"})
    return f'<a class="btn btn-primary" href="/review?{q}">继续复核（{todo} 条待判）</a>'


def _head_actions(run_id: str) -> str:
    rq = urllib.parse.quote(run_id)
    return (f'<a class="btn" href="/runs">全部批次</a>'
            f'<a class="btn" href="/gold?run={rq}">金标准与评测</a>'
            f'<a class="btn" href="/export?run={rq}">导出台账 CSV</a>')


def _legend(by_level: dict) -> str:
    out = []
    for lv in LEVELS:
        out.append(f'<span><i style="background:var(--{lv.lower()})"></i>'
                   f'{LEVEL_CN[lv]} {by_level.get(lv, 0)}</span>')
    return "".join(out)


def _fingerprint_panel(run: dict) -> str:
    return panel(fingerprint(run), title="版本指纹",
                 note="同一页 + 同一规则版本 + 同一模型版本 → 必然得到同样的结论。"
                      "任何历史结论都能按这组指纹重放验证。")


def _running_task(store) -> str:
    t = store.running_task()
    if not t:
        return ""
    total = t.get("total") or 0
    done = t.get("done") or 0
    pct = f"{done}/{total or '?'}"
    return panel(
        f'<div class="row"><span class="tag info">运行中</span>'
        f'<span class="mono">{esc(t["task_id"])}</span>'
        f'<span class="grow">{bar(done, total)}</span>'
        f'<span class="mono">{pct}</span></div>'
        f'<div class="small dim" style="margin-top:6px">{esc(t.get("message", ""))}</div>',
        title="正在审核",
        actions='<span class="htmx-indicator">刷新中…</span>'
                '<a class="btn btn-sm" href="/tasks">任务详情</a>')


def _chart_script(by_level: dict) -> str:
    data = [by_level.get(lv, 0) for lv in LEVELS]
    labels = [LEVEL_CN[lv] for lv in LEVELS]
    return f"""<script src="/static/vendor/chart.umd.min.js"></script>
<script>
(function () {{
  var DATA = {json.dumps(data)};
  var LABELS = {json.dumps(labels, ensure_ascii=False)};
  var chart = null;
  function css(name) {{
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }}
  function draw() {{
    var el = document.getElementById('lvChart');
    if (!el || !window.Chart) return;
    if (chart) chart.destroy();
    chart = new Chart(el, {{
      type: 'doughnut',
      data: {{ labels: LABELS, datasets: [{{
        data: DATA,
        backgroundColor: [css('--critical'), css('--high'), css('--medium'), css('--low')],
        borderColor: css('--bg-1'), borderWidth: 2, hoverOffset: 6
      }}]}},
      options: {{
        responsive: true, maintainAspectRatio: false, cutout: '62%',
        plugins: {{ legend: {{ display: false }},
          tooltip: {{ callbacks: {{ label: function (c) {{
            var sum = DATA.reduce(function (a, b) {{ return a + b; }}, 0) || 1;
            return c.label + ' ' + c.parsed + ' 条（' + Math.round(c.parsed * 100 / sum) + '%）';
          }} }} }} }}
      }}
    }});
  }}
  QA.on(draw);
  document.addEventListener('qa:theme', draw);   // 换主题时重画，否则轴色与底色打架
}})();
</script>"""


def _welcome(ctx: dict) -> bytes:
    from ..render import empty_state
    body = page_head("总览") + panel(empty_state(
        "还没有任何审核批次",
        "从档案根目录选一批档案发起审核，完成后会自动入库，可直接开始复核。",
        '<a class="btn btn-primary" href="/tasks">发起首次审核</a>'))
    return layout("总览", body, ctx["user"], active="home", theme=ctx["theme"])


# ---------------------------------------------------------------- 批次列表

def render_runs(store, ctx: dict) -> bytes:
    rows = []
    for r in ctx["runs"]:
        rq = urllib.parse.quote(r["run_id"])
        pct = round((r["judged"] or 0) * 100 / r["findings"], 1) if r["findings"] else 0
        rows.append(
            f'<tr><td><a class="mono" href="/review?run={rq}">{esc(r["run_id"])}</a></td>'
            f'<td class="small dim">{esc(r["started_at"])}</td>'
            f'<td class="small" title="{esc(r["target"])}">{esc(str(r["target"])[:46])}</td>'
            f'<td class="num">{r["pages"]}</td><td class="num">{r["units"]}</td>'
            f'<td class="num">{r["findings"]}</td>'
            f'<td style="min-width:120px">{bar(r["judged"] or 0, r["findings"])}'
            f'<span class="small dim">{r["judged"] or 0} / {r["findings"]}（{pct}%）</span></td>'
            f'<td class="mono small">{esc(r["rules_version"])}</td>'
            f'<td class="mono small">{esc(str(r["rules_hash"] or "")[:10])}</td>'
            f'<td><a class="btn btn-sm" href="/review?run={rq}">复核</a></td></tr>')
    body = page_head("审核批次", f"共 {len(ctx['runs'])} 批",
                     '<a class="btn btn-primary" href="/tasks">发起审核</a>',
                     back=("/", "总览"))
    body += panel(
        table(["批次", "时间", "范围", ("页", True), ("单据", True), ("疑点", True),
               "复核进度", "规则版本", "规则指纹", "操作"], rows,
              empty="暂无数据，请先发起一次审核"),
        flush=True,
        note="")
    body += panel('<span class="dim small">规则指纹用于复现：同一页 + 同一规则版本 + 同一模型版本 '
                  '→ 必然得到同样的结论。</span>', ticked=False)
    return layout("审核批次", body, ctx["user"], active="home", theme=ctx["theme"],
                  nav_right=run_picker(ctx["runs"], ctx["run_id"], "/runs"))
