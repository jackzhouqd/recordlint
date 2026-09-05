"""界面操作的端到端测试：发起审核、规则维护、台账导出、金标准评测。

交付标准是「用户不碰命令行」，所以这些能力必须在 Web 上可用且受权限约束。
"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit import server as srv
from qaudit.store import Store

PWD = "Passw0rd!23"
PROJ = Path(__file__).resolve().parent.parent
PAYLOAD = {
    "generated_at": "2026.08.12 10:00:00",
    "rulebook": {"name": "测试", "version": "1.0"},
    "stats": {"pages": 1, "units": 1, "findings": 2},
    "pages": [{"doc_id": "D1", "page_no": 1, "form_type": "质量证明单", "source": "",
               "text_lines": 10, "seals": 1, "findings": 2}],
    "units": [{"unit_id": "D1#U001", "doc_id": "D1", "form_type": "质量证明单", "start_page": 1,
               "end_page": 1, "page_count": 1, "declared_total": None, "keys": {}}],
    "findings": [
        {"doc_id": "D1", "page_no": 1, "rule_id": "A13_copy_stamp_wording", "level": "HIGH",
         "title": "措辞不符", "clause": "A13", "message": "措辞为复印件与原件一致",
         "bbox": None, "evidence": "", "confidence": 0.9},
        {"doc_id": "D1", "page_no": 1, "rule_id": "B05_copy_stamp_position", "level": "MEDIUM",
         "title": "位置不符", "clause": "A13", "message": "盖在左下角",
         "bbox": None, "evidence": "", "confidence": 0.75},
    ],
}


@pytest.fixture
def web(tmp_path: Path):
    db = tmp_path / "web.db"
    store = Store(db)
    store.import_report(PAYLOAD, run_id="R1", target="/a")
    for name, role in (("admin1", "admin"), ("review1", "reviewer"), ("view1", "viewer")):
        store.create_user(name, PWD, role=role, display_name=f"{role}用户")

    srv.Config.archive_root = str(tmp_path / "archives")
    (tmp_path / "archives").mkdir()
    srv.Config.rules_path = str(PROJ / "config" / "rules.yaml")
    srv.Config.out_dir = str(tmp_path / "runs")
    srv.Config.mounts_path = ""            # 类属性是全局的，逐个用例复位
    srv.Config.allow_custom_path = False
    srv.Handler.store = Store(db)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class Client:
        port = httpd.server_address[1]
        db_store = store
        tmp = tmp_path

        def req(self, method, path, body=None, cookie="",
                ctype="application/x-www-form-urlencoded"):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
            headers = {"Content-Type": ctype}
            if cookie:
                headers["Cookie"] = cookie
            c.request(method, path, body=body, headers=headers)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()

        def login(self, user):
            st, h, _ = self.req("POST", "/login",
                                urllib.parse.urlencode({"username": user, "password": PWD}))
            return h.get("Set-Cookie", "").split(";")[0]

    yield Client()
    httpd.shutdown()
    httpd.server_close()


# ---------------------------------------------------------------- 页面可达
@pytest.mark.parametrize("path", ["/tasks", "/rules", "/gold", "/"])
def test_pages_render_for_logged_in_user(web, path):
    cookie = web.login("admin1")
    status, _, body = web.req("GET", path, cookie=cookie)
    assert status == 200 and len(body) > 200


@pytest.mark.parametrize("path", ["/tasks", "/rules", "/gold"])
def test_pages_require_login(web, path):
    assert web.req("GET", path)[0] == 302


# ---------------------------------------------------------------- 发起审核
def test_viewer_cannot_start_audit(web):
    cookie = web.login("view1")
    status, _, _ = web.req("POST", "/tasks", urllib.parse.urlencode({"rel": "x"}), cookie=cookie)
    assert status == 403


def test_audit_rejects_path_outside_archive_root(web):
    """界面只能选根目录下的档案，路径穿越必须被挡住。"""
    from qaudit import jobs

    with pytest.raises(ValueError):
        jobs.resolve_target(web.tmp / "archives", "../../etc")


# ---------------------------------------------------------------- 档案来源与下钻
def _deep_archive(web) -> str:
    """造出真实层级，返回「20」那一层的 rel。

    rel 的第一段永远是档案来源名——未配置挂载点时回退成单根，来源名即根目录名。
    """
    deep = web.tmp / "archives" / "records" / "20" / "batch-0001"
    deep.mkdir(parents=True)
    (deep / "p1.jpg").write_bytes(b"x")
    return "archives/records/20"


def test_task_page_can_drill_into_subdirectory(web):
    """真实层级是 records/20/batch-0001，界面必须逐层进得去。"""
    rel = _deep_archive(web)
    cookie = web.login("admin1")

    _, _, body = web.req("GET", "/tasks?rel=" + urllib.parse.quote(rel), cookie=cookie)

    assert "batch-0001" in body.decode("utf-8")


def test_task_page_shows_breadcrumbs(web):
    rel = _deep_archive(web)
    cookie = web.login("admin1")

    _, _, body = web.req("GET", "/tasks?rel=" + urllib.parse.quote(rel), cookie=cookie)
    page = body.decode("utf-8")

    assert "records" in page and "全部来源" in page


def test_traversal_post_creates_no_task(web):
    """越界路径不只是报错，更不能落下任何任务记录。"""
    cookie = web.login("admin1")
    before = len(web.db_store.tasks())

    status, _, _ = web.req("POST", "/tasks",
                           urllib.parse.urlencode({"rel": "../../etc"}), cookie=cookie)

    assert status == 302 and len(web.db_store.tasks()) == before


def test_admin_can_add_archive_mount_and_change_is_audited(web):
    """新增来源不需要重启服务，但必须留痕。"""
    from qaudit import jobs

    cfg = web.tmp / "archives.yaml"
    srv.Config.mounts_path = str(cfg)
    src = web.tmp / "网络盘"
    src.mkdir()
    cookie = web.login("admin1")

    status, _, _ = web.req("POST", "/admin/mounts", urllib.parse.urlencode(
        {"action": "add", "name": "网络盘", "path": str(src), "note": "共享目录"}),
        cookie=cookie)

    assert status == 302
    assert "网络盘" in [m.name for m in jobs.load_mounts(cfg)]
    assert "mount_change" in [r["action"] for r in web.db_store.audit_log()]


def test_admin_can_remove_archive_mount(web):
    from qaudit import jobs

    cfg = web.tmp / "archives.yaml"
    srv.Config.mounts_path = str(cfg)
    jobs.save_mounts(cfg, [jobs.Mount(name="旧来源", path=web.tmp / "archives")])
    cookie = web.login("admin1")

    web.req("POST", "/admin/mounts",
            urllib.parse.urlencode({"action": "remove", "name": "旧来源"}), cookie=cookie)

    assert "旧来源" not in [m.name for m in jobs.load_mounts(cfg)]


def test_reviewer_cannot_change_archive_mounts(web):
    cookie = web.login("review1")

    status, _, _ = web.req("POST", "/admin/mounts", urllib.parse.urlencode(
        {"action": "add", "name": "x", "path": str(web.tmp)}), cookie=cookie)

    assert status == 403


def test_dir_browser_requires_admin(web):
    """服务端目录选择器能看到整台机器的目录结构，必须锁死在管理员。"""
    assert web.req("GET", "/admin/mounts/browse", cookie=web.login("admin1"))[0] == 200
    assert web.req("GET", "/admin/mounts/browse", cookie=web.login("review1"))[0] == 403
    assert web.req("GET", "/admin/mounts/browse")[0] == 302


def test_dir_browser_shows_subdirectories(web):
    (web.tmp / "archives" / "待选目录").mkdir()
    cookie = web.login("admin1")

    _, _, body = web.req("GET", "/admin/mounts/browse?path=" +
                         urllib.parse.quote(str(web.tmp / "archives")), cookie=cookie)

    assert "待选目录" in body.decode("utf-8")


def test_mount_page_requires_admin(web):
    assert web.req("GET", "/admin/mounts", cookie=web.login("admin1"))[0] == 200
    assert web.req("GET", "/admin/mounts", cookie=web.login("review1"))[0] == 403


def test_custom_path_rejected_when_switch_is_off(web):
    """自由路径是逃生通道，默认关闭；关闭时提交绝对路径不得建任务。"""
    cookie = web.login("admin1")
    before = len(web.db_store.tasks())

    status, _, _ = web.req("POST", "/tasks", urllib.parse.urlencode(
        {"custom_path": str(web.tmp / "archives")}), cookie=cookie)

    assert status == 302 and len(web.db_store.tasks()) == before


# ---------------------------------------------------------------- 规则维护
def test_admin_can_toggle_rule_and_record_reason(web):
    cookie = web.login("admin1")
    status, _, _ = web.req("POST", "/rules", urllib.parse.urlencode({
        "rule_id": "A07_square_cubic_unit", "enabled": "0", "level": "LOW",
        "reason": "口径待确认", "action": "save"}), cookie=cookie)
    assert status == 302
    ov = web.db_store.rule_overrides()["A07_square_cubic_unit"]
    assert ov["enabled"] == 0 and ov["level"] == "LOW"
    assert ov["changed_by"] == "admin1" and ov["reason"] == "口径待确认"


def test_rule_change_is_audited(web):
    cookie = web.login("admin1")
    web.req("POST", "/rules", urllib.parse.urlencode({
        "rule_id": "A07_square_cubic_unit", "enabled": "0", "level": "LOW",
        "reason": "口径待确认", "action": "save"}), cookie=cookie)
    assert "rule_change" in [r["action"] for r in web.db_store.audit_log()]


def test_rule_can_be_reset_to_baseline(web):
    cookie = web.login("admin1")
    web.req("POST", "/rules", urllib.parse.urlencode({
        "rule_id": "A07_square_cubic_unit", "enabled": "0", "action": "save"}), cookie=cookie)
    web.req("POST", "/rules", urllib.parse.urlencode({
        "rule_id": "A07_square_cubic_unit", "action": "reset"}), cookie=cookie)
    assert "A07_square_cubic_unit" not in web.db_store.rule_overrides()


def test_reviewer_cannot_change_rules(web):
    cookie = web.login("review1")
    status, _, _ = web.req("POST", "/rules", urllib.parse.urlencode({
        "rule_id": "A07_square_cubic_unit", "enabled": "0", "action": "save"}), cookie=cookie)
    assert status == 403


def test_rule_override_applies_to_next_audit(web):
    """界面停用的规则，下一次审核必须真的不跑。"""
    from qaudit.findings import RuleBook

    web.db_store.set_rule_override("A07_square_cubic_unit", enabled=False, operator="admin1")
    book = RuleBook.load(srv.Config.rules_path).apply_overrides(web.db_store.rule_overrides())
    assert book.get("A07_square_cubic_unit") is None


# ---------------------------------------------------------------- 台账导出
def test_export_csv_has_bom_and_all_rows(web):
    cookie = web.login("admin1")
    status, headers, body = web.req("GET", "/export?run=R1", cookie=cookie)
    assert status == 200
    assert "attachment" in headers.get("Content-Disposition", "")
    assert body.startswith(b"\xef\xbb\xbf"), "需带 BOM，Excel 打开才不乱码"
    lines = body.decode("utf-8-sig").splitlines()
    assert len(lines) == 3 and lines[0].startswith("序号,档案,页码")


def test_export_respects_filters(web):
    cookie = web.login("admin1")
    _, _, body = web.req("GET", "/export?run=R1&level=HIGH", cookie=cookie)
    assert len(body.decode("utf-8-sig").splitlines()) == 2  # 表头 + 1 条


def test_export_escapes_commas(web):
    """疑点说明里含逗号时不得串列。"""
    web.db_store.conn.execute(
        "UPDATE finding SET message='含,逗号,的说明' WHERE rule_id='A13_copy_stamp_wording'")
    web.db_store.conn.commit()
    cookie = web.login("admin1")
    _, _, body = web.req("GET", "/export?run=R1", cookie=cookie)
    line = [l for l in body.decode("utf-8-sig").splitlines() if "逗号" in l][0]
    assert '"含,逗号,的说明"' in line


# ---------------------------------------------------------------- 金标准与评测
def test_merge_goldset_and_evaluate(web):
    cookie = web.login("admin1")
    key = web.db_store.findings("R1")[0]["finding_key"]
    web.db_store.adjudicate(key, "true", reviewer="admin1")

    status, _, body = web.req("POST", "/gold", urllib.parse.urlencode(
        {"run_id": "R1", "action": "merge"}), cookie=cookie)
    page = body.decode("utf-8")
    assert status == 200 and "评测结果" in page and "召回率" in page

    gold = json.loads((Path(srv.Config.out_dir) / "goldset.json").read_text(encoding="utf-8"))
    assert len(gold["findings"]) == 1


def test_evaluate_counts_only_judged(web):
    cookie = web.login("admin1")
    rows = web.db_store.findings("R1")
    web.db_store.adjudicate(rows[0]["finding_key"], "true", reviewer="admin1")
    web.db_store.adjudicate(rows[1]["finding_key"], "false", reviewer="admin1")
    web.req("POST", "/gold", urllib.parse.urlencode({"run_id": "R1", "action": "merge"}),
            cookie=cookie)

    result = web.db_store.evaluate("R1", Path(srv.Config.out_dir) / "goldset.json")
    assert result["tp"] == 1 and result["fp"] == 1 and result["fn"] == 0
    assert result["recall"] == 1.0


# ---------------------------------------------------------------- 批量判定
def test_bulk_adjudicate_writes_every_verdict_and_binds_reviewer(web):
    """系统性问题（如 622 页缺版次）逐条点击是纯粹的浪费，但批量不能牺牲可追溯性：
    判定人仍取自登录会话，且每条单独进审计日志。"""
    cookie = web.login("review1")
    keys = [f["finding_key"] for f in web.db_store.findings("R1")]
    status, _, body = web.req(
        "POST", "/api/adjudicate/bulk",
        json.dumps({"finding_keys": keys, "verdict": "false", "note": "整列空白，现场口径认可"}),
        cookie=cookie, ctype="application/json")
    assert status == 200 and json.loads(body)["count"] == len(keys)

    rows = web.db_store.findings("R1")
    assert all(r["verdict"] == "false" for r in rows)
    assert all(r["reviewer"] == "review1" for r in rows), "判定人必须来自会话，不接受前端传入"
    logged = [r for r in web.db_store.audit_log() if r["action"] == "adjudicate"]
    assert len(logged) >= len(keys), "批量也要逐条留痕"


def test_viewer_cannot_bulk_adjudicate(web):
    cookie = web.login("view1")
    keys = [f["finding_key"] for f in web.db_store.findings("R1")]
    status, _, body = web.req("POST", "/api/adjudicate/bulk",
                              json.dumps({"finding_keys": keys, "verdict": "true"}),
                              cookie=cookie, ctype="application/json")
    assert status == 403 and "无判定权限" in json.loads(body)["error"]
    assert all(r["verdict"] is None for r in web.db_store.findings("R1"))


def test_bulk_adjudicate_requires_login(web):
    status, _, _ = web.req("POST", "/api/adjudicate/bulk",
                           json.dumps({"finding_keys": ["x"], "verdict": "true"}),
                           ctype="application/json")
    assert status == 401


def test_bulk_adjudicate_rejects_oversized_batch(web):
    cookie = web.login("admin1")
    status, _, body = web.req("POST", "/api/adjudicate/bulk",
                              json.dumps({"finding_keys": ["k"] * 5001, "verdict": "false"}),
                              cookie=cookie, ctype="application/json")
    assert status == 400 and "5000" in json.loads(body)["error"]


# ---------------------------------------------------------------- 新界面骨架
@pytest.mark.parametrize("path", ["/", "/review", "/archive", "/runs", "/model",
                                  "/model/samples", "/model/label", "/model/train",
                                  "/model/versions", "/system", "/log"])
def test_new_pages_render(web, path):
    cookie = web.login("admin1")
    status, _, body = web.req("GET", path, cookie=cookie)
    assert status == 200 and len(body) > 200


def test_static_assets_are_served_and_vendored(web):
    for path in ("/static/app.css", "/static/app.js",
                 "/static/vendor/htmx.min.js", "/static/vendor/chart.umd.min.js"):
        status, _, body = web.req("GET", path)
        assert status == 200 and len(body) > 500, f"{path} 未随包分发"


def test_static_rejects_path_traversal(web):
    status, _, _ = web.req("GET", "/static/../../store.py")
    assert status == 404


def test_legacy_run_url_still_renders(web):
    """/run/<id> 是旧版链接，外部文档里引用过，必须继续直接渲染。"""
    cookie = web.login("admin1")
    status, _, body = web.req("GET", "/run/R1", cookie=cookie)
    assert status == 200 and "R1" in body.decode("utf-8")
