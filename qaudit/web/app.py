"""HTTP 入口：路由分发、鉴权、静态资源、证据图裁剪。

刻意只用标准库 http.server + sqlite3。前端引入了 htmx 与 Chart.js 两个
**静态文件**（随包分发、不联网、无构建链路），Python 运行时依赖仍为零。

这个模块只做分发，页面渲染全部在 views/ 下——server.py 曾经长到 831 行
且还要再塞进训练模块，必须拆开。
"""
from __future__ import annotations

import json
import mimetypes
import urllib.parse
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

from .. import ingest
from ..findings import RuleBook
from ..store import CAN_ADJUDICATE, CAN_ADMIN, Store
from . import render
from .views import admin, archive, dashboard, gold, model, review, rules, tasks

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE = "qaudit_session"
THEME_COOKIE = "qaudit_theme"
RUN_COOKIE = "qaudit_run"


class Config:
    """服务端运行参数：档案来源、规则库、输出目录。"""
    archive_root: str = "."          # 未配置挂载点时的单根，兼容老部署
    mounts_path: str = ""            # 挂载点配置（config/archives.yaml）
    allow_custom_path: bool = False  # 逃生通道：管理员直接填绝对路径，默认关闭
    rules_path: str = ""
    no_packs: bool = False
    out_dir: str = "out"
    engine_version: str = ""

    @classmethod
    def archive_sources(cls):
        """每次现读配置——管理员改完档案来源即刻生效，不必重启服务。"""
        from .. import jobs
        try:
            return jobs.load_mounts(cls.mounts_path or None, fallback_root=cls.archive_root)
        except Exception as exc:     # 配置写坏了不能让整个页面打不开
            print(f"[警告] 档案来源配置无法加载，暂按单根运行：{exc}")
            return jobs.load_mounts(None, fallback_root=cls.archive_root)


# ---------------------------------------------------------------- 证据图

@lru_cache(maxsize=64)
def _page_image(source: str, page_no: int) -> np.ndarray | None:
    return ingest.load_page_image(source, page_no)


def evidence_jpeg(source: str, page_no: int, bbox: tuple | None, max_w: int = 900,
                  full: bool = False) -> bytes | None:
    """按需从原始档案裁剪证据图——不预存、不复制。

    full=True 时返回整页并在原位画红框，供复核时确认「这处在整页的什么位置」。
    """
    img = _page_image(source, page_no)
    if img is None:
        return None
    if bbox and full:
        x, y, w, h = bbox
        crop = img.copy()
        cv2.rectangle(crop, (x, y), (x + w, y + h), (0, 0, 255), 4)
    elif bbox:
        x, y, w, h = bbox
        pad_x, pad_y = max(70, w // 2), max(50, h)
        x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
        x2, y2 = min(img.shape[1], x + w + pad_x), min(img.shape[0], y + h + pad_y)
        crop = img[y1:y2, x1:x2].copy()
        cv2.rectangle(crop, (x - x1, y - y1), (x - x1 + w, y - y1 + h), (0, 0, 255), 3)
    else:
        crop = img
    hgt, wid = crop.shape[:2]
    if wid > max_w:
        scale = max_w / wid
        crop = cv2.resize(crop, (max_w, max(1, int(hgt * scale))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    return buf.tobytes() if ok else None


def _csv(v) -> str:
    """CSV 字段转义：逗号、引号、换行都要处理，否则台账会串列。"""
    text = str(v if v is not None else "").replace("\r", " ").replace("\n", " ")
    if any(c in text for c in ',"'):
        text = '"' + text.replace('"', '""') + '"'
    return text


# ---------------------------------------------------------------- Handler

class Handler(BaseHTTPRequestHandler):
    store: Store = None  # type: ignore[assignment]
    server_version = "qaudit"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    # ---------------- 请求上下文

    def _cookie(self, name: str) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _theme(self) -> str:
        return "light" if self._cookie(THEME_COOKIE) == "light" else "dark"

    def _user(self) -> dict | None:
        return self.store.session_user(self._cookie(COOKIE))

    def _redirect(self, location: str, cookie: str = "") -> None:
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _require(self, roles: frozenset | None = None) -> dict | None:
        """未登录跳登录页；权限不足返回 403。"""
        user = self._user()
        if user is None:
            self._redirect("/login")
            return None
        if roles and user["role"] not in roles:
            self._send("权限不足".encode("utf-8"), "text/plain; charset=utf-8", 403)
            return None
        return user

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", code: int = 200,
              headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def _form(self, raw: bytes) -> dict:
        return {k: v[0] for k, v in
                urllib.parse.parse_qs(raw.decode("utf-8", errors="replace")).items()}

    def _ctx(self, user: dict, params: dict) -> dict:
        """页面渲染上下文：当前用户、主题、当前批次、批次列表。

        当前批次的优先级：URL 参数 > cookie > 最近一次批次。
        这样顶栏的批次选择器在各页之间是连贯的，不会切一次页面就丢。
        """
        runs = self.store.runs()
        wanted = params.get("run") or self._cookie(RUN_COOKIE)
        known = {r["run_id"] for r in runs}
        current = wanted if wanted in known else (runs[0]["run_id"] if runs else "")
        return {"user": user, "theme": self._theme(), "runs": runs, "run_id": current,
                "config": Config}

    # ---------------- GET

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        path = parsed.path

        try:
            if path.startswith("/static/"):
                self._static(path)
                return
            if path == "/login":
                hint = ("" if self.store.has_users() else
                        "系统尚未创建任何账号，请先在服务器上执行："
                        " python -m qaudit.cli user add <用户名> --role admin")
                self._send(admin.render_login(theme=self._theme(), hint=hint))
                return
            if path == "/logout":
                user = self._user()
                self.store.delete_session(self._cookie(COOKIE),
                                          operator=user["username"] if user else "")
                self._redirect("/login", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")
                return

            user = self._require()
            if user is None:
                return
            ctx = self._ctx(user, params)
            self._route_get(path, params, ctx, user)
        except Exception as exc:  # 单个请求出错不拖垮服务
            self._send(f"服务器错误：{exc}".encode("utf-8"), "text/plain; charset=utf-8", 500)

    def _route_get(self, path: str, params: dict, ctx: dict, user: dict) -> None:
        set_run = self._run_cookie(ctx)

        if path == "/":
            self._send(dashboard.render(self.store, ctx), headers=set_run)
        elif path in ("/review", "/findings"):
            self._send(review.render_workbench(self.store, ctx, params), headers=set_run)
        elif path == "/review/list":                     # htmx 片段
            self._send(review.render_list(self.store, ctx, params))
        elif path == "/review/detail":                   # htmx 片段
            self._send(review.render_detail(self.store, ctx, params))
        elif path == "/archive":
            self._send(archive.render(self.store, ctx, params), headers=set_run)
        elif path == "/runs":
            self._send(dashboard.render_runs(self.store, ctx), headers=set_run)
        elif path == "/rules":
            self._send(rules.render(self.store, ctx, self._rulebook(), params.get("msg", "")))
        elif path == "/model/thumb":
            self._thumb(params)
        elif path == "/model" or path.startswith("/model/"):
            self._send(model.render(self.store, ctx, path, params))
        elif path == "/system":
            self._send(admin.render_system(self.store, ctx))
        elif path == "/tasks":
            self._send(tasks.render(self.store, ctx, Config, params.get("msg", ""),
                                    rel=params.get("rel", "")))
        elif path == "/log":
            self._send(admin.render_log(self.store, ctx))
        elif path == "/admin/users":
            if self._require(CAN_ADMIN):
                self._send(admin.render_users(self.store, ctx, params.get("msg", "")))
        elif path == "/admin/mounts":
            if self._require(CAN_ADMIN):
                self._send(admin.render_mounts(self.store, ctx, Config,
                                               params.get("msg", ""),
                                               params.get("kind", "ok")))
        elif path == "/admin/mounts/browse":             # htmx 片段：服务端目录选择
            if self._require(CAN_ADMIN):
                self._send(admin.render_dir_browser(params.get("path", "")).encode("utf-8"))
        elif path == "/gold":
            self._send(gold.render(self.store, ctx, params.get("run", "") or ctx["run_id"]))
        elif path == "/export":
            self._export(params, user)
        elif path == "/evidence":
            self._evidence(params)
        elif path == "/api/tasks":
            running = self.store.running_task()
            self._json({"running": bool(running), "task": running})
        elif path == "/api/export":
            self._json(self._adjudication_payload(params.get("run", "")))
        elif path.startswith("/run/") and path.endswith("/units"):
            run_id = urllib.parse.unquote(path[5:-6])
            self._send(archive.render_units(self.store, {**ctx, "run_id": run_id}, params))
        elif path.startswith("/run/"):
            run_id = urllib.parse.unquote(path[5:])
            self._send(review.render_workbench(self.store, {**ctx, "run_id": run_id}, params))
        else:
            self._send(b"not found", "text/plain; charset=utf-8", 404)

    def _run_cookie(self, ctx: dict) -> dict:
        if not ctx["run_id"]:
            return {}
        return {"Set-Cookie": f"{RUN_COOKIE}={urllib.parse.quote(ctx['run_id'])}"
                              "; Path=/; Max-Age=2592000; SameSite=Strict"}

    def _adjudication_payload(self, run_id: str) -> dict:
        data = self.store.export_adjudications(run_id)
        return {
            "reviewer": "", "reviewed_at": datetime.now().strftime("%Y.%m.%d"),
            "source_run": run_id,
            "adjudications": [
                {"doc_id": d["doc_id"], "page_no": d["page_no"], "rule_id": d["rule_id"],
                 "verdict": d["verdict"], "note": d.get("note", "")} for d in data
            ],
        }

    # ---------------- 静态资源

    def _static(self, path: str) -> None:
        rel = urllib.parse.unquote(path[len("/static/"):])
        target = (STATIC_DIR / rel).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            self._send(b"not found", "text/plain", 404)
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or target.suffix in (".js", ".css"):
            ctype += "; charset=utf-8"
        data = target.read_bytes()
        # vendor 文件带版本号、内容不变，可以长缓存；自研文件按修改时间弱缓存
        cache = "public, max-age=31536000, immutable" if "/vendor/" in path else "no-cache"
        self._send(data, ctype, headers={"Cache-Control": cache})

    def _evidence(self, params: dict) -> None:
        row = self.store.page_source(params.get("run", ""), params.get("doc", ""),
                                     int(params.get("page", "0") or 0))
        if not row or not row.get("source"):
            self._send(b"no source", "text/plain", 404)
            return
        bbox = None
        if params.get("bbox"):
            try:
                bbox = tuple(int(v) for v in params["bbox"].split(","))
            except ValueError:
                bbox = None
        full = params.get("full") == "1"
        jpeg = evidence_jpeg(row["source"], int(params.get("page", "1")), bbox,
                             max_w=int(params.get("w", "1400") or 1400), full=full)
        if jpeg is None:
            self._send(b"source unavailable", "text/plain", 404)
            return
        self._send(jpeg, "image/jpeg", headers={"Cache-Control": "private, max-age=600"})

    def _thumb(self, params: dict) -> None:
        """样本缩略图。只按样本库里登记的相对路径取文件，不接受任意路径。"""
        from ..train.jobs import dataset_root
        from ..train.repo import TrainRepo

        repo = TrainRepo(self.store)
        row = repo.conn.execute("SELECT file FROM seal_sample WHERE sample_id=?",
                                (params.get("id", ""),)).fetchone()
        if row is None:
            self._send(b"not found", "text/plain", 404)
            return
        root = dataset_root(Config.out_dir).resolve()
        target = (root / row["file"]).resolve()
        if root not in target.parents or not target.is_file():
            self._send(b"not found", "text/plain", 404)
            return
        self._send(target.read_bytes(), "image/jpeg",
                   headers={"Cache-Control": "private, max-age=86400"})

    def _rulebook(self) -> RuleBook:
        # 只读展示：不替换 formtype 模块默认分类器，避免与后台审核线程互相覆盖
        return RuleBook.load(Config.rules_path, packs=None if Config.no_packs else "auto",
                             configure=False)

    def _export(self, params: dict, user: dict) -> None:
        """疑点台账导出为 CSV（带 BOM，Excel 直接打开不乱码）。"""
        run_id = params.get("run", "")
        rows = self.store.findings(run_id, level=params.get("level", ""),
                                   rule_id=params.get("rule", ""), doc_id=params.get("doc", ""),
                                   judged=params.get("judged", ""), limit=10 ** 9)
        header = ["序号", "档案", "页码", "规则号", "级别", "判定内容", "疑点说明",
                  "依据条款", "置信度", "人工判定", "判定人", "判定说明"]
        lines = [",".join(header)]
        for i, r in enumerate(rows, 1):
            lines.append(",".join(_csv(v) for v in [
                i, r["doc_id"], r["page_no"], r["rule_id"], r["level"], r["title"], r["message"],
                r["clause"], f"{float(r['confidence'] or 0):.0%}",
                render.VERDICT_CN.get(r["verdict"] or "", "未判定"),
                r["reviewer"] or "", r["note"] or "",
            ]))
        body = ("﻿" + "\r\n".join(lines)).encode("utf-8")
        self._send(body, "text/csv; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename=findings_{run_id or 'all'}.csv"})
        self.store._log("export", run_id, f"导出 {len(rows)} 条台账", user["username"])
        self.store.conn.commit()

    # ---------------- POST

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""

        try:
            if path == "/login":
                self._login(raw)
            elif path == "/api/adjudicate":
                self._adjudicate(raw)
            elif path == "/api/adjudicate/bulk":
                self._adjudicate_bulk(raw)
            elif path == "/admin/users":
                self._manage_users(raw)
            elif path == "/admin/mounts":
                self._manage_mounts(raw)
            elif path == "/tasks":
                self._start_audit(raw)
            elif path == "/tasks/cancel":
                self._cancel_task(raw)
            elif path == "/rules":
                self._change_rule(raw)
            elif path == "/gold":
                self._gold(raw)
            elif path.startswith("/model/"):
                self._model_post(path, raw)
            else:
                self._send(b"not found", "text/plain", 404)
        except Exception as exc:
            self._send(f"服务器错误：{exc}".encode("utf-8"), "text/plain; charset=utf-8", 500)

    def _login(self, raw: bytes) -> None:
        form = self._form(raw)
        username, password = form.get("username", "").strip(), form.get("password", "")
        try:
            user = self.store.authenticate(username, password)
        except PermissionError as exc:
            self._send(admin.render_login(theme=self._theme(), error=str(exc)))
            return
        if user is None:
            # 不区分「用户不存在」与「口令错误」，避免探测账号
            self._send(admin.render_login(theme=self._theme(), error="用户名或口令错误"))
            return
        token = self.store.create_session(username, client=self.client_address[0])
        self._redirect("/", f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict")

    def _adjudicate(self, raw: bytes) -> None:
        user = self._user()
        if user is None:
            self._json({"ok": False, "error": "未登录"}, 401)
            return
        if user["role"] not in CAN_ADJUDICATE:
            self._json({"ok": False, "error": "无判定权限"}, 403)
            return
        try:
            data = json.loads((raw or b"{}").decode("utf-8", errors="replace"))
            # 判定人一律取自登录会话，不接受前端传入
            self.store.adjudicate(data["finding_key"], data.get("verdict", ""),
                                  reviewer=user["username"], note=data.get("note", ""))
            self._json({"ok": True, "reviewer": user["display_name"]})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 400)

    def _adjudicate_bulk(self, raw: bytes) -> None:
        """批量判定：同一规则在同一档案里往往是系统性问题，
        逐条点击纯属浪费。批量同样绑定登录账号并逐条写审计日志。"""
        user = self._user()
        if user is None:
            self._json({"ok": False, "error": "未登录"}, 401)
            return
        if user["role"] not in CAN_ADJUDICATE:
            self._json({"ok": False, "error": "无判定权限"}, 403)
            return
        try:
            data = json.loads((raw or b"{}").decode("utf-8", errors="replace"))
            keys = data.get("finding_keys") or []
            verdict = data.get("verdict", "")
            note = data.get("note", "")
            if len(keys) > 5000:
                raise ValueError("单次批量判定不得超过 5000 条")
            done = 0
            for key in keys:
                self.store.adjudicate(key, verdict, reviewer=user["username"], note=note)
                done += 1
            self._json({"ok": True, "count": done, "reviewer": user["display_name"]})
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 400)

    def _manage_users(self, raw: bytes) -> None:
        admin_user = self._require(CAN_ADMIN)
        if admin_user is None:
            return
        form = self._form(raw)
        try:
            if form.get("action") == "create":
                self.store.create_user(
                    form["username"].strip(), form["password"], role=form.get("role", "reviewer"),
                    display_name=form.get("display_name", "").strip(),
                    operator=admin_user["username"])
                msg = f"已创建用户 {form['username']}"
            else:
                username = form["username"]
                self.store.set_user_state(username, role=form.get("role"),
                                          enabled=form.get("enabled") == "1",
                                          operator=admin_user["username"])
                if form.get("password"):
                    self.store.set_password(username, form["password"],
                                            operator=admin_user["username"])
                msg = f"已更新用户 {username}"
            self._redirect("/admin/users?msg=" + urllib.parse.quote(msg))
        except Exception as exc:
            ctx = self._ctx(admin_user, {})
            self._send(admin.render_users(self.store, ctx, f"操作失败：{exc}", kind="error"))

    def _manage_mounts(self, raw: bytes) -> None:
        user = self._require(CAN_ADMIN)
        if user is None:
            return
        try:
            msg = admin.handle_mounts(self.store, Config, user, self._form(raw))
            self._redirect("/admin/mounts?msg=" + urllib.parse.quote(msg))
        except Exception as exc:
            self._redirect("/admin/mounts?kind=error&msg=" +
                           urllib.parse.quote(f"操作失败：{exc}"))

    def _start_audit(self, raw: bytes) -> None:
        user = self._require(CAN_ADJUDICATE)
        if user is None:
            return
        form = self._form(raw)
        custom = (form.get("custom_path") or "").strip()
        try:
            from .. import jobs
            if custom:
                # 逃生通道：默认关闭、仅管理员，且必须留下「审了哪个目录」的痕迹
                if user["role"] not in CAN_ADMIN:
                    raise PermissionError("自由路径仅管理员可用")
                target = jobs.resolve_custom(custom, allowed=Config.allow_custom_path)
                self.store.log_event("audit_custom_path", str(target),
                                     "绕过档案来源直接发起审核", operator=user["username"])
            else:
                rel = (form.get("rel") or "").strip()
                if not rel:
                    raise ValueError("请选择要审核的档案")
                target = jobs.resolve_target(Config.archive_sources(), rel)
            jobs.start_audit(self.store.path, str(target), out_dir=Config.out_dir,
                             rules_path=Config.rules_path, operator=user["username"],
                             run_id=form.get("run_id", "").strip(),
                             engine_version=Config.engine_version, no_packs=Config.no_packs)
            self._redirect("/tasks")
        except Exception as exc:
            self._redirect("/tasks?msg=" + urllib.parse.quote(str(exc)))

    def _cancel_task(self, raw: bytes) -> None:
        user = self._require(CAN_ADJUDICATE)
        if user is None:
            return
        form = self._form(raw)
        back = "/model/train" if form.get("from") == "model" else "/tasks"
        try:
            self.store.cancel_task(form.get("task_id", ""), operator=user["username"])
            self._redirect(f"{back}?msg=" + urllib.parse.quote("任务已标记为取消"))
        except Exception as exc:
            self._redirect(f"{back}?msg=" + urllib.parse.quote(f"操作失败：{exc}") + "&kind=error")

    def _change_rule(self, raw: bytes) -> None:
        admin_user = self._require(CAN_ADMIN)
        if admin_user is None:
            return
        form = self._form(raw)
        rule_id = form.get("rule_id", "")
        action = form.get("action", "")
        try:
            if action == "reset":
                self.store.clear_rule_override(rule_id, operator=admin_user["username"])
                msg = f"{rule_id} 已还原为规则库基线"
            elif action == "reset_params":
                self.store.set_rule_override(
                    rule_id, params={}, operator=admin_user["username"],
                    reason=form.get("reason", "").strip() or "还原参数")
                msg = f"{rule_id} 的参数已还原为规则库基线"
            elif action == "save_params":
                changed = self._diff_params(rule_id, form.get("params", ""))
                self.store.set_rule_override(
                    rule_id, params=changed, operator=admin_user["username"],
                    reason=form.get("reason", "").strip())
                msg = (f"{rule_id} 已改动 {len(changed)} 个参数（下次审核生效）"
                       if changed else f"{rule_id} 的参数与基线一致，未记录覆盖")
            else:
                self.store.set_rule_override(
                    rule_id, enabled=form.get("enabled") == "1", level=form.get("level"),
                    operator=admin_user["username"], reason=form.get("reason", "").strip())
                msg = f"{rule_id} 已更新（下次审核生效）"
            self._redirect("/rules?msg=" + urllib.parse.quote(msg))
        except Exception as exc:
            self._redirect("/rules?msg=" + urllib.parse.quote(f"操作失败：{exc}"))

    def _diff_params(self, rule_id: str, text: str) -> dict:
        """把界面提交的参数与规则库基线求差，只保留被改动的项。

        存全量快照会有个隐蔽的坑：日后规则库基线新增或调整某个参数的默认值时，
        这条覆盖会把老值永久钉死，而且没人看得出来。
        """
        import yaml

        spec = self._rulebook().specs.get(rule_id)
        if spec is None:
            raise KeyError(f"规则不存在: {rule_id}")
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("参数必须是「键: 值」形式的映射")
        unknown = set(data) - set(spec.params)
        if unknown:
            raise ValueError(f"未知参数 {sorted(unknown)}；只能调整规则已有的参数，"
                             f"新增参数需要改代码")
        return {k: v for k, v in data.items() if spec.params.get(k) != v}

    def _gold(self, raw: bytes) -> None:
        user = self._require(CAN_ADJUDICATE)
        if user is None:
            return
        form = self._form(raw)
        run_id = form.get("run_id", "")
        gold_path = Path(Config.out_dir) / "goldset.json"
        if form.get("action") == "merge":
            self.store.merge_goldset(run_id, gold_path, operator=user["username"])
        result = self.store.evaluate(run_id, gold_path)
        ctx = self._ctx(user, {"run": run_id})
        self._send(gold.render(self.store, ctx, run_id, result))

    def _model_post(self, path: str, raw: bytes) -> None:
        user = self._require(CAN_ADJUDICATE)
        if user is None:
            return
        location, payload = model.handle_post(self.store, user, path, raw, Config)
        if payload is not None:          # JSON 接口（标注等高频操作）
            self._json(payload)
        else:
            self._redirect(location)


# ---------------------------------------------------------------- 启动

def _bind(host: str, port: int, auto: bool = True) -> ThreadingHTTPServer:
    """绑定端口。

    Windows 上端口被占用或落在系统保留段（Hyper-V/WSL 会预留大量端口）时，
    抛的是 WinError 10013「访问权限不允许」，很容易被误读成权限不足。
    这里给出可读的提示，并默认自动改用一个空闲端口。
    """
    try:
        return ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        if not auto:
            raise
        print(f"[提示] 端口 {port} 不可用（{exc.__class__.__name__}: {exc}）。")
        print("       常见原因：该端口已被其它进程占用，或落在 Windows 保留端口段。")
        print("       可用 netsh interface ipv4 show excludedportrange protocol=tcp 查看保留段。")
        httpd = ThreadingHTTPServer((host, 0), Handler)  # 0 = 由系统分配空闲端口
        print(f"[提示] 已自动改用空闲端口 {httpd.server_address[1]}。")
        return httpd


def serve(db_path: str | Path, host: str = "127.0.0.1", port: int = 8000,
          auto_port: bool = True, archive_root: str = ".", rules_path: str = "",
          out_dir: str = "out", engine_version: str = "", mounts_path: str = "",
          allow_custom_path: bool = False, no_packs: bool = False) -> None:
    Config.archive_root = archive_root
    Config.no_packs = no_packs
    Config.mounts_path = mounts_path
    Config.allow_custom_path = allow_custom_path
    Config.rules_path = rules_path
    Config.out_dir = out_dir
    Config.engine_version = engine_version
    Handler.store = Store(db_path)
    orphans = Handler.store.reconcile_tasks()
    if orphans:
        print(f"[提示] 已把 {orphans} 个上次未结束的任务标记为中断（进度未保存，需重新发起）")
    httpd = _bind(host, port, auto_port)
    actual = httpd.server_address[1]
    print(f"[服务] http://{host}:{actual}    数据库：{db_path}")
    for m in Config.archive_sources():
        print(f"[档案来源] {m.name or '（单根）'}  {m.path}")
    if allow_custom_path:
        print("[警告] 已启用自由路径：管理员可直接填写本机绝对路径发起审核，每次都会写审计日志")
    print("[提示] 默认只监听本机；供科室多人访问时改用 --host 0.0.0.0，并按网络安全要求做访问控制")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[停止] 服务已退出")
    finally:
        httpd.server_close()
