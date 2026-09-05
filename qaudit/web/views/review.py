"""复核工作台：三栏布局。

原来的实现是卡片流 + 每卡一张小图，一屏看不了几条，翻页丢上下文，判定只能鼠标点。
实测数据里有 622 页缺设计图版次、463 页复印章措辞这种**系统性问题**，
逐条点击是纯粹的浪费，所以这里的两个重点是：

1. 键盘全流程——上下切换、1/2/3 判定、Enter 存并跳下一条，手不离键盘；
2. 批量判定——勾选后按「同规则同档案」一次性判掉，仍然逐条写审计日志。

证据图放中栏且可缩放平移：扫描件的手写与印章边缘必须看得清，
否则复核员会绕过系统去翻原件，那这套东西就白做了。
"""
from __future__ import annotations

import json
import urllib.parse

from ..render import (esc, layout, level_pill, page_head, panel, run_picker,
                      verdict_tag, LEVELS, LEVEL_CN, VERDICT_CN)
from ...store import CAN_ADJUDICATE

PAGE_SIZE = 200


# ---------------------------------------------------------------- 整页

def render_workbench(store, ctx: dict, params: dict) -> bytes:
    run_id = ctx["run_id"]
    run = store.run(run_id)
    if not run:
        from ..render import empty_state
        body = page_head("复核") + panel(empty_state(
            "批次不存在或尚无数据", "先发起一次审核，完成后会自动入库。",
            '<a class="btn btn-primary" href="/tasks">发起审核</a>'))
        return layout("复核", body, ctx["user"], active="review", theme=ctx["theme"])

    flt = _filters(params)
    items = store.findings(run_id, limit=PAGE_SIZE, offset=0, **flt)
    total = store.count_findings(run_id, **flt)
    selected = params.get("sel") or (items[0]["finding_key"] if items else "")

    summary = store.summary(run_id)
    body = f"""{_toolbar(store, ctx, run_id, flt, summary, total)}
<div class="wb">
  <aside class="wb-list" id="list">{_list_html(items, selected, total, ctx["user"])}</aside>
  <section class="wb-detail" id="detail">{_detail_html(store, ctx, selected)}</section>
</div>"""
    return layout("复核", body, ctx["user"], active="review", theme=ctx["theme"],
                  wide=True, nav_right=run_picker(ctx["runs"], run_id, "/review"),
                  scripts=_style() + _script(ctx["user"]["role"] in CAN_ADJUDICATE))


def render_list(store, ctx: dict, params: dict) -> bytes:
    flt = _filters(params)
    items = store.findings(ctx["run_id"], limit=PAGE_SIZE, offset=0, **flt)
    total = store.count_findings(ctx["run_id"], **flt)
    return _list_html(items, params.get("sel", ""), total, ctx["user"]).encode("utf-8")


def render_detail(store, ctx: dict, params: dict) -> bytes:
    return _detail_html(store, ctx, params.get("key", ""),
                        full=params.get("full") == "1").encode("utf-8")


# ---------------------------------------------------------------- 筛选

def _filters(params: dict) -> dict:
    return {"level": params.get("level", ""), "rule_id": params.get("rule", ""),
            "doc_id": params.get("doc", ""), "judged": params.get("judged", "")}


def _toolbar(store, ctx: dict, run_id: str, flt: dict, summary: dict, total: int) -> str:
    def opts(name: str, values: list, cur: str, label: str) -> str:
        o = f"<option value=''>{esc(label)}</option>"
        for v in values:
            sel = " selected" if str(v) == cur else ""
            o += f"<option value='{esc(v)}'{sel}>{esc(v)}</option>"
        return f"<select name='{name}'>{o}</select>"

    rules = [r["rule_id"] for r in summary["by_rule"]]
    docs = [d["doc_id"] for d in summary["docs"]]
    judged = flt["judged"]
    qs = urllib.parse.urlencode({"run": run_id, **{k: v for k, v in (
        ("level", flt["level"]), ("rule", flt["rule_id"]),
        ("doc", flt["doc_id"]), ("judged", judged)) if v}})

    quick = "".join(
        f'<a class="{"on" if judged == val else ""}" '
        f'href="/review?{urllib.parse.urlencode({**_base(run_id, flt), "judged": val})}">{label}</a>'
        for val, label in (("", "全部"), ("todo", "仅未判定"), ("done", "仅已判定")))

    return f"""<form class="wb-bar" method="get" action="/review">
  <input type="hidden" name="run" value="{esc(run_id)}">
  <span class="mono dim small">{esc(run_id)}</span>
  {opts('level', list(LEVELS), flt['level'], '全部级别')}
  {opts('rule', rules, flt['rule_id'], '全部规则')}
  {opts('doc', docs, flt['doc_id'], '全部档案')}
  <button class="btn btn-sm" type="submit">筛选</button>
  <div class="seg">{quick}</div>
  <span class="grow"></span>
  <span class="small dim">命中 <b class="mono">{total}</b> 条</span>
  <a class="btn btn-sm" href="/archive?run={urllib.parse.quote(run_id)}">单据视图</a>
  <a class="btn btn-sm" href="/export?{qs}">导出台账</a>
</form>
<div class="wb-bulk" id="bulk-bar">
  <span>已选 <b class="mono" id="bulk-n">0</b> 条</span>
  <button class="btn btn-sm" type="button" onclick="QA.bulk('true')">批量判真</button>
  <button class="btn btn-sm btn-danger" type="button" onclick="QA.bulk('false')">批量判假</button>
  <button class="btn btn-sm" type="button" onclick="QA.bulk('unsure')">批量存疑</button>
  <span class="grow"></span>
  <button class="btn btn-sm btn-ghost" type="button" onclick="QA.selectAll(false)">取消选择</button>
</div>"""


def _base(run_id: str, flt: dict) -> dict:
    out = {"run": run_id}
    for key, name in (("level", "level"), ("rule_id", "rule"), ("doc_id", "doc")):
        if flt.get(key):
            out[name] = flt[key]
    return out


# ---------------------------------------------------------------- 左栏

def _list_html(items: list[dict], selected: str, total: int, user: dict) -> str:
    can = user["role"] in CAN_ADJUDICATE
    rows = []
    for f in items:
        key = f["finding_key"]
        on = " on" if key == selected else ""
        v = f["verdict"] or ""
        mark = {"true": "●", "false": "○", "unsure": "◐"}.get(v, "")
        box = (f'<input type="checkbox" class="pick" value="{esc(key)}" '
               f'onclick="event.stopPropagation();QA.syncBulk()">') if can else ""
        rows.append(
            f'<div class="wb-item{on}" data-key="{esc(key)}" data-verdict="{esc(v)}" '
            f'onclick="QA.select(this)">'
            f'{box}'
            f'<div class="grow" style="min-width:0">'
            f'  <div class="ln1">{level_pill(f["level"])}'
            f'    <span class="mono dim small">{esc(f["rule_id"])}</span>'
            f'    <span class="vmark v-{esc(v)}">{mark}</span></div>'
            f'  <div class="ln2">{esc(f["title"])}</div>'
            f'  <div class="ln3 mono">{esc(f["doc_id"])} · p{f["page_no"]}</div>'
            f'</div></div>')
    select_all = ('<input type="checkbox" title="全选本页" '
                  'onclick="QA.selectAll(this.checked)">') if can else ""
    head = (f'<div class="wb-list-hd">{select_all}'
            f'<span class="small dim">{len(items)} / {total} 条'
            f'{"（最多显示 " + str(PAGE_SIZE) + " 条，请缩小筛选范围）" if total > PAGE_SIZE else ""}'
            f'</span></div>')
    body = "".join(rows) or '<div class="empty-state">没有符合条件的疑点</div>'
    return head + f'<div class="wb-items">{body}</div>'


# ---------------------------------------------------------------- 右栏

def _detail_html(store, ctx: dict, key: str, full: bool = False) -> str:
    if not key:
        from ..render import empty_state
        return empty_state("选中左侧任意一条疑点开始复核",
                           '按 <span class="kbd">?</span> 查看快捷键')
    f = _find(store, ctx["run_id"], key)
    if not f:
        return '<div class="empty-state">该疑点不存在（可能已被新批次覆盖）</div>'

    bbox = json.loads(f["bbox"]) if f["bbox"] else None
    src = (f"/evidence?run={urllib.parse.quote(ctx['run_id'])}"
           f"&doc={urllib.parse.quote(f['doc_id'])}&page={f['page_no']}")
    if bbox:
        src += "&bbox=" + ",".join(str(int(v)) for v in bbox)
    if full:
        src += "&full=1"

    coord = (f'<span class="mono dim2 small">x={bbox[0]} y={bbox[1]} '
             f'w={bbox[2]} h={bbox[3]}</span>') if bbox else \
            '<span class="dim2 small">整页判定，无局部坐标</span>'

    detail_url = f"/review/detail?run={urllib.parse.quote(ctx['run_id'])}&key={urllib.parse.quote(key)}"
    toggle = (f'<div class="seg">'
              f'<a class="{"" if full else "on"}" hx-get="{detail_url}" hx-target="#detail">局部</a>'
              f'<a class="{"on" if full else ""}" hx-get="{detail_url}&full=1" hx-target="#detail">整页</a>'
              f'</div>') if bbox else ""

    v = f["verdict"] or ""
    can = ctx["user"]["role"] in CAN_ADJUDICATE
    if can:
        buttons = "".join(
            f'<button class="btn jbtn{" on" if v == k else ""}" data-v="{k}" '
            f'onclick="QA.judge(\'{esc(key)}\',\'{k}\')">'
            f'{VERDICT_CN[k]}<span class="kbd">{i}</span></button>'
            for i, k in enumerate(("true", "false", "unsure"), 1))
    else:
        buttons = '<span class="dim small">只读账号，无判定权限</span>'

    who = (f'<span class="small dim">由 <b>{esc(f["reviewer"] or "未署名")}</b> 判定'
           f'{" · " + esc(f["note"]) if f["note"] else ""}</span>') if v else ""

    siblings = _siblings(store, ctx["run_id"], f, can)

    return f"""<div class="wb-detail-hd">
  {level_pill(f['level'])}
  <span class="mono">{esc(f['rule_id'])}</span>
  <b>{esc(f['title'])}</b>
  <span class="dim mono small">{esc(f['doc_id'])} · 第 {f['page_no']} 页</span>
  <span class="grow"></span>
  <span class="tag">置信度 {float(f['confidence'] or 0):.0%}</span>
  {toggle}
</div>
<div class="wb-body">
  <div class="wb-evi">
    <div class="evidence" id="evi"><img src="{esc(src)}" alt="证据图"
      onerror="this.replaceWith(Object.assign(document.createElement('div'),
        {{className:'empty-state',textContent:'原始档案不可用，无法裁剪证据图'}}))"></div>
    <div class="row small dim2" style="margin-top:6px">
      <button class="btn btn-sm btn-ghost" type="button" title="缩小（-）"
        onclick="QA.zoom('zoomOut')">−</button>
      <b class="mono" data-zoom-level style="min-width:44px;text-align:center">100%</b>
      <button class="btn btn-sm btn-ghost" type="button" title="放大（+）"
        onclick="QA.zoom('zoomIn')">＋</button>
      <button class="btn btn-sm btn-ghost" type="button" title="适应窗口（0）"
        onclick="QA.zoom('reset')">适应</button>
      <span>滚轮缩放，放大后按住拖动平移，双击复位</span><span class="grow"></span>{coord}
    </div>
  </div>
  <div class="wb-side">
    <div class="sec"><div class="sec-k">疑点说明</div><div>{esc(f['message'])}</div></div>
    <div class="sec"><div class="sec-k">依据条款</div>
      <div class="mono small">{esc(f['clause'])}</div></div>
    <div class="sec"><div class="sec-k">人工判定</div>
      <div class="jrow">{buttons}</div>
      {'<input id="jnote" placeholder="判定说明（可选，回车保存）" value="' + esc(f['note'] or '') + '">' if can else ''}
      <div style="margin-top:6px">{who}</div>
    </div>
    {siblings}
  </div>
</div>"""


def _find(store, run_id: str, key: str) -> dict | None:
    row = store.conn.execute(
        "SELECT f.*, a.verdict, a.reviewer, a.note FROM finding f"
        " LEFT JOIN adjudication a ON a.finding_key = f.finding_key"
        " WHERE f.finding_key = ?", (key,)).fetchone()
    return dict(row) if row else None


def _siblings(store, run_id: str, f: dict, can: bool) -> str:
    """同规则同档案的其它命中。系统性问题在这里一眼看得出来，
    并且可以一次判掉——这是把 600 页压成一次操作的地方。"""
    row = store.conn.execute(
        "SELECT COUNT(*) n, SUM(CASE WHEN a.verdict IS NULL THEN 1 ELSE 0 END) todo"
        " FROM finding f LEFT JOIN adjudication a ON a.finding_key = f.finding_key"
        " WHERE f.run_id=? AND f.doc_id=? AND f.rule_id=?",
        (run_id, f["doc_id"], f["rule_id"])).fetchone()
    n, todo = int(row["n"] or 0), int(row["todo"] or 0)
    if n <= 1:
        return ""
    q = urllib.parse.urlencode({"run": run_id, "doc": f["doc_id"], "rule": f["rule_id"]})
    act = ""
    if can and todo:
        act = (f'<button class="btn btn-sm btn-danger" type="button" '
               f'onclick="QA.bulkScope(\'{esc(run_id)}\',\'{esc(f["doc_id"])}\','
               f'\'{esc(f["rule_id"])}\',\'false\')">把这 {todo} 条未判的一并判假</button>')
    return (f'<div class="sec"><div class="sec-k">同规则同档案</div>'
            f'<div class="small">本档案里 <b class="mono">{esc(f["rule_id"])}</b> 共命中 '
            f'<b class="mono">{n}</b> 条，其中 <b class="mono">{todo}</b> 条未判定。</div>'
            f'<div class="wrap-row" style="margin-top:6px">'
            f'<a class="btn btn-sm" href="/review?{q}">只看这一组</a>{act}</div></div>')


# ---------------------------------------------------------------- 样式与脚本

def _style() -> str:
    return """<style>
.wb-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 14px;
  background:var(--bg-1);border:1px solid var(--line);border-radius:var(--r);margin-bottom:10px}
.wb-bulk{display:none;gap:10px;align-items:center;padding:7px 14px;margin-bottom:10px;
  background:var(--accent-fade);border:1px solid var(--accent-line);border-radius:var(--r)}
.wb-bulk.show{display:flex}
.wb{display:grid;grid-template-columns:330px minmax(0,1fr);gap:12px;
  height:calc(100vh - var(--nav-h) - 132px);min-height:480px}
.wb-list{background:var(--bg-1);border:1px solid var(--line);border-radius:var(--r);
  display:flex;flex-direction:column;overflow:hidden}
.wb-list-hd{display:flex;gap:8px;align-items:center;padding:7px 10px;
  background:var(--bg-2);border-bottom:1px solid var(--line)}
.wb-items{overflow:auto;flex:1}
.wb-item{display:flex;gap:8px;align-items:flex-start;padding:7px 10px;cursor:pointer;
  border-bottom:1px solid var(--line);border-left:2px solid transparent}
.wb-item:hover{background:var(--bg-2)}
.wb-item.on{background:var(--accent-fade);border-left-color:var(--accent)}
.wb-item .ln1{display:flex;gap:6px;align-items:center}
.wb-item .ln2{font-size:12.5px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wb-item .ln3{font-size:11px;color:var(--fg-3);margin-top:1px}
.vmark{margin-left:auto;font-size:12px}
.vmark.v-true{color:var(--ok)}.vmark.v-false{color:var(--danger)}.vmark.v-unsure{color:var(--medium)}
.wb-detail{background:var(--bg-1);border:1px solid var(--line);border-radius:var(--r);
  display:flex;flex-direction:column;overflow:hidden}
.wb-detail-hd{display:flex;gap:9px;align-items:center;flex-wrap:wrap;padding:8px 14px;
  background:var(--bg-2);border-bottom:1px solid var(--line)}
.wb-body{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:0;flex:1;overflow:hidden}
.wb-evi{padding:12px;overflow:auto;display:flex;flex-direction:column}
.wb-evi .evidence{flex:1;max-height:100%}
.wb-side{border-left:1px solid var(--line);padding:12px 14px;overflow:auto;background:var(--bg-1)}
.sec{margin-bottom:14px}
.sec-k{color:var(--fg-3);font-size:10.5px;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px}
.jrow{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.jbtn .kbd{margin-left:5px}
.jbtn.on[data-v=true]{background:color-mix(in srgb,var(--ok) 18%,transparent);
  border-color:var(--ok);color:var(--ok);font-weight:600}
.jbtn.on[data-v=false]{background:color-mix(in srgb,var(--danger) 18%,transparent);
  border-color:var(--danger);color:var(--danger);font-weight:600}
.jbtn.on[data-v=unsure]{background:color-mix(in srgb,var(--medium) 18%,transparent);
  border-color:var(--medium);color:var(--medium);font-weight:600}
#jnote{width:100%}
@media(max-width:1200px){.wb{grid-template-columns:1fr;height:auto}
  .wb-body{grid-template-columns:1fr}.wb-side{border-left:0;border-top:1px solid var(--line)}}
</style>"""


def _script(can: bool) -> str:
    return f"""<script>
(function () {{
  var CAN = {json.dumps(bool(can))};

  function items() {{ return Array.prototype.slice.call(document.querySelectorAll('.wb-item')); }}
  function current() {{ return document.querySelector('.wb-item.on'); }}

  QA.select = function (el) {{
    items().forEach(function (i) {{ i.classList.toggle('on', i === el); }});
    el.scrollIntoView({{ block: 'nearest' }});
    var url = new URL(location.href);
    url.searchParams.set('sel', el.dataset.key);
    history.replaceState(null, '', url);        // 刷新后仍停在这一条
    htmx.ajax('GET', '/review/detail?run=' + encodeURIComponent(
      new URL(location.href).searchParams.get('run') || '') +
      '&key=' + encodeURIComponent(el.dataset.key), '#detail');
  }};

  function move(step) {{
    var list = items();
    if (!list.length) return;
    var idx = list.indexOf(current());
    QA.select(list[Math.min(list.length - 1, Math.max(0, idx + step))]);
  }}

  QA.judge = function (key, verdict) {{
    if (!CAN) return;
    var el = current();
    var next = (el && el.dataset.verdict === verdict) ? '' : verdict;
    var note = document.getElementById('jnote');
    fetch('/api/adjudicate', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ finding_key: key, verdict: next, note: note ? note.value : '' }})
    }}).then(function (r) {{ return r.json(); }}).then(function (d) {{
      if (!d.ok) {{ alert('保存失败：' + (d.error || '未知错误')); return; }}
      if (el) {{
        el.dataset.verdict = next;
        var m = el.querySelector('.vmark');
        m.className = 'vmark v-' + next;
        m.textContent = {{ 'true': '●', 'false': '○', 'unsure': '◐' }}[next] || '';
      }}
      document.querySelectorAll('.jbtn').forEach(function (b) {{
        b.classList.toggle('on', b.dataset.v === next);
      }});
    }});
  }};

  /* ---- 批量 ---- */

  function picked() {{
    return Array.prototype.slice.call(document.querySelectorAll('.pick:checked'))
      .map(function (c) {{ return c.value; }});
  }}

  QA.syncBulk = function () {{
    var n = picked().length;
    document.getElementById('bulk-n').textContent = n;
    document.getElementById('bulk-bar').classList.toggle('show', n > 0);
  }};

  QA.selectAll = function (on) {{
    document.querySelectorAll('.pick').forEach(function (c) {{ c.checked = on; }});
    QA.syncBulk();
  }};

  QA.bulk = function (verdict) {{
    var keys = picked();
    if (!keys.length) return;
    if (!confirm('确认把选中的 ' + keys.length + ' 条一并「' +
        {{ 'true': '判真', 'false': '判假', 'unsure': '存疑' }}[verdict] + '」？')) return;
    post({{ finding_keys: keys, verdict: verdict, note: '批量判定' }});
  }};

  QA.bulkScope = function (run, doc, rule, verdict) {{
    fetch('/review/list?run=' + encodeURIComponent(run) + '&doc=' + encodeURIComponent(doc) +
          '&rule=' + encodeURIComponent(rule) + '&judged=todo')
      .then(function (r) {{ return r.text(); }}).then(function (html) {{
        var box = document.createElement('div');
        box.innerHTML = html;
        var keys = Array.prototype.slice.call(box.querySelectorAll('.wb-item'))
          .map(function (i) {{ return i.dataset.key; }});
        if (!keys.length) return;
        if (!confirm('确认把本档案该规则下 ' + keys.length + ' 条未判定的一并判假？')) return;
        post({{ finding_keys: keys, verdict: verdict, note: '批量判定：同规则同档案' }});
      }});
  }};

  function post(payload) {{
    fetch('/api/adjudicate/bulk', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload)
    }}).then(function (r) {{ return r.json(); }}).then(function (d) {{
      if (!d.ok) {{ alert('批量判定失败：' + (d.error || '未知错误')); return; }}
      location.reload();
    }});
  }}

  /* ---- 快捷键 ---- */

  QA.on(function () {{
    QA.key('j', '下一条疑点', function () {{ move(1); }});
    QA.key('k', '上一条疑点', function () {{ move(-1); }});
    QA.key('arrowdown', '下一条疑点', function () {{ move(1); }});
    QA.key('arrowup', '上一条疑点', function () {{ move(-1); }});
    if (CAN) {{
      QA.key('1', '判真', function () {{ withCur(function (k) {{ QA.judge(k, 'true'); }}); }});
      QA.key('2', '判假', function () {{ withCur(function (k) {{ QA.judge(k, 'false'); }}); }});
      QA.key('3', '存疑', function () {{ withCur(function (k) {{ QA.judge(k, 'unsure'); }}); }});
      QA.key('enter', '保存并跳到下一条', function () {{ move(1); }});
      QA.key('x', '勾选 / 取消勾选当前条（用于批量）', function () {{
        var el = current(); if (!el) return;
        var c = el.querySelector('.pick'); if (c) {{ c.checked = !c.checked; QA.syncBulk(); }}
      }});
    }}
    QA.key('f', '证据图整页 / 局部切换', function () {{
      var a = document.querySelector('.wb-detail-hd .seg a:not(.on)');
      if (a) htmx.trigger(a, 'click');
    }});
    QA.key('=', '证据图放大', function () {{ QA.zoom('zoomIn'); }});
    QA.key('-', '证据图缩小', function () {{ QA.zoom('zoomOut'); }});
    QA.key('0', '证据图适应窗口', function () {{ QA.zoom('reset'); }});
  }});

  function withCur(fn) {{ var el = current(); if (el) fn(el.dataset.key); }}

  document.body.addEventListener('htmx:afterSwap', function (e) {{
    if (e.target.id === 'detail') {{
      document.querySelectorAll('.evidence').forEach(QA.attachZoom);
    }}
  }});
}})();
</script>"""
