"""界面回归测试：锁住几个已经踩过的坑。

每一条对应一个真实故障，注释里写清「错了会怎样」，避免日后被"顺手优化"掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qaudit.findings import RuleBook  # noqa: E402
from qaudit.store import Store  # noqa: E402
from qaudit.web.render import layout  # noqa: E402

RULES = ROOT / "config" / "rules.yaml"


# ---------------------------------------------------------------- 脚本加载顺序

def test_app_js_is_not_deferred():
    """app.js 带 defer 会在文档解析完成后才执行，而各页面的内联脚本在 body 末尾、
    解析过程中就执行——那时 window.QA 还不存在，QA.on(...) 直接抛 ReferenceError，
    复核导航、标注预览、总览图表、规则搜索会一起失效，且页面看上去完全正常。"""
    html = layout("t", "<p>x</p>", {"username": "u", "display_name": "U", "role": "admin"},
                  scripts="<script>QA.on(function(){});</script>").decode("utf-8")
    assert '<script src="/static/app.js"></script>' in html, "app.js 不得带 defer/async"

    # 只在 <body> 之后找内联脚本：<head> 里的说明性注释也含 QA.on 字样
    body_at = html.index("<body")
    app_pos = html.index('src="/static/app.js"')
    inline_pos = html.index("QA.on(", body_at)
    assert app_pos < body_at < inline_pos, "app.js 必须出现在任何调用 QA 的内联脚本之前"


def test_page_scripts_land_after_content():
    html = layout("t", "<main>正文</main>", {"username": "u", "display_name": "U",
                                             "role": "admin"},
                  scripts="<script>window.__probe=1;</script>").decode("utf-8")
    assert html.index("正文") < html.index("__probe")


# ---------------------------------------------------------------- 卡死的任务

@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "t.db")


def test_orphan_running_tasks_are_reconciled_on_startup(store):
    """任务状态在数据库，执行线程在进程。进程被杀之后没人改 running，
    界面上会永远挂着一个转圈的任务，且「有任务在跑」的互斥会挡住所有新任务。"""
    store.create_task("T1", "audit", "/a", "admin1", total=100)
    store.create_task("T2", "train", "/b", "admin1")
    store.finish_task("T2", "done", "完成")

    assert store.running_task() is not None
    assert store.reconcile_tasks() == 1
    assert store.running_task() is None
    assert store.task("T1")["status"] == "failed"
    assert "中断" in store.task("T1")["message"]
    assert store.task("T2")["status"] == "done", "已结束的任务不得被改动"


def test_reconcile_is_idempotent(store):
    store.create_task("T1", "audit", "/a", "admin1")
    store.reconcile_tasks()
    assert store.reconcile_tasks() == 0


def test_cancel_task_marks_but_does_not_touch_finished_ones(store):
    store.create_task("T1", "audit", "/a", "admin1")
    store.cancel_task("T1", operator="admin1")
    assert store.task("T1")["status"] == "canceled"
    assert "task_cancel" in [r["action"] for r in store.audit_log()]

    with pytest.raises(ValueError, match="无需取消"):
        store.cancel_task("T1", operator="admin1")
    with pytest.raises(KeyError):
        store.cancel_task("不存在", operator="admin1")


# ---------------------------------------------------------------- 规则参数覆盖

def test_param_override_merges_instead_of_replacing(store):
    """只存被改动的参数项。存全量快照的话，日后规则库新增参数默认值时，
    这条覆盖会把老值永久钉死，而且没人看得出来。"""
    base = RuleBook.load(RULES)
    spec = base.specs["B04_blank_cell_no_slash"]
    assert "group_as_column" in spec.params and "field_whitelist" in spec.params

    store.set_rule_override("B04_blank_cell_no_slash", params={"group_as_column": 5},
                            operator="admin1", reason="现场口径")
    merged = base.apply_overrides(store.rule_overrides()).get("B04_blank_cell_no_slash")
    assert merged.params["group_as_column"] == 5, "改动项要生效"
    assert merged.params["field_whitelist"] == spec.params["field_whitelist"], \
        "未改动的参数必须保留基线值"


def test_param_override_can_be_cleared(store):
    base = RuleBook.load(RULES)
    store.set_rule_override("B04_blank_cell_no_slash", params={"group_as_column": 9},
                            operator="admin1")
    store.set_rule_override("B04_blank_cell_no_slash", params={}, operator="admin1")
    merged = base.apply_overrides(store.rule_overrides()).get("B04_blank_cell_no_slash")
    assert merged.params["group_as_column"] == base.specs["B04_blank_cell_no_slash"].params[
        "group_as_column"]


def test_broken_param_json_does_not_break_the_audit(store):
    """一条规则的参数写坏了，不该让整批 1453 页跑不起来。"""
    store.set_rule_override("B04_blank_cell_no_slash", params={"group_as_column": 5},
                            operator="admin1")
    with store.conn:
        store.conn.execute("UPDATE rule_override SET params='{坏掉的 JSON' WHERE rule_id=?",
                           ("B04_blank_cell_no_slash",))
    base = RuleBook.load(RULES)
    merged = base.apply_overrides(store.rule_overrides()).get("B04_blank_cell_no_slash")
    assert merged.params == base.specs["B04_blank_cell_no_slash"].params


def test_param_override_survives_enable_and_level_changes(store):
    """改级别不该把参数覆盖冲掉——两者是独立的调整动作。"""
    store.set_rule_override("B04_blank_cell_no_slash", params={"group_as_column": 5},
                            operator="admin1")
    store.set_rule_override("B04_blank_cell_no_slash", enabled=False, level="LOW",
                            operator="admin1")
    ov = store.rule_overrides()["B04_blank_cell_no_slash"]
    assert '"group_as_column": 5' in ov["params"]
    assert ov["enabled"] == 0 and ov["level"] == "LOW"


def test_migration_adds_params_column_to_old_db(tmp_path: Path):
    """老库的 rule_override 没有 params 列，CREATE TABLE IF NOT EXISTS 不会补列。"""
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE rule_override (rule_id TEXT PRIMARY KEY, enabled INTEGER,"
                " level TEXT, changed_by TEXT, changed_at TEXT, reason TEXT DEFAULT '')")
    con.execute("INSERT INTO rule_override VALUES ('A01_date_format',0,'LOW','u','t','旧数据')")
    con.commit()
    con.close()

    store = Store(path)
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(rule_override)")}
    assert "params" in cols
    assert store.rule_overrides()["A01_date_format"]["reason"] == "旧数据", "老数据不得丢失"


# ---------------------------------------------------------------- 单据页集合

UNIT_PAYLOAD = {
    "generated_at": "2026.08.12 10:00:00",
    "rulebook": {"name": "测试", "version": "1.0"},
    "stats": {"pages": 3, "units": 2, "findings": 0},
    "pages": [{"doc_id": "D1", "page_no": n, "form_type": "质量证明单", "source": "",
               "text_lines": 5, "seals": 1, "findings": 0} for n in (1, 2, 3)],
    # 交错装订：U1 占 p1 和 p3，中间夹着 U2 的 p2
    "units": [
        {"unit_id": "D1#U1", "doc_id": "D1", "form_type": "质量证明单", "start_page": 1,
         "end_page": 3, "page_count": 2, "declared_total": 2, "keys": {}, "pages": [1, 3]},
        {"unit_id": "D1#U2", "doc_id": "D1", "form_type": "质量证明单", "start_page": 2,
         "end_page": 2, "page_count": 1, "declared_total": 1, "keys": {}, "pages": [2]},
    ],
    "findings": [],
}


def test_unit_page_set_is_stored_not_inferred_from_range(store):
    """交错装订时同一份单据的页会被别的单据隔开。只存首末页号的话，
    单据视图会把邻居的页列进来，「声明 2 页实际 3 页」的假缺页也会跟着冒出来。"""
    from qaudit.web.views.archive import unit_page_nos

    store.import_report(UNIT_PAYLOAD, run_id="R1", target="/a")
    units = {u["unit_id"]: u for u in store.units("R1")}
    assert unit_page_nos(units["D1#U1"]) == [1, 3], "必须是实际页集合，不是 1~3 区间"
    assert unit_page_nos(units["D1#U2"]) == [2]


def test_unit_page_set_falls_back_to_range_for_old_runs(store):
    """老批次的 unit 行没有 pages 列，退回区间，不能直接崩。"""
    from qaudit.web.views.archive import unit_page_nos

    store.import_report(UNIT_PAYLOAD, run_id="R1", target="/a")
    with store.conn:
        store.conn.execute("UPDATE unit SET pages='' WHERE unit_id='D1#U1'")
    unit = next(u for u in store.units("R1") if u["unit_id"] == "D1#U1")
    assert unit_page_nos(unit) == [1, 2, 3]
