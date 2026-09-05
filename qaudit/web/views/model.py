"""模型工作台：样本库 / 标注 / 合成与训练 / 模型版本。

原来这条链路只能命令行跑（tools/make_dataset.py + tools/train_seal_cls.py），
交付形态是「用户不碰命令行」，所以整条链路搬进界面，并补上命令行没有的部分：
标注进度存库（多人分工、可追溯到人）、训练实时日志、混淆矩阵、模型版本管理与上线。

一条底线：**模型只回答「这枚章是什么状态」，判不判违规仍由 rules.yaml 决定**，
验收时能解释「为什么判它不合格」，不会变成黑盒。
"""
from __future__ import annotations

import json
import urllib.parse

from ..render import (bar, empty_state, esc, hbars, kpi, layout, notice, page_head,
                      panel, table)
from ...store import CAN_ADMIN
from ...train.features import DEFECT_LABELS, LABEL_CN, LABEL_ORDER
from ...train.imaging import LABELS
from ...train.repo import MIN_HUMAN_LABELS, TrainRepo

TABS = (("/model", "概览"), ("/model/samples", "样本库"), ("/model/label", "标注"),
        ("/model/train", "合成与训练"), ("/model/versions", "模型版本"))
GRID_SIZE = 120


def render(store, ctx: dict, path: str, params: dict) -> bytes:
    repo = TrainRepo(store)
    msg, kind = params.get("msg", ""), params.get("kind", "ok")
    if path == "/model/samples":
        body, scripts = _samples(repo, params), ""
    elif path == "/model/label":
        body, scripts = _label(repo, ctx, params)
    elif path == "/model/train":
        body, scripts = _train(store, repo, ctx, params), ""
    elif path == "/model/versions":
        body, scripts = _versions(repo, ctx), ""
    else:
        path, body, scripts = "/model", _overview(repo, ctx), ""
    head = page_head("模型", "印章状态分类器 · 训练与上线", _tabs(path))
    return layout("模型", head + notice(msg, kind) + body, ctx["user"],
                  active="model", theme=ctx["theme"], scripts=scripts)


def _tabs(active: str) -> str:
    return '<div class="seg">' + "".join(
        f'<a class="{"on" if href == active else ""}" href="{href}">{esc(label)}</a>'
        for href, label in TABS) + "</div>"


# ---------------------------------------------------------------- 概览

def _overview(repo: TrainRepo, ctx: dict) -> str:
    st = repo.stats()
    active = repo.active("seal_cls")
    kpis = "".join([
        kpi("样本总数", st["total"], foot=f"真实 {st['real']} · 合成 {st['synthetic']}"),
        kpi("已标注", st["labeled"], foot=f"待标注 {st['todo']}",
            tone="ok" if st["todo"] == 0 and st["labeled"] else "info"),
        kpi("人工标注", repo.has_human_labels(), unit=f"/ {MIN_HUMAN_LABELS}",
            foot="上线门槛", tone="ok" if repo.has_human_labels() >= MIN_HUMAN_LABELS else "high"),
        kpi("源印章", st["groups"], unit="组", foot="交叉验证的分组单位"),
        kpi("覆盖档案", st["docs"], unit="份", foot="样本来源"),
    ])
    dist = hbars([(f'{LABEL_CN[k]}（{k}）', v,
                   f"/model/samples?label={urllib.parse.quote(k)}")
                  for k, v in st["by_label"].items() if v], top=8)

    if active:
        m = _metrics(active)
        cards = (kpi("分组交叉验证准确率", f'{active["accuracy"]:.1%}', tone="ok")
                 + kpi("训练样本", active["samples"])
                 + kpi("其中人工标注", active["human"])
                 + kpi("源印章分组", active["groups"], unit="组"))
        cur = panel(
            f'<div class="wrap-row"><span class="tag accent mono">{esc(active["model_id"])}</span>'
            f'<span class="tag ok">已上线</span>'
            f'<span class="dim small">训练于 {esc(active["trained_at"])} · '
            f'{esc(active["trainer"])}</span></div>'
            f'<div class="grid g4" style="margin-top:10px">{cards}</div>',
            title="当前上线模型",
            note=esc(m.get("split", "")) + "　" + esc(m.get("caveat", "")))
    else:
        cur = panel(empty_state(
            "尚未上线任何模型",
            "训练完成后到「模型版本」页上线。未上线时，依赖模型的规则会自动跳过，不影响其余审核。",
            '<a class="btn btn-primary" href="/model/train">去训练</a>'))

    flow = panel(
        '<div class="small dim">'
        '① <b>样本库</b>从档案批量裁出印章小图（检测器已定位好，标注只需打标签不需画框）→ '
        '② <b>标注</b>人工确认若干「合格」样本 → '
        '③ <b>合成</b>由合格样本派生倒盖/缺角/模糊/漏墨四类退化，训练集不必人工标注 → '
        '④ <b>训练</b>按源印章分组交叉验证 → '
        '⑤ <b>上线</b>挂到规则上，批次指纹记录模型版本。<br><br>'
        '合成大幅降低标注量：<b>人工只需标注验证集</b>（几百枚，1～2 人天），'
        '而不是标注几千页。</div>',
        title="工作流")

    return (f'<div class="grid g5" style="margin-bottom:14px">{kpis}</div>'
            + cur + f'<div class="grid g2">{panel(dist, title="标签分布")}{flow}</div>'
            + _labelers(repo))


def _labelers(repo: TrainRepo) -> str:
    rows = [f'<tr><td>{esc(r["labeler"] or "—")}</td><td class="num">{r["n"]}</td>'
            f'<td class="small dim">{esc(r["last"])}</td></tr>'
            for r in repo.labeler_stats()]
    return panel(table(["标注人", ("数量", True), "最近一次"], rows, empty="尚无标注记录"),
                 title="标注工作量", flush=True,
                 note="「合成」与「几何粗筛」是自动标注，不计入人工标注量，也不作为上线依据。")


# ---------------------------------------------------------------- 样本库

def _samples(repo: TrainRepo, params: dict) -> str:
    label = params.get("label", "")
    synthetic = params.get("syn", "")
    doc_id = params.get("doc", "")
    page = max(1, int(params.get("p", "1") or 1))
    flt = {"label": label, "synthetic": synthetic, "doc_id": doc_id}
    items = repo.samples(limit=GRID_SIZE, offset=(page - 1) * GRID_SIZE, **flt)
    total = repo.count_samples(**flt)

    if not total:
        return panel(empty_state(
            "样本库还是空的",
            "先从档案批量裁出印章小图。检测器已经把印章定位好了，这一步只是裁剪与入库。",
            '<a class="btn btn-primary" href="/model/train">去入库样本</a>'))

    cells = "".join(
        f'<figure class="thumb">'
        f'<img loading="lazy" src="/model/thumb?id={urllib.parse.quote(s["sample_id"])}" '
        f'alt="{esc(s["sample_id"])}">'
        f'<figcaption>{_label_tag(s["label"])}'
        f'{"<span class=tag>合成</span>" if s["synthetic"] else ""}</figcaption>'
        f'<span class="meta mono">{esc(s["doc_id"] or "—")} p{s["page_no"]}</span>'
        f'</figure>' for s in items)

    pages = (total + GRID_SIZE - 1) // GRID_SIZE
    pager = _pager("/model/samples", {k: v for k, v in
                                      (("label", label), ("syn", synthetic), ("doc", doc_id))
                                      if v}, page, pages)
    return (_sample_filters(repo, label, synthetic, doc_id, total)
            + panel(f'<div class="thumbs">{cells}</div>' + pager, flush=False)
            + _thumb_style())


def _sample_filters(repo: TrainRepo, label: str, synthetic: str, doc_id: str,
                    total: int) -> str:
    label_opts = "".join(
        f'<option value="{k}"{" selected" if label == k else ""}>{esc(v)}</option>'
        for k, v in [("", "全部标签"), ("__todo__", "仅未标注"), ("__done__", "仅已标注")]
        + [(lab, LABEL_CN[lab]) for lab in LABEL_ORDER])
    syn_opts = "".join(
        f'<option value="{k}"{" selected" if synthetic == k else ""}>{esc(v)}</option>'
        for k, v in [("", "真实 + 合成"), ("real", "仅真实样本"), ("synth", "仅合成样本")])
    doc_opts = "".join(
        f'<option value="{esc(d)}"{" selected" if doc_id == d else ""}>{esc(d)}</option>'
        for d in [""] + repo.docs())
    return panel(
        f'<form class="filters" method="get" action="/model/samples">'
        f'<select name="label">{label_opts}</select>'
        f'<select name="syn">{syn_opts}</select>'
        f'<select name="doc"><option value="">全部档案</option>{doc_opts}</select>'
        f'<button class="btn btn-sm" type="submit">筛选</button>'
        f'<span class="grow"></span><span class="small dim">共 {total} 枚</span>'
        f'<a class="btn btn-sm btn-primary" href="/model/label">去标注</a></form>')


def _label_tag(label: str | None) -> str:
    if not label:
        return '<span class="tag">未标注</span>'
    tone = "ok" if label == "ok" else ("" if label == "not_seal" else "warn")
    return f'<span class="tag {tone}">{esc(LABEL_CN.get(label, label))}</span>'


def _pager(base: str, qs: dict, page: int, pages: int) -> str:
    if pages <= 1:
        return ""
    links = []
    for n in range(max(1, page - 4), min(pages, page + 4) + 1):
        q = urllib.parse.urlencode({**qs, "p": n})
        links.append(f"<b>{n}</b>" if n == page else f'<a href="{base}?{q}">{n}</a>')
    return f'<div class="pager row" style="justify-content:center;margin-top:12px">{" ".join(links)}</div>'


# ---------------------------------------------------------------- 标注

def _label(repo: TrainRepo, ctx: dict, params: dict) -> tuple[str, str]:
    only = params.get("label", "__todo__")
    synthetic = params.get("syn", "real")   # 合成样本的标签由构造方式决定，默认不给人标
    items = repo.samples(label=only, synthetic=synthetic, limit=GRID_SIZE)
    st = repo.stats()

    if not items:
        body = panel(empty_state(
            "没有待标注的样本",
            "换个筛选条件，或先从档案裁出更多样本。",
            '<a class="btn" href="/model/samples">看样本库</a>'))
        return body, ""

    keys = "".join(
        f'<button class="btn btn-sm lbtn" data-label="{esc(code)}" '
        f'onclick="QA.mark(\'{esc(code)}\')">'
        f'<span class="kbd">{esc(k)}</span> {esc(text)}</button>'
        for k, (code, text) in LABELS.items())

    cells = "".join(
        f'<figure class="thumb pickable" data-id="{esc(s["sample_id"])}" '
        f'data-label="{esc(s["label"] or "")}" onclick="QA.goto(this)">'
        f'<img loading="lazy" src="/model/thumb?id={urllib.parse.quote(s["sample_id"])}">'
        f'<figcaption>{_label_tag(s["label"])}</figcaption></figure>' for s in items)

    filters = panel(
        f'<form class="filters" method="get" action="/model/label">'
        f'<select name="label">'
        f'<option value="__todo__"{" selected" if only == "__todo__" else ""}>仅未标注</option>'
        f'<option value=""{" selected" if only == "" else ""}>全部</option>'
        f'<option value="__done__"{" selected" if only == "__done__" else ""}>仅已标注（复核）</option>'
        f'</select>'
        f'<select name="syn">'
        f'<option value="real"{" selected" if synthetic == "real" else ""}>仅真实样本</option>'
        f'<option value=""{" selected" if synthetic == "" else ""}>真实 + 合成</option>'
        f'</select>'
        f'<button class="btn btn-sm" type="submit">刷新队列</button>'
        f'<span class="grow"></span>'
        f'<span class="small dim">本页 {len(items)} 枚 · 全库待标注 {st["todo"]} 枚 · '
        f'人工已标 {repo.has_human_labels()} / {MIN_HUMAN_LABELS}</span></form>')

    board = f"""<div class="lab">
  <div class="lab-main">
    <div class="lab-preview evidence" id="preview"><img id="preview-img" alt="当前样本"></div>
    <div class="row small dim2" style="margin-top:8px">
      <span class="mono" id="preview-id">—</span><span class="grow"></span>
      <span id="preview-pos">—</span></div>
    <div class="wrap-row" style="margin-top:10px">{keys}
      <button class="btn btn-sm btn-ghost" onclick="QA.mark('')">
        <span class="kbd">0</span> 撤销标签</button></div>
    <div class="small dim2" style="margin-top:8px">
      键盘：<span class="kbd">1</span>–<span class="kbd">6</span> 打标签并自动跳下一枚，
      <span class="kbd">←</span><span class="kbd">→</span> 移动，
      <span class="kbd">0</span> 撤销。标注即时存库，可随时中断续标。</div>
  </div>
  <div class="lab-grid">{cells}</div>
</div>"""

    body = filters + panel(board, title="标注台", flush=False,
                           note="标注单位是「枚」不是「页」——印章已由检测器定位好，"
                                "只需打标签不需画框，成本比常规目标检测标注低一个数量级。"
                                "标注结果存库并记录标注人，可追溯、可多人分工。")
    return body + _thumb_style(), _label_script()


def _label_script() -> str:
    codes = {k: v[0] for k, v in LABELS.items()}
    return f"""<script>
(function () {{
  var CODES = {json.dumps(codes)};
  var cur = null;

  function cells() {{ return Array.prototype.slice.call(document.querySelectorAll('.pickable')); }}

  QA.goto = function (el) {{
    cells().forEach(function (c) {{ c.classList.toggle('on', c === el); }});
    cur = el;
    el.scrollIntoView({{ block: 'nearest' }});
    document.getElementById('preview-img').src = el.querySelector('img').src;
    document.getElementById('preview-id').textContent = el.dataset.id;
    var list = cells();
    document.getElementById('preview-pos').textContent =
      (list.indexOf(el) + 1) + ' / ' + list.length;
  }};

  function move(step) {{
    var list = cells();
    if (!list.length) return;
    var i = list.indexOf(cur);
    QA.goto(list[Math.min(list.length - 1, Math.max(0, i + step))]);
  }}

  QA.mark = function (label) {{
    if (!cur) return;
    var el = cur;
    fetch('/model/label', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ sample_id: el.dataset.id, label: label }})
    }}).then(function (r) {{ return r.json(); }}).then(function (d) {{
      if (!d.ok) {{ alert('标注保存失败：' + (d.error || '未知错误')); return; }}
      el.dataset.label = label;
      el.querySelector('figcaption').innerHTML = d.tag;
      if (label) move(1);      // 打完标签自动前进，手不离键盘
    }});
  }};

  QA.on(function () {{
    Object.keys(CODES).forEach(function (k) {{
      QA.key(k, '标为「' + CODES[k] + '」并跳下一枚', function () {{ QA.mark(CODES[k]); }});
    }});
    QA.key('0', '撤销当前样本的标签', function () {{ QA.mark(''); }});
    QA.key('arrowright', '下一枚样本', function () {{ move(1); }});
    QA.key('arrowleft', '上一枚样本', function () {{ move(-1); }});
    var first = document.querySelector('.pickable');
    if (first) QA.goto(first);
  }});
}})();
</script>"""


# ---------------------------------------------------------------- 合成与训练

def _train(store, repo: TrainRepo, ctx: dict, params: dict) -> str:
    from ...train import jobs as train_jobs

    st = repo.stats()
    can = ctx["user"]["role"] in CAN_ADMIN
    archives = _archives(ctx)

    imp = (f'<form method="post" action="/model/import" class="wrap-row">'
           f'<label class="field">档案<select name="rel" style="min-width:300px">{archives}'
           f'</select></label>'
           f'<label class="field">最多页数<input name="limit" value="0" style="width:90px" '
           f'title="0 表示不限"></label>'
           f'<label class="field">外扩比例<input name="pad" value="0.15" style="width:80px" '
           f'title="留边才看得出缺角"></label>'
           f'<button class="btn btn-primary" type="submit">裁图入库</button></form>')

    syn = (f'<form method="post" action="/model/synth" class="wrap-row">'
           f'<label class="field">每类生成概率<input name="ratio" value="0.6" style="width:90px">'
           f'</label>'
           f'<label class="field">随机种子<input name="seed" value="42" style="width:90px"></label>'
           f'<button class="btn btn-primary" type="submit">合成退化样本</button></form>')

    trn = (f'<form method="post" action="/model/train" class="wrap-row">'
           f'<label class="field">交叉验证折数<input name="folds" value="5" style="width:90px">'
           f'</label>'
           f'<label class="field">随机种子<input name="seed" value="42" style="width:90px"></label>'
           f'<button class="btn btn-primary" type="submit">开始训练</button></form>')

    if not can:
        imp = syn = trn = '<div class="dim">当前账号无训练权限（需管理员）</div>'

    body = panel(
        f'<div class="row"><span class="dim small">样本 {st["total"]} 枚'
        f'（真实 {st["real"]} · 合成 {st["synthetic"]}）· 已标注 {st["labeled"]} · '
        f'源印章 {st["groups"]} 组</span></div>', ticked=False)
    body += panel(imp, title="① 从档案裁图入库",
                  note="只在配置的档案根目录内枚举，界面不接受任意路径。"
                       "红色线框（复印确认章）不是印章，自动跳过。")
    body += panel(syn, title="② 合成退化样本",
                  note="从人工确认为「合格」的样本派生倒盖 / 缺角 / 模糊 / 漏墨四类。"
                       "尚无人工标注时退回几何粗筛，此时标注人记为「几何粗筛」，"
                       "<b>不计入人工标注量</b>。")
    body += panel(trn, title="③ 训练",
                  note="按<b>源印章</b>分组交叉验证——同一枚真章派生的退化样本不得跨越"
                       "训练/验证边界，否则准确率会被严重高估。"
                       "训练依赖 sklearn，导出的是 npz 线性权重，推理只用 numpy，"
                       "<b>部署侧不引入任何新依赖</b>。")

    root = train_jobs.dataset_root(ctx["config"].out_dir)
    body += _job_panel(store, root)
    return body


def _job_panel(store, root) -> str:
    from ...train import jobs as train_jobs

    tasks = [t for t in store.tasks(limit=12)
             if t["kind"] in ("sample_import", "sample_synth", "train")]
    if not tasks:
        return panel(empty_state("还没有训练相关的任务记录"), title="任务与日志")

    latest = tasks[0]
    log = train_jobs.read_log(root, latest["task_id"])
    running = latest["status"] == "running"
    poll = (' hx-get="/model/train" hx-trigger="every 3s" hx-select="#train-log"'
            ' hx-swap="outerHTML"' if running else "")
    kinds = {"sample_import": "裁图入库", "sample_synth": "退化合成", "train": "训练"}
    rows = "".join(
        f'<tr><td class="mono">{esc(t["task_id"])}</td>'
        f'<td>{esc(kinds.get(t["kind"], t["kind"]))}</td>'
        f'<td>{_badge(t["status"])}</td>'
        f'<td class="small dim">{esc(str(t["message"])[:70])}</td>'
        f'<td>{esc(t["operator"])}</td><td class="small dim">{esc(t["started_at"])}</td>'
        f'<td>{_cancel_btn(t)}</td></tr>'
        for t in tasks)

    return panel(
        f'<pre id="train-log"{poll} class="trainlog">{esc(log) or "（暂无日志）"}</pre>'
        + table(["任务号", "类型", "状态", "最近消息", "发起人", "开始时间", "操作"], rows),
        title=f"任务与日志 · {esc(latest['task_id'])}",
        actions='<span class="htmx-indicator">实时刷新</span>' if running else "",
        note="" ) + """<style>
.trainlog{background:var(--bg);border:1px solid var(--line);border-radius:var(--r);
  padding:10px 12px;max-height:280px;overflow:auto;font-family:var(--mono);font-size:11.5px;
  color:var(--fg-2);white-space:pre-wrap;margin:0 0 12px}
</style>"""


def _cancel_btn(t: dict) -> str:
    """卡死的任务要有出口，否则「有任务在跑」的互斥会一直挡着新任务。"""
    if t["status"] != "running":
        return '<span class="dim2">—</span>'
    return (f'<form method="post" action="/tasks/cancel" style="margin:0"'
            f' onsubmit="return confirm(\'确认把该任务标记为已取消？\')">'
            f'<input type="hidden" name="task_id" value="{esc(t["task_id"])}">'
            f'<input type="hidden" name="from" value="model">'
            f'<button class="btn btn-sm btn-danger">标记取消</button></form>')


def _badge(status: str) -> str:
    tone, label = {"running": ("info", "运行中"), "done": ("ok", "已完成"),
                   "failed": ("danger", "失败")}.get(status, ("", status))
    return f'<span class="tag {tone}">{esc(label)}</span>'


def _archives(ctx: dict) -> str:
    """样本裁图的档案下拉：列出各来源下两层以内的档案，够选到具体档案号。"""
    from ... import jobs

    config = ctx["config"]
    mounts = config.archive_sources()
    opts: list[str] = []
    for m in mounts:
        for lvl1 in jobs.list_archives(mounts, m.name, with_pages=False)["items"]:
            opts.append((lvl1["rel"], f'{m.name} / {lvl1["name"]}'))
            if lvl1["kind"] != "目录":
                continue
            for lvl2 in jobs.list_archives(mounts, lvl1["rel"], with_pages=False)["items"]:
                opts.append((lvl2["rel"], f'{m.name} / {lvl1["name"]} / {lvl2["name"]}'))
    if not opts:
        return '<option value="">未在档案来源下发现可用档案</option>'
    return "".join(f'<option value="{esc(rel)}">{esc(label)}</option>' for rel, label in opts)


# ---------------------------------------------------------------- 模型版本

def _versions(repo: TrainRepo, ctx: dict) -> str:
    models = repo.models("seal_cls")
    can = ctx["user"]["role"] in CAN_ADMIN
    if not models:
        return panel(empty_state("还没有训练出任何模型",
                                 "完成标注后到「合成与训练」页发起一次训练。",
                                 '<a class="btn btn-primary" href="/model/train">去训练</a>'))

    rows = []
    for m in models:
        blocked = m["human"] < MIN_HUMAN_LABELS
        if not can:
            act = ""
        elif m["status"] == "active":
            act = (f'<form method="post" action="/model/retire" style="margin:0">'
                   f'<input type="hidden" name="model_id" value="{esc(m["model_id"])}">'
                   f'<button class="btn btn-sm btn-danger">下线</button></form>')
        elif blocked:
            act = (f'<span class="small" style="color:var(--medium)" '
                   f'title="人工标注 {m["human"]} 枚，门槛 {MIN_HUMAN_LABELS} 枚">'
                   f'人工标注不足，禁止上线</span>')
        else:
            act = (f'<form method="post" action="/model/activate" style="margin:0">'
                   f'<input type="hidden" name="model_id" value="{esc(m["model_id"])}">'
                   f'<button class="btn btn-sm btn-primary">上线</button></form>')
        state = {"active": '<span class="tag ok">已上线</span>',
                 "retired": '<span class="tag">已下线</span>'}.get(
                     m["status"], '<span class="tag info">待上线</span>')
        rows.append(
            f'<tr><td class="mono">{esc(m["model_id"])}</td><td>{state}</td>'
            f'<td class="num">{m["accuracy"]:.1%}</td><td class="num">{m["samples"]}</td>'
            f'<td class="num">{m["human"]}</td><td class="num">{m["groups"]}</td>'
            f'<td class="small dim">{esc(m["trained_at"])}<br>{esc(m["trainer"])}</td>'
            f'<td>{act}</td></tr>')

    latest = models[0]
    body = panel(table(["模型", "状态", ("准确率", True), ("样本", True), ("人工标注", True),
                        ("源印章组", True), "训练时间 / 训练人", "操作"], rows),
                 title="模型版本", flush=True,
                 note=f"上线是排他的：同类模型同时只允许一个生效，否则批次指纹会含糊。"
                      f"人工标注少于 {MIN_HUMAN_LABELS} 枚的模型禁止上线——"
                      f"全合成样本上的准确率是能力上界，不能作为验收依据。")
    body += _confusion(latest)
    body += panel(
        '<div class="small dim">上线后，规则库中依赖印章状态的规则开始生效，'
        '判定阈值（<span class="mono">reject_labels</span> / '
        '<span class="mono">min_confidence</span>）仍写在 rules.yaml 里；'
        '每个审核批次会记录当时的模型版本，任何历史结论都能按指纹重放验证。<br>'
        '<b>模型只回答「这枚章是什么状态」，判不判违规由规则决定</b>——'
        '验收时要能解释「为什么判它不合格」。</div>', title="上线后会发生什么")
    return body


def _metrics(m: dict) -> dict:
    try:
        return json.loads(m.get("metrics") or "{}")
    except (ValueError, TypeError):
        return {}


def _confusion(m: dict) -> str:
    metrics = _metrics(m)
    cm = metrics.get("confusion") or {}
    labels, matrix = cm.get("labels") or [], cm.get("matrix") or []
    if not labels or not matrix:
        return ""
    peak = max((max(row) for row in matrix if row), default=1) or 1
    head = "".join(f'<th class="num">{esc(LABEL_CN.get(l, l))}</th>' for l in labels)
    rows = []
    for label, row in zip(labels, matrix):
        cells = []
        for j, v in enumerate(row):
            alpha = round(v / peak, 3)
            good = labels[j] == label
            color = "var(--ok)" if good else "var(--critical)"
            cells.append(f'<td class="num" style="background:color-mix(in srgb,{color} '
                         f'{int(alpha * 55)}%,transparent)">{v}</td>')
        rows.append(f'<tr><th style="text-align:left">{esc(LABEL_CN.get(label, label))}</th>'
                    f'{"".join(cells)}</tr>')

    report = metrics.get("report") or {}
    prows = []
    for label in labels:
        r = report.get(label) or {}
        prows.append(f'<tr><td>{esc(LABEL_CN.get(label, label))}</td>'
                     f'<td class="num">{r.get("precision", 0):.1%}</td>'
                     f'<td class="num">{r.get("recall", 0):.1%}</td>'
                     f'<td class="num">{int(r.get("support", 0))}</td></tr>')

    cv = metrics.get("cv") or {}
    cvrows = [f'<tr><td class="mono">{esc(k)}</td>'
              f'<td class="num">{v.get("acc_mean", 0):.3f}</td>'
              f'<td class="num">± {v.get("acc_std", 0):.3f}</td></tr>' for k, v in cv.items()]

    matrix_html = (f'<table class="tbl"><thead><tr><th>真实 \\ 预测</th>{head}</tr></thead>'
                   f'<tbody>{"".join(rows)}</tbody></table>')
    return (f'<div class="grid g2">'
            + panel(matrix_html, title=f"混淆矩阵 · {esc(m['model_id'])}", flush=True,
                    note="行为真实标签，列为预测标签。对角线越亮越好。")
            + panel(table(["类别", ("精确率", True), ("召回率", True), ("样本", True)], prows)
                    + '<div style="height:10px"></div>'
                    + table(["候选模型", ("准确率均值", True), ("标准差", True)], cvrows),
                    title="逐类指标",
                    note=f'折数 {metrics.get("folds", "—")} · {esc(metrics.get("split", ""))}')
            + "</div>")


# ---------------------------------------------------------------- 样式

def _thumb_style() -> str:
    return """<style>
.thumbs{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:8px}
.thumb{margin:0;background:var(--paper);border:1px solid var(--line);border-radius:var(--r);
  padding:5px;display:flex;flex-direction:column;gap:4px;align-items:center;position:relative}
.thumb img{max-width:100%;max-height:86px;display:block}
.thumb figcaption{display:flex;gap:4px;flex-wrap:wrap;justify-content:center}
.thumb .meta{font-size:9.5px;color:#66707c;text-align:center;word-break:break-all}
.thumb.pickable{cursor:pointer}
.thumb.pickable:hover{border-color:var(--accent-line)}
.thumb.pickable.on{outline:2px solid var(--accent);outline-offset:1px}
.lab{display:grid;grid-template-columns:340px minmax(0,1fr);gap:14px}
.lab-preview{min-height:260px;padding:10px}
.lab-preview img{max-height:240px}
.lab-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:7px;
  max-height:560px;overflow:auto;align-content:start}
.lbtn{font-size:11.5px}
@media(max-width:960px){.lab{grid-template-columns:1fr}}
</style>"""


# ---------------------------------------------------------------- POST

def handle_post(store, user: dict, path: str, raw: bytes, config):
    """返回 (跳转地址, JSON 负载)。JSON 负载非空时按接口返回，用于高频的标注操作。"""
    from ...train import jobs as train_jobs

    repo = TrainRepo(store)
    if path == "/model/label":
        try:
            data = json.loads((raw or b"{}").decode("utf-8", errors="replace"))
            repo.set_label(data["sample_id"], data.get("label", ""),
                           labeler=user["username"])
            store.conn.commit()
            return "", {"ok": True, "tag": _label_tag(data.get("label", ""))}
        except Exception as exc:
            return "", {"ok": False, "error": str(exc)}

    form = {k: v[0] for k, v in
            urllib.parse.parse_qs(raw.decode("utf-8", errors="replace")).items()}
    if user["role"] not in CAN_ADMIN:
        return _back("/model/train", "需要管理员权限", "error"), None
    try:
        if path == "/model/import":
            from ... import jobs
            target = jobs.resolve_target(config.archive_sources(), form.get("rel", ""))
            train_jobs.start_import(store.path, config.out_dir, str(target),
                                    user["username"], limit=int(form.get("limit") or 0),
                                    pad=float(form.get("pad") or 0.15))
            return _back("/model/train", "裁图任务已发起"), None
        if path == "/model/synth":
            train_jobs.start_synth(store.path, config.out_dir, user["username"],
                                   ratio=float(form.get("ratio") or 0.6),
                                   seed=int(form.get("seed") or 42))
            return _back("/model/train", "合成任务已发起"), None
        if path == "/model/train":
            train_jobs.start_train(store.path, config.out_dir, user["username"],
                                   folds=int(form.get("folds") or 5),
                                   seed=int(form.get("seed") or 42))
            return _back("/model/train", "训练任务已发起"), None
        if path == "/model/activate":
            repo.set_status(form["model_id"], "active", operator=user["username"])
            store.conn.commit()
            return _back("/model/versions", f"{form['model_id']} 已上线"), None
        if path == "/model/retire":
            repo.set_status(form["model_id"], "retired", operator=user["username"])
            store.conn.commit()
            return _back("/model/versions", f"{form['model_id']} 已下线"), None
    except Exception as exc:
        target = "/model/versions" if "model" in path else "/model/train"
        return _back(target, f"操作失败：{exc}", "error"), None
    return "/model", None


def _back(path: str, msg: str, kind: str = "ok") -> str:
    return f"{path}?msg={urllib.parse.quote(msg)}&kind={kind}"
