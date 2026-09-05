"""规则分层：通用层（config/rules.yaml）+ 规则包（config/packs/*.yaml）。

- 不加载规则包时：F 类为空，A12/A15/A16 关闭，表单识别退回内置通用清单
- 加载示例包后：F 类 12 条带 pack 标记，覆盖项生效，客户表单可识别
- 规则包变了，批次指纹必须变
"""
from pathlib import Path

import pytest

from qaudit import formtype
from qaudit.findings import RuleBook, rules_bundle_hash
from qaudit.ocr import TextLine
from qaudit.store import file_hash

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "config" / "rules.yaml"
PACK = ROOT / "config" / "packs" / "example-mfg.yaml"


def _title(text: str) -> list[TextLine]:
    return [TextLine(text, (100, 50, 400, 30), 0.99)]


def test_core_only_has_no_form_specific_rules():
    book = RuleBook.load(RULES, packs=None)
    assert book.section("rules_f") == {}
    assert book.packs == [] and book.forms == []
    for rid in ("A12_special_record_empty", "A15_missing_unit_special", "A16_multi_point_record"):
        assert book.get(rid) is None, f"{rid} 应在通用层默认关闭"
    # 通用层仍能跑 A/B/U：随便取一条通用规则确认启用
    assert book.get("A01_date_format") is not None


def test_core_params_reference_only_generic_forms():
    """通用层的 applies_to / exclude_forms 不得引用只有规则包才认识的表单名（否则 --no-packs 时静默失效）。"""
    book = RuleBook.load(RULES, packs=None)
    known = set(book.classifier.known_forms())
    for rid, spec in book.specs.items():
        names = set(spec.params.get("applies_to") or []) | set(spec.params.get("exclude_forms") or [])
        assert names <= known, f"{rid} 引用了通用层不认识的表单: {names - known}"


def test_classifier_travels_with_book_without_touching_global():
    """configure=False 的加载不能替换模块默认分类器（Web 只读路径 / 后台线程用）。"""
    RuleBook.load(RULES)  # 默认：示例包口径
    before = formtype.active()
    book = RuleBook.load(RULES, packs=None, configure=False)
    assert formtype.active() is before
    assert book.classifier.classify(_title("质量证明单"), 1200) == formtype.UNKNOWN
    assert formtype.classify(_title("质量证明单"), 1200) == "质量证明单"


def test_rules_f_id_collision_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("meta: {name: bad}\nrules_f:\n  A01_date_format: {title: x}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        RuleBook.load(RULES, packs=[bad])


def test_duplicate_pack_name_is_rejected(tmp_path):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("meta: {name: same}\n", encoding="utf-8")
    b.write_text("meta: {name: same}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        RuleBook.load(RULES, packs=[a, b])


def test_unimplemented_rule_in_pack_warns(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("meta: {name: p}\nrules_f:\n  F99_not_implemented: {title: x}\n", encoding="utf-8")
    with pytest.warns(UserWarning, match="F99_not_implemented"):
        RuleBook.load(RULES, packs=[p])


def test_string_enabled_false_is_false(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text('meta: {name: p}\noverrides:\n  A01_date_format: {enabled: "false"}\n', encoding="utf-8")
    assert RuleBook.load(RULES, packs=[p]).get("A01_date_format") is None


def test_core_only_formtype_is_generic():
    RuleBook.load(RULES, packs=None)
    assert formtype.active().packs == ()
    assert formtype.classify(_title("产品质量证明书"), 1200) == "供方合格证"
    assert formtype.classify(_title("成品检验记录"), 1200) == "检验记录"
    assert formtype.classify(_title("质量证明单"), 1200) == formtype.UNKNOWN
    assert not formtype.is_internal("质量证明单")


def test_example_pack_adds_rules_and_forms():
    book = RuleBook.load(RULES, packs=[PACK])
    f = book.section("rules_f")
    assert len(f) == 12 and all(spec.pack == "example-mfg" for spec in f.values())
    assert book.get("A12_special_record_empty") is not None
    assert book.get("A12_special_record_empty").pack == "example-mfg"
    assert [m["name"] for m in book.packs] == ["example-mfg"]
    assert formtype.active().packs == ("example-mfg",)
    assert formtype.classify(_title("质量证明单"), 1200) == "质量证明单"
    assert formtype.is_internal("质量证明单")
    # 供方证明文件永远优先于规则包里的任何表单
    assert formtype.classify(_title("产品质量证明书 质量证明单"), 1200) == "供方合格证"


def test_auto_discovery_equals_explicit():
    auto = RuleBook.load(RULES)
    explicit = RuleBook.load(RULES, packs=[PACK])
    assert set(auto.specs) == set(explicit.specs)
    assert auto.all_ids == explicit.all_ids


def test_overrides_keep_core_params_and_merge():
    core = RuleBook.load(RULES, packs=None).specs["A16_multi_point_record"]
    packed = RuleBook.load(RULES, packs=[PACK]).specs["A16_multi_point_record"]
    assert packed.enabled and not core.enabled
    assert packed.params == core.params  # 示例包只改 enabled，参数原样保留


def test_unknown_override_target_is_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("meta: {name: bad}\noverrides:\n  Z99_nope: {enabled: true}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        RuleBook.load(RULES, packs=[bad])


def test_bundle_hash_changes_with_pack(tmp_path):
    rules = tmp_path / "rules.yaml"
    rules.write_text(RULES.read_text(encoding="utf-8"), encoding="utf-8")
    h0 = file_hash(rules)
    (tmp_path / "packs").mkdir()
    (tmp_path / "packs" / "p.yaml").write_text("meta: {name: p}\n", encoding="utf-8")
    h1 = file_hash(rules)
    assert h0 != h1 and h1 == rules_bundle_hash(rules)
    assert rules_bundle_hash(rules, packs=None) == h0
