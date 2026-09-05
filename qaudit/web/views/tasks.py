"""发起审核与任务记录。

审核是长任务（首次约 5s/页），不能让浏览器干等。原来的做法是每 5 秒整页 reload，
滚动位置和展开状态全丢；这里改成 htmx 只换任务表那一块，页面其余部分不动。
"""
from __future__ import annotations

import urllib.parse

from ..render import bar, esc, layout, notice, page_head, panel, table
from ...store import CAN_ADJUDICATE, CAN_ADMIN

BADGE = {"running": ("info", "运行中"), "done": ("ok", "已完成"),
         "failed": ("danger", "失败"), "canceled": ("", "已取消")}

# 条目多的时候逐个 rglob 统计页数会把整页拖住（网络盘尤其明显），超过这个数就
# 只列名字不数页数——真正要审的那一项在下一层还会数。
COUNT_PAGES_LIMIT = 60


def _href(rel: str) -> str:
    return "/tasks?rel=" + urllib.parse.quote(rel)


def _browser(config, rel: str, can: bool) -> str:
    """档案浏览器：面包屑 + 当前层条目，逐层下钻，任意一层都能直接提交审核。"""
    from ... import jobs

    mounts = config.archive_sources()
    listing = jobs.list_archives(mounts, rel, with_pages=True)
    if len(listing["items"]) > COUNT_PAGES_LIMIT:
        listing = jobs.list_archives(mounts, rel, with_pages=False)

    crumbs = [f'<a class="btn btn-sm" href="{_href("")}">全部来源</a>']
    crumbs += [f'<a class="btn btn-sm" href="{_href(c["rel"])}">{esc(c["name"])}</a>'
               for c in listing["crumbs"]]
    head = '<div class="wrap-row" style="gap:6px;margin-bottom:10px">' + \
           ' <span class="dim2">/</span> '.join(crumbs) + '</div>'

    rows = []
    for it in listing["items"]:
        pages = f'{it["pages"]} 页' if it["pages"] is not None else "—"
        enter = (f'<a class="btn btn-sm" href="{_href(it["rel"])}">进入</a>'
                 if it["kind"] != "PDF" else '<span class="dim2">—</span>')
        pick = (f'<form method="post" action="/tasks" style="margin:0">'
                f'<input type="hidden" name="rel" value="{esc(it["rel"])}">'
                f'<input name="run_id" placeholder="批次号(可空)" style="width:130px">'
                f'<button class="btn btn-sm btn-primary">审核此项</button></form>'
                if can and it["kind"] != "挂载点" else '<span class="dim2">—</span>')
        rows.append(
            f'<tr><td>{esc(it["name"])}</td><td><span class="tag">{esc(it["kind"])}</span></td>'
            f'<td class="mono small dim">{pages}</td><td>{enter}</td><td>{pick}</td></tr>')

    empty = ("未配置档案来源，请管理员到「档案来源」添加" if not rel
             else "该目录下没有可审核的子目录或 PDF")
    return head + table(["名称", "类型", "页数", "", "操作"], rows, empty=empty)


def _custom_form(config, ctx: dict) -> str:
    """逃生通道：仅在服务以 --allow-custom-path 启动且当前是管理员时出现。"""
    if not getattr(config, "allow_custom_path", False):
        return ""
    if ctx["user"]["role"] not in CAN_ADMIN:
        return ""
    return panel(
        '<form method="post" action="/tasks" class="wrap-row">'
        '<label class="field">本机绝对路径'
        '<input name="custom_path" style="min-width:420px" '
        'placeholder="D:\\档案\\2026\\batch-0001"></label>'
        '<label class="field">批次号<input name="run_id" placeholder="留空自动生成" '
        'style="width:170px"></label>'
        '<button class="btn" type="submit">按路径审核</button></form>',
        title="自由路径（管理员）",
        note="绕过档案来源直接审核本机路径，<b>每次都会写审计日志</b>。"
             "常规做法仍是把来源登记到「档案来源」——那样才有可追溯的名字。")


def render(store, ctx: dict, config, msg: str = "", rel: str = "") -> bytes:
    can = ctx["user"]["role"] in CAN_ADJUDICATE

    if can:
        form = _browser(config, rel, can)
    else:
        form = '<div class="dim">当前账号无发起审核的权限</div>'

    sources = ", ".join(m.name for m in config.archive_sources() if m.name) or "单根"
    body = page_head("发起审核", f'档案来源 <span class="mono">{esc(sources)}</span>',
                     actions=('<a class="btn btn-sm" href="/admin/mounts">档案来源</a>'
                              if ctx["user"]["role"] in CAN_ADMIN else ""),
                     back=("/system", "系统"))
    body += notice(msg, "error")
    body += panel(form, title="选择档案",
                  note="逐层进入，任意一层都能提交审核；界面只传相对路径，"
                       "服务端校验归属——防止路径穿越。"
                       "首次审核约 5 秒/页，识别结果会落盘缓存，同一批再审只需几分钟。")
    body += _custom_form(config, ctx)
    body += panel(_table(store), title="任务记录", flush=True,
                  actions='<span class="htmx-indicator">自动刷新</span>')
    return layout("发起审核", body, ctx["user"], active="system", theme=ctx["theme"])


def _table(store) -> str:
    rows = []
    for t in store.tasks():
        tone, label = BADGE.get(t["status"], ("", t["status"]))
        if t["run_id"]:
            link = f'<a class="btn btn-sm" href="/review?run={esc(t["run_id"])}">查看结果</a>'
        elif t["status"] == "running":
            # 卡住的任务要有出口：服务重启会自动收敛，但正在运行的进程里卡死的
            # 只能手动标记，否则「有任务在跑」的互斥会一直挡着新任务
            link = (f'<form method="post" action="/tasks/cancel" style="margin:0"'
                    f' onsubmit="return confirm(\'确认把该任务标记为已取消？\\n\\n'
                    f'注意：只改状态不强杀线程——真在跑的任务会继续跑完并覆盖此状态。\')">'
                    f'<input type="hidden" name="task_id" value="{esc(t["task_id"])}">'
                    f'<button class="btn btn-sm btn-danger">标记取消</button></form>')
        else:
            link = '<span class="dim2">—</span>'
        total, done = t["total"] or 0, t["done"] or 0
        prog = (f'{bar(done, total)}<span class="small dim mono">{done} / {total or "?"}</span>'
                if t["status"] == "running" else
                f'<span class="mono small dim">{done} / {total or "?"}</span>')
        rows.append(
            f'<tr><td class="mono">{esc(t["task_id"])}</td>'
            f'<td class="small" title="{esc(t["target"])}">{esc(str(t["target"])[:42])}</td>'
            f'<td><span class="tag {tone}">{esc(label)}</span></td>'
            f'<td style="min-width:120px">{prog}</td>'
            f'<td class="small dim">{esc(str(t["message"])[:60])}</td>'
            f'<td>{esc(t["operator"])}</td><td class="small dim">{esc(t["started_at"])}</td>'
            f'<td>{link}</td></tr>')
    # 只换这一块，页面其余部分（表单、滚动位置）不受影响
    return ('<div id="task-table" hx-get="/tasks" hx-trigger="every 4s"'
            ' hx-select="#task-table" hx-swap="outerHTML">' + table(
                ["任务号", "范围", "状态", "进度", "说明", "发起人", "开始时间", "结果"],
                rows, empty="暂无任务") + "</div>")
