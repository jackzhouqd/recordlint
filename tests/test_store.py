"""存储层与服务端测试。

覆盖受监管制造场景的两个硬要求：版本指纹可复现、人工判定有留痕。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit.store import Store, file_hash


@pytest.fixture
def payload() -> dict:
    return {
        "generated_at": "2026.08.12 10:00:00",
        "rulebook": {"name": "测试规则库", "version": "1.0"},
        "stats": {"pages": 2, "units": 1, "findings": 2, "seconds": 3.5},
        "pages": [
            {"doc_id": "D1", "page_no": 1, "form_type": "质量证明单", "source": "/a/1.jpg",
             "text_lines": 30, "seals": 2, "findings": 1},
            {"doc_id": "D1", "page_no": 2, "form_type": "质量证明单", "source": "/a/2.jpg",
             "text_lines": 28, "seals": 1, "findings": 1},
        ],
        "units": [
            {"unit_id": "D1#U001", "doc_id": "D1", "form_type": "质量证明单", "start_page": 1,
             "end_page": 2, "page_count": 2, "declared_total": 3, "keys": {"零件号": "A"}},
        ],
        "findings": [
            {"doc_id": "D1", "page_no": 1, "rule_id": "A13_copy_stamp_wording", "level": "HIGH",
             "title": "措辞不符", "clause": "A13", "message": "措辞为复印件与原件一致",
             "bbox": [10, 20, 30, 40], "evidence": "复印件与原件一致", "confidence": 0.9},
            {"doc_id": "D1", "page_no": 2, "rule_id": "U01_unit_page_missing", "level": "CRITICAL",
             "title": "单据缺页", "clause": "U01", "message": "声明共3页实际2页",
             "bbox": None, "evidence": "", "confidence": 0.7},
        ],
    }


@pytest.fixture
def store(tmp_path: Path, payload: dict) -> Store:
    s = Store(tmp_path / "t.db")
    s.import_report(payload, run_id="R1", target="/archives", operator="张三",
                    engine_version="1.2.0")
    return s


def test_import_records_run_and_children(store: Store):
    run = store.run("R1")
    assert run["pages"] == 2 and run["units"] == 1
    assert run["operator"] == "张三" and run["engine_version"] == "1.2.0"
    assert len(store.units("R1")) == 1
    assert len(store.findings("R1")) == 2


def test_findings_sorted_by_severity(store: Store):
    rows = store.findings("R1")
    assert rows[0]["level"] == "CRITICAL", "最严重的应排在最前"


def test_filters(store: Store):
    assert len(store.findings("R1", level="HIGH")) == 1
    assert len(store.findings("R1", rule_id="U01_unit_page_missing")) == 1
    assert len(store.findings("R1", doc_id="D1")) == 2
    assert len(store.findings("R1", doc_id="X")) == 0


def test_adjudicate_roundtrip_and_filter(store: Store):
    key = store.findings("R1")[0]["finding_key"]
    store.adjudicate(key, "true", reviewer="李四", note="确认缺页")

    assert len(store.findings("R1", judged="done")) == 1
    assert len(store.findings("R1", judged="todo")) == 1
    row = next(r for r in store.findings("R1") if r["finding_key"] == key)
    assert row["verdict"] == "true" and row["reviewer"] == "李四"

    exported = store.export_adjudications("R1")
    assert exported[0]["verdict"] == "true" and exported[0]["rule_id"]


def test_adjudicate_can_be_revoked(store: Store):
    key = store.findings("R1")[0]["finding_key"]
    store.adjudicate(key, "false")
    store.adjudicate(key, "")
    assert store.export_adjudications("R1") == []


def test_adjudicate_rejects_bad_input(store: Store):
    key = store.findings("R1")[0]["finding_key"]
    with pytest.raises(ValueError):
        store.adjudicate(key, "maybe")
    with pytest.raises(KeyError):
        store.adjudicate("不存在的键", "true")


def test_audit_log_is_appended(store: Store):
    key = store.findings("R1")[0]["finding_key"]
    store.adjudicate(key, "true", reviewer="李四")
    actions = [r["action"] for r in store.audit_log()]
    assert "import" in actions and "adjudicate" in actions


def test_reimport_replaces_same_run(store: Store, payload: dict):
    """同一批次重跑应覆盖而非累加，否则历史统计会翻倍。"""
    store.import_report(payload, run_id="R1", target="/archives")
    assert len(store.findings("R1")) == 2
    assert len(store.runs()) == 1


def test_history_lists_runs_touching_a_doc(store: Store, payload: dict):
    store.import_report(payload, run_id="R2", target="/archives")
    hist = store.history("D1")
    assert {h["run_id"] for h in hist} == {"R1", "R2"}


def test_rules_fingerprint_recorded(tmp_path: Path, payload: dict):
    """规则库指纹是可复现的前提：规则变了，指纹必须跟着变。"""
    rules = tmp_path / "rules.yaml"
    rules.write_text("meta:\n  version: '1.0'\n", encoding="utf-8")
    s = Store(tmp_path / "t2.db")
    s.import_report(payload, run_id="R1", target="/a", rules_path=rules)
    first = s.run("R1")["rules_hash"]

    rules.write_text("meta:\n  version: '1.1'\n", encoding="utf-8")
    s.import_report(payload, run_id="R2", target="/a", rules_path=rules)
    assert first and s.run("R2")["rules_hash"] != first


def test_file_hash_missing_file_is_empty(tmp_path: Path):
    assert file_hash(tmp_path / "nope.yaml") == ""


def test_page_source_lookup(store: Store):
    row = store.page_source("R1", "D1", 2)
    assert row and row["source"] == "/a/2.jpg", "证据图按需裁剪依赖来源路径"
