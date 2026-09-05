"""人工复核回填闭环测试：报告导出判定 → 合并金标准 → 评测。

关键行为锁定：
- 判假必须进入负样本，并在评测中记为误报；
- 未经人工判定的系统输出不得计入误报（人工往往只判了一页里的部分疑点）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qaudit.cli import main


def write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def review_file(tmp_path: Path) -> Path:
    return write(
        tmp_path / "review.json",
        {
            "reviewer": "质量部-张三",
            "adjudications": [
                {"verdict": "true", "doc_id": "D1", "page_no": 2, "rule_id": "A13_copy_stamp_wording"},
                {"verdict": "false", "doc_id": "D1", "page_no": 1, "rule_id": "A09_page_number"},
                {"verdict": "unsure", "doc_id": "D1", "page_no": 1, "rule_id": "B04_blank_cell_no_slash"},
            ],
        },
    )


def test_gold_merge_splits_verdicts(tmp_path: Path, review_file: Path):
    gold = tmp_path / "goldset.json"
    assert main(["gold", "--review", str(review_file), "--gold", str(gold)]) == 0

    data = json.loads(gold.read_text(encoding="utf-8"))
    assert [(f["doc_id"], f["page_no"], f["rule_id"]) for f in data["findings"]] == [
        ("D1", 2, "A13_copy_stamp_wording")
    ]
    assert data["false_positives"] == [
        {"doc_id": "D1", "page_no": 1, "rule_id": "A09_page_number"}
    ]
    assert data["unsure"][0]["rule_id"] == "B04_blank_cell_no_slash"
    assert data["reviewed_pages"] == [["D1", 1], ["D1", 2]]
    assert data["findings"][0]["reviewer"] == "质量部-张三"


def test_gold_merge_is_idempotent(tmp_path: Path, review_file: Path):
    gold = tmp_path / "goldset.json"
    main(["gold", "--review", str(review_file), "--gold", str(gold)])
    main(["gold", "--review", str(review_file), "--gold", str(gold)])
    data = json.loads(gold.read_text(encoding="utf-8"))
    assert len(data["findings"]) == 1
    assert len(data["false_positives"]) == 1


def test_later_verdict_overrides_earlier(tmp_path: Path, review_file: Path):
    """复议：同一条疑点后来改判，负样本要相应移除。"""
    gold = tmp_path / "goldset.json"
    main(["gold", "--review", str(review_file), "--gold", str(gold)])
    second = write(
        tmp_path / "review2.json",
        {"adjudications": [{"verdict": "true", "doc_id": "D1", "page_no": 1, "rule_id": "A09_page_number"}]},
    )
    main(["gold", "--review", str(second), "--gold", str(gold)])
    data = json.loads(gold.read_text(encoding="utf-8"))
    assert data["false_positives"] == []
    assert len(data["findings"]) == 2


def test_eval_counts_only_judged(tmp_path: Path, review_file: Path, capsys):
    gold = tmp_path / "goldset.json"
    main(["gold", "--review", str(review_file), "--gold", str(gold)])

    pred = write(
        tmp_path / "findings.json",
        {
            "findings": [
                {"doc_id": "D1", "page_no": 2, "rule_id": "A13_copy_stamp_wording"},  # 判真 → TP
                {"doc_id": "D1", "page_no": 1, "rule_id": "A09_page_number"},          # 判假 → FP
                {"doc_id": "D1", "page_no": 1, "rule_id": "B04_blank_cell_no_slash"},  # 存疑 → 不计
                {"doc_id": "D1", "page_no": 1, "rule_id": "F02_design_version_missing"},  # 未判 → 不计
            ]
        },
    )
    capsys.readouterr()
    assert main(["eval", "--gold", str(gold), "--pred", str(pred)]) == 0
    out = capsys.readouterr().out
    assert "TP=1" in out and "FP=1" in out and "FN=0" in out
    assert "未经人工判定 1 条" in out
    assert "召回率 100.0%" in out


def test_eval_reports_missed_findings(tmp_path: Path, review_file: Path, capsys):
    gold = tmp_path / "goldset.json"
    main(["gold", "--review", str(review_file), "--gold", str(gold)])
    pred = write(tmp_path / "findings.json", {"findings": []})
    capsys.readouterr()
    main(["eval", "--gold", str(gold), "--pred", str(pred)])
    out = capsys.readouterr().out
    assert "FN=1" in out and "漏检率 100.0%" in out
    assert "[漏检] D1 p2 A13_copy_stamp_wording" in out
