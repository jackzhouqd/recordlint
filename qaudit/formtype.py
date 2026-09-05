"""表单类型识别。

表单专项规则是按表单分别规定的，同一条规则对不同表单适用性不同：
供方随带的“产品质量证明书/合格证”属外购器材证明文件，不适用内部表单的
填写格式要求；把类型判出来做规则门控，是压制误报最有效的一步。

表单类型清单来自**规则包**（`config/packs/*.yaml` 的 `forms` 段）。
:class:`FormClassifier` 是不可变对象，由 :meth:`RuleBook.load` 构造并挂在
``book.classifier`` 上，审核流水线从 book 取用——后台审核线程与界面线程各持各的，
互不干扰。模块级 :func:`classify` / :func:`is_internal` 只是便捷入口，读取的是
最近一次 :func:`configure` 原子替换的默认分类器；没有任何规则包时退回内置通用清单。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ocr import TextLine

UNKNOWN = "未识别"

# ---------------------------------------------------------------- 内置通用清单
# 供方证明文件必须排第一：它决定「不适用内部表单格式要求」这条最重要的门控。
_SUPPLIER_PATTERN: tuple[str, tuple[str, ...]] = (
    "供方合格证",
    (
        "产品质量证明书", "技术质量证明书", "质量证明书", "材质证明书", "合格证明书",
        "Certificate of Quality", "Quality Certification",
    ),
)
_DEFAULT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    _SUPPLIER_PATTERN,
    ("卷宗目录", ("卷宗表格目录", "卷宗目录", "表格目录")),
    ("检验记录", ("成品检验记录", "工序检验记录", "检验记录", "复验记录", "试验记录")),
    ("产品合格证", ("产品合格证",)),
)
_DEFAULT_INTERNAL = frozenset({"检验记录"})
_DEFAULT_SUPPLIER = frozenset({"供方合格证", "产品合格证"})
_DEFAULT_STRUCTURAL: tuple[tuple[tuple[str, ...], str], ...] = (
    (("规定", "实际", "检验员"), "检验记录"),
)


@dataclass(frozen=True)
class FormClassifier:
    """一套表单识别口径。不可变：构造完成后不再改动，可安全跨线程共享。"""

    patterns: tuple[tuple[str, tuple[str, ...]], ...] = _DEFAULT_PATTERNS
    internal: frozenset[str] = _DEFAULT_INTERNAL
    supplier: frozenset[str] = _DEFAULT_SUPPLIER
    structural: tuple[tuple[tuple[str, ...], str], ...] = _DEFAULT_STRUCTURAL
    packs: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_packs(cls, forms_specs: list[tuple[str, dict]]) -> "FormClassifier":
        """按规则包构造。``forms_specs`` 为 ``[(pack_name, forms_dict), ...]``：

        .. code-block:: yaml

            patterns:   [{form: 质量证明单, keywords: [质量证明单, 证明单]}, ...]   # 顺序即优先级
            internal:   [质量证明单, ...]
            supplier:   [供方合格证, 产品合格证]      # 可省略，缺省沿用内置
            structural: [{all: [规定, 实际, 检验员], form: 质量证明单}, ...]

        合并规则：供方证明文件永远排第一；规则包的 pattern 按包顺序排在内置通用项之前；
        同名表单以规则包为准；``internal`` 取并集；``structural`` 规则包在前。
        """
        if not forms_specs:
            return cls()
        pack_patterns: list[tuple[str, tuple[str, ...]]] = []
        internal: set[str] = set(_DEFAULT_INTERNAL)
        supplier: set[str] = set()
        structural: list[tuple[tuple[str, ...], str]] = []
        names: list[str] = []
        for pack_name, spec in forms_specs:
            spec = spec or {}
            names.append(pack_name)
            for item in spec.get("patterns") or []:
                form = str(item.get("form", "")).strip()
                keys = tuple(str(k) for k in (item.get("keywords") or []) if str(k).strip())
                if form and keys:
                    pack_patterns.append((form, keys))
            internal.update(str(f) for f in (spec.get("internal") or []))
            supplier.update(str(f) for f in (spec.get("supplier") or []))
            for item in spec.get("structural") or []:
                keys = tuple(str(k) for k in (item.get("all") or []))
                form = str(item.get("form", "")).strip()
                if keys and form:
                    structural.append((keys, form))
        overridden = {form for form, _ in pack_patterns}
        tail = [p for p in _DEFAULT_PATTERNS[1:] if p[0] not in overridden]
        return cls(
            patterns=tuple([_SUPPLIER_PATTERN] + pack_patterns + tail),
            internal=frozenset(internal),
            supplier=frozenset(supplier) if supplier else _DEFAULT_SUPPLIER,
            structural=tuple(structural) + _DEFAULT_STRUCTURAL,
            packs=tuple(names),
        )

    def known_forms(self) -> list[str]:
        """可识别的全部表单类型（供规则包校验与界面用）。"""
        seen: list[str] = []
        for form, _ in self.patterns:
            if form not in seen:
                seen.append(form)
        for _, form in self.structural:
            if form not in seen:
                seen.append(form)
        return seen

    def classify(self, lines: list[TextLine], page_height: int) -> str:
        """按标题区（页面上 1/3）优先匹配，其次全页匹配，最后用表头结构兜底。"""
        head = "".join(l.text for l in lines if l.cy < page_height * 0.34)
        whole = "".join(l.text for l in lines)
        for scope in (head, whole):
            for form, keys in self.patterns:
                if any(k in scope for k in keys):
                    return form
        for keys, form in self.structural:
            if all(k in whole for k in keys):
                return form
        return UNKNOWN

    def is_internal(self, form_type: str) -> bool:
        return form_type in self.internal


DEFAULT = FormClassifier()
_active: FormClassifier = DEFAULT  # 只做整对象替换，读方取一次引用即可，不存在撕裂读


def configure(forms_specs: list[tuple[str, dict]]) -> FormClassifier:
    """构造并原子替换模块默认分类器；返回构造出的对象。"""
    global _active
    clf = FormClassifier.from_packs(forms_specs)
    _active = clf
    return clf


def reset() -> None:
    """回到内置通用清单（测试与「--no-packs」用）。"""
    global _active
    _active = DEFAULT


def active() -> FormClassifier:
    return _active


def classify(lines: list[TextLine], page_height: int,
             classifier: FormClassifier | None = None) -> str:
    return (classifier or _active).classify(lines, page_height)


def is_internal(form_type: str, classifier: FormClassifier | None = None) -> bool:
    return (classifier or _active).is_internal(form_type)


def known_forms(classifier: FormClassifier | None = None) -> list[str]:
    return (classifier or _active).known_forms()
