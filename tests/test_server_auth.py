"""服务端认证与权限的端到端测试。

锁住三条底线：
- 未登录拿不到任何业务数据；
- 查阅员不能判定；
- 判定人取自登录会话，不接受前端传入（受监管制造场景必须能追溯到人）。
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

from qaudit.server import Handler
from qaudit.store import Store

PWD = "Passw0rd!23"
PAYLOAD = {
    "generated_at": "2026.08.12 10:00:00",
    "rulebook": {"name": "测试", "version": "1.0"},
    "stats": {"pages": 1, "units": 1, "findings": 1},
    "pages": [{"doc_id": "D1", "page_no": 1, "form_type": "质量证明单", "source": "",
               "text_lines": 10, "seals": 1, "findings": 1}],
    "units": [{"unit_id": "D1#U001", "doc_id": "D1", "form_type": "质量证明单", "start_page": 1,
               "end_page": 1, "page_count": 1, "declared_total": None, "keys": {}}],
    "findings": [{"doc_id": "D1", "page_no": 1, "rule_id": "A13_copy_stamp_wording", "level": "HIGH",
                  "title": "措辞不符", "clause": "A13", "message": "措辞不符",
                  "bbox": None, "evidence": "", "confidence": 0.9}],
}


@pytest.fixture
def service(tmp_path: Path):
    db = tmp_path / "srv.db"
    store = Store(db)
    store.import_report(PAYLOAD, run_id="R1", target="/a")
    for name, role in (("admin1", "admin"), ("review1", "reviewer"), ("view1", "viewer")):
        store.create_user(name, PWD, role=role, display_name=f"{role}用户")
    key = store.findings("R1")[0]["finding_key"]

    Handler.store = Store(db)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class Client:
        port = httpd.server_address[1]
        finding_key = key
        db_store = store

        def req(self, method, path, body=None, cookie="", ctype="application/x-www-form-urlencoded"):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            headers = {"Content-Type": ctype}
            if cookie:
                headers["Cookie"] = cookie
            c.request(method, path, body=body, headers=headers)
            r = c.getresponse()
            return r.status, (r.getheader("Set-Cookie") or ""), r.read()

        def login(self, user, pwd=PWD):
            st, cookie, _ = self.req(
                "POST", "/login", urllib.parse.urlencode({"username": user, "password": pwd}))
            return cookie.split(";")[0] if cookie else ""

        def judge(self, key, verdict, cookie):
            return self.req("POST", "/api/adjudicate",
                            json.dumps({"finding_key": key, "verdict": verdict}),
                            cookie=cookie, ctype="application/json")

    yield Client()
    httpd.shutdown()
    httpd.server_close()


def test_anonymous_is_redirected(service):
    assert service.req("GET", "/")[0] == 302
    assert service.req("GET", "/run/R1")[0] == 302
    assert service.req("GET", "/login")[0] == 200


def test_anonymous_cannot_adjudicate(service):
    status, _, body = service.judge(service.finding_key, "true", cookie="")
    assert status == 401 and json.loads(body)["ok"] is False


def test_wrong_password_grants_no_session(service):
    assert service.login("review1", "wrong-password") == ""


def test_reviewer_can_login_and_judge(service):
    cookie = service.login("review1")
    assert cookie
    assert service.req("GET", "/", cookie=cookie)[0] == 200
    status, _, body = service.judge(service.finding_key, "true", cookie)
    assert status == 200 and json.loads(body)["ok"] is True


def test_reviewer_cannot_access_admin(service):
    cookie = service.login("review1")
    assert service.req("GET", "/admin/users", cookie=cookie)[0] == 403


def test_viewer_cannot_adjudicate(service):
    cookie = service.login("view1")
    status, _, body = service.judge(service.finding_key, "false", cookie)
    assert status == 403 and "无判定权限" in json.loads(body)["error"]


def test_viewer_sees_readonly_notice(service):
    cookie = service.login("view1")
    _, _, page = service.req("GET", "/run/R1", cookie=cookie)
    assert "只读账号" in page.decode("utf-8")


def test_admin_can_manage_users(service):
    cookie = service.login("admin1")
    status, _, page = service.req("GET", "/admin/users", cookie=cookie)
    assert status == 200 and "用户管理" in page.decode("utf-8")


def test_reviewer_identity_comes_from_session(service):
    """前端即便伪造 reviewer 字段也无效——判定人只认登录账号。"""
    cookie = service.login("review1")
    service.req("POST", "/api/adjudicate",
                json.dumps({"finding_key": service.finding_key, "verdict": "true",
                            "reviewer": "冒充的人"}),
                cookie=cookie, ctype="application/json")
    row = service.db_store.conn.execute(
        "SELECT reviewer FROM adjudication WHERE finding_key=?", (service.finding_key,)).fetchone()
    assert row["reviewer"] == "review1"


def test_logout_invalidates_session(service):
    cookie = service.login("review1")
    assert service.req("GET", "/", cookie=cookie)[0] == 200
    service.req("GET", "/logout", cookie=cookie)
    assert service.req("GET", "/", cookie=cookie)[0] == 302


def test_disabled_user_session_dies_immediately(service):
    cookie = service.login("review1")
    service.db_store.set_user_state("review1", enabled=False, operator="admin1")
    assert service.req("GET", "/", cookie=cookie)[0] == 302
