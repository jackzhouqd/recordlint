"""规则库维护。

规则库文件是带条款注释的基线，界面上的调整不改文件，而是记为覆盖项，
带上变更人、时间与理由，可在操作日志中追溯，也可随时还原回基线。
这是「质量部能自己调口径」与「受监管行业要可追溯」之间的折中。
"""
from __future__ import annotations

import json

import yaml

from ..render import esc, layout, level_pill, notice, page_head, panel, table, LEVELS
from ...store import CAN_ADMIN

GROUPS = (
    ("rules_a", "A 类 · 文本确定性", "通用填写规则，纯文本判定，可单元测试"),
    ("rules_b", "B 类 · 视觉判定", "通用填写规则，OpenCV 经典算法，仅作提示不作结论"),
    ("rules_f", "F 类 · 表单专项", "规则包表单专项，强绑定表单类型"),
    ("rules_u", "U 类 · 单据级", "单据切分后才有正确判定单元"),
)


def render(store, ctx: dict, book, msg: str = "") -> bytes:
    overrides = store.rule_overrides()
    hits = store.rule_hit_counts()
    can = ctx["user"]["role"] in CAN_ADMIN

    total = enabled_n = changed_n = 0
    blocks = []
    for section, title, hint in GROUPS:
        specs = book.section(section)
        if not specs:
            continue
        rows = []
        for rid, spec in specs.items():
            ov = overrides.get(rid, {})
            enabled = spec.enabled if ov.get("enabled") is None else bool(ov["enabled"])
            level = ov.get("level") or spec.level
            total += 1
            enabled_n += 1 if enabled else 0
            changed_n += 1 if ov else 0
            rows.append(_row(rid, spec, enabled, level, ov, hits.get(rid, 0), can))
            if spec.params:
                rows.append(_params_row(rid, spec, ov, can))
        blocks.append(panel(
            table(["规则号", "判定内容", "级别", "依据条款", ("累计命中", True), "变更记录", "操作"],
                  rows), title=f"{title}（{len(specs)} 条）", flush=True,
            actions=f'<span class="small dim">{hint}</span>'))

    head = page_head("规则库", f"共 {total} 条 · 启用 {enabled_n} 条 · 已调整 {changed_n} 条")
    intro = panel(
        '<div class="small dim">规则库文件是带条款注释的基线。界面上的调整<b>不改文件</b>，'
        '而是记为覆盖项，带变更人、时间与理由，可在操作日志中追溯，也可随时「还原」回基线。'
        '「累计命中」为库中全部批次的合计。<b>规则调整在下一次审核时生效，不改动既有批次的历史结论。</b></div>'
        + ('' if can else '<div class="small" style="margin-top:6px;color:var(--medium)">'
                          '当前账号无规则维护权限，以下仅可查看。</div>'))

    search = ('<div class="panel ticked"><div class="row">'
              '<input id="rule-q" placeholder="按规则号 / 判定内容 / 条款筛选（即时生效）" '
              'style="flex:1" oninput="QA.filterRules(this.value)">'
              '<span class="small dim" id="rule-hit"></span></div></div>')

    body = head + notice(msg, "ok") + intro + search + "".join(blocks)
    return layout("规则库", body, ctx["user"], active="rules", theme=ctx["theme"],
                  scripts=_script())


def _row(rid: str, spec, enabled: bool, level: str, ov: dict, hits: int, can: bool) -> str:
    if ov:
        changed = (f'<span class="small">{esc(ov.get("changed_by", ""))} '
                   f'{esc(ov.get("changed_at", ""))}</span>'
                   f'<div class="small dim2">{esc(ov.get("reason", "") or "未填理由")}</div>')
    else:
        changed = '<span class="dim2">—</span>'

    if can:
        control = (
            f'<form method="post" action="/rules" class="row" style="gap:5px">'
            f'<input type="hidden" name="rule_id" value="{esc(rid)}">'
            f'<select name="enabled"><option value="1"{" selected" if enabled else ""}>启用</option>'
            f'<option value="0"{"" if enabled else " selected"}>停用</option></select>'
            f'<select name="level">' + "".join(
                f'<option value="{lv}"{" selected" if lv == level else ""}>{lv}</option>'
                for lv in LEVELS) + "</select>"
            f'<input name="reason" placeholder="变更理由" style="width:110px">'
            f'<button class="btn btn-sm" name="action" value="save">保存</button>'
            f'<button class="btn btn-sm btn-ghost" name="action" value="reset" '
            f'title="恢复规则库基线">还原</button></form>')
    else:
        control = '<span class="tag ok">启用</span>' if enabled else '<span class="tag">停用</span>'

    state = "" if enabled else ' style="opacity:.55"'
    pack = (f' <span class="tag" title="来自规则包">{esc(spec.pack)}</span>'
            if getattr(spec, "pack", "") else "")
    return (f'<tr class="rule-row"{state} data-q="{esc((rid + " " + spec.title + " " + spec.clause + " " + getattr(spec, "pack", "")).lower())}">'
            f'<td class="mono">{esc(rid)}</td><td>{esc(spec.title)}{pack}</td>'
            f'<td>{level_pill(level)}</td>'
            f'<td class="small dim">{esc(spec.clause)}</td>'
            f'<td class="num">{hits or "—"}</td><td>{changed}</td><td>{control}</td></tr>')


def _params_row(rid: str, spec, ov: dict, can: bool) -> str:
    """参数编辑行。

    阈值、适用表单、白名单这些才是质量部真正要调的东西——只能改启停和级别
    等于把「规则库交由质量部自行维护」这条承诺打了对折。
    编辑用 YAML 而不是 JSON，和 rules.yaml 里看到的写法一致。
    """
    effective = {**spec.params, **_ov_params(ov)}
    changed_keys = set(_ov_params(ov))
    text = yaml.safe_dump(effective, allow_unicode=True, sort_keys=False, default_flow_style=False)

    badge = (f'<span class="tag warn">已改 {len(changed_keys)} 项：'
             f'{esc("、".join(sorted(changed_keys)))}</span>') if changed_keys else ""

    if can:
        editor = (
            f'<form method="post" action="/rules">'
            f'<input type="hidden" name="rule_id" value="{esc(rid)}">'
            f'<textarea name="params" rows="{min(14, max(4, text.count(chr(10)) + 1))}" '
            f'class="mono" style="width:100%;resize:vertical">{esc(text)}</textarea>'
            f'<div class="wrap-row" style="margin-top:6px">'
            f'<input name="reason" placeholder="变更理由" style="flex:1;min-width:180px">'
            f'<button class="btn btn-sm btn-primary" name="action" value="save_params">'
            f'保存参数</button>'
            f'<button class="btn btn-sm btn-ghost" name="action" value="reset_params" '
            f'title="清除参数覆盖，回到规则库基线">还原参数</button>'
            f'<span class="small dim2">保存后于<b>下一次审核</b>生效，不改动既有批次的历史结论</span>'
            f'</div></form>')
    else:
        editor = f'<pre class="mono small" style="margin:0">{esc(text)}</pre>'

    return (f'<tr class="rule-row param-row" data-q="{esc((rid + " " + spec.title).lower())}">'
            f'<td colspan="7" style="padding:0 10px 10px 10px">'
            f'<details><summary>参数（{len(effective)} 项）{badge}</summary>'
            f'<div style="margin-top:8px">{editor}</div></details></td></tr>')


def _ov_params(ov: dict) -> dict:
    raw = (ov or {}).get("params") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _script() -> str:
    return """<script>
QA.filterRules = function (q) {
  q = (q || '').trim().toLowerCase();
  var shown = 0;
  document.querySelectorAll('.rule-row').forEach(function (tr) {
    var hit = !q || tr.dataset.q.indexOf(q) >= 0;
    tr.style.display = hit ? '' : 'none';
    if (hit) shown++;
  });
  document.getElementById('rule-hit').textContent = q ? ('匹配 ' + shown + ' 条') : '';
};
QA.on(function () { QA.key('/', '聚焦规则搜索框', function () {
  var el = document.getElementById('rule-q'); if (el) el.focus();
}); });
</script>"""
