"""页面骨架与可复用组件。

所有页面共用同一套骨架：顶栏 → 主导航 → 内容区。组件都是纯函数，
输入数据输出 HTML 字符串，不碰数据库也不碰请求上下文——这样每个视图
只需要关心「取什么数据」，不必重复拼样式。

主题在服务端首屏就写进 <html data-theme>，避免刷新时先白后黑的闪烁。
"""
from __future__ import annotations

from .. import store as store_mod

LEVELS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
LEVEL_CN = {"CRITICAL": "严重", "HIGH": "较重", "MEDIUM": "一般", "LOW": "提示"}
VERDICT_CN = {"true": "判真", "false": "判假", "unsure": "存疑"}
VERDICT_TAG = {"true": "ok", "false": "danger", "unsure": "warn"}

# 与 app.css 中的 token 保持一致。Chart.js 需要具体色值，不能用 var()，
# 因此这里留一份镜像，主题切换时由前端按 data-theme 取对应组。
CHART_COLORS = {
    "dark": {"CRITICAL": "#ff5f56", "HIGH": "#ff9f43", "MEDIUM": "#e3b341", "LOW": "#7d8da1",
             "grid": "#24303f", "text": "#94a5b8", "accent": "#2dd4bf"},
    "light": {"CRITICAL": "#c22c26", "HIGH": "#c05a10", "MEDIUM": "#8c6d12", "LOW": "#67737f",
              "grid": "#d8dee6", "text": "#4a5765", "accent": "#0f8b80"},
}

NAV = (
    ("home", "总览", "/"),
    ("review", "复核", "/review"),
    ("archive", "档案", "/archive"),
    ("rules", "规则", "/rules"),
    ("model", "模型", "/model"),
    ("system", "系统", "/system"),
)


def esc(v) -> str:
    return (str(v if v is not None else "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------- 组件

def panel(body: str, *, title: str = "", actions: str = "", note: str = "",
          flush: bool = False, ticked: bool = True) -> str:
    cls = "panel" + (" flush" if flush else "") + (" ticked" if ticked else "")
    head = ""
    if title or actions:
        head = (f'<div class="panel-hd"><h3>{esc(title)}</h3>'
                f'<div class="actions">{actions}</div></div>')
    tail = f'<p class="panel-note">{note}</p>' if note else ""
    return f'<div class="{cls}">{head}{body}{tail}</div>'


def kpi(label: str, value, *, unit: str = "", foot: str = "", tone: str = "") -> str:
    tone_cls = f" c-{tone}" if tone else ""
    unit_html = f"<small>{esc(unit)}</small>" if unit else ""
    return (f'<div class="kpi{tone_cls}"><div class="k-label">{esc(label)}</div>'
            f'<div class="k-value">{esc(value)}{unit_html}</div>'
            f'<div class="k-foot">{foot}</div></div>')


def level_pill(level: str) -> str:
    lv = (level or "LOW").upper()
    return f'<span class="lv lv-{esc(lv)}">{esc(lv)}</span>'


def verdict_tag(verdict: str | None) -> str:
    if not verdict:
        return '<span class="dim2">未判定</span>'
    return f'<span class="tag {VERDICT_TAG.get(verdict, "")}">{VERDICT_CN.get(verdict, verdict)}</span>'


def table(headers: list, rows: list[str], *, empty: str = "暂无数据",
          scroll: bool = False) -> str:
    """headers 元素可以是字符串，也可以是 (标题, 是否右对齐) 二元组。"""
    ths = []
    for h in headers:
        label, is_num = (h, False) if isinstance(h, str) else h
        ths.append(f'<th class="num">{esc(label)}</th>' if is_num else f"<th>{esc(label)}</th>")
    body = "".join(rows) or f'<tr><td class="empty" colspan="{len(headers)}">{esc(empty)}</td></tr>'
    html = f'<table class="tbl"><thead><tr>{"".join(ths)}</tr></thead><tbody>{body}</tbody></table>'
    return f'<div class="tbl-scroll">{html}</div>' if scroll else html


def hbars(items: list[tuple[str, int, str]], *, top: int = 10) -> str:
    """横向条形榜。items 为 (标签, 数量, 链接)。"""
    items = items[:top]
    if not items:
        return '<div class="empty-state">暂无数据</div>'
    peak = max(n for _, n, _ in items) or 1
    rows = []
    for label, n, href in items:
        pct = round(n * 100 / peak, 1)
        lbl = f'<a class="lbl" href="{esc(href)}" title="{esc(label)}">{esc(label)}</a>' if href \
            else f'<span class="lbl" title="{esc(label)}">{esc(label)}</span>'
        rows.append(f'<div class="hbar">{lbl}'
                    f'<div class="track"><i style="width:{pct}%"></i></div>'
                    f'<span class="val">{n}</span></div>')
    return f'<div class="hbars">{"".join(rows)}</div>'


def bar(done: int, total: int) -> str:
    pct = round(done * 100 / total, 1) if total else 0
    return f'<div class="bar"><i style="width:{pct}%"></i></div>'


def fingerprint(run: dict) -> str:
    """版本指纹卡。受监管制造场景要能回答「这条结论当时是怎么算出来的」，
    所以规则版本、规则指纹、引擎、OCR 模型、判定模型必须常驻可见。"""
    fields = [
        ("规则版本", run.get("rules_version") or "—"),
        ("规则指纹", (run.get("rules_hash") or "—")[:16]),
        ("引擎版本", run.get("engine_version") or "—"),
        ("OCR 模型", run.get("ocr_model") or "—"),
        ("判定模型", _model_versions(run)),
    ]
    cells = "".join(f'<div><div class="fp-k">{esc(k)}</div><div class="fp-v">{esc(v)}</div></div>'
                    for k, v in fields)
    return f'<div class="fingerprint">{cells}</div>'


def _model_versions(run: dict) -> str:
    import json
    raw = run.get("model_versions") or ""
    try:
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return str(raw)
    if not data:
        return "未启用"
    return "；".join(f"{k}@{v}" for k, v in data.items())


def empty_state(title: str, hint: str = "", action: str = "") -> str:
    return (f'<div class="empty-state"><div class="big">{esc(title)}</div>'
            f'<div>{hint}</div>'
            f'{f"<div style=margin-top:14px>{action}</div>" if action else ""}</div>')


def notice(text: str, kind: str = "") -> str:
    if not text:
        return ""
    return f'<div class="notice {kind}">{esc(text)}</div>'


def page_head(title: str, sub: str = "", actions: str = "",
              back: tuple[str, str] | None = None) -> str:
    """back=(链接, 上级名称) 时在标题左侧渲染返回。

    主导航只有六个顶级入口，「发起审核」「用户管理」「单据详情」这类二级页
    在导航上没有对应项，点进去就只能靠浏览器后退——键盘复核时手要离开工作区，
    所以每个二级页都必须自带一条回上级的路。
    """
    nav = (f'<a class="back-link" href="{esc(back[0])}" title="返回{esc(back[1])}">'
           f'← {esc(back[1])}</a>') if back else ""
    return (f'<div class="page-hd">{nav}<h2>{esc(title)}</h2>'
            f'<span class="sub">{sub}</span>'
            f'<div class="actions">{actions}</div></div>')


def run_picker(runs: list[dict], current: str, target: str = "/review") -> str:
    """顶栏的当前批次选择器。复核、档案、总览都跟着它走。"""
    if not runs:
        return ""
    opts = "".join(
        f'<option value="{esc(r["run_id"])}"{" selected" if r["run_id"] == current else ""}>'
        f'{esc(r["run_id"])} · {r["findings"]} 条</option>' for r in runs)
    return (f'<form method="get" action="{esc(target)}" style="margin:0">'
            f'<select name="run" onchange="this.form.submit()" title="当前批次" '
            f'style="max-width:230px">{opts}</select></form>')


# ---------------------------------------------------------------- 骨架

def _nav(user: dict | None, active: str, extra_right: str = "") -> str:
    if not user:
        return ""
    links = "".join(
        f'<a class="nav-link{" on" if key == active else ""}" href="{esc(href)}">{esc(label)}</a>'
        for key, label, href in NAV)
    role_cn = store_mod.ROLES.get(user["role"], user["role"]).split("（")[0]
    return f"""<nav class="nav">
  <div class="brand"><span class="mark">QA</span>
    <span class="name">RecordLint 质量记录预审<small>QUALITY DOSSIER PRE-AUDIT</small></span></div>
  {links}
  <div class="nav-right">{extra_right}
    <button class="icon-btn" id="theme-btn" type="button" title="切换主题">☾</button>
    <button class="icon-btn" type="button" title="快捷键帮助" onclick="QA.showHelp()">?</button>
    <span class="nav-user"><b>{esc(user["display_name"])}</b> · {esc(role_cn)}</span>
    <a class="btn btn-ghost btn-sm" href="/logout">退出</a>
  </div>
</nav>"""


HELP_MODAL = """<div class="modal-back" id="help-modal"><div class="modal">
  <div class="panel-hd" style="margin:0"><h3>快捷键</h3>
    <div class="actions"><button class="btn btn-sm btn-ghost"
      onclick="document.getElementById('help-modal').classList.remove('open')">关闭</button></div></div>
  <table class="tbl"><tbody></tbody></table>
</div></div>"""


def layout(title: str, body: str, user: dict | None = None, *, active: str = "",
           theme: str = "dark", nav_right: str = "", scripts: str = "",
           bare: bool = False, wide: bool = False) -> bytes:
    """bare=True 用于登录页等无导航的独立页面。"""
    chrome = "" if bare else _nav(user, active, nav_right)
    content = body if bare else (
        f'<div class="page{" page-wide" if wide else ""}">{body}</div>{HELP_MODAL}')
    return f"""<!doctype html>
<html lang="zh-CN" data-theme="{esc(theme)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 质量档案审核</title>
<link rel="stylesheet" href="/static/app.css">
<script src="/static/vendor/htmx.min.js" defer></script>
<!-- app.js 必须同步加载，不能带 defer：各页面的内联脚本在 body 末尾、
     解析过程中就执行，而 defer 脚本要等解析完成。带 defer 会让内联脚本
     跑的时候 window.QA 还不存在，QA.on(...) 直接抛 ReferenceError，
     整页交互（复核导航、标注预览、图表、规则搜索）全部失效。
     app.js 自身只定义对象并把初始化挂到 DOMContentLoaded，同步加载无副作用。 -->
<script src="/static/app.js"></script>
</head>
<body>{chrome}{content}{scripts}</body>
</html>""".encode("utf-8")
