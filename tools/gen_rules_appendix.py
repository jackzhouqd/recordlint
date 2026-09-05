"""由 config/rules.yaml + config/packs/*.yaml 重生成《系统全景手册》附录 A。

用法：python tools/gen_rules_appendix.py            # 原地改写 docs/系统全景手册.md
      python tools/gen_rules_appendix.py --check    # 只比对，不一致则非零退出（供验收）
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qaudit.findings import RuleBook  # noqa: E402

DOC = ROOT / "docs" / "系统全景手册.md"
SECTIONS = (
    ("rules_a", "A 类 · 文本确定性"),
    ("rules_b", "B 类 · 视觉判定"),
    ("rules_f", "F 类 · 表单专项（规则包）"),
    ("rules_u", "U 类 · 单据级"),
)


def render() -> str:
    book = RuleBook.load(ROOT / "config" / "rules.yaml")
    packs = "、".join(m.get("name", "") for m in book.packs) or "无"
    out = ["## 附录 A 规则总表", "",
           f"> 由 `tools/gen_rules_appendix.py` 从 `config/rules.yaml` 与规则包（{packs}）生成，不要手改；"
           "命中数据看各批次报告，不在手册里固化。", ""]
    for key, title in SECTIONS:
        rules = book.section(key)
        out += [f"#### {title}（{len(rules)} 条）", "",
                "| 规则号 | 判定内容 | 级别 | 状态 | 来源 | 依据 |", "|---|---|---|---|---|---|"]
        for rid, r in rules.items():
            state = "启用" if r.enabled else "**默认关闭**"
            out.append(f"| `{rid}` | {r.title} | {r.level} | {state} | {r.pack or '通用层'} | {r.clause} |")
        out.append("")
    return "\n".join(out)


def main() -> int:
    text = DOC.read_text(encoding="utf-8")
    if not re.search(r"## 附录 A 规则总表\n.*?\n## 附录 B", text, flags=re.S):
        print("手册里找不到「## 附录 A 规则总表 … ## 附录 B」锚点，拒绝处理")
        return 2
    new = re.sub(r"## 附录 A 规则总表\n.*?(?=\n## 附录 B)", lambda _m: render(), text, flags=re.S)
    if "--check" in sys.argv:
        if new != text:
            print("附录 A 与规则库不一致，运行 python tools/gen_rules_appendix.py 重生成")
            return 1
        print("附录 A 一致")
        return 0
    DOC.write_text(new, encoding="utf-8")
    print("附录 A 已重生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
