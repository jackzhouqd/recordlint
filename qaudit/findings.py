"""疑点（Finding）数据结构与规则库加载。

设计原则：Finding 一律不可变，规则引擎只产生新对象，不修改既有结果。
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

import yaml

LEVELS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
LEVEL_ORDER = {lv: i for i, lv in enumerate(LEVELS)}


@dataclass(frozen=True)
class Finding:
    """一条审核疑点。bbox 为原图像素坐标 (x, y, w, h)，供报告红框定位。"""

    rule_id: str
    level: str
    title: str
    clause: str
    message: str
    doc_id: str
    page_no: int
    bbox: tuple[int, int, int, int] | None = None
    evidence: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bbox"] = list(self.bbox) if self.bbox else None
        return d


@dataclass(frozen=True)
class RuleSpec:
    """单条规则的配置。"""

    rule_id: str
    enabled: bool
    level: str
    title: str
    clause: str
    params: dict[str, Any] = field(default_factory=dict)
    pack: str = ""  # 来自哪个规则包；空 = 通用层


SECTIONS = ("rules_a", "rules_b", "rules_f", "rules_u")
PACK_DIR_NAME = "packs"


def discover_packs(rules_path: str | Path) -> list[Path]:
    """规则库同级 ``packs/`` 目录下的 ``*.yaml``，按文件名排序（顺序即加载顺序）。"""
    d = Path(rules_path).parent / PACK_DIR_NAME
    return sorted(p for p in d.glob("*.yaml") if p.is_file()) if d.is_dir() else []


def rules_bundle_hash(rules_path: str | Path, packs: "str | list | None" = "auto") -> str:
    """通用层 + 规则包 的合并指纹。批次登记用它，而不是只哈希 rules.yaml——
    换一个规则包判定结果就不同，指纹必须跟着变。"""
    import hashlib

    h = hashlib.sha256()
    p = Path(rules_path)
    if p.exists():
        h.update(p.read_bytes())
    for pk in _resolve_packs(rules_path, packs):
        h.update(pk.name.encode("utf-8"))
        h.update(pk.read_bytes())
    return h.hexdigest()[:16]


def _resolve_packs(rules_path: str | Path, packs: "str | list | None") -> list[Path]:
    if packs is None:
        return []
    if packs == "auto":
        return discover_packs(rules_path)
    return [Path(x) for x in packs]


def _as_bool(v: Any) -> bool:
    """YAML/表单里的 enabled 可能是字符串；"false"/"0"/"off"/"no" 一律按假处理。"""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "off", "no")
    return bool(v)


def _override_params(override: dict | None) -> dict[str, Any]:
    """解析覆盖项里的参数。存的是 JSON 文本，解析失败按「无覆盖」处理——
    一条规则的参数写坏了不该让整批审核跑不起来。"""
    if not override:
        return {}
    raw = override.get("params") or ""
    if not raw:
        return {}
    try:
        import json

        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


class RuleBook:
    """规则库。代码只认 rule_id，参数与级别全部来自 rules.yaml。

    分节：rules_a/rules_b 为通用填写规则，
    rules_f 为表单专项规则（来自规则包），rules_u 为单据级规则（需先完成单据切分）。
    """

    def __init__(self, sections: dict[str, dict[str, RuleSpec]], meta: dict,
                 packs: list[dict] | None = None, forms: list[tuple[str, dict]] | None = None,
                 classifier=None):
        self._sections = sections
        self._specs: dict[str, RuleSpec] = {
            rid: spec for group in sections.values() for rid, spec in group.items()
        }
        self.meta = meta
        self.packs: list[dict] = list(packs or [])          # 各规则包的 meta
        self.forms: list[tuple[str, dict]] = list(forms or [])  # (包名, forms 段)
        if classifier is None:
            from .formtype import FormClassifier

            classifier = FormClassifier.from_packs(self.forms)
        self.classifier = classifier  # 不可变；审核流水线从这里取表单识别口径

    @classmethod
    def load(cls, path: str | Path, packs: "str | list | None" = "auto",
             configure: bool = True) -> "RuleBook":
        """加载通用层 ``rules.yaml``，再按顺序合并规则包。

        ``packs``: ``"auto"``（同级 packs/ 目录全部加载）、显式路径列表、或 ``None``（不加载）。
        规则包可提供 ``forms``（表单类型清单）、``rules_f``（表单专项规则）与 ``overrides``
        （对通用规则逐字段覆盖）。表单识别口径随 book 一起返回（``book.classifier``）；
        ``configure=True`` 时同时替换 :mod:`qaudit.formtype` 的模块默认分类器（便捷入口用），
        Web 的只读路径应传 ``configure=False``，避免与后台审核线程互相覆盖。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"规则库不存在: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        sections = {name: cls._parse(raw.get(name, {})) for name in SECTIONS}
        metas: list[dict] = []
        forms: list[tuple[str, dict]] = []
        seen_names: set[str] = set()
        for pk in _resolve_packs(path, packs):
            praw = yaml.safe_load(pk.read_text(encoding="utf-8")) or {}
            pmeta = dict(praw.get("meta") or {})
            pname = str(pmeta.get("name") or pk.stem)
            if pname in seen_names:
                raise ValueError(f"规则包重名: {pname}（{pk}）")
            seen_names.add(pname)
            pmeta["name"] = pname
            pmeta["file"] = pk.name
            metas.append(pmeta)
            if praw.get("forms"):
                forms.append((pname, dict(praw["forms"])))
            new_f = cls._parse(praw.get("rules_f", {}), pack=pname)
            for rid in new_f:
                owner = next((n for n, g in sections.items() if rid in g), None)
                if owner is not None:
                    prev = sections[owner][rid].pack or "通用层"
                    raise ValueError(f"规则包 {pname} 的 rules_f 与已有规则冲突: {rid}"
                                     f"（已在 {owner}，来自 {prev}）；改通用规则请用 overrides")
            sections["rules_f"].update(new_f)
            cls._warn_unimplemented(new_f, pname)
            cls._apply_pack_overrides(sections, praw.get("overrides") or {}, pname)
        from .formtype import FormClassifier, configure as _configure  # 延迟导入，避免加载时循环

        classifier = _configure(forms) if configure else FormClassifier.from_packs(forms)
        return cls(sections=sections, meta=raw.get("meta", {}), packs=metas, forms=forms,
                   classifier=classifier)

    @staticmethod
    def _warn_unimplemented(rules: dict[str, RuleSpec], pack: str) -> None:
        """规则包声明了代码里没有实现的 F 类规则：界面会显示「启用」但永远不跑，必须提醒。"""
        import warnings

        from . import rules_f as _rf

        missing = [rid for rid in rules if rid not in _rf._REGISTRY]
        if missing:
            warnings.warn(f"规则包 {pack} 声明了未实现的规则，将被忽略: {', '.join(missing)}",
                          stacklevel=3)

    @staticmethod
    def _apply_pack_overrides(sections: dict[str, dict[str, RuleSpec]],
                              overrides: dict, pack: str) -> None:
        """规则包对通用规则的覆盖：只改写明的字段，参数逐项合并。"""
        from dataclasses import replace as _replace

        for rid, ov in overrides.items():
            ov = ov or {}
            for group in sections.values():
                spec = group.get(rid)
                if spec is None:
                    continue
                level = str(ov.get("level") or spec.level).upper()
                if level not in LEVEL_ORDER:
                    raise ValueError(f"规则包 {pack} 对 {rid} 的 level 非法: {level}")
                group[rid] = _replace(
                    spec,
                    enabled=spec.enabled if ov.get("enabled") is None else _as_bool(ov["enabled"]),
                    level=level,
                    params={**spec.params, **dict(ov.get("params") or {})},
                    pack=pack,
                )
                break
            else:
                raise ValueError(f"规则包 {pack} 覆盖了不存在的规则: {rid}")

    def section(self, name: str) -> dict[str, RuleSpec]:
        return self._sections.get(name, {})

    def apply_overrides(self, overrides: dict[str, dict]) -> "RuleBook":
        """套用界面上的规则调整，返回新的 RuleBook（基线本身不改）。

        参数是**逐项合并**而不是整体替换：界面上只改「某一个阈值」时，
        规则库基线里其余参数（包括日后新增的）仍然生效。
        """
        from dataclasses import replace as _replace

        sections = {
            name: {
                rid: _replace(
                    spec,
                    enabled=(spec.enabled if overrides.get(rid, {}).get("enabled") is None
                             else bool(overrides[rid]["enabled"])),
                    level=(overrides.get(rid, {}).get("level") or spec.level),
                    params={**spec.params, **_override_params(overrides.get(rid))},
                )
                for rid, spec in group.items()
            }
            for name, group in self._sections.items()
        }
        return RuleBook(sections, dict(self.meta), packs=self.packs, forms=self.forms,
                        classifier=self.classifier)

    @property
    def specs(self) -> dict[str, RuleSpec]:
        """全部规则（含被停用的），供规则管理界面展示。"""
        return dict(self._specs)

    @staticmethod
    def _parse(section: dict, pack: str = "") -> dict[str, RuleSpec]:
        out: dict[str, RuleSpec] = {}
        for rid, cfg in (section or {}).items():
            cfg = cfg or {}
            level = str(cfg.get("level", "MEDIUM")).upper()
            if level not in LEVEL_ORDER:
                raise ValueError(f"规则 {rid} 的 level 非法: {level}")
            out[rid] = RuleSpec(
                rule_id=rid,
                enabled=_as_bool(cfg.get("enabled", True)),
                level=level,
                title=str(cfg.get("title", rid)),
                clause=str(cfg.get("clause", "")),
                params=dict(cfg.get("params") or {}),
                pack=pack,
            )
        return out

    @staticmethod
    def applies(spec: RuleSpec, form_type: str) -> bool:
        """规则可用 applies_to / exclude_forms 限定适用表单，缺省对所有表单生效。"""
        include = spec.params.get("applies_to")
        exclude = spec.params.get("exclude_forms") or []
        if form_type in exclude:
            return False
        if include:
            return form_type in include
        return True

    def get(self, rule_id: str) -> RuleSpec | None:
        spec = self._specs.get(rule_id)
        return spec if (spec and spec.enabled) else None

    @property
    def all_ids(self) -> list[str]:
        return [rid for rid, spec in self._specs.items() if spec.enabled]


def make_finding(
    spec: RuleSpec,
    *,
    doc_id: str,
    page_no: int,
    message: str,
    bbox: tuple[int, int, int, int] | None = None,
    evidence: str = "",
    confidence: float = 1.0,
) -> Finding:
    """由规则配置生成 Finding，保证 level/clause/title 始终与规则库一致。"""
    return Finding(
        rule_id=spec.rule_id,
        level=spec.level,
        title=spec.title,
        clause=spec.clause,
        message=message,
        doc_id=doc_id,
        page_no=page_no,
        bbox=bbox,
        evidence=evidence,
        confidence=confidence,
    )


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """按严重度→页码→规则号排序，返回新列表。"""
    return sorted(
        findings,
        key=lambda f: (LEVEL_ORDER.get(f.level, 9), f.doc_id, f.page_no, f.rule_id),
    )
