"""登录、用户管理、操作日志、系统总入口。

判定必须能追溯到具体人员，因此判定人一律取自登录会话，不接受前端传入；
所有写操作追加写进 audit_log，不修改。
"""
from __future__ import annotations

import urllib.parse
from pathlib import Path

from ..render import esc, layout, notice, page_head, panel, table
from ...store import CAN_ADMIN, ROLES

ACTION_CN = {
    "login": "登录", "login_failed": "登录失败", "logout": "退出", "import": "导入批次",
    "adjudicate": "复核判定", "export": "导出台账", "rule_change": "规则调整",
    "rule_reset": "规则还原", "task_start": "任务开始", "task_finish": "任务结束",
    "user_create": "创建用户", "user_update": "变更用户", "user_passwd": "重置口令",
    "gold_merge": "并入金标准", "sample_import": "样本入库", "label": "样本标注",
    "train": "模型训练", "model_state": "模型上线/下线",
    "mount_change": "档案来源变更", "audit_custom_path": "按自由路径审核",
}
ACTION_TONE = {"login_failed": "danger", "rule_change": "warn", "rule_reset": "warn",
               "user_create": "info", "user_update": "info", "user_passwd": "danger",
               "model_state": "accent", "mount_change": "warn",
               "audit_custom_path": "danger"}


# ---------------------------------------------------------------- 登录

def render_login(theme: str = "dark", error: str = "", hint: str = "") -> bytes:
    body = f"""<div class="login-wrap"><div class="login-card">
  <div class="brand"><span class="mark">QA</span>
    <span class="name">RecordLint 质量记录预审<small>QUALITY DOSSIER PRE-AUDIT</small></span></div>
  <div class="panel ticked">
    {notice(error, "error")}
    <form method="post" action="/login">
      <label class="field" style="margin-bottom:10px">用户名
        <input name="username" autofocus autocomplete="username"></label>
      <label class="field" style="margin-bottom:14px">口令
        <input name="password" type="password" autocomplete="current-password"></label>
      <button class="btn btn-primary" type="submit" style="width:100%;justify-content:center">
        登录</button>
    </form>
    {f'<p class="small dim" style="margin:12px 0 0">{esc(hint)}</p>' if hint else ''}
    <p class="small dim2" style="margin:14px 0 0">
      判定结果绑定登录账号，操作全程留痕。连续 5 次口令错误将锁定 15 分钟。</p>
  </div>
</div></div>"""
    return layout("登录", body, theme=theme, bare=True)


# ---------------------------------------------------------------- 系统总入口

def render_system(store, ctx: dict) -> bytes:
    is_admin = ctx["user"]["role"] in CAN_ADMIN
    cards = [
        ("发起审核", "/tasks", "选一批档案提交审核，实时看进度，完成后自动入库", True),
        ("金标准与评测", "/gold", "把人工判定并入金标准集，当场算出漏检率与准确率", True),
        ("全部批次", "/runs", "历次批次的页数、疑点数、复核进度与版本指纹", True),
        ("操作日志", "/log", "谁在何时导入了什么、判了什么、改了哪条规则", True),
        ("档案来源", "/admin/mounts", "登记允许审核的根目录，增删即时生效、无需重启", is_admin),
        ("用户管理", "/admin/users", "建号、改角色、重置口令、停用", is_admin),
    ]
    items = "".join(
        f'<a class="panel ticked" href="{href}" style="display:block;margin:0">'
        f'<h3 style="font-size:14px;margin-bottom:4px">{esc(title)}</h3>'
        f'<div class="small dim">{esc(hint)}</div></a>'
        for title, href, hint, show in cards if show)
    body = page_head("系统", "运维与配置入口")
    body += f'<div class="grid g3">{items}</div>'
    body += panel(
        '<div class="small dim">命令行入口仍然保留（audit / import / serve / user / gold / eval），'
        '用于首次建管理员账号、无人值守批处理与排障，<b>日常操作不需要</b>。<br>'
        '服务默认只监听 127.0.0.1；供科室多人访问时需 <span class="mono">--host 0.0.0.0</span>，'
        '并按网络安全要求做访问控制。离线单机场景按明文 HTTP 部署，'
        '如需加密须由前置反向代理承担。</div>',
        title="部署说明")
    return layout("系统", body, ctx["user"], active="system", theme=ctx["theme"])


# ---------------------------------------------------------------- 用户

def render_users(store, ctx: dict, msg: str = "", kind: str = "ok") -> bytes:
    rows = []
    for u in store.users():
        state = ('<span class="tag ok">启用</span>' if u["enabled"]
                 else '<span class="tag danger">停用</span>')
        locked = (f'<span class="tag warn">{esc(u["locked_until"])}</span>'
                  if u["locked_until"] else '<span class="dim2">—</span>')
        role_opts = "".join(
            f'<option value="{r}"{" selected" if r == u["role"] else ""}>{esc(r)}</option>'
            for r in ROLES)
        rows.append(
            f'<tr><td class="mono">{esc(u["username"])}</td><td>{esc(u["display_name"])}</td>'
            f'<td>{esc(ROLES.get(u["role"], u["role"]).split("（")[0])}</td>'
            f'<td>{state}</td><td class="small dim">{esc(u["created_at"])}</td><td>{locked}</td>'
            f'<td><form method="post" action="/admin/users" class="row" style="gap:5px">'
            f'<input type="hidden" name="username" value="{esc(u["username"])}">'
            f'<select name="role">{role_opts}</select>'
            f'<select name="enabled"><option value="1"{" selected" if u["enabled"] else ""}>启用</option>'
            f'<option value="0"{"" if u["enabled"] else " selected"}>停用</option></select>'
            f'<input name="password" type="password" placeholder="重置口令(可空)" style="width:130px">'
            f'<button class="btn btn-sm" name="action" value="update">保存</button>'
            f'</form></td></tr>')

    new_roles = "".join(f'<option value="{r}">{esc(ROLES[r])}</option>' for r in ROLES)
    create = (f'<form method="post" action="/admin/users" class="wrap-row">'
              f'<label class="field">用户名<input name="username" required></label>'
              f'<label class="field">姓名<input name="display_name"></label>'
              f'<label class="field">角色<select name="role">{new_roles}</select></label>'
              f'<label class="field">口令<input name="password" type="password" '
              f'placeholder="至少 8 位" required></label>'
              f'<button class="btn btn-primary" name="action" value="create">创建</button></form>')

    body = page_head("用户管理", f"{len(rows)} 个账号",
                     back=("/system", "系统")) + notice(msg, kind)
    body += panel(table(["用户名", "姓名", "角色", "状态", "创建时间", "锁定至", "操作"], rows),
                  title="账号", flush=True)
    body += panel(create, title="新增用户",
                  note="角色权限：管理员可管用户与复核；复核员可复核判定；查阅员只读。"
                       "口令用 scrypt 加盐散列存储，绝不明文落盘。"
                       "<b>停用或改密会立即让该账号的既有会话失效。</b>")
    return layout("用户管理", body, ctx["user"], active="system", theme=ctx["theme"])


# ---------------------------------------------------------------- 档案来源

def render_dir_browser(path: str = "") -> str:
    """服务端目录选择器片段（htmx 局部刷新，整页不动）。

    浏览器不会把本机绝对路径交给网页，所以目录只能在服务端列、由管理员点选；
    UNC 共享（\\\\nas\\share）没法枚举，仍要手填——两条路并存。
    """
    from ... import jobs

    d = jobs.list_dirs(path)
    crumbs = ['<button class="btn btn-sm" type="button" hx-get="/admin/mounts/browse">'
              '全部盘符</button>']
    crumbs += [f'<button class="btn btn-sm" type="button" '
               f'hx-get="/admin/mounts/browse?path={urllib.parse.quote(c["path"])}">'
               f'{esc(c["name"])}</button>' for c in d["crumbs"]]

    rows = []
    for it in d["items"]:
        q = urllib.parse.quote(it["path"])
        rows.append(
            f'<tr><td>{esc(it["name"])}</td>'
            f'<td class="mono small dim" title="{esc(it["path"])}">'
            f'{esc(it["path"][:60])}</td>'
            f'<td><button class="btn btn-sm" type="button" '
            f'hx-get="/admin/mounts/browse?path={q}">进入</button></td>'
            f'<td><button class="btn btn-sm btn-primary" type="button" '
            f'data-pick="{esc(it["path"])}">选此目录</button></td></tr>')

    here = (f'<div class="wrap-row" style="margin:10px 0;gap:8px">'
            f'<span class="dim small">当前目录</span>'
            f'<span class="mono small">{esc(d["path"] or "（尚未进入任何目录）")}</span>'
            + (f'<button class="btn btn-sm btn-primary" type="button" '
               f'data-pick="{esc(d["path"])}">选定当前目录</button>' if d["path"] else "")
            + '</div>')

    return ('<div id="dir-browser" hx-target="#dir-browser" hx-swap="outerHTML">'
            + '<div class="wrap-row" style="gap:6px">' + " ".join(crumbs) + '</div>'
            + here
            + notice(d["error"], "error")
            + table(["子目录", "完整路径", "", ""], rows,
                    empty="该目录下没有子目录", scroll=True)
            + '</div>')


def render_mounts(store, ctx: dict, config, msg: str = "", kind: str = "ok") -> bytes:
    """档案来源（挂载点）维护：增删即时生效，不需要重启服务。"""
    from ... import jobs

    mounts = config.archive_sources()
    configured = bool(getattr(config, "mounts_path", ""))
    rows = []
    for m in mounts:
        exists = m.path.exists()
        state = ('<span class="tag ok">可用</span>' if exists
                 else '<span class="tag danger">路径不可达</span>')
        op = (f'<form method="post" action="/admin/mounts" style="margin:0"'
              f' onsubmit="return confirm(\'移除来源 {esc(m.name)}？只是取消授权，'
              f'不会删除任何档案文件。\')">'
              f'<input type="hidden" name="name" value="{esc(m.name)}">'
              f'<button class="btn btn-sm btn-danger" name="action" value="remove">移除'
              f'</button></form>') if configured and m.name else '<span class="dim2">—</span>'
        rows.append(
            f'<tr><td>{esc(m.name or "（单根）")}</td>'
            f'<td class="mono small" title="{esc(str(m.path))}">{esc(str(m.path)[:70])}</td>'
            f'<td>{state}</td><td class="small dim">{esc(m.note)}</td><td>{op}</td></tr>')

    add = ('<form method="post" action="/admin/mounts" class="wrap-row">'
           '<label class="field">名称<input id="mount-name" name="name" required '
           'placeholder="选定目录后自动填" style="width:180px"></label>'
           '<label class="field">路径<input id="mount-path" name="path" required '
           'placeholder="在下方点选，或手填 \\\\nas\\share\\档案" '
           'style="min-width:360px"></label>'
           '<label class="field">备注<input name="note" style="width:180px"></label>'
           '<button class="btn btn-primary" name="action" value="add">添加</button></form>'
           '<div class="small dim" style="margin:12px 0 4px">'
           '在服务器上点选目录（浏览器不会把本机路径交给网页，所以只能在这里选）：'
           '</div>' + render_dir_browser(config.archive_root))

    body = page_head("档案来源", f"{len(mounts)} 个来源" + ("" if configured else "（未配置，按单根运行）"),
                     back=("/system", "系统"))
    body += notice(msg, kind)
    body += panel(table(["名称", "路径", "状态", "备注", "操作"], rows,
                        empty="尚未配置任何档案来源"), title="已授权的来源", flush=True)
    body += panel(add, title="新增来源", flush=True,
                  note="来源即「允许审核的根目录」：界面只能在这些目录内逐层选择，"
                       "浏览器传来的相对路径由服务端校验归属，越界一律拒绝。"
                       "名称会成为路径的第一段，不能含 <span class=\"mono\">/ \\ :</span> 等分隔符。"
                       "<b>增删只改授权范围，不会移动或删除任何档案文件</b>，且全部写入操作日志。")
    if not configured:
        body += panel(
            '<div class="small dim">当前服务未指定挂载点配置文件，正按 '
            f'<span class="mono">--archive-root {esc(config.archive_root)}</span> 的单根模式运行。'
            '要启用多来源，请以 <span class="mono">--mounts config/archives.yaml</span> 启动服务。'
            '</div>', ticked=False)
    return layout("档案来源", body, ctx["user"], active="system", theme=ctx["theme"])


def handle_mounts(store, config, user: dict, form: dict) -> str:
    """处理增删；返回提示语。写配置文件 + 写审计日志，调用方负责跳转。"""
    from ... import jobs

    path_cfg = getattr(config, "mounts_path", "")
    if not path_cfg:
        raise ValueError("服务未指定挂载点配置文件，请以 --mounts config/archives.yaml 启动")

    action = form.get("action", "")
    mounts = list(jobs.load_mounts(path_cfg, fallback_root=config.archive_root)) \
        if Path(path_cfg).exists() else []
    name = (form.get("name") or "").strip()

    if action == "add":
        target = Path((form.get("path") or "").strip())
        if not target.exists():
            raise FileNotFoundError(f"路径不存在或不可达：{target}")
        if any(m.name == name for m in mounts):
            raise ValueError(f"来源名已存在：{name}")
        mounts.append(jobs.Mount(name=name, path=target.resolve(),
                                 note=(form.get("note") or "").strip()))
        detail = f"新增来源 → {target}"
    elif action == "remove":
        if not any(m.name == name for m in mounts):
            raise ValueError(f"没有这个来源：{name}")
        mounts = [m for m in mounts if m.name != name]
        detail = "移除来源"
    else:
        raise ValueError(f"未知操作：{action}")

    jobs.save_mounts(path_cfg, mounts)
    store.log_event("mount_change", name, detail, operator=user["username"])
    return f"已{'添加' if action == 'add' else '移除'}档案来源 {name}"


# ---------------------------------------------------------------- 日志

def render_log(store, ctx: dict) -> bytes:
    rows = []
    for r in store.audit_log():
        action = ACTION_CN.get(r["action"], r["action"])
        tone = ACTION_TONE.get(r["action"], "")
        rows.append(
            f'<tr><td class="small dim mono nowrap">{esc(r["at"])}</td>'
            f'<td class="mono">{esc(r["operator"] or "—")}</td>'
            f'<td><span class="tag {tone}">{esc(action)}</span></td>'
            f'<td class="mono small" title="{esc(r["target"])}">{esc(str(r["target"])[:56])}</td>'
            f'<td class="small dim">{esc(r["detail"])}</td></tr>')
    body = page_head("操作日志", "最近 200 条", back=("/system", "系统"))
    body += panel(table(["时间", "操作人", "动作", "对象", "详情"], rows, empty="暂无记录",
                        scroll=True), flush=True)
    body += panel('<div class="small dim">追加写、不修改。记录谁在什么时候导入了什么、'
                  '判了什么、改了哪条规则、训练并上线了哪个模型。</div>', ticked=False)
    return layout("操作日志", body, ctx["user"], active="system", theme=ctx["theme"])
