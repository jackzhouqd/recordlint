"""开源脱敏门禁：仓内不得出现原客户 / 原行业 / 内部标准的标识。

扫描仓内**全部文本文件**（不看后缀，按能否以 UTF-8 解码判断），只跳过二进制、
生成物目录与内部计划目录。

用法：python tools/check_scrub.py            # 命中则非零退出
      pytest tests/test_scrub.py            # 同一逻辑，纳入测试
"""
import pathlib
import sys

BAD = [
    "军", "放行", "涉密", "记录通用管理要求", "外购产品质量档案", "qdoc-audit",
    "claude-code-project", "S20.", "S10.", "Hofmann", "燃油", "承制单位", "内网",
    "5.1.1(", "5.1.2(",
]
# 生成物 / 版本库 / 内部计划（含待清除词表本身，发布前整目录不随仓发布，见 CLAUDE.md）
SKIP_DIRS = {".git", "out", "__pycache__", ".pytest_cache", "superpowers", "node_modules", ".venv", "venv"}
BINARY_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".pdf", ".npz", ".db", ".pyc",
               ".ttf", ".otf", ".woff", ".woff2", ".ico", ".zip", ".gz"}
MAX_BYTES = 4 * 1024 * 1024


def _text_of(p: pathlib.Path) -> str | None:
    if p.suffix.lower() in BINARY_EXTS or p.stat().st_size > MAX_BYTES:
        return None
    try:
        return p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan(root: pathlib.Path) -> list[str]:
    hits: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or set(p.relative_to(root).parts) & SKIP_DIRS:
            continue
        if p.name == "check_scrub.py":
            continue
        text = _text_of(p)
        if text is None:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for b in BAD:
                if b in line:
                    hits.append(f"{p.relative_to(root).as_posix()}:{no}: {b}")
    return hits


if __name__ == "__main__":
    found = scan(pathlib.Path(__file__).resolve().parent.parent)
    print("\n".join(found) if found else "scrub OK")
    sys.exit(1 if found else 0)
