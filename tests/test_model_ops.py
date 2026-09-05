"""模型工作台的关键约束测试。

这里锁的都是「错了会让验收失效」的规则，不是单纯的 CRUD：
上线门槛、自动标注不算人工标注、上线排他、无模型时规则静默跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qaudit.context import PageContext  # noqa: E402
from qaudit.findings import RuleBook  # noqa: E402
from qaudit.rules_b import check_seal_clarity, check_seal_upside_down  # noqa: E402
from qaudit.store import Store  # noqa: E402
from qaudit.train.repo import AUTO_LABELERS, MIN_HUMAN_LABELS, TrainRepo  # noqa: E402


@pytest.fixture
def repo(tmp_path: Path) -> TrainRepo:
    return TrainRepo(Store(tmp_path / "t.db"))


def _samples(repo: TrainRepo, n: int, *, synthetic: bool = False) -> list[str]:
    rows = [{"sample_id": f"{'syn' if synthetic else 'real'}{i}",
             "file": f"seals/x{i}.jpg", "source": f"src{i}", "synthetic": synthetic}
            for i in range(n)]
    repo.add_samples(rows)
    return [r["sample_id"] for r in rows]


def _model(repo: TrainRepo, version: str, human: int) -> str:
    return repo.add_model(kind="seal_cls", version=version, path="", trainer="t",
                          samples=100, human=human, groups=20, accuracy=0.9, metrics={})


# ---------------------------------------------------------------- 上线门槛

def test_model_below_human_label_threshold_cannot_go_live(repo):
    """全合成样本上的准确率是能力上界，不能拿来验收——必须卡住。"""
    model_id = _model(repo, "v1", human=MIN_HUMAN_LABELS - 1)
    with pytest.raises(ValueError, match="上线门槛"):
        repo.set_status(model_id, "active")
    assert repo.active("seal_cls") is None


def test_model_at_threshold_can_go_live(repo):
    model_id = _model(repo, "v1", human=MIN_HUMAN_LABELS)
    repo.set_status(model_id, "active", operator="admin1")
    assert repo.active("seal_cls")["model_id"] == model_id


def test_activation_is_exclusive(repo):
    """同类模型同时只允许一个生效，否则批次指纹会含糊。"""
    first = _model(repo, "v1", human=MIN_HUMAN_LABELS)
    second = _model(repo, "v2", human=MIN_HUMAN_LABELS)
    repo.set_status(first, "active")
    repo.set_status(second, "active")
    actives = [m for m in repo.models("seal_cls") if m["status"] == "active"]
    assert [m["model_id"] for m in actives] == [second]
    assert repo.model(first)["status"] == "retired"


def test_active_versions_feed_the_run_fingerprint(repo):
    repo.set_status(_model(repo, "v3", human=MIN_HUMAN_LABELS), "active")
    assert repo.active_versions() == {"seal_cls": "v3"}


# ---------------------------------------------------------------- 标注计数

@pytest.mark.parametrize("auto_labeler", AUTO_LABELERS)
def test_auto_labelers_do_not_count_as_human(repo, auto_labeler):
    """否则几何粗筛出的基底会把计数撑起来，上线门槛形同虚设。"""
    ids = _samples(repo, 5)
    for sid in ids:
        repo.set_label(sid, "ok", labeler=auto_labeler)
    assert repo.has_human_labels() == 0


def test_real_labelers_count_as_human(repo):
    ids = _samples(repo, 3)
    for sid in ids:
        repo.set_label(sid, "ok", labeler="zhang3")
    assert repo.has_human_labels() == 3


def test_synthetic_samples_never_count_as_human(repo):
    for sid in _samples(repo, 4, synthetic=True):
        repo.set_label(sid, "chipped", labeler="zhang3")
    assert repo.has_human_labels() == 0


def test_label_is_recorded_in_audit_log(repo):
    sid = _samples(repo, 1)[0]
    repo.set_label(sid, "blurred", labeler="li4")
    actions = [(r["action"], r["operator"]) for r in repo.store.audit_log()]
    assert ("label", "li4") in actions


def test_illegal_label_rejected(repo):
    sid = _samples(repo, 1)[0]
    with pytest.raises(ValueError, match="非法标签"):
        repo.set_label(sid, "不存在的标签", labeler="li4")


def test_stats_separate_real_and_synthetic(repo):
    _samples(repo, 3)
    _samples(repo, 2, synthetic=True)
    st = repo.stats()
    assert st["total"] == 5 and st["real"] == 3 and st["synthetic"] == 2
    assert st["todo"] == 5


# ---------------------------------------------------------------- 规则侧

def _ctx(seal_model) -> PageContext:
    img = np.full((300, 300, 3), 255, dtype=np.uint8)
    lines = [type("L", (), {"text": "示例", "cx": 10, "cy": 10 * i})() for i in range(5)]
    return PageContext(doc_id="D1", page_no=1, page_count=1, image=img, lines=lines,
                       cells=[], seals=[], h_segments=[], form_type="质量证明单",
                       seal_model=seal_model)


SEAL_RULES = [("B12_seal_clarity", check_seal_clarity),
              ("B13_seal_upside_down", check_seal_upside_down)]


@pytest.mark.parametrize("rule_id,fn", SEAL_RULES)
def test_seal_state_rule_is_silent_without_model(rule_id, fn):
    """未训练/未上线不应导致审核失败，只是少一类判定。"""
    spec = RuleBook.load(ROOT / "config" / "rules.yaml").get(rule_id)
    assert spec is not None, f"{rule_id} 必须存在于规则库基线中"
    assert fn(_ctx(None), spec) == []


@pytest.mark.parametrize("rule_id,fn", SEAL_RULES)
def test_seal_state_rule_keeps_verdict_in_the_rulebook(rule_id, fn):
    """判定阈值必须留在 rules.yaml 里——验收时要能解释为什么判它不合格。"""
    spec = RuleBook.load(ROOT / "config" / "rules.yaml").get(rule_id)
    assert set(spec.params["reject_labels"]) <= {
        "chipped", "blurred", "faint", "upside_down", "not_seal"}
    assert 0 < float(spec.params["min_confidence"]) <= 1


def test_clarity_and_upside_down_are_separate_rules():
    """两者交付节奏不同——倒盖合成即真实，清晰度必须有真实标注。
    合成一条会逼着质量部要么全开要么全关。"""
    book = RuleBook.load(ROOT / "config" / "rules.yaml")
    clarity = set(book.get("B12_seal_clarity").params["reject_labels"])
    flipped = set(book.get("B13_seal_upside_down").params["reject_labels"])
    assert clarity.isdisjoint(flipped), "两条规则的标签集不得重叠，否则同一枚章会报两次"
    assert flipped == {"upside_down"}
