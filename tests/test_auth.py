"""认证与权限测试。

受监管制造场景的要求：判定必须能追溯到具体人员，口令不得明文落盘，
离线环境也要防口令穷举，停用账号即时失效。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit.store import CAN_ADJUDICATE, CAN_ADMIN, MAX_FAILED, ROLES, Store

PWD = "Passw0rd!23"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "auth.db")
    s.create_user("zhangsan", PWD, role="reviewer", display_name="张三", operator="setup")
    return s


def test_roles_defined():
    assert set(ROLES) == {"admin", "reviewer", "viewer"}
    assert "viewer" not in CAN_ADJUDICATE, "查阅员不得判定"
    assert CAN_ADMIN == {"admin"}


def test_authenticate_success_and_failure(store: Store):
    assert store.authenticate("zhangsan", PWD)["role"] == "reviewer"
    assert store.authenticate("zhangsan", "wrong-password") is None
    assert store.authenticate("nobody", "whatever1") is None


def test_password_is_not_stored_in_plaintext(tmp_path: Path):
    db = tmp_path / "p.db"
    s = Store(db)
    s.create_user("u1", "SecretPass123", role="viewer")
    blob = db.read_bytes()
    assert b"SecretPass123" not in blob, "口令绝不能明文落盘"


def test_same_password_gets_different_hash(store: Store):
    """每个账号独立盐值，相同口令的散列必须不同。"""
    store.create_user("lisi", PWD, role="reviewer")
    rows = {r["username"]: r for r in store.conn.execute("SELECT username, salt, pwd_hash FROM user")}
    assert rows["zhangsan"]["salt"] != rows["lisi"]["salt"]
    assert rows["zhangsan"]["pwd_hash"] != rows["lisi"]["pwd_hash"]


def test_weak_password_rejected(store: Store):
    with pytest.raises(ValueError):
        store.create_user("weak", "short", role="viewer")


def test_invalid_role_rejected(store: Store):
    with pytest.raises(ValueError):
        store.create_user("bad", PWD, role="superuser")


def test_lockout_after_repeated_failures(store: Store):
    for _ in range(MAX_FAILED):
        store.authenticate("zhangsan", "bad-password")
    with pytest.raises(PermissionError):
        store.authenticate("zhangsan", PWD)  # 锁定期内即便口令正确也拒绝


def test_password_reset_clears_lock_and_sessions(store: Store):
    token = store.create_session("zhangsan")
    for _ in range(MAX_FAILED):
        store.authenticate("zhangsan", "bad-password")
    store.set_password("zhangsan", "BrandNewPass1", operator="admin")

    assert store.authenticate("zhangsan", "BrandNewPass1"), "改密应解除锁定"
    assert store.session_user(token) is None, "改密应使既有会话失效"


def test_session_lifecycle(store: Store):
    token = store.create_session("zhangsan", client="127.0.0.1")
    user = store.session_user(token)
    assert user["username"] == "zhangsan" and user["display_name"] == "张三"
    store.delete_session(token, operator="zhangsan")
    assert store.session_user(token) is None


def test_disabled_user_cannot_login_and_is_kicked(store: Store):
    token = store.create_session("zhangsan")
    store.set_user_state("zhangsan", enabled=False, operator="admin")
    assert store.authenticate("zhangsan", PWD) is None
    assert store.session_user(token) is None, "停用应即时踢下线"


def test_role_change_takes_effect(store: Store):
    store.set_user_state("zhangsan", role="admin", operator="admin")
    assert store.authenticate("zhangsan", PWD)["role"] == "admin"


def test_expired_session_is_rejected(store: Store):
    token = store.create_session("zhangsan")
    store.conn.execute("UPDATE session SET expires_at='2000.01.01 00:00:00' WHERE token=?", (token,))
    store.conn.commit()
    assert store.session_user(token) is None


def test_bad_token_rejected(store: Store):
    assert store.session_user("") is None
    assert store.session_user("not-a-real-token") is None


def test_user_actions_are_audited(store: Store):
    store.create_session("zhangsan")
    store.authenticate("zhangsan", "bad-password")
    actions = [r["action"] for r in store.audit_log()]
    assert "user_create" in actions and "login" in actions and "login_failed" in actions


def test_audit_log_never_records_password(store: Store):
    store.set_password("zhangsan", "AnotherSecret9", operator="admin")
    blob = " ".join(str(r) for r in store.audit_log())
    assert "AnotherSecret9" not in blob
